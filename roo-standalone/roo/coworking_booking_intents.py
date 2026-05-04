from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx

from .config import get_settings


DEFAULT_COWORKING_INTENTS_DB_PATH = "data/coworking_booking_intents.db"
DEFAULT_RETRY_POLL_SECONDS = 30.0
DEFAULT_PROCESSING_LEASE_SECONDS = 90.0


def build_coworking_intent_key(slack_user_id: str, booking_date: str) -> str:
    return f"coworking:{str(slack_user_id).strip()}:{str(booking_date).strip()}"


def _now() -> float:
    return time.time()


def retry_delay_seconds(attempt_count: int) -> float:
    attempt_number = max(1, int(attempt_count or 1))
    return min(15 * 60, 30 * (2 ** (attempt_number - 1)))


def has_active_booking(bookings: list[dict[str, Any]], booking_date: str) -> bool:
    for booking in bookings:
        if (
            str(booking.get("date") or "") == str(booking_date)
            and str(booking.get("status") or "booked") == "booked"
        ):
            return True
    return False


def is_retryable_coworking_exception(exc: Exception) -> bool:
    from .clients.mlai_backend import MLAIBackendUnavailableError

    if isinstance(exc, MLAIBackendUnavailableError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in {408, 425, 429} or 500 <= status_code < 600
    return False


class CoworkingBookingIntentStore:
    def __init__(self, db_path: str | Path = DEFAULT_COWORKING_INTENTS_DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS coworking_booking_intents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        slack_user_id TEXT NOT NULL,
                        requested_by_slack_id TEXT,
                        booking_date TEXT NOT NULL,
                        channel_id TEXT,
                        thread_ts TEXT,
                        request_text TEXT,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        locked_until REAL,
                        locked_by TEXT,
                        last_error TEXT,
                        backend_booking_id TEXT,
                        backend_result_json TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        confirmed_at REAL
                    )
                    """
                )
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(coworking_booking_intents)").fetchall()
                }
                if "requested_by_slack_id" not in columns:
                    conn.execute(
                        "ALTER TABLE coworking_booking_intents "
                        "ADD COLUMN requested_by_slack_id TEXT"
                    )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_coworking_intents_due
                    ON coworking_booking_intents (status, next_attempt_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_coworking_intents_user_date
                    ON coworking_booking_intents (slack_user_id, booking_date)
                    """
                )
            self._initialized = True

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    def record_intent(
        self,
        *,
        slack_user_id: str,
        requested_by_slack_id: Optional[str] = None,
        booking_date: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        request_text: Optional[str],
    ) -> dict[str, Any]:
        self._ensure_schema()
        current_time = _now()
        idempotency_key = build_coworking_intent_key(slack_user_id, booking_date)
        cleaned_slack_user_id = str(slack_user_id).strip()
        cleaned_requested_by = str(requested_by_slack_id or cleaned_slack_user_id).strip()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO coworking_booking_intents (
                        idempotency_key,
                        slack_user_id,
                        requested_by_slack_id,
                        booking_date,
                        channel_id,
                        thread_ts,
                        request_text,
                        status,
                        next_attempt_at,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO UPDATE SET
                        requested_by_slack_id = excluded.requested_by_slack_id,
                        channel_id = excluded.channel_id,
                        thread_ts = excluded.thread_ts,
                        request_text = excluded.request_text,
                        status = CASE
                            WHEN coworking_booking_intents.status = 'processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.status
                            ELSE 'pending'
                        END,
                        next_attempt_at = CASE
                            WHEN coworking_booking_intents.status = 'processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.next_attempt_at
                            ELSE ?
                        END,
                        locked_until = CASE
                            WHEN coworking_booking_intents.status = 'processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.locked_until
                            ELSE NULL
                        END,
                        locked_by = CASE
                            WHEN coworking_booking_intents.status = 'processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.locked_by
                            ELSE NULL
                        END,
                        last_error = NULL,
                        updated_at = ?
                    """,
                    (
                        idempotency_key,
                        cleaned_slack_user_id,
                        cleaned_requested_by,
                        str(booking_date).strip(),
                        channel_id,
                        thread_ts,
                        request_text,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                        current_time,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return dict(row)

    def reserve_for_processing(
        self,
        intent_id: int,
        *,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        owner = owner or f"roo-{uuid4().hex}"
        locked_until = current_time + lease_seconds
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        status = 'processing',
                        attempt_count = attempt_count + 1,
                        locked_until = ?,
                        locked_by = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND (
                        status IN ('pending', 'pending_retry')
                        OR (status = 'processing' AND (locked_until IS NULL OR locked_until <= ?))
                      )
                    """,
                    (locked_until, owner, current_time, int(intent_id), current_time),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def claim_due(
        self,
        *,
        limit: int = 10,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        claimed: list[dict[str, Any]] = []
        owner = owner or f"roo-worker-{uuid4().hex}"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id
                    FROM coworking_booking_intents
                    WHERE (
                        status IN ('pending', 'pending_retry')
                        AND next_attempt_at <= ?
                    )
                    OR (
                        status = 'processing'
                        AND (locked_until IS NULL OR locked_until <= ?)
                    )
                    ORDER BY next_attempt_at ASC, id ASC
                    LIMIT ?
                    """,
                    (current_time, current_time, int(limit)),
                ).fetchall()

        for row in rows:
            reserved = self.reserve_for_processing(
                int(row["id"]),
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if reserved:
                claimed.append(reserved)
        return claimed

    def mark_confirmed(
        self,
        intent_id: int,
        *,
        backend_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        current_time = _now()
        backend_result = backend_result or {}
        backend_booking_id = backend_result.get("id")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        status = 'confirmed',
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = NULL,
                        backend_booking_id = ?,
                        backend_result_json = ?,
                        confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(backend_booking_id) if backend_booking_id else None,
                        json.dumps(backend_result, sort_keys=True),
                        current_time,
                        current_time,
                        int(intent_id),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def mark_retryable_failure(
        self,
        intent_id: int,
        *,
        error: str,
        delay_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        current_time = _now()
        row = self.get(intent_id)
        attempts = int(row.get("attempt_count") or 1) if row else 1
        delay = retry_delay_seconds(attempts) if delay_seconds is None else delay_seconds
        next_attempt_at = current_time + delay
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        status = 'pending_retry',
                        next_attempt_at = ?,
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (next_attempt_at, str(error), current_time, int(intent_id)),
                )
                updated = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(updated)

    def mark_blocked(self, intent_id: int, *, error: str) -> dict[str, Any]:
        self._ensure_schema()
        current_time = _now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        status = 'blocked',
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (str(error), current_time, int(intent_id)),
                )
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def get(self, intent_id: int) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return self._row_to_dict(row)

    def get_by_key(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return self._row_to_dict(row)

    def counts_by_status(self) -> dict[str, int]:
        self._ensure_schema()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM coworking_booking_intents
                    GROUP BY status
                    ORDER BY status
                    """
                ).fetchall()
                return {row["status"]: int(row["count"]) for row in rows}


_store: CoworkingBookingIntentStore | None = None


def get_coworking_intent_store() -> CoworkingBookingIntentStore:
    global _store
    if _store is None:
        settings = get_settings()
        db_path = getattr(
            settings,
            "COWORKING_INTENTS_DB_PATH",
            DEFAULT_COWORKING_INTENTS_DB_PATH,
        )
        _store = CoworkingBookingIntentStore(db_path)
    return _store


def _build_backend_client():
    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    return MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )


