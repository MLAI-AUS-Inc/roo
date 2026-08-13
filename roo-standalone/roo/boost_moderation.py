"""Durable admission and moderation for direct boost-channel root posts.

Slack publishes member messages before Roo sees them. This module records that
published root, asks mlai-backend to atomically price/debit it, and only then
marks the campaign approved. Rejected roots can be removed by the separate,
allowlisted Workspace Admin client in :mod:`roo.slack_moderation`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from .clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from .config import get_settings
from .link_love import extract_boost_url

DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
TERMINAL_REJECTION_STATUSES = {
    "rejected_insufficient_points",
    "rejected_member_unlinked",
    "rejected_invalid",
    "deleted",
    "delete_failed",
    "removed_by_author",
    "blocked",
}
RETRYABLE_STATUSES = {"pending", "retry"}
BOOST_RECHECK_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark"})


def _now() -> float:
    return time.time()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def boost_post_submission_key(
    workspace_id: str,
    channel_id: str,
    root_message_ts: str,
) -> str:
    return f"boost-post:{workspace_id}:{channel_id}:{root_message_ts}"


def _retry_delay(attempt_count: int) -> float:
    return min(15 * 60, 15 * (2 ** max(0, int(attempt_count or 1) - 1)))


class BoostPostAdmissionStore:
    """SQLite outbox/ledger for Slack-side boost admission state."""

    def __init__(self, db_path: str | Path):
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
                    CREATE TABLE IF NOT EXISTS boost_post_admissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submission_key TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        root_message_ts TEXT NOT NULL,
                        poster_slack_id TEXT NOT NULL,
                        root_text TEXT,
                        social_post_url TEXT,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        locked_until REAL,
                        locked_by TEXT,
                        backend_admission_id TEXT,
                        base_cost_points INTEGER,
                        charged_points INTEGER,
                        discount_applied INTEGER,
                        new_balance INTEGER,
                        backend_result_json TEXT,
                        rejection_reason TEXT,
                        last_error TEXT,
                        pending_notified_at REAL,
                        decision_notified_at REAL,
                        decision_message_ts TEXT,
                        dm_notified_at REAL,
                        deleted_at REAL,
                        restore_requested_at REAL,
                        restored_message_ts TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(channel_id, root_message_ts)
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(boost_post_admissions)").fetchall()
                }
                if "pending_notified_at" not in columns:
                    conn.execute(
                        "ALTER TABLE boost_post_admissions ADD COLUMN pending_notified_at REAL"
                    )
                for column, column_type in (
                    ("decision_message_ts", "TEXT"),
                    ("restore_requested_at", "REAL"),
                    ("restored_message_ts", "TEXT"),
                ):
                    if column not in columns:
                        conn.execute(
                            f"ALTER TABLE boost_post_admissions ADD COLUMN {column} {column_type}"
                        )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_boost_post_admissions_due
                    ON boost_post_admissions (status, next_attempt_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_boost_post_decision_message
                    ON boost_post_admissions (channel_id, decision_message_ts)
                    """
                )
            self._initialized = True

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get(self, channel_id: str, root_message_ts: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            return self._row(
                conn.execute(
                    """SELECT * FROM boost_post_admissions
                       WHERE channel_id = ? AND root_message_ts = ?""",
                    (channel_id, root_message_ts),
                ).fetchone()
            )

    def get_for_decision_message(
        self,
        channel_id: str,
        decision_message_ts: str,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            return self._row(
                conn.execute(
                    """SELECT * FROM boost_post_admissions
                       WHERE channel_id = ? AND decision_message_ts = ?""",
                    (channel_id, decision_message_ts),
                ).fetchone()
            )

    def record_root(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        root_message_ts: str,
        poster_slack_id: str,
        root_text: str,
        social_post_url: str,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = _now()
        key = boost_post_submission_key(workspace_id, channel_id, root_message_ts)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO boost_post_admissions (
                    submission_key, workspace_id, channel_id, root_message_ts,
                    poster_slack_id, root_text, social_post_url, status,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    key,
                    workspace_id,
                    channel_id,
                    root_message_ts,
                    poster_slack_id,
                    root_text,
                    social_post_url,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """SELECT * FROM boost_post_admissions
                   WHERE channel_id = ? AND root_message_ts = ?""",
                (channel_id, root_message_ts),
            ).fetchone()
            conn.execute("COMMIT")
        result = self._row(row)
        if result is None:
            raise RuntimeError("Failed to record boost post admission")
        return result

    def update_root_text(
        self,
        *,
        channel_id: str,
        root_message_ts: str,
        root_text: str,
        social_post_url: str,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET root_text = ?, social_post_url = ?, updated_at = ?
                WHERE channel_id = ? AND root_message_ts = ?
                """,
                (root_text, social_post_url, _now(), channel_id, root_message_ts),
            )
        return self.get(channel_id, root_message_ts)

    def claim_one(
        self,
        admission_id: int,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM boost_post_admissions
                WHERE id = ?
                  AND (
                    (status IN ('pending', 'retry') AND next_attempt_at <= ?)
                    OR (status = 'processing' AND COALESCE(locked_until, 0) <= ?)
                  )
                """,
                (int(admission_id), now, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'processing', attempt_count = attempt_count + 1,
                    locked_by = ?, locked_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (owner, now + max(10.0, float(lease_seconds)), now, int(admission_id)),
            )
            claimed = conn.execute(
                "SELECT * FROM boost_post_admissions WHERE id = ?",
                (int(admission_id),),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def claim_due(self, *, limit: int, owner: str) -> list[dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        with self._connect() as conn:
            ids = [
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT id FROM boost_post_admissions
                    WHERE (status IN ('pending', 'retry') AND next_attempt_at <= ?)
                       OR (status = 'processing' AND COALESCE(locked_until, 0) <= ?)
                    ORDER BY next_attempt_at, id
                    LIMIT ?
                    """,
                    (now, now, max(1, int(limit))),
                ).fetchall()
            ]
        claimed = [self.claim_one(row_id, owner=owner) for row_id in ids]
        return [row for row in claimed if row is not None]

    def claim_recheck(
        self,
        admission_id: int,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Claim one explicit founder-requested balance recheck."""

        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM boost_post_admissions WHERE id = ?",
                (int(admission_id),),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            current = dict(row)
            status = str(current.get("status") or "")
            claimable = status in {
                "deleted",
                "delete_failed",
                "rejected_insufficient_points",
            } or (
                status == "recheck_processing"
                and float(current.get("locked_until") or 0) <= now
            )
            backend_result = _json_object(current.get("backend_result_json"))
            backend_status = str(
                backend_result.get("status") or backend_result.get("code") or ""
            ).strip().lower()
            if not claimable or backend_status not in {
                "insufficient_points",
                "insufficient_balance",
                "not_enough_points",
            }:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'recheck_processing', locked_by = ?, locked_until = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    owner,
                    now + max(10.0, float(lease_seconds)),
                    now,
                    int(admission_id),
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM boost_post_admissions WHERE id = ?",
                (int(admission_id),),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def mark_recheck_insufficient(
        self,
        admission_id: int,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a completed recheck to its removed or rejected state."""

        self._ensure_schema()
        current = self.get_by_id(admission_id)
        status = (
            "deleted"
            if current.get("deleted_at") is not None
            else "rejected_insufficient_points"
        )
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = ?, locked_by = NULL, locked_until = NULL,
                    base_cost_points = ?, charged_points = ?, discount_applied = ?,
                    new_balance = ?, backend_result_json = ?, rejection_reason = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    int(result.get("base_cost_points") or 8),
                    result.get("charged_points"),
                    1 if bool(result.get("discount_applied")) else 0,
                    result.get("new_balance", result.get("balance_before")),
                    json.dumps(result, sort_keys=True, default=str),
                    str(result.get("message") or "Insufficient Roo points"),
                    now,
                    int(admission_id),
                ),
            )
        return self.get_by_id(admission_id)

    def mark_recheck_failed(self, admission_id: int, *, error: str) -> dict[str, Any]:
        self._ensure_schema()
        current = self.get_by_id(admission_id)
        status = (
            "deleted"
            if current.get("deleted_at") is not None
            else "rejected_insufficient_points"
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = ?, locked_by = NULL, locked_until = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error[:1000], _now(), int(admission_id)),
            )
        return self.get_by_id(admission_id)

    def request_restore(self, admission_id: int) -> dict[str, Any]:
        self._ensure_schema()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET restore_requested_at = COALESCE(restore_requested_at, ?),
                    next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (now, now, now, int(admission_id)),
            )
        return self.get_by_id(admission_id)

    def claim_restore(
        self,
        admission_id: int,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'restoring', locked_by = ?, locked_until = ?, updated_at = ?
                WHERE id = ? AND restored_message_ts IS NULL
                  AND restore_requested_at IS NOT NULL AND next_attempt_at <= ?
                  AND (
                    status = 'approved'
                    OR (status = 'restoring' AND COALESCE(locked_until, 0) <= ?)
                  )
                """,
                (
                    owner,
                    now + max(10.0, float(lease_seconds)),
                    now,
                    int(admission_id),
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_by_id(admission_id)

    def claim_due_restores(self, *, limit: int, owner: str) -> list[dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        with self._connect() as conn:
            ids = [
                int(row[0])
                for row in conn.execute(
                    """
                    SELECT id FROM boost_post_admissions
                    WHERE restored_message_ts IS NULL
                      AND restore_requested_at IS NOT NULL AND next_attempt_at <= ?
                      AND (
                        status = 'approved'
                        OR (status = 'restoring' AND COALESCE(locked_until, 0) <= ?)
                      )
                    ORDER BY next_attempt_at, id LIMIT ?
                    """,
                    (now, now, max(1, int(limit))),
                ).fetchall()
            ]
        claimed = [self.claim_restore(row_id, owner=owner) for row_id in ids]
        return [row for row in claimed if row is not None]

    def mark_restore_failed(self, admission_id: int, *, error: str) -> dict[str, Any]:
        self._ensure_schema()
        current = self.get_by_id(admission_id)
        attempt_count = int(current.get("attempt_count") or 1) + 1
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'approved', attempt_count = ?, next_attempt_at = ?,
                    locked_by = NULL, locked_until = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    attempt_count,
                    now + _retry_delay(attempt_count),
                    error[:1000],
                    now,
                    int(admission_id),
                ),
            )
        return self.get_by_id(admission_id)

    def mark_restored(
        self,
        admission_id: int,
        *,
        restored_message_ts: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record the bot-restored root as an approved alias of the paid campaign."""

        self._ensure_schema()
        source = self.get_by_id(admission_id)
        now = _now()
        restored_key = boost_post_submission_key(
            str(source["workspace_id"]),
            str(source["channel_id"]),
            restored_message_ts,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO boost_post_admissions (
                    submission_key, workspace_id, channel_id, root_message_ts,
                    poster_slack_id, root_text, social_post_url, status,
                    next_attempt_at, backend_admission_id, base_cost_points,
                    charged_points, discount_applied, new_balance, backend_result_json,
                    decision_notified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    restored_key,
                    source["workspace_id"],
                    source["channel_id"],
                    restored_message_ts,
                    source["poster_slack_id"],
                    source.get("root_text") or "",
                    source.get("social_post_url") or "",
                    now,
                    source.get("backend_admission_id"),
                    source.get("base_cost_points"),
                    source.get("charged_points"),
                    source.get("discount_applied"),
                    source.get("new_balance"),
                    source.get("backend_result_json"),
                    now,
                    now,
                    now,
                ),
            )
            alias = conn.execute(
                """SELECT * FROM boost_post_admissions
                   WHERE channel_id = ? AND root_message_ts = ?""",
                (source["channel_id"], restored_message_ts),
            ).fetchone()
            if alias is None or str(alias["poster_slack_id"]) != str(source["poster_slack_id"]):
                conn.execute("ROLLBACK")
                raise RuntimeError("Restored boost root conflicts with an existing admission")
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'approved', restored_message_ts = ?, locked_by = NULL,
                    locked_until = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (restored_message_ts, now, int(admission_id)),
            )
            conn.execute("COMMIT")
        return self.get_by_id(admission_id), dict(alias)

    def mark_approved(self, admission_id: int, result: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        now = _now()
        charged = int(result.get("charged_points") or result.get("points_charged") or 0)
        if charged not in {4, 8}:
            raise ValueError("Boost admission returned an unexpected charged_points value")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'approved', locked_by = NULL, locked_until = NULL,
                    backend_admission_id = ?, base_cost_points = ?, charged_points = ?,
                    discount_applied = ?, new_balance = ?, backend_result_json = ?,
                    rejection_reason = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(result.get("admission_id") or result.get("id") or ""),
                    int(result.get("base_cost_points") or 8),
                    charged,
                    1 if bool(result.get("discount_applied")) else 0,
                    result.get("new_balance"),
                    json.dumps(result, sort_keys=True, default=str),
                    now,
                    int(admission_id),
                ),
            )
        return self.get_by_id(admission_id)

    def mark_rejected(
        self,
        admission_id: int,
        *,
        status: str,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {
            "rejected_insufficient_points",
            "rejected_member_unlinked",
            "rejected_invalid",
        }:
            raise ValueError("Invalid boost rejection status")
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = ?, locked_by = NULL, locked_until = NULL,
                    rejection_reason = ?, backend_result_json = ?, updated_at = ?
                    , decision_notified_at = NULL, dm_notified_at = NULL
                WHERE id = ?
                """,
                (
                    status,
                    reason,
                    json.dumps(result, sort_keys=True, default=str) if result else None,
                    _now(),
                    int(admission_id),
                ),
            )
        return self.get_by_id(admission_id)

    def mark_retry(
        self,
        admission_id: int,
        *,
        error: str,
        max_attempts: int,
    ) -> dict[str, Any]:
        row = self.get_by_id(admission_id)
        attempt_count = int(row.get("attempt_count") or 0)
        status = "blocked" if attempt_count >= max(1, int(max_attempts)) else "retry"
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = ?, next_attempt_at = ?, locked_by = NULL,
                    locked_until = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _now() + _retry_delay(attempt_count),
                    error[:1000],
                    _now(),
                    int(admission_id),
                ),
            )
        return self.get_by_id(admission_id)

    def mark_notification(
        self,
        admission_id: int,
        *,
        dm: bool = False,
        message_ts: str | None = None,
    ) -> None:
        column = "dm_notified_at" if dm else "decision_notified_at"
        self._ensure_schema()
        with self._connect() as conn:
            if dm or not message_ts:
                conn.execute(
                    f"UPDATE boost_post_admissions SET {column} = ?, updated_at = ? WHERE id = ?",
                    (_now(), _now(), int(admission_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE boost_post_admissions
                    SET decision_notified_at = ?, decision_message_ts = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_now(), message_ts, _now(), int(admission_id)),
                )

    def mark_pending_notification(self, admission_id: int) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET pending_notified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (_now(), _now(), int(admission_id)),
            )

    def mark_deleted(
        self,
        admission_id: int,
        *,
        ok: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        status = "deleted" if ok else "delete_failed"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = ?, deleted_at = ?, last_error = COALESCE(?, last_error), updated_at = ?
                WHERE id = ?
                """,
                (status, now if ok else None, error, now, int(admission_id)),
            )
        return self.get_by_id(admission_id)

    def mark_removed(self, channel_id: str, root_message_ts: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE boost_post_admissions
                SET status = 'removed_by_author', locked_by = NULL, locked_until = NULL,
                    updated_at = ?
                WHERE channel_id = ? AND root_message_ts = ? AND status != 'deleted'
                """,
                (_now(), channel_id, root_message_ts),
            )
        return self.get(channel_id, root_message_ts)

    def get_by_id(self, admission_id: int) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM boost_post_admissions WHERE id = ?",
                (int(admission_id),),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise KeyError(f"Unknown boost admission {admission_id}")
        return result


_stores: dict[str, BoostPostAdmissionStore] = {}
_stores_lock = threading.RLock()


def get_boost_post_store() -> BoostPostAdmissionStore:
    settings = get_settings()
    db_path = str(getattr(settings, "BOOST_LINK_LOVE_DB_PATH", "data/link_love_awards.db"))
    with _stores_lock:
        if db_path not in _stores:
            _stores[db_path] = BoostPostAdmissionStore(db_path)
        return _stores[db_path]


def boost_reward_admission_decision(
    channel_id: str,
    root_message_ts: str,
    *,
    store: BoostPostAdmissionStore | None = None,
) -> str:
    """Return ``legacy``, ``approved``, ``pending``, or ``rejected``."""

    try:
        settings = get_settings()
    except (RuntimeError, ValueError):
        # Direct library/test callers may run without an application Settings
        # instance. The production app validates Settings before any worker or
        # Slack event starts, so no enabled enforcement deployment reaches this
        # compatibility path.
        return "legacy"
    if not bool(getattr(settings, "BOOST_POST_MODERATION_ENABLED", False)):
        return "legacy"
    if channel_id != str(getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or ""):
        return "rejected"
    try:
        cutoff = float(str(getattr(settings, "BOOST_POST_ENFORCEMENT_CUTOFF_TS", "") or "0"))
        root_ts = float(root_message_ts)
    except (TypeError, ValueError):
        return "rejected"
    if root_ts < cutoff:
        return "legacy"
    admission = (store or get_boost_post_store()).get(channel_id, root_message_ts)
    if not admission:
        return "pending"
    status = str(admission.get("status") or "")
    if status == "approved":
        return "approved"
    if status in TERMINAL_REJECTION_STATUSES:
        return "rejected"
    return "pending"


def boost_reward_root_poster(
    channel_id: str,
    root_message_ts: str,
    *,
    store: BoostPostAdmissionStore | None = None,
) -> str:
    """Return the attributed founder for an approved original or restored root."""

    admission = (store or get_boost_post_store()).get(channel_id, root_message_ts)
    if not admission or str(admission.get("status") or "") != "approved":
        return ""
    return str(admission.get("poster_slack_id") or "")


def _backend_error_payload(exc: Exception) -> tuple[str, dict[str, Any]]:
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response is None:
        return "", {}
    try:
        payload = exc.response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = str(
        payload.get("code")
        or payload.get("error_code")
        or payload.get("error")
        or ""
    ).strip().lower()
    return code, payload


def _rejection_from_code(code: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    normalized = code.replace("-", "_").replace(" ", "_")
    reason = str(payload.get("message") or payload.get("detail") or code or "Post rejected")
    if normalized in {"insufficient_points", "insufficient_balance", "not_enough_points"}:
        return "rejected_insufficient_points", reason
    if normalized in {"member_unlinked", "slack_user_not_found", "member_not_found"}:
        return "rejected_member_unlinked", reason
    if normalized in {"invalid_post", "invalid_social_url", "ineligible_post"}:
        return "rejected_invalid", reason
    return None


def _approval_notice(admission: dict[str, Any]) -> str:
    charged = int(admission.get("charged_points") or 0)
    discount = bool(admission.get("discount_applied"))
    discount_text = " Your Australian startup monthly-update discount was applied." if discount else ""
    return f":white_check_mark: Approved — {charged} Roo points deducted.{discount_text}"


def _backend_result(admission: dict[str, Any]) -> dict[str, Any]:
    """Return the stored backend decision without trusting its shape."""

    return _json_object(admission.get("backend_result_json"))


def _rejection_notice(admission: dict[str, Any]) -> str:
    poster = str(admission.get("poster_slack_id") or "")
    status = str(admission.get("status") or "")
    if status == "rejected_insufficient_points":
        result = _backend_result(admission)
        required = result.get("charged_points")
        if required is not None:
            balance_detail = f"This boost costs {required} Roo points. "
        else:
            balance_detail = "Your Roo points balance is below the price of this boost. "
        discount_detail = (
            "Your 50% Australian-startup monthly-update discount was included in that price. "
            if bool(result.get("discount_applied"))
            else ""
        )
        return (
            f"<@{poster}> I couldn't approve this boost because you don't have enough Roo points. "
            f"{balance_detail}{discount_detail}"
            "I'll remove this message so nobody engages with an unapproved campaign. "
            "You can earn more points by liking or meaningfully engaging with other founders' "
            "posts in this channel—helping everyone get more engagement—or buy a fixed Top-up "
            "Roo Points pack by DMing me `topup` for a private Stripe Checkout link. "
            "Once you have enough points, react to this Roo guidance reply with ✅ or ✔️. "
            "I'll recheck your balance and restore your post automatically. "
            "To check your balance, DM me `points` or ask “what's my points balance?”."
        )
    if status == "rejected_member_unlinked":
        return (
            f"<@{poster}> I couldn't approve this boost because I can't match your Slack profile "
            "to a Roo Points account, so I can't check or charge your balance. I'll remove this "
            "message. DM me “what's my points balance?” to check whether I can see your account. "
            "If I still can't find it, ask an MLAI admin to link your Slack profile, then create a "
            "new top-level post."
        )
    return (
        f"<@{poster}> I couldn't process this boost because some required Slack information was "
        "missing or invalid. No Roo points were charged. I'll remove this message; create a new "
        "top-level post and try again. Any website or social link is allowed. If this happens "
        "again, please ask an MLAI admin for help."
    )


def _post_decision_notice(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore,
    text: str,
    post_message_fn: Callable[..., Any] | None = None,
) -> None:
    if admission.get("decision_notified_at") is not None:
        return
    if post_message_fn is None:
        from .slack_client import post_message

        post_message_fn = post_message
    try:
        response = post_message_fn(
            channel=str(admission["channel_id"]),
            thread_ts=str(admission["root_message_ts"]),
            text=text,
        )
        response_ts = ""
        if isinstance(response, dict):
            response_ts = str(response.get("ts") or "")
        elif hasattr(response, "get"):
            response_ts = str(response.get("ts") or "")
        store.mark_notification(
            int(admission["id"]),
            message_ts=response_ts or None,
        )
    except Exception as exc:  # noqa: BLE001 - Slack SDK errors vary by transport.
        print(
            "BOOST_POST_NOTICE_FAILED "
            f"admission_id={admission.get('id')} error_type={exc.__class__.__name__}"
        )


def _post_pending_notice(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore,
    post_message_fn: Callable[..., Any] | None = None,
) -> None:
    if admission.get("pending_notified_at") is not None:
        return
    if post_message_fn is None:
        from .slack_client import post_message

        post_message_fn = post_message
    try:
        post_message_fn(
            channel=str(admission["channel_id"]),
            thread_ts=str(admission["root_message_ts"]),
            text=(
                f"<@{admission['poster_slack_id']}> approval is pending while I verify the "
                "Roo points charge. Please wait for an approved message before anyone engages."
            ),
        )
        store.mark_pending_notification(int(admission["id"]))
    except Exception as exc:  # noqa: BLE001 - Slack SDK errors vary by transport.
        print(
            "BOOST_POST_PENDING_NOTICE_FAILED "
            f"admission_id={admission.get('id')} error_type={exc.__class__.__name__}"
        )


def _dm_rejection(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore,
    text: str,
    send_dm_fn: Callable[..., Any] | None = None,
) -> None:
    if admission.get("dm_notified_at") is not None:
        return
    if send_dm_fn is None:
        from .slack_client import send_dm

        send_dm_fn = send_dm
    try:
        send_dm_fn(str(admission["poster_slack_id"]), text)
        store.mark_notification(int(admission["id"]), dm=True)
    except Exception as exc:  # noqa: BLE001 - Slack SDK errors vary by transport.
        print(
            "BOOST_POST_DM_FAILED "
            f"admission_id={admission.get('id')} error_type={exc.__class__.__name__}"
        )


def _moderate_rejected_root(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore,
    delete_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if not bool(getattr(settings, "BOOST_POST_AUTO_DELETE_ENABLED", False)):
        return admission
    if delete_fn is None:
        from .slack_moderation import delete_boost_root_as_moderator

        delete_fn = delete_boost_root_as_moderator
    result = delete_fn(
        channel_id=str(admission["channel_id"]),
        message_ts=str(admission["root_message_ts"]),
        reason_code=str(admission["status"]),
    )
    return store.mark_deleted(
        int(admission["id"]),
        ok=bool(getattr(result, "ok", False)),
        error=getattr(result, "error_code", None),
    )


def _post_recheck_feedback(
    admission: dict[str, Any],
    text: str,
    *,
    post_message_fn: Callable[..., Any] | None = None,
) -> None:
    if post_message_fn is None:
        from .slack_client import post_message

        post_message_fn = post_message
    try:
        post_message_fn(
            channel=str(admission["channel_id"]),
            thread_ts=str(admission["root_message_ts"]),
            text=text,
        )
    except Exception as exc:  # noqa: BLE001 - feedback must not roll back the recheck.
        print(
            "BOOST_POST_RECHECK_FEEDBACK_FAILED "
            f"admission_id={admission.get('id')} error_type={exc.__class__.__name__}"
        )


def _restored_boost_text(admission: dict[str, Any]) -> str:
    poster = str(admission.get("poster_slack_id") or "")
    root_text = str(admission.get("root_text") or "").strip()
    heading = (
        f":white_check_mark: Restored for <@{poster}> after Roo confirmed the points charge."
    )
    return f"{heading}\n\n{root_text}" if root_text else heading


async def process_boost_restore(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore | None = None,
    claimed: bool = False,
    owner: str | None = None,
    post_message_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Repost a paid, previously deleted campaign and register its approved alias."""

    store = store or get_boost_post_store()
    worker = owner or f"roo-boost-restore-{uuid4().hex}"
    current = admission if claimed else store.claim_restore(int(admission["id"]), owner=worker)
    if current is None:
        return {"status": "restore_not_due"}
    if post_message_fn is None:
        from .slack_client import post_message

        post_message_fn = post_message
    try:
        response = post_message_fn(
            channel=str(current["channel_id"]),
            text=_restored_boost_text(current),
            client_msg_id=str(
                uuid5(NAMESPACE_URL, f"{current['submission_key']}:restore")
            ),
            metadata={
                "event_type": "roo_restored_boost",
                "event_payload": {
                    "admission_id": str(current["id"]),
                    "poster_slack_id": str(current["poster_slack_id"]),
                },
            },
        )
        restored_ts = ""
        if isinstance(response, dict):
            restored_ts = str(response.get("ts") or "")
        elif hasattr(response, "get"):
            restored_ts = str(response.get("ts") or "")
        if not restored_ts:
            raise RuntimeError("Slack did not return the restored message timestamp")
        source, restored = store.mark_restored(
            int(current["id"]),
            restored_message_ts=restored_ts,
        )
        return {
            "status": "restored",
            "admission": source,
            "restored_admission": restored,
        }
    except Exception as exc:  # noqa: BLE001 - Slack SDK failures vary.
        error = f"{exc.__class__.__name__}: {exc}"
        updated = store.mark_restore_failed(int(current["id"]), error=error)
        print(
            "BOOST_POST_RESTORE_RETRY "
            f"admission_id={current.get('id')} error_type={exc.__class__.__name__}"
        )
        return {"status": "restore_pending", "admission": updated, "error": error}


async def handle_boost_recheck_reaction(
    event: dict[str, Any],
    *,
    store: BoostPostAdmissionStore | None = None,
    client: Any = None,
    post_message_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Recheck and restore an insufficient-points boost on its author's check reaction."""

    reaction = str(event.get("reaction") or "")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    channel_id = str(item.get("channel") or "")
    decision_message_ts = str(item.get("ts") or "")
    reactor_user_id = str(event.get("user") or "")
    if (
        reaction not in BOOST_RECHECK_REACTIONS
        or item.get("type") != "message"
        or not channel_id
        or not decision_message_ts
        or not reactor_user_id
    ):
        return {"handled": False, "status": "ignored"}

    try:
        settings = get_settings()
    except (RuntimeError, ValueError):
        return {"handled": False, "status": "ignored"}
    if (
        not bool(getattr(settings, "BOOST_POST_MODERATION_ENABLED", False))
        or channel_id
        != str(getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or "")
    ):
        return {"handled": False, "status": "ignored"}

    store = store or get_boost_post_store()
    admission = store.get_for_decision_message(channel_id, decision_message_ts)
    if admission is None:
        return {"handled": False, "status": "ignored"}
    if reactor_user_id != str(admission.get("poster_slack_id") or ""):
        return {"handled": True, "status": "ignored_not_poster"}

    claimed = store.claim_recheck(
        int(admission["id"]),
        owner=f"roo-boost-recheck-{uuid4().hex}",
    )
    if claimed is None:
        refreshed = store.get_by_id(int(admission["id"]))
        if refreshed.get("restored_message_ts"):
            status = "already_restored"
        elif refreshed.get("status") == "recheck_processing":
            status = "recheck_in_progress"
        else:
            status = "not_recheckable"
        return {"handled": True, "status": status, "admission": refreshed}

    client = client or MLAIBackendClient()
    try:
        result = await client.admit_boost_post(
            submission_key=str(claimed["submission_key"]),
            workspace_id=str(claimed["workspace_id"]),
            channel_id=str(claimed["channel_id"]),
            root_message_ts=str(claimed["root_message_ts"]),
            poster_slack_id=str(claimed["poster_slack_id"]),
            root_text=str(claimed.get("root_text") or ""),
            social_post_url=str(claimed.get("social_post_url") or ""),
            timeout=float(
                getattr(settings, "BOOST_POST_DECISION_TIMEOUT_SECONDS", 30.0)
            ),
            recheck_insufficient_points=True,
        )
    except Exception as exc:  # noqa: BLE001 - backend client exceptions vary.
        code, payload = _backend_error_payload(exc)
        rejection = _rejection_from_code(code, payload)
        if rejection and rejection[0] == "rejected_insufficient_points":
            result = payload
        else:
            error = f"{exc.__class__.__name__}: {exc}"
            updated = store.mark_recheck_failed(int(claimed["id"]), error=error)
            _post_recheck_feedback(
                updated,
                (
                    f"<@{reactor_user_id}> I couldn't recheck your Roo points just now. "
                    "Nothing was charged; please try the ✅ reaction again shortly."
                ),
                post_message_fn=post_message_fn,
            )
            return {"handled": True, "status": "recheck_failed", "admission": updated}

    backend_status = str(result.get("status") or result.get("code") or "").strip().lower()
    if backend_status in {"approved", "charged", "already_approved"}:
        approved = store.mark_approved(int(claimed["id"]), result)
        if approved.get("deleted_at") is None:
            _post_recheck_feedback(
                approved,
                _approval_notice(approved),
                post_message_fn=post_message_fn,
            )
            return {"handled": True, "status": "approved", "admission": approved}
        queued = store.request_restore(int(approved["id"]))
        restored = await process_boost_restore(
            queued,
            store=store,
            post_message_fn=post_message_fn,
        )
        return {"handled": True, **restored}

    rejection = _rejection_from_code(backend_status, result)
    if rejection and rejection[0] == "rejected_insufficient_points":
        updated = store.mark_recheck_insufficient(int(claimed["id"]), result)
        backend_result = _backend_result(updated)
        required = backend_result.get("charged_points")
        if required is not None:
            detail = f"This boost needs {required} Roo points. "
        else:
            detail = "Your balance is still below this boost's Roo points price. "
        _post_recheck_feedback(
            updated,
            (
                f"<@{reactor_user_id}> I checked again, but there still aren't enough points. "
                f"{detail}Keep engaging with other founders or DM me `topup`, "
                "then react ✅ again."
            ),
            post_message_fn=post_message_fn,
        )
        return {"handled": True, "status": "still_insufficient", "admission": updated}

    updated = store.mark_recheck_failed(
        int(claimed["id"]),
        error=f"Unexpected boost admission status: {backend_status or 'missing'}",
    )
    return {"handled": True, "status": "recheck_failed", "admission": updated}


async def process_boost_post_admission(
    admission: dict[str, Any],
    *,
    store: BoostPostAdmissionStore | None = None,
    client: Any = None,
    claimed: bool = False,
    owner: str | None = None,
    post_message_fn: Callable[..., Any] | None = None,
    send_dm_fn: Callable[..., Any] | None = None,
    delete_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    store = store or get_boost_post_store()
    settings = get_settings()
    worker = owner or f"roo-boost-{uuid4().hex}"
    current = admission if claimed else store.claim_one(int(admission["id"]), owner=worker)
    if current is None:
        return {"status": "not_due"}
    client = client or MLAIBackendClient()
    try:
        result = await client.admit_boost_post(
            submission_key=str(current["submission_key"]),
            workspace_id=str(current["workspace_id"]),
            channel_id=str(current["channel_id"]),
            root_message_ts=str(current["root_message_ts"]),
            poster_slack_id=str(current["poster_slack_id"]),
            root_text=str(current.get("root_text") or ""),
            social_post_url=str(current.get("social_post_url") or ""),
            timeout=float(getattr(settings, "BOOST_POST_DECISION_TIMEOUT_SECONDS", 30.0)),
        )
        backend_status = str(result.get("status") or "approved").strip().lower()
        if backend_status in {"approved", "charged", "already_approved"}:
            approved = store.mark_approved(int(current["id"]), result)
            _post_decision_notice(
                approved,
                store=store,
                text=_approval_notice(approved),
                post_message_fn=post_message_fn,
            )
            return {"status": "approved", "admission": store.get_by_id(int(current["id"]))}
        rejection = _rejection_from_code(backend_status, result)
        if rejection is None:
            raise ValueError(f"Unexpected boost admission status: {backend_status}")
        rejected = store.mark_rejected(
            int(current["id"]), status=rejection[0], reason=rejection[1], result=result
        )
    except Exception as exc:  # noqa: BLE001 - backend errors are classified below.
        code, payload = _backend_error_payload(exc)
        rejection = _rejection_from_code(code, payload)
        if rejection is not None:
            rejected = store.mark_rejected(
                int(current["id"]), status=rejection[0], reason=rejection[1], result=payload
            )
        else:
            retryable = isinstance(
                exc,
                (MLAIBackendUnavailableError, httpx.TransportError, httpx.TimeoutException),
            ) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500)
            error = f"{exc.__class__.__name__}: {exc}"
            updated = store.mark_retry(
                int(current["id"]),
                error=error,
                max_attempts=int(getattr(settings, "BOOST_POST_MAX_RETRY_ATTEMPTS", 5)),
            )
            print(
                "BOOST_POST_ADMISSION_RETRY "
                f"admission_id={current.get('id')} status={updated.get('status')} "
                f"retryable={retryable} error_type={exc.__class__.__name__}"
            )
            _post_pending_notice(
                updated,
                store=store,
                post_message_fn=post_message_fn,
            )
            refreshed = store.get_by_id(int(updated["id"]))
            if refreshed["status"] == "blocked" and refreshed.get("dm_notified_at") is None:
                _dm_rejection(
                    refreshed,
                    store=store,
                    text=(
                        "I could not confirm the Roo points charge after several attempts. "
                        "Your boost remains unapproved and helper rewards are blocked. "
                        "Please contact a Roo admin before reposting."
                    ),
                    send_dm_fn=send_dm_fn,
                )
                refreshed = store.get_by_id(int(updated["id"]))
            return {"status": str(refreshed["status"]), "admission": refreshed, "error": error}

    notice = _rejection_notice(rejected)
    _post_decision_notice(
        rejected, store=store, text=notice, post_message_fn=post_message_fn
    )
    refreshed = store.get_by_id(int(rejected["id"]))
    _dm_rejection(refreshed, store=store, text=notice, send_dm_fn=send_dm_fn)
    refreshed = store.get_by_id(int(rejected["id"]))
    moderated = _moderate_rejected_root(refreshed, store=store, delete_fn=delete_fn)
    return {"status": str(moderated["status"]), "admission": moderated}


async def handle_boost_root_post(
    event: dict[str, Any],
    *,
    workspace_id: str,
    store: BoostPostAdmissionStore | None = None,
    client: Any = None,
    **process_kwargs: Any,
) -> dict[str, Any]:
    settings = get_settings()
    if not bool(getattr(settings, "BOOST_POST_MODERATION_ENABLED", False)):
        return {"status": "ignored", "reason": "moderation_disabled"}
    if event.get("thread_ts") or event.get("bot_id") or event.get("subtype"):
        return {"status": "ignored", "reason": "not_human_root"}
    channel_id = str(event.get("channel") or "").strip()
    root_message_ts = str(event.get("ts") or "").strip()
    poster_slack_id = str(event.get("user") or "").strip()
    if channel_id != str(getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or ""):
        return {"status": "ignored", "reason": "wrong_channel"}
    if not workspace_id or not root_message_ts or not poster_slack_id:
        return {"status": "ignored", "reason": "missing_required_event_fields"}
    try:
        if float(root_message_ts) < float(settings.BOOST_POST_ENFORCEMENT_CUTOFF_TS):
            return {"status": "ignored", "reason": "before_enforcement_cutoff"}
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "invalid_message_ts"}

    root_text = str(event.get("text") or "")
    store = store or get_boost_post_store()
    admission = store.record_root(
        workspace_id=workspace_id,
        channel_id=channel_id,
        root_message_ts=root_message_ts,
        poster_slack_id=poster_slack_id,
        root_text=root_text,
        social_post_url=extract_boost_url(root_text) or "",
    )
    return await process_boost_post_admission(
        admission,
        store=store,
        client=client,
        **process_kwargs,
    )


async def handle_boost_root_edit(
    event: dict[str, Any],
    *,
    store: BoostPostAdmissionStore | None = None,
    post_message_fn: Callable[..., Any] | None = None,
    send_dm_fn: Callable[..., Any] | None = None,
    delete_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    message = event.get("message") or {}
    channel_id = str(event.get("channel") or "")
    root_ts = str(message.get("ts") or "")
    if not channel_id or not root_ts or message.get("thread_ts"):
        return {"status": "ignored"}
    text = str(message.get("text") or "")
    store = store or get_boost_post_store()
    admission = store.get(channel_id, root_ts)
    if admission is None:
        return {"status": "ignored", "reason": "unknown_root"}
    new_url = extract_boost_url(text) or ""
    updated = store.update_root_text(
        channel_id=channel_id,
        root_message_ts=root_ts,
        root_text=text,
        social_post_url=new_url,
    )
    return {"status": "updated", "admission": updated}


def mark_boost_root_removed(
    channel_id: str,
    root_message_ts: str,
    *,
    store: BoostPostAdmissionStore | None = None,
) -> dict[str, Any] | None:
    return (store or get_boost_post_store()).mark_removed(channel_id, root_message_ts)


async def process_due_boost_posts_once(
    *,
    store: BoostPostAdmissionStore | None = None,
    client: Any = None,
    limit: int = 20,
    **process_kwargs: Any,
) -> list[dict[str, Any]]:
    store = store or get_boost_post_store()
    owner = f"roo-boost-worker-{uuid4().hex}"
    claimed = store.claim_due(limit=limit, owner=owner)
    results = []
    for admission in claimed:
        results.append(
            await process_boost_post_admission(
                admission,
                store=store,
                client=client,
                claimed=True,
                owner=owner,
                **process_kwargs,
            )
        )
    return results


async def process_due_boost_restores_once(
    *,
    store: BoostPostAdmissionStore | None = None,
    limit: int = 20,
    post_message_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    store = store or get_boost_post_store()
    owner = f"roo-boost-restore-worker-{uuid4().hex}"
    claimed = store.claim_due_restores(limit=limit, owner=owner)
    results = []
    for admission in claimed:
        results.append(
            await process_boost_restore(
                admission,
                store=store,
                claimed=True,
                owner=owner,
                post_message_fn=post_message_fn,
            )
        )
    return results


async def boost_post_retry_loop() -> None:
    settings = get_settings()
    poll_seconds = max(1.0, float(getattr(settings, "BOOST_POST_RETRY_POLL_SECONDS", 15.0)))
    while True:
        try:
            await process_due_boost_posts_once()
            await process_due_boost_restores_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the durable worker alive.
            print(f"BOOST_POST_WORKER_ERROR error_type={exc.__class__.__name__}")
        await asyncio.sleep(poll_seconds)
