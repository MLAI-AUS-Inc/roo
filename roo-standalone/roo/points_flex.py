"""Explicit, replay-safe consent for public Roo Points flex posts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4


POINTS_FLEX_ACTION_ID = "points_flex_confirm"
POINTS_FLEX_CONFIRMATION_TTL_SECONDS = 10 * 60
POINTS_FLEX_PROCESSING_LEASE_SECONDS = 30

_SIGNING_CONTEXT = b"roo-points-flex-v1"
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]+$")
_SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]+$")


class PointsFlexTokenError(ValueError):
    """Raised when a flex confirmation token cannot be trusted."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PointsFlexConfirmation:
    request_id: str
    slack_user_id: str
    channel_id: str
    issued_at: int
    expires_at: int


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise PointsFlexTokenError("invalid_token") from exc


def _signing_key(signing_secret: str) -> bytes:
    secret = str(signing_secret or "").encode("utf-8")
    if not secret:
        raise PointsFlexTokenError("signing_unavailable")
    return hmac.new(secret, _SIGNING_CONTEXT, hashlib.sha256).digest()


def issue_points_flex_confirmation(
    *,
    signing_secret: str,
    slack_user_id: str,
    channel_id: str,
    now: Optional[float] = None,
    ttl_seconds: int = POINTS_FLEX_CONFIRMATION_TTL_SECONDS,
) -> tuple[str, PointsFlexConfirmation]:
    """Create a signed button value bound to one member and one channel."""

    cleaned_user_id = str(slack_user_id or "").strip()
    cleaned_channel_id = str(channel_id or "").strip()
    if not _SLACK_USER_ID.fullmatch(cleaned_user_id):
        raise PointsFlexTokenError("invalid_user")
    if not _SLACK_CHANNEL_ID.fullmatch(cleaned_channel_id):
        raise PointsFlexTokenError("invalid_channel")
    if not 1 <= int(ttl_seconds) <= POINTS_FLEX_CONFIRMATION_TTL_SECONDS:
        raise PointsFlexTokenError("invalid_expiry")

    issued_at = int(time.time() if now is None else now)
    confirmation = PointsFlexConfirmation(
        request_id=str(uuid4()),
        slack_user_id=cleaned_user_id,
        channel_id=cleaned_channel_id,
        issued_at=issued_at,
        expires_at=issued_at + int(ttl_seconds),
    )
    payload = {
        "c": confirmation.channel_id,
        "exp": confirmation.expires_at,
        "iat": confirmation.issued_at,
        "rid": confirmation.request_id,
        "u": confirmation.slack_user_id,
        "v": 1,
    }
    encoded_payload = _urlsafe_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _signing_key(signing_secret),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_urlsafe_encode(signature)}", confirmation


def verify_points_flex_confirmation(
    token: str,
    *,
    signing_secret: str,
    now: Optional[float] = None,
) -> PointsFlexConfirmation:
    """Verify and decode a flex confirmation token without trusting its fields."""

    try:
        encoded_payload, encoded_signature = str(token or "").split(".", 1)
    except ValueError as exc:
        raise PointsFlexTokenError("invalid_token") from exc
    if not encoded_payload or not encoded_signature:
        raise PointsFlexTokenError("invalid_token")

    actual_signature = _urlsafe_decode(encoded_signature)
    expected_signature = hmac.new(
        _signing_key(signing_secret),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise PointsFlexTokenError("invalid_token")

    try:
        payload = json.loads(_urlsafe_decode(encoded_payload).decode("utf-8"))
        request_id = str(UUID(str(payload["rid"])))
        slack_user_id = str(payload["u"])
        channel_id = str(payload["c"])
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        version = int(payload["v"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PointsFlexTokenError("invalid_token") from exc

    if version != 1:
        raise PointsFlexTokenError("invalid_token")
    if not _SLACK_USER_ID.fullmatch(slack_user_id):
        raise PointsFlexTokenError("invalid_token")
    if not _SLACK_CHANNEL_ID.fullmatch(channel_id):
        raise PointsFlexTokenError("invalid_token")
    if not 1 <= expires_at - issued_at <= POINTS_FLEX_CONFIRMATION_TTL_SECONDS:
        raise PointsFlexTokenError("invalid_token")

    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 30:
        raise PointsFlexTokenError("invalid_token")
    if current_time >= expires_at:
        raise PointsFlexTokenError("expired_token")

    return PointsFlexConfirmation(
        request_id=request_id,
        slack_user_id=slack_user_id,
        channel_id=channel_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def parse_lifetime_earned(payload: dict[str, Any]) -> int:
    """Return a trusted non-negative integer from the backend balance payload."""

    value = payload.get("lifetime_earned")
    if isinstance(value, bool):
        raise ValueError("invalid lifetime_earned")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid lifetime_earned") from exc
    if parsed < 0 or str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise ValueError("invalid lifetime_earned")
    return parsed


def build_points_flex_preview_blocks(*, lifetime_earned: int, token: str) -> list[dict[str, Any]]:
    """Build the private confirmation card shown before any public post."""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Share your contribution total publicly in this channel?\n\n"
                    f"You have earned *{lifetime_earned} Roo Points* through MLAI contributions."
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Only your lifetime-earned total will be shared. "
                        "Your balance, purchases, spending, and history stay private. "
                        "This confirmation expires in 10 minutes."
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": POINTS_FLEX_ACTION_ID,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Share publicly"},
                    "value": token,
                }
            ],
        },
    ]


