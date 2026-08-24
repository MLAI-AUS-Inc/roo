"""Durable public-thread clarification state for Meeting Room bookings.

Only non-sensitive routing metadata is stored here. Availability, balances,
booking previews, and confirmations continue to be delivered by private DM.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .meeting_room_booking import ROOM_NAMES, parse_backend_timestamp


DEFAULT_CHOICE_TTL_SECONDS = 10 * 60
DEFAULT_PROCESSING_LEASE_SECONDS = 60.0
TERMINAL_RETENTION_SECONDS = 24 * 60 * 60


def room_choice_from_reply(text: str) -> Optional[str]:
    """Return an unambiguous supported room from a short Slack reply."""

    normalized = re.sub(
        r"<@[A-Z0-9]+(?:\|[^>]+)?>",
        " ",
        str(text or ""),
        flags=re.IGNORECASE,
    ).lower()
    normalized = " ".join(re.findall(r"[a-z]+", normalized))
    match = re.fullmatch(
        r"(?:please\s+)?(?:the\s+)?"
        r"(?P<size>small|big|large)"
        r"(?:\s+(?:meeting\s+)?room|\s+one)?"
        r"(?:\s+please)?",
        normalized,
    )
    if match is None:
        return None
    return (
        "small-meeting-room"
        if match.group("size") == "small"
        else "big-meeting-room"
    )


class MeetingRoomClarificationStore:
    """SQLite-backed room-choice prompts scoped to one owner and Slack thread."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._initialised = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialise(self) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meeting_room_clarifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        team_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        thread_ts TEXT NOT NULL,
                        request_message_ts TEXT NOT NULL,
                        owner_user_id TEXT NOT NULL,
                        starts_at TEXT NOT NULL,
                        ends_at TEXT NOT NULL,
                        target_user_id TEXT,
                        available_room_slugs TEXT NOT NULL,
                        status TEXT NOT NULL,
                        selected_room_slug TEXT,
                        choice_message_ts TEXT,
                        booking_client_request_id TEXT,
                        locked_until REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        completed_at REAL,
                        UNIQUE (team_id, channel_id, thread_ts, owner_user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_meeting_room_clarifications_expiry
                    ON meeting_room_clarifications (status, expires_at)
                    """
                )
                connection.commit()
            self._initialised = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        try:
            result["available_room_slugs"] = json.loads(
                str(result.get("available_room_slugs") or "[]")
            )
        except json.JSONDecodeError:
            result["available_room_slugs"] = []
        return result

    def record_prompt(
        self,
        *,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        request_message_ts: str,
        owner_user_id: str,
        starts_at: str,
        ends_at: str,
        available_room_slugs: list[str],
        target_user_id: Optional[str] = None,
        ttl_seconds: int = DEFAULT_CHOICE_TTL_SECONDS,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Persist the exact request before Roo asks publicly for a room."""

        cleaned_team = str(team_id or "").strip()
        cleaned_channel = str(channel_id or "").strip()
        cleaned_thread = str(thread_ts or "").strip()
        cleaned_request = str(request_message_ts or "").strip()
        cleaned_owner = str(owner_user_id or "").strip()
        if (
            not cleaned_team
            or not cleaned_channel
            or cleaned_channel.startswith("D")
            or not cleaned_thread
            or not cleaned_request
            or not cleaned_owner
        ):
            raise ValueError(
                "A public Slack workspace, channel, thread, request, and owner "
                "are required"
            )
        target = str(target_user_id or "").strip()
        if target and not re.fullmatch(r"[A-Z0-9]+", target):
            raise ValueError("target_user_id must be a Slack user ID")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        parsed_start = parse_backend_timestamp(starts_at)
        parsed_end = parse_backend_timestamp(ends_at)
        if parsed_end <= parsed_start:
            raise ValueError("ends_at must be after starts_at")
        supported_rooms = sorted(
            {
                str(slug).strip()
                for slug in available_room_slugs
                if str(slug).strip() in ROOM_NAMES
            }
        )
        if not supported_rooms:
            raise ValueError("At least one supported room is required")

        self._initialise()
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM meeting_room_clarifications
                WHERE status IN ('completed', 'failed', 'expired')
                  AND updated_at < ?
                """,
                (current_time - TERMINAL_RETENTION_SECONDS,),
            )
            existing = connection.execute(
                """
                SELECT * FROM meeting_room_clarifications
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
                  AND owner_user_id = ?
                """,
                (cleaned_team, cleaned_channel, cleaned_thread, cleaned_owner),
            ).fetchone()
            if existing is not None and str(existing["request_message_ts"]) == cleaned_request:
                connection.commit()
                return self._row(existing) or {}
            connection.execute(
                """
                INSERT INTO meeting_room_clarifications (
                    team_id, channel_id, thread_ts, request_message_ts,
                    owner_user_id, starts_at, ends_at, target_user_id,
                    available_room_slugs, status, created_at, updated_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_choice', ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts, owner_user_id) DO UPDATE SET
                    request_message_ts = excluded.request_message_ts,
                    owner_user_id = excluded.owner_user_id,
                    starts_at = excluded.starts_at,
                    ends_at = excluded.ends_at,
                    target_user_id = excluded.target_user_id,
                    available_room_slugs = excluded.available_room_slugs,
                    status = 'awaiting_choice',
                    selected_room_slug = NULL,
                    choice_message_ts = NULL,
                    booking_client_request_id = NULL,
                    locked_until = NULL,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    completed_at = NULL
                """,
                (
                    cleaned_team,
                    cleaned_channel,
                    cleaned_thread,
                    cleaned_request,
                    cleaned_owner,
                    parsed_start.isoformat(),
                    parsed_end.isoformat(),
                    target or None,
                    json.dumps(supported_rooms, separators=(",", ":")),
                    current_time,
                    current_time,
                    current_time + ttl_seconds,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM meeting_room_clarifications
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
                  AND owner_user_id = ?
                """,
                (cleaned_team, cleaned_channel, cleaned_thread, cleaned_owner),
            ).fetchone()
            connection.commit()
            return self._row(row) or {}

    def find(
        self,
        *,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        owner_user_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        self._initialise()
        current_time = time.time() if now is None else float(now)
        owner = str(owner_user_id or "").strip()
        query = """
                SELECT * FROM meeting_room_clarifications
                WHERE team_id = ? AND channel_id = ? AND thread_ts = ?
        """
        parameters: tuple[Any, ...] = (
            str(team_id or "").strip(),
            str(channel_id or "").strip(),
            str(thread_ts or "").strip(),
        )
        if owner:
            query += " AND owner_user_id = ?"
            parameters += (owner,)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._lock, self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            if (
                row is not None
                and row["status"] in {"awaiting_choice", "processing"}
                and float(row["expires_at"]) <= current_time
            ):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE meeting_room_clarifications
                    SET status = 'expired', locked_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (current_time, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM meeting_room_clarifications WHERE id = ?",
                    (row["id"],),
                ).fetchone()
            connection.commit()
            return self._row(row)

    def claim_choice(
        self,
        *,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        actor_user_id: str,
        room_slug: str,
        choice_message_ts: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Atomically let only the original requester choose the first room."""

        current_time = time.time() if now is None else float(now)
        record = self.find(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            owner_user_id=actor_user_id,
            now=current_time,
        )
        if record is None:
            another_owner = self.find(
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                now=current_time,
            )
            if another_owner is not None:
                return {
                    "disposition": "wrong_owner",
                    "record": another_owner,
                }
            return {"disposition": "missing"}
        if record.get("status") == "expired":
            return {"disposition": "expired", "record": record}
        if room_slug not in set(record.get("available_room_slugs") or []):
            return {"disposition": "unsupported_room", "record": record}

        self._initialise()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM meeting_room_clarifications WHERE id = ?",
                (record["id"],),
            ).fetchone()
            if row is None:
                connection.commit()
                return {"disposition": "missing"}
            status = str(row["status"])
            if status == "completed":
                connection.commit()
                disposition = (
                    "duplicate"
                    if str(row["choice_message_ts"] or "")
                    == str(choice_message_ts or "")
                    else "completed"
                )
                return {"disposition": disposition, "record": self._row(row)}
            if status == "failed":
                connection.commit()
                return {"disposition": "failed", "record": self._row(row)}
            if status == "expired" or float(row["expires_at"]) <= current_time:
                connection.execute(
                    """
                    UPDATE meeting_room_clarifications
                    SET status = 'expired', locked_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (current_time, row["id"]),
                )
                connection.commit()
                return {
                    "disposition": "expired",
                    "record": self.find(
                        team_id=team_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        owner_user_id=actor_user_id,
                        now=current_time,
                    ),
                }
            if status == "processing":
                if (
                    str(row["choice_message_ts"] or "")
                    == str(choice_message_ts or "")
                    and float(row["locked_until"] or 0) > current_time
                ):
                    connection.commit()
                    return {"disposition": "duplicate", "record": self._row(row)}
                if str(row["selected_room_slug"] or "") != room_slug:
                    connection.commit()
                    return {"disposition": "already_selected", "record": self._row(row)}
                if float(row["locked_until"] or 0) > current_time:
                    connection.commit()
                    return {"disposition": "processing", "record": self._row(row)}

            booking_request_id = str(row["booking_client_request_id"] or uuid4())
            connection.execute(
                """
                UPDATE meeting_room_clarifications
                SET status = 'processing', selected_room_slug = ?,
                    choice_message_ts = ?, booking_client_request_id = ?,
                    locked_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    room_slug,
                    str(choice_message_ts or "").strip(),
                    booking_request_id,
                    current_time + max(1.0, float(lease_seconds)),
                    current_time,
                    row["id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM meeting_room_clarifications WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
            return {"disposition": "claimed", "record": self._row(claimed)}

    def finish(
        self,
        record_id: int,
        *,
        success: bool,
        now: Optional[float] = None,
    ) -> bool:
        self._initialise()
        current_time = time.time() if now is None else float(now)
        status = "completed" if success else "failed"
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE meeting_room_clarifications
                SET status = ?, locked_until = NULL, updated_at = ?,
                    completed_at = ?
                WHERE id = ? AND status = 'processing'
                """,
                (status, current_time, current_time, int(record_id)),
            )
            return cursor.rowcount == 1


@lru_cache(maxsize=8)
def get_meeting_room_clarification_store(
    database_path: str,
) -> MeetingRoomClarificationStore:
    return MeetingRoomClarificationStore(database_path)
