"""Durable processing for acknowledged Meeting Room Slack actions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from .meeting_room_booking import (
    BOOK_ACTION_ID,
    CANCEL_ACTION_ID,
    MeetingRoomInputError,
    parse_action_value,
)


DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
DEFAULT_RETRY_POLL_SECONDS = 5.0
COMPLETED_RETENTION_SECONDS = 90 * 24 * 60 * 60

MeetingRoomActionProcessor = Callable[[dict[str, Any]], Awaitable[None]]


def _canonical_action(
    action_id: str,
    action_value: str,
) -> tuple[str, str]:
    parsed = parse_action_value(action_value, expected_action=action_id)
    if action_id == BOOK_ACTION_ID:
        action_key = f"book:{parsed['client_request_id']}"
    elif action_id == CANCEL_ACTION_ID:
        action_key = (
            f"cancel:{parsed['owner_slack_user_id']}:{parsed['booking_id']}"
        )
    else:
        raise MeetingRoomInputError(
            "invalid_action",
            "This Meeting Room action is not supported.",
        )
    return action_key, json.dumps(parsed, separators=(",", ":"), sort_keys=True)


def _retry_delay(attempt_count: int) -> float:
    return min(5 * 60, 5 * (2 ** max(0, min(int(attempt_count), 7) - 1)))


class MeetingRoomActionStore:
    """SQLite outbox shared by Public Roo workers and process restarts."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=2,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meeting_room_action_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action_key TEXT NOT NULL UNIQUE,
                        action_id TEXT NOT NULL,
                        action_value TEXT NOT NULL,
                        actor_user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        message_ts TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        locked_until REAL,
                        locked_by TEXT,
                        last_error TEXT,
                        target_notified_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_meeting_room_actions_due
                    ON meeting_room_action_outbox (status, next_attempt_at)
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(meeting_room_action_outbox)"
                    ).fetchall()
                }
                if "target_notified_at" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE meeting_room_action_outbox
                        ADD COLUMN target_notified_at REAL
                        """
                    )
            self._initialized = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def record_action(
        self,
        *,
        action_id: str,
        action_value: str,
        actor_user_id: str,
        channel_id: str,
        message_ts: str,
    ) -> dict[str, Any]:
        """Persist one verified click before Roo acknowledges it to Slack."""
        self._ensure_schema()
        action_key, canonical_value = _canonical_action(action_id, action_value)
        current_time = time.time()
        actor_user_id = str(actor_user_id or "").strip()
        channel_id = str(channel_id or "").strip()
        message_ts = str(message_ts or "").strip()
        parsed = json.loads(canonical_value)
        if parsed["owner_slack_user_id"] != actor_user_id:
            raise MeetingRoomInputError(
                "invalid_actor",
                "Only the member who requested this action can use it.",
            )
        if not channel_id.startswith("D"):
            raise MeetingRoomInputError(
                "invalid_channel",
                "Meeting Room actions must be completed in your private Roo DM.",
            )
        if not message_ts:
            raise MeetingRoomInputError(
                "invalid_action",
                "This action is not valid. Ask Roo to start again.",
            )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM meeting_room_action_outbox
                WHERE status = 'completed' AND completed_at < ?
                """,
                (current_time - COMPLETED_RETENTION_SECONDS,),
            )
            existing = connection.execute(
                """
                SELECT * FROM meeting_room_action_outbox WHERE action_key = ?
                """,
                (action_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO meeting_room_action_outbox (
                        action_key, action_id, action_value, actor_user_id,
                        channel_id, message_ts, status, next_attempt_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        action_key,
                        action_id,
                        canonical_value,
                        actor_user_id,
                        channel_id,
                        message_ts,
                        current_time,
                        current_time,
                        current_time,
                    ),
                )
            else:
                immutable_values = (
                    existing["action_id"],
                    existing["action_value"],
                    existing["actor_user_id"],
                )
                if immutable_values != (
                    action_id,
                    canonical_value,
                    actor_user_id,
                ):
                    connection.rollback()
                    raise MeetingRoomInputError(
                        "invalid_action",
                        "This action conflicts with an earlier request.",
                    )
                lease_is_active = (
                    existing["status"] == "processing"
                    and existing["locked_until"] is not None
                    and float(existing["locked_until"]) > current_time
                )
                if existing["status"] != "completed" and not lease_is_active:
                    connection.execute(
                        """
                        UPDATE meeting_room_action_outbox
                        SET channel_id = ?, message_ts = ?, status = 'pending',
                            next_attempt_at = ?, locked_until = NULL,
                            locked_by = NULL, last_error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            channel_id,
                            message_ts,
                            current_time,
                            current_time,
                            int(existing["id"]),
                        ),
                    )
            row = connection.execute(
                """
                SELECT * FROM meeting_room_action_outbox WHERE action_key = ?
                """,
                (action_key,),
            ).fetchone()
            connection.commit()
        result = self._row(row)
        if result is None:
            raise RuntimeError("Failed to persist Meeting Room action")
        return result

    def reserve(
        self,
        action_id: int,
        *,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        """Lease one due action to one worker."""
        self._ensure_schema()
        current_time = time.time()
        owner = owner or f"roo-meeting-room-{uuid4().hex}"
        locked_until = current_time + max(1.0, float(lease_seconds))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE meeting_room_action_outbox
                SET status = 'processing', attempt_count = attempt_count + 1,
                    locked_until = ?, locked_by = ?, updated_at = ?
                WHERE id = ? AND (
                    (status = 'pending' AND next_attempt_at <= ?)
                    OR (
                        status = 'processing'
                        AND (locked_until IS NULL OR locked_until <= ?)
                    )
                )
                """,
                (
                    locked_until,
                    owner,
                    current_time,
                    int(action_id),
                    current_time,
                    current_time,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self._row(
                connection.execute(
                    "SELECT * FROM meeting_room_action_outbox WHERE id = ?",
                    (int(action_id),),
                ).fetchone()
            )

    def claim_due(
        self,
        *,
        limit: int = 10,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Lease pending actions and abandoned processing actions."""
        self._ensure_schema()
        current_time = time.time()
        owner = owner or f"roo-meeting-room-retry-{uuid4().hex}"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM meeting_room_action_outbox
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (
                       status = 'processing'
                       AND (locked_until IS NULL OR locked_until <= ?)
                   )
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT ?
                """,
                (current_time, current_time, max(1, int(limit))),
            ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            action = self.reserve(
                int(row["id"]),
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if action:
                claimed.append(action)
        return claimed

    def mark_completed(self, action_id: int, *, owner: str) -> bool:
        self._ensure_schema()
        current_time = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE meeting_room_action_outbox
                SET status = 'completed', locked_until = NULL,
                    locked_by = NULL, last_error = NULL, completed_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'processing' AND locked_by = ?
                """,
                (current_time, current_time, int(action_id), str(owner)),
            )
            return cursor.rowcount == 1

    def mark_target_notified(self, action_id: int, *, owner: str) -> bool:
        """Persist target-DM success without completing admin result delivery."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE meeting_room_action_outbox
                SET target_notified_at = COALESCE(target_notified_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'processing' AND locked_by = ?
                """,
                (current_time, current_time, int(action_id), str(owner)),
            )
            return cursor.rowcount == 1

    def release(
        self,
        action_id: int,
        *,
        owner: str,
        error: str,
        delay_seconds: float,
    ) -> bool:
        self._ensure_schema()
        current_time = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE meeting_room_action_outbox
                SET status = 'pending', next_attempt_at = ?,
                    locked_until = NULL, locked_by = NULL, last_error = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'processing' AND locked_by = ?
                """,
                (
                    current_time + max(0.0, float(delay_seconds)),
                    str(error)[:500],
                    current_time,
                    int(action_id),
                    str(owner),
                ),
            )
            return cursor.rowcount == 1

    def get(self, action_id: int) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM meeting_room_action_outbox WHERE id = ?",
                    (int(action_id),),
                ).fetchone()
            )


@lru_cache(maxsize=8)
def get_meeting_room_action_store(database_path: str) -> MeetingRoomActionStore:
    return MeetingRoomActionStore(database_path)


async def _process_leased_action(
    action: dict[str, Any],
    *,
    store: MeetingRoomActionStore,
    processor: MeetingRoomActionProcessor,
) -> None:
    action_id = int(action["id"])
    owner = str(action["locked_by"])
    try:
        await processor(action)
    except asyncio.CancelledError:
        await asyncio.to_thread(
            store.release,
            action_id,
            owner=owner,
            error="worker_cancelled",
            delay_seconds=0,
        )
        raise
    except Exception as exc:
        await asyncio.to_thread(
            store.release,
            action_id,
            owner=owner,
            error=exc.__class__.__name__,
            delay_seconds=_retry_delay(int(action.get("attempt_count") or 1)),
        )
        print(
            "MEETING_ROOM_ACTION_RETRY "
            f"action_id={action_id} error_type={exc.__class__.__name__}"
        )
        return
    await asyncio.to_thread(store.mark_completed, action_id, owner=owner)


async def process_meeting_room_action(
    action_id: int,
    *,
    store: MeetingRoomActionStore,
    processor: MeetingRoomActionProcessor,
) -> bool:
    action = await asyncio.to_thread(store.reserve, action_id)
    if not action:
        return False
    await _process_leased_action(action, store=store, processor=processor)
    return True


async def process_due_meeting_room_actions(
    *,
    store: MeetingRoomActionStore,
    processor: MeetingRoomActionProcessor,
    limit: int = 10,
) -> int:
    actions = await asyncio.to_thread(store.claim_due, limit=limit)
    for action in actions:
        await _process_leased_action(action, store=store, processor=processor)
    return len(actions)


async def meeting_room_action_retry_loop(
    *,
    store: MeetingRoomActionStore,
    processor: MeetingRoomActionProcessor,
    poll_seconds: float = DEFAULT_RETRY_POLL_SECONDS,
) -> None:
    poll_seconds = max(0.05, float(poll_seconds))
    while True:
        try:
            await process_due_meeting_room_actions(
                store=store,
                processor=processor,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "MEETING_ROOM_ACTION_RETRY_LOOP_FAILED "
                f"error_type={exc.__class__.__name__}"
            )
        await asyncio.sleep(poll_seconds)
