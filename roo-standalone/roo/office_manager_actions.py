"""Durable processing for acknowledged Office Manager Slack actions."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5


DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
DEFAULT_RETRY_POLL_SECONDS = 5.0
COMPLETED_RETENTION_SECONDS = 90 * 24 * 60 * 60


def _retry_delay(attempt_count: int) -> float:
    """Return capped exponential backoff for a failed processing attempt."""
    bounded_attempt = max(1, min(int(attempt_count), 16))
    return min(5 * 60.0, 5.0 * (2 ** (bounded_attempt - 1)))


def build_office_manager_action_key(slack_user_id: str, booking_date: str) -> str:
    """Return the backend-idempotent identity for one member and day."""
    return (
        f"office_manager:{str(slack_user_id).strip()}:"
        f"{str(booking_date).strip()}"
    )


def build_office_manager_feedback_client_msg_id(idempotency_key: str) -> str:
    """Return the stable Slack message identity for one terminal result."""
    return str(uuid5(NAMESPACE_URL, f"{idempotency_key}:private-feedback"))


def build_office_manager_uncertainty_client_msg_id(idempotency_key: str) -> str:
    """Return the stable Slack message identity for the optional retry notice."""
    return str(uuid5(NAMESPACE_URL, f"{idempotency_key}:uncertainty-notice"))


class OfficeManagerActionLeaseLostError(RuntimeError):
    """Raised when a replaced worker must stop without mutating or notifying."""


class OfficeManagerActionStore:
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
                        slack_user_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        booking_date TEXT NOT NULL,
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
                if "uncertainty_notice_attempted_at" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE office_manager_action_outbox
                        ADD COLUMN uncertainty_notice_attempted_at REAL
                        """
                    )
                for column_name, column_type in (
                    ("feedback_text", "TEXT"),
                    ("feedback_client_msg_id", "TEXT"),
                    ("feedback_prepared_at", "REAL"),
                ):
                    if column_name not in columns:
                        connection.execute(
                            "ALTER TABLE office_manager_action_outbox "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
            self._initialized = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def record_action(
        self,
        *,
        slack_user_id: str,
        channel_id: str,
        booking_date: str,
        replay_existing: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Persist a signed click and report whether it needs a worker."""
        self._ensure_schema()
        current_time = time.time()
        slack_user_id = str(slack_user_id).strip()
        channel_id = str(channel_id).strip()
        booking_date = str(booking_date).strip()
        idempotency_key = build_office_manager_action_key(
            slack_user_id,
            booking_date,
        )

        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    DELETE FROM office_manager_action_outbox
                    WHERE status = 'completed'
                      AND completed_at IS NOT NULL
                      AND completed_at <= ?
                    """,
                    (current_time - COMPLETED_RETENTION_SECONDS,),
                )
                existing = connection.execute(
                    """
                    SELECT * FROM office_manager_action_outbox
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                should_process = existing is None
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO office_manager_action_outbox (
                            idempotency_key,
                            slack_user_id,
                            channel_id,
                            booking_date,
                            status,
                            next_attempt_at,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            idempotency_key,
                            slack_user_id,
                            channel_id,
                            booking_date,
                            current_time,
                            current_time,
                            current_time,
                        ),
                    )
                elif replay_existing and not (
                    existing["status"] == "processing"
                    and existing["locked_until"] is not None
                    and float(existing["locked_until"]) > current_time
                ):
                    # A distinct signed click for the same member/day may safely
                    # replay the backend's idempotent claim operation.
                    connection.execute(
                        """
                        UPDATE office_manager_action_outbox
                        SET
                            channel_id = ?,
                            status = 'pending',
                            next_attempt_at = ?,
                            locked_until = NULL,
                            locked_by = NULL,
                            last_error = NULL,
                            feedback_text = NULL,
                            feedback_client_msg_id = NULL,
                            feedback_prepared_at = NULL,
                            completed_at = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            channel_id,
                            current_time,
                            current_time,
                            int(existing["id"]),
                        ),
                    )
                    should_process = True
                row = connection.execute(
                    """
                    SELECT * FROM office_manager_action_outbox
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                connection.commit()
                return dict(row), should_process

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
                    SET locked_until = ?, updated_at = ?
                    WHERE id = ? AND status = 'processing' AND locked_by = ?
                    """,
                    (
                        current_time + max(1.0, float(lease_seconds)),
                        current_time,
                        int(action_id),
                        str(owner),
                    ),
                )
                return cursor.rowcount == 1

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
        self._ensure_schema()
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
                        completed_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'processing' AND locked_by = ?
                    """,
                    (current_time, current_time, int(action_id), str(owner)),
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
                lease_lost.set()
                return
            if not renewed:
                lease_lost.set()
                return

    heartbeat = asyncio.create_task(keep_lease_alive())
    try:
        await processor(action, store)
    except OfficeManagerActionLeaseLostError:
        print(f"OFFICE_MANAGER_ACTION_LEASE_LOST action_id={action_id}")
        return
    except asyncio.CancelledError:
        await asyncio.to_thread(
            store.release,
            action_id,
            owner=owner,
            error="worker_cancelled",
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
            "OFFICE_MANAGER_ACTION_RETRY "
            f"action_id={action_id} error_type={exc.__class__.__name__}"
        )
        return
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
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
) -> None:
    """Continuously recover pending or abandoned Office Manager actions."""
    poll_seconds = max(0.05, float(poll_seconds))
    while True:
        try:
            await process_due_office_manager_actions(
                store=store,
                processor=processor,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "OFFICE_MANAGER_ACTION_WORKER_FAILED "
                f"error_type={exc.__class__.__name__}"
            )
        await asyncio.sleep(poll_seconds)