def _safe_post_thread_message(*, channel_id: Optional[str], thread_ts: Optional[str], text: str) -> None:
    if not channel_id or not thread_ts:
        return
    from .slack_client import post_message

    post_message(channel=channel_id, thread_ts=thread_ts, text=text)


def _coworking_retry_confirmed_message(
    *,
    slack_user_id: str,
    requested_by_slack_id: str,
    booking_date: str,
) -> str:
    if requested_by_slack_id and requested_by_slack_id != slack_user_id:
        return (
            "I retried the queued coworking check-in for "
            f"<@{slack_user_id}> and confirmed {booking_date}. They're booked."
        )
    return (
        "I retried your queued coworking booking and confirmed "
        f"{booking_date}. You're booked."
    )


def _coworking_retry_blocked_message(
    *,
    slack_user_id: str,
    requested_by_slack_id: str,
    error: str,
) -> str:
    if requested_by_slack_id and requested_by_slack_id != slack_user_id:
        return (
            "I retried the coworking check-in for "
            f"<@{slack_user_id}>, but I still can't process it. Reason: {error}"
        )
    return (
        "I retried your coworking booking request, but I still can't process it. "
        f"Reason: {error}"
    )


async def process_coworking_booking_intent(
    intent: dict[str, Any],
    *,
    store: Optional[CoworkingBookingIntentStore] = None,
    client: Any = None,
    notify: bool = True,
) -> dict[str, Any]:
    store = store or get_coworking_intent_store()
    client = client or _build_backend_client()
    intent_id = int(intent["id"])
    booking_date = str(intent["booking_date"])
    slack_user_id = str(intent["slack_user_id"])
    requested_by_slack_id = str(intent.get("requested_by_slack_id") or slack_user_id)
    channel_id = intent.get("channel_id")
    thread_ts = intent.get("thread_ts")

    try:
        bookings = await client.get_my_bookings(slack_user_id)
        if has_active_booking(bookings, booking_date):
            backend_result = {"already_booked": True, "date": booking_date}
            store.mark_confirmed(intent_id, backend_result=backend_result)
            if notify:
                _safe_post_thread_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=_coworking_retry_confirmed_message(
                        slack_user_id=slack_user_id,
                        requested_by_slack_id=requested_by_slack_id,
                        booking_date=booking_date,
                    ),
                )
            return {"status": "confirmed", "already_booked": True, "intent_id": intent_id}

        backend_result = await client.book_coworking(slack_user_id, booking_date, channel_id)
        store.mark_confirmed(intent_id, backend_result=backend_result)
        if notify:
            _safe_post_thread_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=_coworking_retry_confirmed_message(
                    slack_user_id=slack_user_id,
                    requested_by_slack_id=requested_by_slack_id,
                    booking_date=booking_date,
                ),
            )
        return {"status": "confirmed", "backend_result": backend_result, "intent_id": intent_id}

    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        if is_retryable_coworking_exception(exc):
            updated = store.mark_retryable_failure(intent_id, error=error)
            print(
                "🏢 coworking_intent_retryable_failure "
                f"intent_id={intent_id} slack_user_id={slack_user_id} "
                f"booking_date={booking_date} attempts={updated.get('attempt_count')} "
                f"next_attempt_at={updated.get('next_attempt_at')} error={error}"
            )
            return {"status": "pending_retry", "error": error, "intent_id": intent_id}

        store.mark_blocked(intent_id, error=error)
        if notify:
            _safe_post_thread_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=_coworking_retry_blocked_message(
                    slack_user_id=slack_user_id,
                    requested_by_slack_id=requested_by_slack_id,
                    error=error,
                ),
            )
        return {"status": "blocked", "error": error, "intent_id": intent_id}


async def coworking_booking_retry_loop(
    *,
    store: Optional[CoworkingBookingIntentStore] = None,
    poll_seconds: Optional[float] = None,
) -> None:
    settings = get_settings()
    store = store or get_coworking_intent_store()
    poll_interval = float(
        poll_seconds
        if poll_seconds is not None
        else getattr(settings, "COWORKING_RETRY_POLL_SECONDS", DEFAULT_RETRY_POLL_SECONDS)
    )
    owner = f"roo-retry-worker-{uuid4().hex}"
    print(f"🏢 Coworking booking retry worker started owner={owner} poll_seconds={poll_interval}")

    while True:
        try:
            due = store.claim_due(limit=10, owner=owner)
            for intent in due:
                await process_coworking_booking_intent(intent, store=store, notify=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"🏢 coworking_retry_worker_error exc_type={exc.__class__.__name__} exc={exc}")
        await asyncio.sleep(poll_interval)
