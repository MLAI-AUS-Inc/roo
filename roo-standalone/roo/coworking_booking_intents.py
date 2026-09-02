from __future__ import annotations

import asyncio
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
                      AND status IN ('confirmed', 'blocked')
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
                    WHERE status IN ('confirmed', 'blocked')
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
                      AND status IN ('confirmed', 'blocked')
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
                      AND status IN ('confirmed', 'blocked')
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
        if mutation_status == "blocked":
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
            private_client_msg_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"roo:coworking-confirmation:{backend_result['id']}",
                )
            )
            should_post_public = (
                int(intent.get("notification_attempt_count") or 0) <= 1
                if post_public_message is None
                else bool(post_public_message)
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
