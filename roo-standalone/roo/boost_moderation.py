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
from uuid import uuid4

import httpx

from .clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from .config import get_settings
from .link_love import extract_social_post_url

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


def _now() -> float:
    return time.time()


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
                        dm_notified_at REAL,
                        deleted_at REAL,
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
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_boost_post_admissions_due
                    ON boost_post_admissions (status, next_attempt_at)
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

    def mark_notification(self, admission_id: int, *, dm: bool = False) -> None:
        column = "dm_notified_at" if dm else "decision_notified_at"
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                f"UPDATE boost_post_admissions SET {column} = ?, updated_at = ? WHERE id = ?",
                (_now(), _now(), int(admission_id)),
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
    balance = admission.get("new_balance")
    discount = bool(admission.get("discount_applied"))
    discount_text = " Your Australian startup monthly-update discount was applied." if discount else ""
    balance_text = f" New balance: {balance}." if balance is not None else ""
    return f":white_check_mark: Approved — {charged} Roo points deducted.{discount_text}{balance_text}"


def _backend_result(admission: dict[str, Any]) -> dict[str, Any]:
    """Return the stored backend decision without trusting its shape."""

    raw = admission.get("backend_result_json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rejection_notice(admission: dict[str, Any]) -> str:
    poster = str(admission.get("poster_slack_id") or "")
    status = str(admission.get("status") or "")
    if status == "rejected_insufficient_points":
        result = _backend_result(admission)
        required = result.get("charged_points")
        available = result.get("new_balance", result.get("balance_before"))
        if required is not None and available is not None:
            balance_detail = (
                f"This boost costs {required} Roo points, and you currently have {available}. "
            )
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
            "Earn enough points, then create a new top-level post with the social link. "
            "You can DM me “what's my points balance?” at any time."
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
        f"<@{poster}> I couldn't approve this boost because I couldn't find a supported social "
        "link in the message. To qualify, create a new top-level post containing a direct "
        "LinkedIn or lnkd.in, X/Twitter, Instagram, or Facebook link. General website, product, "
        "signup, and article links aren't eligible right now. I'll remove this message; add a "
        "supported social link and try again."
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
        post_message_fn(
            channel=str(admission["channel_id"]),
            thread_ts=str(admission["root_message_ts"]),
            text=text,
        )
        store.mark_notification(int(admission["id"]))
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
        social_post_url=extract_social_post_url(root_text) or "",
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
    old_url = str(admission.get("social_post_url") or "")
    new_url = extract_social_post_url(text) or ""
    updated = store.update_root_text(
        channel_id=channel_id,
        root_message_ts=root_ts,
        root_text=text,
        social_post_url=new_url,
    )
    if str(admission.get("status") or "") == "approved" and new_url != old_url:
        rejected = store.mark_rejected(
            int(admission["id"]),
            status="rejected_invalid",
            reason="approved boost root changed its social post URL",
        )
        notice = (
            f"<@{rejected['poster_slack_id']}> the approved link was changed. "
            "To prevent a paid boost being swapped for a different post, I will remove this root. "
            "Post the new link separately for a new approval."
        )
        _post_decision_notice(
            rejected,
            store=store,
            text=notice,
            post_message_fn=post_message_fn,
        )
        refreshed = store.get_by_id(int(rejected["id"]))
        _dm_rejection(refreshed, store=store, text=notice, send_dm_fn=send_dm_fn)
        moderated = _moderate_rejected_root(
            store.get_by_id(int(rejected["id"])),
            store=store,
            delete_fn=delete_fn,
        )
        return {"status": str(moderated["status"]), "admission": moderated}
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


async def boost_post_retry_loop() -> None:
    settings = get_settings()
    poll_seconds = max(1.0, float(getattr(settings, "BOOST_POST_RETRY_POLL_SECONDS", 15.0)))
    while True:
        try:
            await process_due_boost_posts_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the durable worker alive.
            print(f"BOOST_POST_WORKER_ERROR error_type={exc.__class__.__name__}")
        await asyncio.sleep(poll_seconds)
