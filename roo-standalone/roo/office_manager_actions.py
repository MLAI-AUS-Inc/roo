"""Durable processing for acknowledged Office Manager Slack actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
# A private Slack delivery can require both conversations.open and
# chat.postMessage. Keep replacement workers fenced for longer than the
# bounded Slack client calls, even if the ordinary lease heartbeat fails while
# one of those calls is in flight.
OFFICE_MANAGER_SLACK_DELIVERY_LEASE_SECONDS = 5 * 60.0
DEFAULT_RETRY_POLL_SECONDS = 5.0
DEFAULT_HOUSEKEEPING_POLL_SECONDS = 60 * 60.0
COMPLETED_RETENTION_SECONDS = 90 * 24 * 60 * 60
TERMINAL_FAILURE_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_OFFICE_MANAGER_GENERATION = (2**31) - 1


def _retry_delay(attempt_count: int) -> float:
    """Return capped exponential backoff for a failed processing attempt."""
    bounded_attempt = max(1, min(int(attempt_count), 16))
    return min(5 * 60.0, 5.0 * (2 ** (bounded_attempt - 1)))


def _canonical_attempt_id(value: str) -> str:
    """Return a canonical UUID or reject an invalid durable operation identity."""
    candidate = str(value or "").strip()
    try:
        parsed = UUID(candidate)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("attempt_id must be a canonical UUID") from exc
    if str(parsed) != candidate:
        raise ValueError("attempt_id must be a canonical UUID")
    return candidate


def canonical_office_manager_generation(value: Any = 1) -> int:
    """Return a positive PostgreSQL-int4-safe generation or reject the value."""
    if (
        type(value) is not int
        or value < 1
        or value > MAX_OFFICE_MANAGER_GENERATION
    ):
        raise ValueError("generation must be a positive canonical integer")
    return value


def build_office_manager_feedback_client_msg_id(attempt_id: str) -> str:
    """Return the stable Slack message identity for one terminal result."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"{_canonical_attempt_id(attempt_id)}:private-feedback",
        )
    )