def format_points_flex_public_message(*, slack_user_id: str, lifetime_earned: int) -> str:
    return (
        f"<@{slack_user_id}> has earned *{lifetime_earned} Roo Points* "
        "through MLAI contributions."
    )


class PointsFlexShareStore:
    """SQLite state machine preventing duplicate flex posts across workers."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._initialised = False
        self._initialise_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialise(self) -> None:
        if self._initialised:
            return
        with self._initialise_lock:
            if self._initialised:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS points_flex_shares (
                        request_id TEXT PRIMARY KEY,
                        slack_user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        locked_by TEXT,
                        locked_until REAL,
                        message_ts TEXT,
                        last_error_code TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        shared_at REAL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS points_flex_shares_expiry_idx
                    ON points_flex_shares (expires_at)
                    """
                )
            self._initialised = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def claim(
        self,
        confirmation: PointsFlexConfirmation,
        *,
        owner: Optional[str] = None,
        now: Optional[float] = None,
        lease_seconds: int = POINTS_FLEX_PROCESSING_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Atomically claim a confirmation or report its existing state."""

        self._initialise()
        current_time = time.time() if now is None else float(now)
        owner = str(owner or f"roo-flex-{uuid4()}")
        locked_until = current_time + int(lease_seconds)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM points_flex_shares
                WHERE updated_at < ?
                  AND (
                    (status != 'shared' AND expires_at <= ?)
                    OR (status = 'shared' AND shared_at < ?)
                  )
                """,
                (current_time - 86400, current_time, current_time - 90 * 86400),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO points_flex_shares (
                    request_id, slack_user_id, channel_id, expires_at, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    confirmation.request_id,
                    confirmation.slack_user_id,
                    confirmation.channel_id,
                    confirmation.expires_at,
                    current_time,
                    current_time,
                ),
            )
            row = connection.execute(
                "SELECT * FROM points_flex_shares WHERE request_id = ?",
                (confirmation.request_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("points flex share state was not created")

            stored = dict(row)
            if (
                stored["slack_user_id"] != confirmation.slack_user_id
                or stored["channel_id"] != confirmation.channel_id
                or int(stored["expires_at"]) != confirmation.expires_at
            ):
                connection.commit()
                return {"state": "conflict", "record": stored}
            if stored["status"] == "shared":
                connection.commit()
                return {"state": "shared", "record": stored}
            if current_time >= confirmation.expires_at:
                connection.commit()
                return {"state": "expired", "record": stored}
            # A process crash could happen after Slack accepted the public post
            # but before mark_shared persisted. Never reclaim that ambiguous
            # state: the member can start a fresh confirmation instead.
            if stored["status"] == "processing":
                connection.commit()
                return {"state": "processing", "record": stored}

            cursor = connection.execute(
                """
                UPDATE points_flex_shares
                SET status = 'processing', locked_by = ?, locked_until = ?,
                    last_error_code = NULL, updated_at = ?
                WHERE request_id = ?
                  AND status = 'pending'
                """,
                (
                    owner,
                    locked_until,
                    current_time,
                    confirmation.request_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.commit()
                return {"state": "processing", "record": stored}
            claimed = connection.execute(
                "SELECT * FROM points_flex_shares WHERE request_id = ?",
                (confirmation.request_id,),
            ).fetchone()
            connection.commit()
            return {"state": "claimed", "record": dict(claimed)}

    def mark_shared(
        self,
        request_id: str,
        *,
        owner: str,
        message_ts: Optional[str],
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        self._initialise()
        current_time = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE points_flex_shares
                SET status = 'shared', locked_by = NULL, locked_until = NULL,
                    message_ts = ?, last_error_code = NULL, shared_at = ?, updated_at = ?
                WHERE request_id = ? AND status = 'processing' AND locked_by = ?
                """,
                (message_ts, current_time, current_time, request_id, owner),
            )
            row = connection.execute(
                "SELECT * FROM points_flex_shares WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            connection.commit()
            if cursor.rowcount != 1 and (row is None or row["status"] != "shared"):
                raise RuntimeError("points flex share claim was lost")
            return dict(row)

    def release(
        self,
        request_id: str,
        *,
        owner: str,
        error_code: str,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Release a failed claim so the same unexpired button can be retried."""

        self._initialise()
        current_time = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE points_flex_shares
                SET status = 'pending', locked_by = NULL, locked_until = NULL,
                    last_error_code = ?, updated_at = ?
                WHERE request_id = ? AND status = 'processing' AND locked_by = ?
                """,
                (str(error_code)[:64], current_time, request_id, owner),
            )
            row = connection.execute(
                "SELECT * FROM points_flex_shares WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("points flex share state is missing")
            return dict(row)

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        self._initialise()
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM points_flex_shares WHERE request_id = ?",
                    (str(request_id),),
                ).fetchone()
            )


@lru_cache(maxsize=8)
def get_points_flex_store(database_path: str) -> PointsFlexShareStore:
    return PointsFlexShareStore(database_path)
