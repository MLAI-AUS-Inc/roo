from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from .config import get_settings
from .coworking_booking_schema_v3 import (
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    SCHEMA_VERSION,
)


DEFAULT_COWORKING_INTENTS_DB_PATH = "data/coworking_booking_intents.db"
DEFAULT_RETRY_POLL_SECONDS = 30.0
DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
DEFAULT_TERMINAL_RETENTION_DAYS = 30
TERMINAL_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


def build_coworking_intent_key(slack_user_id: str, booking_date: str) -> str:
    return f"coworking:{str(slack_user_id).strip()}:{str(booking_date).strip()}"


def build_coworking_operation_id(idempotency_key: str) -> str:
    """Map one durable Roo intent lifecycle to a stable backend operation."""
    return str(uuid5(NAMESPACE_URL, f"roo:{str(idempotency_key).strip()}"))


def build_coworking_batch_intent_key(
    admin_slack_user_id: str,
    target_slack_user_ids: list[str],
    booking_date: str,
) -> str:
    canonical = json.dumps(
        {
            "admin": str(admin_slack_user_id).strip(),
            "date": str(booking_date).strip(),
            "targets": sorted({str(value).strip() for value in target_slack_user_ids}),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"coworking-batch:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _now() -> float:
    return time.time()


def retry_delay_seconds(attempt_count: int) -> float:
    attempt_number = max(1, int(attempt_count or 1))
    return min(15 * 60, 30 * (2 ** (attempt_number - 1)))


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


def coworking_failure_code(exc: Exception) -> str:
    """Return a bounded diagnostic code without persisting external error text."""
    from .clients.mlai_backend import MLAIBackendUnavailableError

    if isinstance(exc, MLAIBackendUnavailableError):
        reason_code = str(getattr(exc, "reason_code", ""))
        if reason_code == "invalid_backend_response":
            return reason_code
        return "backend_unavailable"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"backend_http_{int(exc.response.status_code)}"
    if isinstance(exc, httpx.TransportError):
        return "backend_transport_error"
    if isinstance(exc, (TypeError, ValueError, json.JSONDecodeError)):
        return "invalid_backend_response"
    return "unexpected_error"


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

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            if not self.db_path.is_file():
                raise RuntimeError(
                    "Coworking booking database is not initialized; run "
                    "scripts/migrate_coworking_booking_intents_v3.py before startup"
                )
            with self._connect_readonly() as conn:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(coworking_booking_intents)").fetchall()
                }
                indexes = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA index_list(coworking_booking_intents)"
                    ).fetchall()
                }
                missing_columns = sorted(REQUIRED_COLUMNS - columns)
                missing_indexes = sorted(REQUIRED_INDEXES - indexes)
                schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if (
                    schema_version < SCHEMA_VERSION
                    or missing_columns
                    or missing_indexes
                ):
                    raise RuntimeError(
                        "Coworking booking schema v3 is not applied; run "
                        "scripts/migrate_coworking_booking_intents_v3.py before startup "
                        f"(version={schema_version}, missing columns={missing_columns}, "
                        f"indexes={missing_indexes})"
                    )
            self._initialized = True

    def validate_schema(self) -> None:
        self._ensure_schema()

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
    ) -> dict[str, Any]:
        self._ensure_schema()
        current_time = _now()
        idempotency_key = build_coworking_intent_key(slack_user_id, booking_date)
        cleaned_slack_user_id = str(slack_user_id).strip()
        cleaned_requested_by = str(requested_by_slack_id or cleaned_slack_user_id).strip()
        with self._lock:
            with self._connect() as conn:
                prior = conn.execute(
                    "SELECT idempotency_key, status FROM coworking_booking_intents "
                    "WHERE idempotency_key = ? "
                    "OR substr(idempotency_key, 1, ?) = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (
                        idempotency_key,
                        len(f"{idempotency_key}:"),
                        f"{idempotency_key}:",
                    ),
                ).fetchone()
                # A completed or rejected intent is one finished lifecycle. A
                # later user action must get a fresh backend operation so a
                # genuine cancel/rebook can create a fresh debit and receipt.
                if prior and prior["status"] in {"confirmed", "blocked"}:
                    idempotency_key = f"{idempotency_key}:{uuid4().hex}"
                elif prior:
                    idempotency_key = str(prior["idempotency_key"])
                conn.execute(
                    """
                    INSERT INTO coworking_booking_intents (
                        idempotency_key,
                        slack_user_id,
                        requested_by_slack_id,
                        booking_date,
                        channel_id,
                        thread_ts,
                        status,
                        next_attempt_at,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO UPDATE SET
                        requested_by_slack_id = excluded.requested_by_slack_id,
                        channel_id = excluded.channel_id,
                        thread_ts = excluded.thread_ts,
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

    def record_batch_intent(
        self,
        *,
        admin_slack_user_id: str,
        target_slack_user_ids: list[str],
        booking_date: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> dict[str, Any]:
        """Persist an atomic batch request before any backend mutation."""
        self._ensure_schema()
        current_time = _now()
        admin_id = str(admin_slack_user_id).strip()
        targets = sorted({str(value).strip() for value in target_slack_user_ids})
        if not admin_id or not targets or any(not value for value in targets):
            raise ValueError("A batch intent requires an admin and at least one target")
        idempotency_key = build_coworking_batch_intent_key(admin_id, targets, booking_date)
        encoded_targets = json.dumps(targets, separators=(",", ":"))
        with self._lock:
            with self._connect() as conn:
                prior = conn.execute(
                    "SELECT idempotency_key, status FROM coworking_booking_intents "
                    "WHERE idempotency_key = ? "
                    "OR substr(idempotency_key, 1, ?) = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (
                        idempotency_key,
                        len(f"{idempotency_key}:"),
                        f"{idempotency_key}:",
                    ),
                ).fetchone()
                # A completed or rejected batch is one finished lifecycle.
                # A later admin action must use a fresh backend operation so a
                # genuine cancel/rebook remains possible.
                if prior and prior["status"] in {"batch_confirmed", "batch_blocked"}:
                    idempotency_key = f"{idempotency_key}:{uuid4().hex}"
                elif prior:
                    idempotency_key = str(prior["idempotency_key"])
                conn.execute(
                    """
                    INSERT INTO coworking_booking_intents (
                        idempotency_key, slack_user_id, requested_by_slack_id,
                        booking_date, channel_id, thread_ts, status,
                        next_attempt_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'batch_pending', ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        thread_ts = excluded.thread_ts,
                        status = CASE
                            WHEN coworking_booking_intents.status = 'batch_confirmed'
                            THEN coworking_booking_intents.status
                            WHEN coworking_booking_intents.status = 'batch_processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.status
                            ELSE 'batch_pending'
                        END,
                        next_attempt_at = CASE
                            WHEN coworking_booking_intents.status = 'batch_confirmed'
                            THEN coworking_booking_intents.next_attempt_at
                            WHEN coworking_booking_intents.status = 'batch_processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.next_attempt_at
                            ELSE ?
                        END,
                        locked_until = CASE
                            WHEN coworking_booking_intents.status = 'batch_processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.locked_until
                            ELSE NULL
                        END,
                        locked_by = CASE
                            WHEN coworking_booking_intents.status = 'batch_processing'
                                 AND coworking_booking_intents.locked_until > ?
                            THEN coworking_booking_intents.locked_by
                            ELSE NULL
                        END,
                        last_error = CASE
                            WHEN coworking_booking_intents.status = 'batch_confirmed'
                            THEN coworking_booking_intents.last_error
                            ELSE NULL
                        END,
                        updated_at = ?
                    """,
                    (
                        idempotency_key, encoded_targets, admin_id,
                        str(booking_date).strip(), channel_id, thread_ts,
                        current_time, current_time, current_time,
                        current_time, current_time, current_time,
                        current_time, current_time, current_time,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return dict(row)

    @staticmethod
    def batch_target_user_ids(intent: dict[str, Any]) -> list[str]:
        try:
            targets = json.loads(str(intent.get("slack_user_id") or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Batch intent target data is invalid") from exc
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(value, str) or not value.strip() for value in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("Batch intent target data is invalid")
        return targets

    def reserve_batch_for_processing(
        self,
        intent_id: int,
        *,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        owner = owner or f"roo-batch-{uuid4().hex}"
        locked_until = current_time + lease_seconds
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET status = 'batch_processing',
                        attempt_count = attempt_count + 1,
                        locked_until = ?, locked_by = ?, updated_at = ?
                    WHERE id = ?
                      AND (
                        status IN ('batch_pending', 'batch_pending_retry')
                        OR (
                            status = 'batch_processing'
                            AND (locked_until IS NULL OR locked_until <= ?)
                        )
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

    def claim_due_batches(
        self,
        *,
        limit: int = 10,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        owner = owner or f"roo-batch-worker-{uuid4().hex}"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id FROM coworking_booking_intents
                    WHERE (
                        status IN ('batch_pending', 'batch_pending_retry')
                        AND next_attempt_at <= ?
                    ) OR (
                        status = 'batch_processing'
                        AND (locked_until IS NULL OR locked_until <= ?)
                    )
                    ORDER BY next_attempt_at ASC, id ASC LIMIT ?
                    """,
                    (current_time, current_time, int(limit)),
                ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            reserved = self.reserve_batch_for_processing(
                int(row["id"]), owner=owner, lease_seconds=lease_seconds
            )
            if reserved:
                claimed.append(reserved)
        return claimed

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
        owner: str,
        backend_result: Optional[dict[str, Any]] = None,
        notification_required: bool = False,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        backend_result = backend_result or {}
        backend_booking_id = backend_result.get("id")
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
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
                        notification_status = ?,
                        notification_next_attempt_at = ?,
                        notification_locked_until = NULL,
                        notification_locked_by = NULL,
                        notification_last_error = NULL,
                        notification_delivered_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                    """,
                    (
                        str(backend_booking_id) if backend_booking_id else None,
                        json.dumps(backend_result, sort_keys=True),
                        current_time,
                        "pending" if notification_required else "not_required",
                        current_time if notification_required else None,
                        current_time,
                        int(intent_id),
                        str(owner),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def reserve_notification(
        self,
        intent_id: int,
        *,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        owner = owner or f"roo-notification-{uuid4().hex}"
        locked_until = current_time + lease_seconds
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        notification_status = 'delivering',
                        notification_attempt_count = notification_attempt_count + 1,
                        notification_locked_until = ?,
                        notification_locked_by = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('confirmed', 'blocked', 'batch_blocked')
                      AND (
                        notification_status IN ('pending', 'pending_retry')
                        OR (
                            notification_status = 'delivering'
                            AND (
                                notification_locked_until IS NULL
                                OR notification_locked_until <= ?
                            )
                        )
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

    def claim_due_notifications(
        self,
        *,
        limit: int = 10,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        owner = owner or f"roo-notification-worker-{uuid4().hex}"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id
                    FROM coworking_booking_intents
                    WHERE status IN ('confirmed', 'blocked', 'batch_blocked')
                      AND (
                        (
                            notification_status IN ('pending', 'pending_retry')
                            AND notification_next_attempt_at <= ?
                        )
                        OR (
                            notification_status = 'delivering'
                            AND (
                                notification_locked_until IS NULL
                                OR notification_locked_until <= ?
                            )
                        )
                      )
                    ORDER BY notification_next_attempt_at ASC, id ASC
                    LIMIT ?
                    """,
                    (current_time, current_time, int(limit)),
                ).fetchall()

        claimed: list[dict[str, Any]] = []
        for row in rows:
            reserved = self.reserve_notification(
                int(row["id"]),
                owner=owner,
                lease_seconds=lease_seconds,
            )
            if reserved:
                claimed.append(reserved)
        return claimed

    def mark_notification_delivered(
        self,
        intent_id: int,
        *,
        owner: str,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        notification_status = 'delivered',
                        notification_next_attempt_at = NULL,
                        notification_locked_until = NULL,
                        notification_locked_by = NULL,
                        notification_last_error = NULL,
                        notification_delivered_at = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('confirmed', 'blocked', 'batch_blocked')
                      AND notification_status = 'delivering'
                      AND notification_locked_by = ?
                    """,
                    (current_time, current_time, int(intent_id), str(owner)),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def mark_notification_retryable_failure(
        self,
        intent_id: int,
        *,
        owner: str,
        error: str,
        delay_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        row = self.get(intent_id)
        attempts = int(row.get("notification_attempt_count") or 1) if row else 1
        delay = retry_delay_seconds(attempts) if delay_seconds is None else delay_seconds
        next_attempt_at = current_time + delay
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        notification_status = 'pending_retry',
                        notification_next_attempt_at = ?,
                        notification_locked_until = NULL,
                        notification_locked_by = NULL,
                        notification_last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('confirmed', 'blocked', 'batch_blocked')
                      AND notification_status = 'delivering'
                      AND notification_locked_by = ?
                    """,
                    (
                        next_attempt_at,
                        str(error),
                        current_time,
                        int(intent_id),
                        str(owner),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                updated = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(updated)

    def mark_retryable_failure(
        self,
        intent_id: int,
        *,
        owner: str,
        error: str,
        delay_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        row = self.get(intent_id)
        attempts = int(row.get("attempt_count") or 1) if row else 1
        delay = retry_delay_seconds(attempts) if delay_seconds is None else delay_seconds
        next_attempt_at = current_time + delay
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
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
                      AND status = 'processing'
                      AND locked_by = ?
                    """,
                    (
                        next_attempt_at,
                        str(error),
                        current_time,
                        int(intent_id),
                        str(owner),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                updated = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(updated)

    def mark_batch_retryable_failure(
        self,
        intent_id: int,
        *,
        owner: str,
        error: str,
        delay_seconds: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        row = self.get(intent_id)
        attempts = int(row.get("attempt_count") or 1) if row else 1
        delay = retry_delay_seconds(attempts) if delay_seconds is None else delay_seconds
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET status = 'batch_pending_retry', next_attempt_at = ?,
                        locked_until = NULL, locked_by = NULL,
                        last_error = ?, updated_at = ?
                    WHERE id = ? AND status = 'batch_processing' AND locked_by = ?
                    """,
                    (current_time + delay, str(error), current_time, int(intent_id), str(owner)),
                )
                if cursor.rowcount != 1:
                    return None
                updated = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(updated)

    def mark_batch_blocked(
        self,
        intent_id: int,
        *,
        owner: str,
        error: str,
        notification_required: bool = True,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET status = 'batch_blocked', locked_until = NULL,
                        locked_by = NULL, last_error = ?,
                        notification_status = ?,
                        notification_next_attempt_at = ?,
                        notification_locked_until = NULL,
                        notification_locked_by = NULL,
                        notification_last_error = NULL,
                        notification_delivered_at = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'batch_processing' AND locked_by = ?
                    """,
                    (
                        str(error),
                        "pending" if notification_required else "not_required",
                        current_time if notification_required else None,
                        current_time,
                        int(intent_id), str(owner),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def mark_batch_confirmed(
        self,
        intent_id: int,
        *,
        owner: str,
        backend_result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Commit the batch outcome and every member notification atomically."""
        self._ensure_schema()
        current_time = _now()
        results = backend_result.get("results")
        if not isinstance(results, list):
            raise ValueError("A validated batch result is required")
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                batch_row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?", (int(intent_id),)
                ).fetchone()
                if (
                    batch_row is None
                    or batch_row["status"] != "batch_processing"
                    or batch_row["locked_by"] != str(owner)
                ):
                    conn.rollback()
                    return None
                admin_id = str(batch_row["requested_by_slack_id"] or "")
                booking_date = str(batch_row["booking_date"])
                from .clients.mlai_backend import validate_coworking_booking_batch_result

                backend_result = validate_coworking_booking_batch_result(
                    backend_result,
                    expected_date=booking_date,
                    expected_admin_slack_user_id=admin_id,
                    expected_target_slack_user_ids=self.batch_target_user_ids(
                        dict(batch_row)
                    ),
                )
                child_ids: list[int] = []
                for result in results:
                    slack_user_id = str(result["slack_user_id"])
                    booking_result = {
                        **dict(result["booking"]),
                        "standard_points_cost": result["standard_points_cost"],
                        "monthly_update_discount_applied": result[
                            "monthly_update_discount_applied"
                        ],
                        "founder_tools_explicitly_linked": result[
                            "founder_tools_explicitly_linked"
                        ],
                        "founder_tools_connection_type": result[
                            "founder_tools_connection_type"
                        ],
                        "founder_tools_account_linked": result[
                            "founder_tools_account_linked"
                        ],
                    }
                    if backend_result.get("operation_replayed") is True:
                        booking_result["operation_replayed"] = True
                    child_key = build_coworking_intent_key(slack_user_id, booking_date)
                    existing = conn.execute(
                        "SELECT idempotency_key, status, notification_status, "
                        "backend_booking_id "
                        "FROM coworking_booking_intents "
                        "WHERE idempotency_key = ? "
                        "OR substr(idempotency_key, 1, ?) = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (
                            child_key,
                            len(f"{child_key}:"),
                            f"{child_key}:",
                        ),
                    ).fetchone()
                    if existing:
                        child_key = str(existing["idempotency_key"])
                    same_booking = bool(
                        existing
                        and str(existing["backend_booking_id"] or "")
                        == str(booking_result["id"])
                    )
                    preserve_notification = bool(
                        existing
                        and same_booking
                        and existing["status"] == "confirmed"
                        and existing["notification_status"]
                        in {"delivered", "delivering", "reconciliation_required"}
                    )
                    if existing and not same_booking:
                        # A new backend booking is a new notification lifecycle,
                        # even when the member/date tuple is unchanged.
                        child_key = f"{child_key}:{uuid4().hex}"
                        existing = None
                    conn.execute(
                        """
                        INSERT INTO coworking_booking_intents (
                            idempotency_key, slack_user_id, requested_by_slack_id,
                            booking_date, channel_id, thread_ts, status,
                            next_attempt_at, backend_booking_id, backend_result_json,
                            created_at, updated_at, confirmed_at,
                            notification_status, notification_next_attempt_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, 'pending', ?)
                        ON CONFLICT(idempotency_key) DO UPDATE SET
                            requested_by_slack_id = excluded.requested_by_slack_id,
                            channel_id = excluded.channel_id,
                            thread_ts = excluded.thread_ts,
                            status = 'confirmed', locked_until = NULL, locked_by = NULL,
                            last_error = NULL,
                            backend_booking_id = excluded.backend_booking_id,
                            backend_result_json = excluded.backend_result_json,
                            confirmed_at = excluded.confirmed_at,
                            notification_status = ?,
                            notification_attempt_count = CASE WHEN ? THEN notification_attempt_count ELSE 0 END,
                            notification_next_attempt_at = CASE WHEN ? THEN notification_next_attempt_at ELSE excluded.notification_next_attempt_at END,
                            notification_locked_until = CASE WHEN ? AND notification_status = 'delivering' THEN notification_locked_until ELSE NULL END,
                            notification_locked_by = CASE WHEN ? AND notification_status = 'delivering' THEN notification_locked_by ELSE NULL END,
                            notification_last_error = CASE WHEN ? THEN notification_last_error ELSE NULL END,
                            notification_delivered_at = CASE WHEN ? AND notification_status = 'delivered' THEN notification_delivered_at ELSE NULL END,
                            updated_at = excluded.updated_at
                        """,
                        (
                            child_key, slack_user_id, admin_id, booking_date,
                            batch_row["channel_id"], batch_row["thread_ts"], current_time,
                            str(booking_result["id"]), json.dumps(booking_result, sort_keys=True),
                            current_time, current_time, current_time, current_time,
                            existing["notification_status"] if preserve_notification else "pending",
                            preserve_notification, preserve_notification, preserve_notification,
                            preserve_notification, preserve_notification, preserve_notification,
                        ),
                    )
                    child = conn.execute(
                        "SELECT id FROM coworking_booking_intents WHERE idempotency_key = ?",
                        (child_key,),
                    ).fetchone()
                    child_ids.append(int(child["id"]))
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET status = 'batch_confirmed', locked_until = NULL,
                        locked_by = NULL, last_error = NULL,
                        backend_result_json = ?, confirmed_at = ?,
                        notification_status = 'not_required',
                        notification_next_attempt_at = NULL, updated_at = ?
                    WHERE id = ? AND status = 'batch_processing' AND locked_by = ?
                    """,
                    (json.dumps(backend_result, sort_keys=True), current_time, current_time,
                     int(intent_id), str(owner)),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
                batch = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?", (int(intent_id),)
                ).fetchone()
                return {"batch": dict(batch), "child_intent_ids": child_ids}

    def mark_blocked(
        self,
        intent_id: int,
        *,
        owner: str,
        error: str,
        notification_required: bool = False,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        current_time = _now()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE coworking_booking_intents
                    SET
                        status = 'blocked',
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = ?,
                        notification_status = ?,
                        notification_next_attempt_at = ?,
                        notification_locked_until = NULL,
                        notification_locked_by = NULL,
                        notification_last_error = NULL,
                        notification_delivered_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                    """,
                    (
                        str(error),
                        "pending" if notification_required else "not_required",
                        current_time if notification_required else None,
                        current_time,
                        int(intent_id),
                        str(owner),
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM coworking_booking_intents WHERE id = ?",
                    (int(intent_id),),
                ).fetchone()
                return dict(row)

    def purge_terminal(
        self,
        *,
        retention_days: int = DEFAULT_TERMINAL_RETENTION_DAYS,
    ) -> int:
        """Delete old intents only after mutation and notification are terminal."""
        self._ensure_schema()
        days = int(retention_days)
        if days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = _now() - (days * 24 * 60 * 60)
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM coworking_booking_intents
                    WHERE updated_at < ?
                      AND (
                        status IN ('blocked', 'confirmed')
                        AND notification_status IN ('delivered', 'not_required')
                        OR status IN ('batch_blocked', 'batch_confirmed')
                        AND notification_status IN ('delivered', 'not_required')
                      )
                    """,
                    (cutoff,),
                )
                return max(0, int(cursor.rowcount))

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


def _safe_post_message(
    *,
    channel_id: Optional[str],
    thread_ts: Optional[str],
    text: str,
    client_msg_id: Optional[str] = None,
) -> bool:
    if not channel_id or not text:
        return False
    from .slack_client import post_message

    options = {"client_msg_id": client_msg_id} if client_msg_id else {}
    try:
        response = post_message(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            **options,
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        try:
            duplicate_message = bool(
                client_msg_id
                and error_response is not None
                and error_response.get("error") == "duplicate_message"
            )
        except (AttributeError, TypeError):
            duplicate_message = False
        if duplicate_message:
            return True
        raise
    if not response or not response.get("ok"):
        raise RuntimeError("Slack did not confirm coworking notification delivery")
    return True


def _safe_send_dm(
    *,
    user_id: str,
    text: str,
    client_msg_id: str,
) -> bool:
    if not user_id or not text:
        return False
    from .slack_client import send_dm

    try:
        response = send_dm(
            user_id,
            text,
            client_msg_id=client_msg_id,
            raise_on_error=True,
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        try:
            duplicate_message = bool(
                error_response is not None
                and error_response.get("error") == "duplicate_message"
            )
        except (AttributeError, TypeError):
            duplicate_message = False
        if duplicate_message:
            return True
        raise
    return bool(
        response
        and (
            response.get("ok")
            or response.get("error") == "duplicate_message"
        )
    )


async def _deliver_coworking_retry_confirmation(
    *,
    client: Any,
    backend_result: dict[str, Any],
    slack_user_id: str,
    requested_by_slack_id: str,
    booking_date: str,
    channel_id: Optional[str],
    thread_ts: Optional[str],
    executor: Any = None,
    post_public_message: bool = True,
    private_client_msg_id: Optional[str] = None,
) -> dict[str, Any]:
    # Import lazily because SkillExecutor owns the immediate delivery path and
    # imports this module for intent persistence.
    from .skills.executor import SkillExecutor

    executor = executor or SkillExecutor()
    delivery = await executor._deliver_coworking_booking_success(
        client=client,
        backend_result=backend_result,
        booking_date=booking_date,
        target_user_id=slack_user_id,
        requested_by_user_id=requested_by_slack_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        admin_checkin=(requested_by_slack_id != slack_user_id),
        client_msg_id=private_client_msg_id,
    )
    delivery_data = delivery.get("data") if isinstance(delivery, dict) else None
    delivery_data = delivery_data if isinstance(delivery_data, dict) else {}
    delivery_mode = str(delivery_data.get("delivery") or "")
    private_delivered = delivery_data.get("dm_delivered") is True

    if delivery_mode == "current_direct_message":
        private_delivered = _safe_post_message(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=str(delivery.get("message") or ""),
            client_msg_id=private_client_msg_id,
        )
    elif post_public_message and not delivery.get("suppress_post"):
        try:
            _safe_post_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=str(delivery.get("message") or ""),
                client_msg_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"roo:coworking-public-confirmation:{backend_result['id']}",
                    )
                ),
            )
        except Exception as exc:
            # The target's private confirmation is the durable obligation.
            # A failed public acknowledgement must not trigger a duplicate DM.
            print(
                "🏢 coworking_public_confirmation_delivery_failed "
                f"exc_type={exc.__class__.__name__}"
            )

    delivery["private_delivery_confirmed"] = private_delivered
    return delivery


async def deliver_coworking_booking_notification(
    intent: dict[str, Any],
    *,
    store: Optional[CoworkingBookingIntentStore] = None,
    client: Any = None,
    executor: Any = None,
    post_public_message: Optional[bool] = None,
    blocked_message: Optional[str] = None,
) -> dict[str, Any]:
    """Deliver a confirmed booking notification with durable retry ownership."""
    store = store or get_coworking_intent_store()
    client = client or _build_backend_client()
    intent_id = int(intent["id"])
    notification_owner = str(intent.get("notification_locked_by") or "")
    if not notification_owner:
        raise ValueError("A leased notification owner is required")
    booking_date = str(intent["booking_date"])
    slack_user_id = str(intent["slack_user_id"])
    requested_by_slack_id = str(intent.get("requested_by_slack_id") or slack_user_id)

    mutation_status = str(intent.get("status") or "")
    delivery = None
    try:
        if mutation_status == "batch_blocked":
            error_code = str(intent.get("last_error") or "unexpected_error")
            delivered = _safe_send_dm(
                user_id=requested_by_slack_id,
                text=_coworking_batch_retry_blocked_message(
                    booking_date=booking_date,
                ),
                client_msg_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"roo:coworking-batch-rejection:{intent_id}:{error_code}",
                    )
                ),
            )
            if not delivered:
                raise RuntimeError(
                    "Slack did not confirm coworking batch rejection delivery"
                )
            delivery = {"mode": "requester_direct_message"}
            backend_result = None
        elif mutation_status == "blocked":
            error_code = str(intent.get("last_error") or "unexpected_error")
            delivered = _safe_post_message(
                channel_id=intent.get("channel_id"),
                thread_ts=intent.get("thread_ts"),
                text=(
                    blocked_message
                    or _coworking_retry_blocked_message(
                        slack_user_id=slack_user_id,
                        requested_by_slack_id=requested_by_slack_id,
                        error=error_code,
                    )
                ),
                client_msg_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"roo:coworking-rejection:{intent_id}:{error_code}",
                    )
                ),
            )
            if not delivered:
                raise RuntimeError("Slack did not confirm coworking rejection delivery")
            delivery = {"mode": "original_context"}
            backend_result = None
        else:
            backend_result = json.loads(str(intent.get("backend_result_json") or ""))
            from .clients.mlai_backend import validate_coworking_booking_result

            backend_result = validate_coworking_booking_result(
                backend_result,
                expected_date=booking_date,
            )
            current_rows = await client.get_my_bookings(
                slack_user_id,
                booking_id=str(backend_result["id"]),
            )
            if not isinstance(current_rows, list) or len(current_rows) > 1:
                raise ValueError("Backend returned invalid current booking state")
            if current_rows:
                current_row = current_rows[0]
                if (
                    not isinstance(current_row, dict)
                    or str(current_row.get("id") or "") != str(backend_result["id"])
                    or current_row.get("status") not in {"booked", "cancelled"}
                ):
                    raise ValueError("Backend returned contradictory current booking state")
                current_status = str(current_row["status"])
            else:
                current_status = "deleted"
            backend_result = {
                **backend_result,
                "booking_state_refreshed": True,
                "operation_booking_current_status": current_status,
            }
            private_client_msg_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"roo:coworking-confirmation:{backend_result['id']}",
                )
            )
            should_post_public = (
                False
                if requested_by_slack_id != slack_user_id
                else (
                    int(intent.get("notification_attempt_count") or 0) <= 1
                    if post_public_message is None
                    else bool(post_public_message)
                )
            )
            delivery = await _deliver_coworking_retry_confirmation(
                client=client,
                backend_result=backend_result,
                slack_user_id=slack_user_id,
                requested_by_slack_id=requested_by_slack_id,
                booking_date=booking_date,
                channel_id=intent.get("channel_id"),
                thread_ts=intent.get("thread_ts"),
                executor=executor,
                post_public_message=should_post_public,
                private_client_msg_id=private_client_msg_id,
            )
            if delivery.get("private_delivery_confirmed") is not True:
                raise RuntimeError("Slack did not confirm private coworking notification")
    except Exception as exc:
        error = coworking_failure_code(exc)
        updated = store.mark_notification_retryable_failure(
            intent_id,
            owner=notification_owner,
            error=error,
        )
        if updated is None:
            return {
                "status": "stale",
                "notification_status": "stale",
                "intent_id": intent_id,
            }
        print(
            "🏢 coworking_notification_retryable_failure "
            f"intent_id={intent_id} attempts={updated.get('notification_attempt_count')} "
            f"next_attempt_at={updated.get('notification_next_attempt_at')} "
            f"exc_type={exc.__class__.__name__}"
        )
        return {
            "status": mutation_status,
            "notification_status": updated.get("notification_status"),
            "delivery": delivery,
            "error": error,
            "intent_id": intent_id,
        }

    delivered = store.mark_notification_delivered(
        intent_id,
        owner=notification_owner,
    )
    if delivered is None:
        return {
            "status": "stale",
            "notification_status": "stale",
            "intent_id": intent_id,
        }
    return {
        "status": mutation_status,
        "notification_status": delivered.get("notification_status"),
        "backend_result": backend_result,
        "delivery": delivery,
        "intent_id": intent_id,
    }


def _coworking_retry_blocked_message(
    *,
    slack_user_id: str,
    requested_by_slack_id: str,
    error: str,
) -> str:
    if requested_by_slack_id and requested_by_slack_id != slack_user_id:
        return (
            "I retried the coworking check-in for "
            f"<@{slack_user_id}>, but I still couldn't process it. "
            "No new booking was created. Please try again or contact an MLAI admin."
        )
    return (
        "I retried your coworking booking request, but I still couldn't process it. "
        "No new booking was created. Please try again or contact an MLAI admin."
    )


def _coworking_batch_retry_blocked_message(*, booking_date: str) -> str:
    return (
        "I retried your queued multi-person coworking check-in for "
        f"**{booking_date}**, but MLAI backend rejected it. No new bookings "
        "were created. Please review availability, member balances, and your "
        "admin access, then submit the batch again."
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
    mutation_owner = str(intent.get("locked_by") or "")
    if not mutation_owner:
        return {"status": "stale", "intent_id": intent_id}
    channel_id = intent.get("channel_id")
    thread_ts = intent.get("thread_ts")

    try:
        backend_result = await client.book_coworking(
            slack_user_id,
            booking_date,
            channel_id,
            operation_id=build_coworking_operation_id(str(intent["idempotency_key"])),
        )
        from .clients.mlai_backend import validate_coworking_booking_result

        backend_result = validate_coworking_booking_result(
            backend_result,
            expected_date=booking_date,
        )
    except Exception as exc:
        error = coworking_failure_code(exc)
        if is_retryable_coworking_exception(exc):
            updated = store.mark_retryable_failure(
                intent_id,
                owner=mutation_owner,
                error=error,
            )
            if updated is None:
                return {"status": "stale", "intent_id": intent_id}
            print(
                "🏢 coworking_intent_retryable_failure "
                f"intent_id={intent_id} attempts={updated.get('attempt_count')} "
                f"next_attempt_at={updated.get('next_attempt_at')} "
                f"exc_type={exc.__class__.__name__}"
            )
            return {"status": "pending_retry", "error": error, "intent_id": intent_id}

        blocked = store.mark_blocked(
            intent_id,
            owner=mutation_owner,
            error=error,
            notification_required=notify,
        )
        if blocked is None:
            return {"status": "stale", "intent_id": intent_id}
        if not notify:
            return {"status": "blocked", "error": error, "intent_id": intent_id}
        notification = store.reserve_notification(
            intent_id,
            owner=f"roo-booking-rejection-{uuid4().hex}",
        )
        if notification is None:
            return {
                "status": "blocked",
                "notification_status": blocked.get("notification_status"),
                "error": error,
                "intent_id": intent_id,
            }
        return await deliver_coworking_booking_notification(
            notification,
            store=store,
            client=client,
        )

    confirmed = store.mark_confirmed(
        intent_id,
        owner=mutation_owner,
        backend_result=backend_result,
        notification_required=notify,
    )
    if confirmed is None:
        return {"status": "stale", "intent_id": intent_id}
    if not notify:
        return {
            "status": "confirmed",
            "notification_status": "not_required",
            "backend_result": backend_result,
            "intent_id": intent_id,
        }

    notification = store.reserve_notification(
        intent_id,
        owner=f"roo-booking-notification-{uuid4().hex}",
    )
    if notification is None:
        return {
            "status": "confirmed",
            "notification_status": confirmed.get("notification_status"),
            "backend_result": backend_result,
            "intent_id": intent_id,
        }
    return await deliver_coworking_booking_notification(
        notification,
        store=store,
        client=client,
    )


async def process_coworking_booking_batch_intent(
    intent: dict[str, Any],
    *,
    store: Optional[CoworkingBookingIntentStore] = None,
    client: Any = None,
) -> dict[str, Any]:
    """Retry one durable admin batch while preserving atomic backend semantics."""
    store = store or get_coworking_intent_store()
    client = client or _build_backend_client()
    intent_id = int(intent["id"])
    owner = str(intent.get("locked_by") or "")
    if not owner:
        return {"status": "stale", "intent_id": intent_id}
    try:
        targets = store.batch_target_user_ids(intent)
        backend_result = await client.book_coworking_many(
            admin_slack_user_id=str(intent.get("requested_by_slack_id") or ""),
            target_slack_user_ids=targets,
            booking_date=str(intent["booking_date"]),
            slack_channel_id=intent.get("channel_id"),
            operation_id=build_coworking_operation_id(str(intent["idempotency_key"])),
        )
    except Exception as exc:
        error = coworking_failure_code(exc)
        if is_retryable_coworking_exception(exc):
            updated = store.mark_batch_retryable_failure(intent_id, owner=owner, error=error)
            if updated is None:
                return {"status": "stale", "intent_id": intent_id}
            return {"status": "batch_pending_retry", "error": error, "intent_id": intent_id}
        blocked = store.mark_batch_blocked(intent_id, owner=owner, error=error)
        if blocked is None:
            return {"status": "stale", "error": error, "intent_id": intent_id}
        notification = store.reserve_notification(
            intent_id,
            owner=f"roo-batch-rejection-{uuid4().hex}",
        )
        if notification is None:
            return {
                "status": "batch_blocked",
                "notification_status": blocked.get("notification_status"),
                "error": error,
                "intent_id": intent_id,
            }
        return await deliver_coworking_booking_notification(
            notification,
            store=store,
            client=client,
            post_public_message=False,
        )
    confirmed = store.mark_batch_confirmed(
        intent_id, owner=owner, backend_result=backend_result
    )
    if confirmed is None:
        return {"status": "stale", "intent_id": intent_id}
    return {
        "status": "batch_confirmed",
        "intent_id": intent_id,
        "child_intent_ids": confirmed["child_intent_ids"],
    }


async def coworking_booking_retry_loop(
    *,
    store: Optional[CoworkingBookingIntentStore] = None,
    poll_seconds: Optional[float] = None,
    health_reporter: Optional[Callable[[dict[str, Any]], None]] = None,
) -> None:
    settings = get_settings()
    store = store or get_coworking_intent_store()
    poll_interval = float(
        poll_seconds
        if poll_seconds is not None
        else getattr(settings, "COWORKING_RETRY_POLL_SECONDS", DEFAULT_RETRY_POLL_SECONDS)
    )
    owner = f"roo-retry-worker-{uuid4().hex}"
    last_cleanup_at = 0.0
    consecutive_failures = 0
    print(f"🏢 Coworking booking retry worker started owner={owner} poll_seconds={poll_interval}")

    while True:
        try:
            current_time = _now()
            if current_time - last_cleanup_at >= TERMINAL_CLEANUP_INTERVAL_SECONDS:
                deleted = store.purge_terminal(
                    retention_days=int(
                        getattr(
                            settings,
                            "COWORKING_INTENT_RETENTION_DAYS",
                            DEFAULT_TERMINAL_RETENTION_DAYS,
                        )
                    )
                )
                if deleted:
                    print(
                        "🏢 coworking_intent_retention_completed "
                        f"deleted_count={deleted}"
                    )
                last_cleanup_at = current_time
            due = store.claim_due(limit=10, owner=owner)
            for intent in due:
                await process_coworking_booking_intent(intent, store=store, notify=True)
            batch_due = store.claim_due_batches(limit=10, owner=f"{owner}-batches")
            for intent in batch_due:
                await process_coworking_booking_batch_intent(intent, store=store)
            notification_due = store.claim_due_notifications(
                limit=10,
                owner=f"{owner}-notifications",
            )
            for intent in notification_due:
                await deliver_coworking_booking_notification(
                    intent,
                    store=store,
                )
            consecutive_failures = 0
            if health_reporter is not None:
                health_reporter(
                    {
                        "status": "ok",
                        "consecutive_failures": 0,
                        "last_success_at": _now(),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            if health_reporter is not None:
                health_reporter(
                    {
                        "status": "degraded",
                        "consecutive_failures": consecutive_failures,
                        "last_error_type": exc.__class__.__name__,
                        "last_failure_at": _now(),
                    }
                )
            print(
                "🏢 coworking_retry_worker_error "
                f"exc_type={exc.__class__.__name__}"
            )
        await asyncio.sleep(poll_interval)