def build_office_manager_supersession_client_msg_id(attempt_id: str) -> str:
    """Return a distinct stable identity for a later correction message."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"{_canonical_attempt_id(attempt_id)}:supersession-feedback",
        )
    )


def build_office_manager_reconciled_feedback_client_msg_id(
    attempt_id: str,
    *,
    booking_date: str,
    outcome: str,
) -> str:
    """Return a stable identity for corrected feedback after a backend re-read."""
    return str(
        uuid5(
            NAMESPACE_URL,
            (
                f"{_canonical_attempt_id(attempt_id)}:reconciled-feedback:"
                f"{str(booking_date).strip()}:{str(outcome).strip()}"
            ),
        )
    )


def build_office_manager_uncertainty_client_msg_id(attempt_id: str) -> str:
    """Return the stable Slack message identity for the optional retry notice."""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"{_canonical_attempt_id(attempt_id)}:uncertainty-notice",
        )
    )


def build_office_manager_action_occurrence_key(
    *,
    slack_team_id: str,
    slack_user_id: str,
    channel_id: str,
    action_id: str,
    action_ts: str,
    message_ts: str,
    booking_date: str,
    generation: int = 1,
) -> str:
    """Hash immutable Slack action fields into one logical click identity."""
    canonical_action_ts = str(action_ts or "").strip()
    if not canonical_action_ts:
        raise ValueError("action_ts is required for a logical action identity")
    canonical_generation = canonical_office_manager_generation(generation)
    identity = {
        "action_id": str(action_id or "").strip(),
        "action_ts": canonical_action_ts,
        "booking_date": str(booking_date or "").strip(),
        "channel_id": str(channel_id or "").strip(),
        "message_ts": str(message_ts or "").strip(),
        "slack_team_id": str(slack_team_id or "").strip(),
        "slack_user_id": str(slack_user_id or "").strip(),
    }
    # Generation 1 predates the explicit epoch field. Preserve its historical
    # occurrence hash so a resigned retry after rollout cannot create a second
    # durable attempt. Reopened generation 2+ announcements bind the epoch.
    if canonical_generation > 1:
        identity["generation"] = canonical_generation
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OfficeManagerActionLeaseLostError(RuntimeError):
    """Raised when a replaced worker must stop without mutating or notifying."""


class OfficeManagerActionPermanentError(RuntimeError):
    """Raised when retrying cannot deliver to the immutable Slack target."""


class OfficeManagerActionStore:
    """SQLite outbox shared by Public Roo workers and process restarts."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(6):
            connection = sqlite3.connect(
                str(self.database_path),
                timeout=2,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute("PRAGMA journal_mode=WAL")
                return connection
            except sqlite3.OperationalError as exc:
                connection.close()
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.02 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _ensure_schema(self) -> None:
        """Initialize safely when multiple fresh processes share this database."""
        if self._initialized:
            return
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(7):
            try:
                self._initialize_schema_once()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.02 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _initialize_schema_once(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as connection:
                # Schema discovery and additive upgrades must be one
                # cross-process critical section. Otherwise two freshly
                # started workers can both observe a missing column and race
                # the same ALTER TABLE statement.
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS office_manager_action_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        attempt_id TEXT NOT NULL UNIQUE,
                        request_fingerprint TEXT UNIQUE,
                        action_occurrence_key TEXT UNIQUE,
                        slack_user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        booking_date TEXT NOT NULL,
                        generation INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        locked_until REAL,
                        locked_by TEXT,
                        last_error TEXT,
                        uncertainty_notice_attempted_at REAL,
                        feedback_text TEXT,
                        feedback_client_msg_id TEXT,
                        feedback_prepared_at REAL,
                        feedback_outcome TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_office_manager_actions_due
                    ON office_manager_action_outbox (status, next_attempt_at)
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(office_manager_action_outbox)"
                    ).fetchall()
                }
                for column_name, column_type in (
                    ("attempt_id", "TEXT"),
                    ("request_fingerprint", "TEXT"),
                    ("action_occurrence_key", "TEXT"),
                ):
                    if column_name not in columns:
                        connection.execute(
                            "ALTER TABLE office_manager_action_outbox "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
                missing_attempts = connection.execute(
                    """
                    SELECT id FROM office_manager_action_outbox
                    WHERE attempt_id IS NULL OR attempt_id = ''
                    """
                ).fetchall()
                for row in missing_attempts:
                    connection.execute(
                        """
                        UPDATE office_manager_action_outbox
                        SET attempt_id = ? WHERE id = ?
                        """,
                        (str(uuid4()), int(row["id"])),
                    )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_office_manager_actions_attempt_id
                    ON office_manager_action_outbox (attempt_id)
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_office_manager_actions_request_fingerprint
                    ON office_manager_action_outbox (request_fingerprint)
                    WHERE request_fingerprint IS NOT NULL
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_office_manager_actions_occurrence_key
                    ON office_manager_action_outbox (action_occurrence_key)
                    WHERE action_occurrence_key IS NOT NULL
                    """
                )
                if "uncertainty_notice_attempted_at" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE office_manager_action_outbox
                        ADD COLUMN uncertainty_notice_attempted_at REAL
                        """
                    )
                if "generation" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE office_manager_action_outbox
                        ADD COLUMN generation INTEGER NOT NULL DEFAULT 1
                        """
                    )
                for column_name, column_type in (
                    ("feedback_text", "TEXT"),
                    ("feedback_client_msg_id", "TEXT"),
                    ("feedback_prepared_at", "REAL"),
                    ("feedback_outcome", "TEXT"),
                ):
                    if column_name not in columns:
                        connection.execute(
                            "ALTER TABLE office_manager_action_outbox "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
            self._initialized = True

    def operability_snapshot(self, *, now: Optional[float] = None) -> dict[str, Any]:
        """Return content-free backlog signals for readiness and alerting."""
        self._ensure_schema()
        current_time = time.time() if now is None else float(now)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('pending', 'processing') THEN 1 ELSE 0 END)
                        AS non_terminal_count,
                    SUM(CASE
                        WHEN status IN ('pending', 'processing')
                         AND last_error IN (
                            'OfficeManagerClaimAuthenticationError',
                            'OfficeManagerSlackAuthenticationError'
                         )
                        THEN 1 ELSE 0
                    END) AS authentication_failure_count,
                    SUM(CASE
                        WHEN status IN ('pending', 'processing')
                         AND last_error = 'OfficeManagerClaimAuthenticationError'
                        THEN 1 ELSE 0
                    END) AS backend_authentication_failure_count,
                    SUM(CASE
                        WHEN status IN ('pending', 'processing')
                         AND last_error = 'OfficeManagerSlackAuthenticationError'
                        THEN 1 ELSE 0
                    END) AS slack_authentication_failure_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                        AS terminal_failure_count,
                    MIN(CASE
                        WHEN status = 'pending' AND next_attempt_at <= ?
                        THEN created_at
                    END) AS oldest_due_created_at
                FROM office_manager_action_outbox
                """,
                (current_time,),
            ).fetchone()
        oldest_due_created_at = (
            float(row["oldest_due_created_at"])
            if row and row["oldest_due_created_at"] is not None
            else None
        )
        return {
            "non_terminal_count": int((row or {})["non_terminal_count"] or 0),
            "authentication_failure_count": int(
                (row or {})["authentication_failure_count"] or 0
            ),
            "backend_authentication_failure_count": int(
                (row or {})["backend_authentication_failure_count"] or 0
            ),
            "slack_authentication_failure_count": int(
                (row or {})["slack_authentication_failure_count"] or 0
            ),
            "terminal_failure_count": int(
                (row or {})["terminal_failure_count"] or 0
            ),
            "oldest_due_age_seconds": (
                max(0.0, current_time - oldest_due_created_at)
                if oldest_due_created_at is not None
                else None
            ),
        }

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def record_action(
        self,
        *,
        slack_user_id: str,
        channel_id: str,
        booking_date: str,
        generation: int = 1,
        request_fingerprint: Optional[str] = None,
        action_occurrence_key: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one signed click and report whether it created durable work."""
        self._ensure_schema()
        current_time = time.time()
        slack_user_id = str(slack_user_id).strip()
        channel_id = str(channel_id).strip()
        booking_date = str(booking_date).strip()
        generation = canonical_office_manager_generation(generation)
        if request_fingerprint is None:
            request_fingerprint = uuid4().hex
        request_fingerprint = str(request_fingerprint).strip()
        if not request_fingerprint:
            raise ValueError("request_fingerprint is required")
        action_occurrence_key = str(action_occurrence_key or "").strip() or None
        self.prune_completed(now=current_time)

        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = None
                if action_occurrence_key is not None:
                    existing = connection.execute(
                        """
                        SELECT * FROM office_manager_action_outbox
                        WHERE action_occurrence_key = ?
                        """,
                        (action_occurrence_key,),
                    ).fetchone()
                if existing is None:
                    existing = connection.execute(
                        """
                        SELECT * FROM office_manager_action_outbox
                        WHERE request_fingerprint = ?
                        """,
                        (request_fingerprint,),
                    ).fetchone()
                if (
                    existing is not None
                    and canonical_office_manager_generation(existing["generation"])
                    != generation
                ):
                    connection.rollback()
                    raise ValueError(
                        "generation does not match the persisted action identity"
                    )
                should_process = existing is None
                if existing is None:
                    attempt_id = str(uuid4())
                    cursor = connection.execute(
                        """
                        INSERT INTO office_manager_action_outbox (
                            idempotency_key,
                            attempt_id,
                            request_fingerprint,
                            action_occurrence_key,
                            slack_user_id,
                            channel_id,
                            booking_date,
                            generation,
                            status,
                            next_attempt_at,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            attempt_id,
                            request_fingerprint,
                            action_occurrence_key,
                            slack_user_id,
                            channel_id,
                            booking_date,
                            generation,
                            current_time,
                            current_time,
                            current_time,
                        ),
                    )
                    action_id = int(cursor.lastrowid)
                elif (
                    action_occurrence_key is not None
                    and not str(existing["action_occurrence_key"] or "").strip()
                ):
                    connection.execute(
                        """
                        UPDATE office_manager_action_outbox
                        SET action_occurrence_key = ?, updated_at = ?
                        WHERE id = ? AND action_occurrence_key IS NULL
                        """,
                        (action_occurrence_key, current_time, int(existing["id"])),
                    )
                    action_id = int(existing["id"])
                else:
                    action_id = int(existing["id"])
                row = connection.execute(
                    """
                    SELECT * FROM office_manager_action_outbox
                    WHERE id = ?
                    """,
                    (action_id,),
                ).fetchone()
                connection.commit()
                return dict(row), should_process

    def prune_completed(self, *, now: Optional[float] = None) -> int:
        """Delete terminal personal data after retention, independent of ingress."""
        self._ensure_schema()
        current_time = time.time() if now is None else float(now)
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM office_manager_action_outbox
                    WHERE completed_at IS NOT NULL
                      AND (
                        (status = 'completed' AND completed_at <= ?)
                        OR (status = 'failed' AND completed_at <= ?)
                      )
                    """,
                    (
                        current_time - COMPLETED_RETENTION_SECONDS,
                        current_time - TERMINAL_FAILURE_RETENTION_SECONDS,
                    ),
                )
                return max(0, int(cursor.rowcount))

    def reserve(
        self,
        action_id: int,
        *,
        owner: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        """Lease a pending action to one worker."""
        self._ensure_schema()
        current_time = time.time()
        owner = owner or f"roo-office-manager-{uuid4().hex}"
        locked_until = current_time + max(1.0, float(lease_seconds))
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET
                        status = 'processing',
                        attempt_count = attempt_count + 1,
                        locked_until = ?,
                        locked_by = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND (
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
                        """
                        SELECT * FROM office_manager_action_outbox WHERE id = ?
                        """,
                        (int(action_id),),
                    ).fetchone()
                )

    def renew(
        self,
        action_id: int,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> bool:
        """Extend only the current worker's lease."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET locked_until = MAX(locked_until, ?), updated_at = ?
                    WHERE id = ? AND status = 'processing' AND locked_by = ?
                      AND locked_until IS NOT NULL
                      AND locked_until > ?
                    """,
                    (
                        current_time + max(1.0, float(lease_seconds)),
                        current_time,
                        int(action_id),
                        str(owner),
                        current_time,
                    ),
                )
                return cursor.rowcount == 1

    def renew_for_slack_delivery(self, action_id: int, *, owner: str) -> bool:
        """Fence replacement workers across one bounded private Slack send."""
        return self.renew(
            action_id,
            owner=owner,
            lease_seconds=OFFICE_MANAGER_SLACK_DELIVERY_LEASE_SECONDS,
        )

    def owns_lease(self, action_id: int, *, owner: str) -> bool:
        """Return whether this worker still owns a live processing lease."""
        self._ensure_schema()
        current_time = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM office_manager_action_outbox
                WHERE id = ?
                  AND status = 'processing'
                  AND locked_by = ?
                  AND locked_until IS NOT NULL
                  AND locked_until > ?
                """,
                (int(action_id), str(owner), current_time),
            ).fetchone()
        return row is not None

    def stage_feedback(
        self,
        action_id: int,
        *,
        owner: str,
        text: str,
        client_msg_id: str,
        outcome: str = "terminal",
    ) -> Optional[dict[str, Any]]:
        """Durably store a terminal result before attempting Slack delivery."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET
                        feedback_text = COALESCE(feedback_text, ?),
                        feedback_client_msg_id = COALESCE(feedback_client_msg_id, ?),
                        feedback_prepared_at = COALESCE(feedback_prepared_at, ?),
                        feedback_outcome = COALESCE(feedback_outcome, ?),
                        updated_at = ?
                    WHERE id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                      AND locked_until IS NOT NULL
                      AND locked_until > ?
                    """,
                    (
                        str(text),
                        str(client_msg_id),
                        current_time,
                        str(outcome),
                        current_time,
                        int(action_id),
                        str(owner),
                        current_time,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                return self._row(
                    connection.execute(
                        "SELECT * FROM office_manager_action_outbox WHERE id = ?",
                        (int(action_id),),
                    ).fetchone()
                )

    def supersede_feedback(
        self,
        action_id: int,
        *,
        owner: str,
        text: str,
        client_msg_id: str,
        outcome: str,
    ) -> Optional[dict[str, Any]]:
        """Replace staged feedback with a monotonic correction generation."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET feedback_text = ?, feedback_client_msg_id = ?,
                        feedback_prepared_at = ?, feedback_outcome = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'processing'
                      AND locked_by = ? AND locked_until > ?
                    """,
                    (
                        str(text),
                        str(client_msg_id),
                        current_time,
                        str(outcome),
                        current_time,
                        int(action_id),
                        str(owner),
                        current_time,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                return self._row(
                    connection.execute(
                        "SELECT * FROM office_manager_action_outbox WHERE id = ?",
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
        """Lease pending actions and processing actions whose worker disappeared."""
        self.prune_completed()
        current_time = time.time()
        owner = owner or f"roo-office-manager-retry-{uuid4().hex}"
        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id FROM office_manager_action_outbox
                    WHERE
                        (status = 'pending' AND next_attempt_at <= ?)
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
        """Record that the backend result and private feedback were handled."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET
                        status = 'completed',
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = NULL,
                        feedback_text = NULL,
                        feedback_client_msg_id = NULL,
                        feedback_prepared_at = NULL,
                        feedback_outcome = NULL,
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'processing' AND locked_by = ?
                    """,
                    (current_time, current_time, int(action_id), str(owner)),
                )
                return cursor.rowcount == 1

    def mark_terminal_failure(
        self,
        action_id: int,
        *,
        owner: str,
        reason: str,
    ) -> bool:
        """Stop retrying a permanent target error and redact its payload."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET status = 'failed', locked_until = NULL, locked_by = NULL,
                        last_error = ?, slack_user_id = '', channel_id = '',
                        booking_date = '', feedback_text = NULL,
                        feedback_client_msg_id = NULL,
                        feedback_prepared_at = NULL, feedback_outcome = NULL,
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'processing' AND locked_by = ?
                    """,
                    (
                        str(reason)[:500],
                        current_time,
                        current_time,
                        int(action_id),
                        str(owner),
                    ),
                )
                return cursor.rowcount == 1

    def claim_uncertainty_notice(self, action_id: int, *, owner: str) -> bool:
        """Claim the one allowed transient-status notification attempt."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET uncertainty_notice_attempted_at = ?, updated_at = ?
                    WHERE id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                      AND uncertainty_notice_attempted_at IS NULL
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
        delay_seconds: float = 0.0,
    ) -> bool:
        """Return interrupted work to the outbox for another worker."""
        self._ensure_schema()
        current_time = time.time()
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE office_manager_action_outbox
                    SET
                        status = 'pending',
                        next_attempt_at = ?,
                        locked_until = NULL,
                        locked_by = NULL,
                        last_error = ?,
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
                    """
                    SELECT * FROM office_manager_action_outbox WHERE id = ?
                    """,
                    (int(action_id),),
                ).fetchone()
            )

    def get_by_request_fingerprint(
        self,
        request_fingerprint: str,
    ) -> Optional[dict[str, Any]]:
        """Return the durable attempt for one exact signed Slack delivery."""
        self._ensure_schema()
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    """
                    SELECT * FROM office_manager_action_outbox
                    WHERE request_fingerprint = ?
                    """,
                    (str(request_fingerprint).strip(),),
                ).fetchone()
            )

    def get_by_action_occurrence_key(
        self,
        action_occurrence_key: str,
    ) -> Optional[dict[str, Any]]:
        """Return the durable attempt for one logical Slack action occurrence."""
        self._ensure_schema()
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    """
                    SELECT * FROM office_manager_action_outbox
                    WHERE action_occurrence_key = ?
                    """,
                    (str(action_occurrence_key).strip(),),
                ).fetchone()
            )


OfficeManagerActionProcessor = Callable[
    [dict[str, Any], OfficeManagerActionStore],
    Awaitable[None],
]


@lru_cache(maxsize=8)
def get_office_manager_action_store(database_path: str) -> OfficeManagerActionStore:
    return OfficeManagerActionStore(database_path)


async def _process_leased_action(
    action: dict[str, Any],
    *,
    store: OfficeManagerActionStore,
    processor: OfficeManagerActionProcessor,
) -> None:
    action_id = int(action["id"])
    owner = str(action["locked_by"])
    lease_lost = asyncio.Event()
    worker_task = asyncio.current_task()

    def revoke_worker() -> None:
        lease_lost.set()
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()

    async def keep_lease_alive() -> None:
        while True:
            await asyncio.sleep(DEFAULT_PROCESSING_LEASE_SECONDS / 3.0)
            try:
                renewed = await asyncio.to_thread(
                    store.renew,
                    action_id,
                    owner=owner,
                )
            except Exception as exc:
                print(
                    "OFFICE_MANAGER_ACTION_LEASE_RENEW_FAILED "
                    f"action_id={action_id} error_type={exc.__class__.__name__}"
                )
                # We cannot prove continued ownership after a renewal error.
                # Revoke this processor and leave the current lease in place;
                # a replacement may recover it only after that lease expires.
                revoke_worker()
                return
            if not renewed:
                revoke_worker()
                return

    heartbeat = asyncio.create_task(keep_lease_alive())
    try:
        await processor(action, store)
    except OfficeManagerActionLeaseLostError:
        print(f"OFFICE_MANAGER_ACTION_LEASE_LOST action_id={action_id}")
        return
    except asyncio.CancelledError:
        if lease_lost.is_set():
            print(f"OFFICE_MANAGER_ACTION_LEASE_LOST action_id={action_id}")
            return
        # Cancellation cannot stop synchronous provider work already running
        # in asyncio.to_thread. Preserve the current (possibly extended Slack
        # delivery) fence until expiry so a replacement cannot publish while
        # the old mutation may still land.
        raise
    except OfficeManagerActionPermanentError as exc:
        reason = str(exc) or exc.__class__.__name__
        await asyncio.to_thread(
            store.mark_terminal_failure,
            action_id,
            owner=owner,
            reason=reason,
        )
        print(
            "OFFICE_MANAGER_ACTION_TERMINAL_FAILURE "
            f"action_id={action_id} reason={reason}"
        )
        return
    except Exception as exc:
        await asyncio.to_thread(
            store.release,
            action_id,
            owner=owner,
            error=exc.__class__.__name__,
            delay_seconds=_retry_delay(int(action.get("attempt_count") or 1)),
        )
        print(
            "OFFICE_MANAGER_ACTION_RETRY "
            f"action_id={action_id} error_type={exc.__class__.__name__}"
        )
        return
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if (
                current_task is not None
                and current_task.cancelling()
                and not lease_lost.is_set()
            ):
                # The processor itself was cancelled (for example during
                # shutdown). Do not confuse that with the expected
                # cancellation of the heartbeat task we just requested.
                raise
    if lease_lost.is_set():
        print(f"OFFICE_MANAGER_ACTION_LEASE_LOST action_id={action_id}")
        return
    completed = await asyncio.to_thread(
        store.mark_completed,
        action_id,
        owner=owner,
    )
    if not completed:
        print(f"OFFICE_MANAGER_ACTION_COMPLETION_FENCED action_id={action_id}")


async def process_office_manager_action(
    action_id: int,
    *,
    store: OfficeManagerActionStore,
    processor: OfficeManagerActionProcessor,
) -> bool:
    """Lease and process one newly persisted action."""
    action = await asyncio.to_thread(store.reserve, action_id)
    if not action:
        return False
    await _process_leased_action(action, store=store, processor=processor)
    return True


async def process_due_office_manager_actions(
    *,
    store: OfficeManagerActionStore,
    processor: OfficeManagerActionProcessor,
    limit: int = 10,
) -> int:
    """Recover actions left pending by a previous Roo process."""
    processed = 0
    for _ in range(max(1, int(limit))):
        actions = await asyncio.to_thread(store.claim_due, limit=1)
        if not actions:
            break
        action = actions[0]
        await _process_leased_action(action, store=store, processor=processor)
        processed += 1
    return processed


async def office_manager_action_retry_loop(
    *,
    store: OfficeManagerActionStore,
    processor: OfficeManagerActionProcessor,
    poll_seconds: float = DEFAULT_RETRY_POLL_SECONDS,
    health_state: Optional[dict[str, Any]] = None,
) -> None:
    """Continuously recover pending or abandoned Office Manager actions."""
    poll_seconds = max(0.05, float(poll_seconds))

    async def pulse_health() -> None:
        interval = min(10.0, poll_seconds)
        while True:
            if health_state is not None:
                health_state["heartbeat_at"] = time.time()
            await asyncio.sleep(interval)

    health_task = asyncio.create_task(pulse_health())
    try:
        while True:
            try:
                await process_due_office_manager_actions(
                    store=store,
                    processor=processor,
                )
                if health_state is not None:
                    health_state["last_success_at"] = time.time()
                    health_state["last_error"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if health_state is not None:
                    health_state["last_error"] = exc.__class__.__name__
                print(
                    "OFFICE_MANAGER_ACTION_WORKER_FAILED "
                    f"error_type={exc.__class__.__name__}"
                )
            await asyncio.sleep(poll_seconds)
    finally:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass


async def office_manager_action_housekeeping_loop(
    *,
    store: OfficeManagerActionStore,
    poll_seconds: float = DEFAULT_HOUSEKEEPING_POLL_SECONDS,
) -> None:
    """Purge expired terminal rows even while new actions are disabled."""
    poll_seconds = max(0.05, float(poll_seconds))
    while True:
        try:
            await asyncio.to_thread(store.prune_completed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "OFFICE_MANAGER_ACTION_HOUSEKEEPING_FAILED "
                f"error_type={exc.__class__.__name__}"
            )
        await asyncio.sleep(poll_seconds)
