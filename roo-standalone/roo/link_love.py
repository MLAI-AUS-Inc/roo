from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .config import get_settings


LINK_LOVE_POINTS = 2
LINK_LOVE_REASON = "link-love"
DEFAULT_LINK_LOVE_DB_PATH = "data/link_love_awards.db"
DEFAULT_RETRY_POLL_SECONDS = 15.0
DEFAULT_PROCESSING_LEASE_SECONDS = 90.0
DEFAULT_MAX_RETRY_ATTEMPTS = 5
DEFAULT_MAX_ROOT_AGE_DAYS = 7
SECONDS_PER_DAY = 24 * 60 * 60
SUPPORTED_SOCIAL_HOSTS = (
    "linkedin.com",
    "lnkd.in",
    "x.com",
    "twitter.com",
    "instagram.com",
    "facebook.com",
)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "lipi", "trk", "trackingid"}


def _now() -> float:
    return time.time()


def retry_delay_seconds(attempt_count: int) -> float:
    attempt_number = max(1, int(attempt_count or 1))
    return min(15 * 60, 30 * (2 ** (attempt_number - 1)))


def link_love_backend_idempotency_key(award: dict[str, Any]) -> str:
    return (
        f"link_love:{award['channel_id']}:"
        f"{award['root_message_ts']}:{award['slack_user_id']}"
    )


def link_love_max_retry_attempts() -> int:
    try:
        value = int(getattr(get_settings(), "BOOST_LINK_LOVE_MAX_RETRY_ATTEMPTS", DEFAULT_MAX_RETRY_ATTEMPTS))
    except Exception:
        value = DEFAULT_MAX_RETRY_ATTEMPTS
    return max(1, value)


def link_love_max_root_age_seconds() -> float:
    try:
        days = float(getattr(get_settings(), "BOOST_LINK_LOVE_MAX_ROOT_AGE_DAYS", DEFAULT_MAX_ROOT_AGE_DAYS))
    except Exception:
        days = DEFAULT_MAX_ROOT_AGE_DAYS
    return max(0.0, days) * SECONDS_PER_DAY


def extract_social_post_url(text: str) -> Optional[str]:
    """Return a stable supported social-post URL without fetching content."""

    candidates = re.findall(r"https?://[^\s<>|]+", str(text or ""), flags=re.IGNORECASE)
    candidates.extend(
        match.group(1)
        for match in re.finditer(
            r"<(https?://[^>|]+)(?:\|[^>]+)?>", str(text or ""), re.IGNORECASE
        )
    )
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:!?)\\]}\"")
        try:
            parts = urlsplit(candidate)
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port
        except ValueError:
            continue
        if not any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in SUPPORTED_SOCIAL_HOSTS
        ):
            continue
        netloc = host
        if port and port not in {80, 443}:
            netloc = f"{host}:{port}"
        filtered_query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in TRACKING_QUERY_KEYS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(("https", netloc, path, urlencode(filtered_query), ""))
    return None


def slack_timestamp_to_epoch_seconds(slack_ts: str) -> float:
    return float(str(slack_ts or "").strip())


def is_link_love_root_expired(
    root_message_ts: str,
    *,
    now: Optional[float] = None,
    max_age_seconds: Optional[float] = None,
) -> bool:
    root_epoch_seconds = slack_timestamp_to_epoch_seconds(root_message_ts)
    current_time = _now() if now is None else float(now)
    allowed_age = link_love_max_root_age_seconds() if max_age_seconds is None else float(max_age_seconds)
    return current_time - root_epoch_seconds > allowed_age


def clean_slack_user_id(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if value.startswith("<@") and value.endswith(">"):
        value = value[2:-1]
    return value.strip()


def is_thread_reply_event(event: dict[str, Any]) -> bool:
    thread_ts = str(event.get("thread_ts") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    return bool(thread_ts and message_ts and thread_ts != message_ts)


def is_retryable_link_love_exception(exc: Exception) -> bool:
    from .clients.mlai_backend import MLAIBackendUnavailableError

    if isinstance(exc, MLAIBackendUnavailableError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in {408, 425, 429} or 500 <= status_code < 600
    return False


@dataclass(frozen=True)
class LinkLoveClassification:
    engaged: bool
    confidence: float
    reason: str
    raw_response: str


def build_link_love_classification_messages(
    *,
    root_text: str,
    reply_text: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You classify Slack thread replies in #boost-my-startup. "
                "Founders post social content and other founders reply after engaging off Slack. "
                "Return only JSON with keys engaged, confidence, and reason. "
                "Set engaged=true only when the reply clearly claims the user liked, commented, "
                "shared, reposted, boosted, supported, or otherwise engaged with the original "
                "social post. Count short channel shorthand like 'done' or 'liked'. "
                "Do not count questions, discussion about the post, pure praise, or vague support "
                "such as 'love it', 'great launch', 'congrats', 'huge', or 'this is awesome'."
            ),
        },
        {
            "role": "user",
            "content": (
                "Examples:\n"
                'Reply: "Liked" -> {"engaged": true, "confidence": 0.98, "reason": "explicit like"}\n'
                'Reply: "Done" -> {"engaged": true, "confidence": 0.9, "reason": "channel shorthand for completed engagement"}\n'
                'Reply: "Liked and commented" -> {"engaged": true, "confidence": 0.99, "reason": "explicit like and comment"}\n'
                'Reply: "Love it!!" -> {"engaged": false, "confidence": 0.9, "reason": "vague support only"}\n'
                'Reply: "How did you get Batko as cofounder?" -> {"engaged": false, "confidence": 0.96, "reason": "question only"}\n\n'
                f"Original top-level post:\n{root_text[:2000]}\n\n"
                f"Thread reply to classify:\n{reply_text[:1000]}"
            ),
        },
    ]


def _extract_json_object(raw_content: str) -> dict[str, Any]:
    content = str(raw_content or "").strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(content[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("classifier response must be a JSON object")
    return parsed


def parse_link_love_classification(raw_content: str) -> LinkLoveClassification:
    try:
        payload = _extract_json_object(raw_content)
        confidence = float(payload.get("confidence") or 0.0)
        return LinkLoveClassification(
            engaged=bool(payload.get("engaged")),
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(payload.get("reason") or "").strip(),
            raw_response=str(raw_content or ""),
        )
    except Exception as exc:
        return LinkLoveClassification(
            engaged=False,
            confidence=0.0,
            reason=f"classifier_parse_error: {exc}",
            raw_response=str(raw_content or ""),
        )


async def classify_link_love_reply(
    *,
    root_text: str,
    reply_text: str,
    llm_chat: Optional[Callable[..., Any]] = None,
) -> LinkLoveClassification:
    if llm_chat is None:
        from .llm import chat as llm_chat

    response = await llm_chat(
        build_link_love_classification_messages(root_text=root_text, reply_text=reply_text),
        max_tokens=160,
        temperature=0,
    )
    return parse_link_love_classification(getattr(response, "content", response))


class LinkLoveAwardStore:
    def __init__(self, db_path: str | Path = DEFAULT_LINK_LOVE_DB_PATH):
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
                    CREATE TABLE IF NOT EXISTS link_love_reply_checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT NOT NULL,
                        root_message_ts TEXT NOT NULL,
                        reply_message_ts TEXT NOT NULL,
                        slack_user_id TEXT NOT NULL,
                        root_author_slack_id TEXT,
                        reply_text TEXT,
                        root_text TEXT,
                        classification_status TEXT NOT NULL,
                        qualifies INTEGER NOT NULL DEFAULT 0,
                        classifier_reason TEXT,
                        classifier_response_json TEXT,
                        award_id INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(channel_id, reply_message_ts)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS link_love_awards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT NOT NULL,
                        root_message_ts TEXT NOT NULL,
                        slack_user_id TEXT NOT NULL,
                        root_author_slack_id TEXT,
                        source_reply_message_ts TEXT,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL,
                        locked_until REAL,
                        locked_by TEXT,
                        last_error TEXT,
                        backend_result_json TEXT,
                        points_awarded INTEGER,
                        new_balance INTEGER,
                        notification_available_at REAL,
                        notified_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(channel_id, root_message_ts, slack_user_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_link_love_awards_due
                    ON link_love_awards (status, next_attempt_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_link_love_awards_notifications
                    ON link_love_awards (status, notification_available_at)
                    """
                )
            self._initialized = True

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_reply_check(self, channel_id: str, reply_message_ts: str) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            return self._row_to_dict(
                conn.execute(
                    """
                    SELECT * FROM link_love_reply_checks
                    WHERE channel_id = ? AND reply_message_ts = ?
                    """,
                    (channel_id, reply_message_ts),
                ).fetchone()
            )

    def has_expired_reply_check(
        self,
        *,
        channel_id: str,
        root_message_ts: str,
        slack_user_id: str,
    ) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            return (
                conn.execute(
                    """
                    SELECT 1 FROM link_love_reply_checks
                    WHERE channel_id = ?
                      AND root_message_ts = ?
                      AND slack_user_id = ?
                      AND classification_status = 'expired'
                    LIMIT 1
                    """,
                    (channel_id, root_message_ts, slack_user_id),
                ).fetchone()
                is not None
            )

    def create_reply_check(
        self,
        *,
        channel_id: str,
        root_message_ts: str,
        reply_message_ts: str,
        slack_user_id: str,
        root_author_slack_id: str,
        reply_text: str,
        root_text: str,
    ) -> Optional[dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO link_love_reply_checks (
                        channel_id,
                        root_message_ts,
                        reply_message_ts,
                        slack_user_id,
                        root_author_slack_id,
                        reply_text,
                        root_text,
                        classification_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        channel_id,
                        root_message_ts,
                        reply_message_ts,
                        slack_user_id,
                        root_author_slack_id,
                        reply_text,
                        root_text,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            return self._row_to_dict(
                conn.execute(
                    "SELECT * FROM link_love_reply_checks WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            )

    def mark_reply_check_classified(
        self,
        reply_check_id: int,
        *,
        status: str,
        qualifies: bool,
        reason: str,
        raw_response: str,
        award_id: Optional[int] = None,
    ) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE link_love_reply_checks
                SET classification_status = ?,
                    qualifies = ?,
                    classifier_reason = ?,
                    classifier_response_json = ?,
                    award_id = COALESCE(?, award_id),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    1 if qualifies else 0,
                    reason,
                    raw_response,
                    award_id,
                    _now(),
                    reply_check_id,
                ),
            )

    def create_award(
        self,
        *,
        channel_id: str,
        root_message_ts: str,
        slack_user_id: str,
        root_author_slack_id: str,
        source_reply_message_ts: str,
        locked_by: Optional[str] = None,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> tuple[bool, dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        locked_until = now + lease_seconds if locked_by else None
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO link_love_awards (
                        channel_id,
                        root_message_ts,
                        slack_user_id,
                        root_author_slack_id,
                        source_reply_message_ts,
                        status,
                        next_attempt_at,
                        locked_until,
                        locked_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending_award', ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_id,
                        root_message_ts,
                        slack_user_id,
                        root_author_slack_id,
                        source_reply_message_ts,
                        now,
                        locked_until,
                        locked_by,
                        now,
                        now,
                    ),
                )
                created = True
                award_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                created = False
                award_id = None

            row = (
                conn.execute(
                    "SELECT * FROM link_love_awards WHERE id = ?",
                    (award_id,),
                ).fetchone()
                if award_id is not None
                else conn.execute(
                    """
                    SELECT * FROM link_love_awards
                    WHERE channel_id = ? AND root_message_ts = ? AND slack_user_id = ?
                    """,
                    (channel_id, root_message_ts, slack_user_id),
                ).fetchone()
            )
            return created, dict(row)

    def claim_due_awards(
        self,
        *,
        limit: int,
        owner: str,
        lease_seconds: float = DEFAULT_PROCESSING_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        locked_until = now + lease_seconds
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM link_love_awards
                WHERE status = 'pending_award'
                  AND next_attempt_at <= ?
                  AND (locked_until IS NULL OR locked_until <= ?)
                ORDER BY next_attempt_at, id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE link_love_awards
                SET locked_until = ?, locked_by = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (locked_until, owner, now, *ids),
            )
            claimed = conn.execute(
                f"SELECT * FROM link_love_awards WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
            return [dict(row) for row in claimed]

    def mark_awarded(
        self,
        award_id: int,
        *,
        backend_result: dict[str, Any],
        notification_delay_seconds: float,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = _now()
        notification_available_at = now + max(0.0, float(notification_delay_seconds))
        points_awarded = backend_result.get("points_awarded")
        new_balance = backend_result.get("new_balance")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE link_love_awards
                SET status = 'awarded',
                    locked_until = NULL,
                    locked_by = NULL,
                    last_error = NULL,
                    backend_result_json = ?,
                    points_awarded = ?,
                    new_balance = ?,
                    notification_available_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(backend_result, sort_keys=True),
                    int(points_awarded) if points_awarded is not None else None,
                    int(new_balance) if new_balance is not None else None,
                    notification_available_at,
                    now,
                    award_id,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM link_love_awards WHERE id = ?",
                    (award_id,),
                ).fetchone()
            )

    def mark_retryable_failure(
        self,
        award_id: int,
        *,
        error: str,
        max_attempts: Optional[int] = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM link_love_awards WHERE id = ?",
                (award_id,),
            ).fetchone()
            next_attempt_count = int(row["attempt_count"] or 0) + 1 if row else 1
            should_block = max_attempts is not None and next_attempt_count >= int(max_attempts)
            conn.execute(
                """
                UPDATE link_love_awards
                SET status = ?,
                    attempt_count = ?,
                    next_attempt_at = ?,
                    locked_until = NULL,
                    locked_by = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    "blocked" if should_block else "pending_award",
                    next_attempt_count,
                    now if should_block else now + retry_delay_seconds(next_attempt_count),
                    error,
                    now,
                    award_id,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM link_love_awards WHERE id = ?",
                    (award_id,),
                ).fetchone()
            )

    def mark_blocked(self, award_id: int, *, error: str) -> dict[str, Any]:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE link_love_awards
                SET status = 'blocked',
                    locked_until = NULL,
                    locked_by = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, _now(), award_id),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM link_love_awards WHERE id = ?",
                    (award_id,),
                ).fetchone()
            )

    def get_due_notification_groups(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_schema()
        now = _now()
        with self._connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM link_love_awards
                    WHERE status = 'awarded'
                      AND notified_at IS NULL
                      AND notification_available_at <= ?
                    ORDER BY channel_id, root_message_ts, created_at
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
        groups_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (str(row["channel_id"]), str(row["root_message_ts"]))
            groups_by_key.setdefault(key, []).append(row)
        return [
            {"channel_id": key[0], "root_message_ts": key[1], "awards": awards}
            for key, awards in groups_by_key.items()
        ]

    def mark_notified(self, award_ids: list[int]) -> None:
        if not award_ids:
            return
        self._ensure_schema()
        now = _now()
        with self._lock, self._connect() as conn:
            placeholders = ",".join("?" for _ in award_ids)
            conn.execute(
                f"""
                UPDATE link_love_awards
                SET status = 'notified',
                    notified_at = ?,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, now, *award_ids),
            )


_store: LinkLoveAwardStore | None = None


def get_link_love_store() -> LinkLoveAwardStore:
    global _store
    if _store is None:
        settings = get_settings()
        db_path = getattr(settings, "BOOST_LINK_LOVE_DB_PATH", DEFAULT_LINK_LOVE_DB_PATH)
        _store = LinkLoveAwardStore(db_path)
    return _store


def _build_backend_client():
    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    return MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )


def _build_notification_text(awards: list[dict[str, Any]]) -> str:
    if len(awards) == 1:
        heading = ":tada: Awarded 2 points for link-love."
    else:
        heading = ":tada: Awarded 2 points each for link-love."

    lines = [heading]
    for award in awards:
        user_id = award.get("slack_user_id")
        new_balance = award.get("new_balance")
        if new_balance is None:
            lines.append(f":white_check_mark: <@{user_id}>: awarded 2 pts")
        else:
            lines.append(f":white_check_mark: <@{user_id}>: now has {new_balance} pts")
    return "\n".join(lines)


def _build_expired_link_love_notice(slack_user_id: str) -> str:
    return (
        f"Sorry <@{slack_user_id}>, this post is more than a week old, "
        "so it no longer qualifies for link-love points."
    )


def post_expired_link_love_notice(*, channel_id: str, root_message_ts: str, slack_user_id: str) -> None:
    from .slack_client import post_message

    post_message(
        channel=channel_id,
        thread_ts=root_message_ts,
        text=_build_expired_link_love_notice(slack_user_id),
    )


async def process_link_love_award(
    award: dict[str, Any],
    *,
    store: Optional[LinkLoveAwardStore] = None,
    client: Any = None,
    bot_user_id: Optional[str] = None,
    notification_delay_seconds: Optional[float] = None,
    max_retry_attempts: Optional[int] = None,
) -> dict[str, Any]:
    store = store or get_link_love_store()
    from .boost_moderation import boost_reward_admission_decision

    admission_decision = boost_reward_admission_decision(
        str(award.get("channel_id") or ""),
        str(award.get("root_message_ts") or ""),
    )
    if admission_decision not in {"legacy", "approved"}:
        error = f"boost root admission is {admission_decision}"
        if admission_decision == "rejected":
            updated = store.mark_blocked(int(award["id"]), error=error)
            return {"status": "blocked_admission", "award": updated, "error": error}
        updated = store.mark_retryable_failure(
            int(award["id"]),
            error=error,
            max_attempts=max_retry_attempts or link_love_max_retry_attempts(),
        )
        return {"status": "pending_admission", "award": updated, "error": error}
    client = client or _build_backend_client()
    if bot_user_id is None:
        from .slack_client import get_bot_user_id

        bot_user_id = get_bot_user_id()

    if notification_delay_seconds is not None:
        delay = notification_delay_seconds
    else:
        settings = get_settings()
        delay = getattr(settings, "BOOST_LINK_LOVE_NOTIFICATION_DELAY_SECONDS", 60)
    resolved_max_retry_attempts = (
        max(1, int(max_retry_attempts))
        if max_retry_attempts is not None
        else link_love_max_retry_attempts()
    )

    try:
        backend_result = await client.system_award_points(
            admin_slack_id=bot_user_id,
            target_slack_id=str(award["slack_user_id"]),
            points=LINK_LOVE_POINTS,
            reason=LINK_LOVE_REASON,
            idempotency_key=link_love_backend_idempotency_key(award),
        )
        updated = store.mark_awarded(
            int(award["id"]),
            backend_result=backend_result,
            notification_delay_seconds=float(delay),
        )
        return {"status": "awarded", "award": updated, "backend_result": backend_result}
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        if is_retryable_link_love_exception(exc):
            updated = store.mark_retryable_failure(
                int(award["id"]),
                error=error,
                max_attempts=resolved_max_retry_attempts,
            )
            if updated.get("status") == "blocked":
                print(
                    "⚠️ link_love_award_blocked_after_retries "
                    f"award_id={award.get('id')} slack_user_id={award.get('slack_user_id')} "
                    f"attempt_count={updated.get('attempt_count')} max_attempts={resolved_max_retry_attempts} "
                    f"error={error}"
                )
                return {"status": "blocked", "award": updated, "error": error}
            print(
                "🔁 link_love_award_retryable_failure "
                f"award_id={award.get('id')} slack_user_id={award.get('slack_user_id')} error={error}"
            )
            return {"status": "pending_retry", "award": updated, "error": error}
        updated = store.mark_blocked(int(award["id"]), error=error)
        print(
            "⚠️ link_love_award_blocked "
            f"award_id={award.get('id')} slack_user_id={award.get('slack_user_id')} error={error}"
        )
        return {"status": "blocked", "award": updated, "error": error}


async def handle_link_love_reply(
    event: dict[str, Any],
    *,
    store: Optional[LinkLoveAwardStore] = None,
    client: Any = None,
    get_root_message: Optional[Callable[[str, str], Optional[dict[str, Any]]]] = None,
    llm_chat: Optional[Callable[..., Any]] = None,
    bot_user_id: Optional[str] = None,
    notification_delay_seconds: Optional[float] = None,
) -> dict[str, Any]:
    if not is_thread_reply_event(event):
        return {"status": "ignored", "reason": "not_thread_reply"}

    store = store or get_link_love_store()
    channel_id = str(event.get("channel") or "").strip()
    root_message_ts = str(event.get("thread_ts") or "").strip()
    reply_message_ts = str(event.get("ts") or "").strip()
    slack_user_id = clean_slack_user_id(event.get("user"))
    reply_text = str(event.get("text") or "")
    if not channel_id or not root_message_ts or not reply_message_ts or not slack_user_id:
        return {"status": "ignored", "reason": "missing_required_event_fields"}

    from .boost_moderation import boost_reward_admission_decision

    admission_decision = boost_reward_admission_decision(channel_id, root_message_ts)
    if admission_decision == "pending":
        return {"status": "ignored", "reason": "boost_admission_pending"}
    if admission_decision == "rejected":
        return {"status": "ignored", "reason": "boost_admission_rejected"}

    if store.get_reply_check(channel_id, reply_message_ts):
        return {"status": "duplicate_reply"}

    if get_root_message is None:
        from .slack_client import get_message

        get_root_message = get_message
    root_message = get_root_message(channel_id, root_message_ts)
    if not root_message:
        print(
            "⚠️ link_love_missing_root "
            f"channel_id={channel_id} root_message_ts={root_message_ts} reply_message_ts={reply_message_ts}"
        )
        return {"status": "ignored", "reason": "missing_root_message"}

    root_author_slack_id = clean_slack_user_id(root_message.get("user"))
    root_text = str(root_message.get("text") or "")
    reply_check = store.create_reply_check(
        channel_id=channel_id,
        root_message_ts=root_message_ts,
        reply_message_ts=reply_message_ts,
        slack_user_id=slack_user_id,
        root_author_slack_id=root_author_slack_id,
        reply_text=reply_text,
        root_text=root_text,
    )
    if reply_check is None:
        return {"status": "duplicate_reply"}

    if not root_author_slack_id:
        store.mark_reply_check_classified(
            int(reply_check["id"]),
            status="ineligible",
            qualifies=False,
            reason="root author missing",
            raw_response="",
        )
        return {"status": "ignored", "reason": "root_author_missing"}

    if root_author_slack_id == slack_user_id:
        store.mark_reply_check_classified(
            int(reply_check["id"]),
            status="ineligible",
            qualifies=False,
            reason="root author cannot earn link-love on their own post",
            raw_response="",
        )
        return {"status": "ignored", "reason": "root_author_reply"}

    try:
        classification = await classify_link_love_reply(
            root_text=root_text,
            reply_text=reply_text,
            llm_chat=llm_chat,
        )
    except Exception as exc:
        classification = LinkLoveClassification(
            engaged=False,
            confidence=0.0,
            reason=f"classifier_error: {exc.__class__.__name__}: {exc}",
            raw_response="",
        )

    if not classification.engaged:
        store.mark_reply_check_classified(
            int(reply_check["id"]),
            status="ineligible",
            qualifies=False,
            reason=classification.reason,
            raw_response=classification.raw_response,
        )
        return {"status": "ineligible", "classification": classification}

    try:
        root_expired = is_link_love_root_expired(root_message_ts)
    except (TypeError, ValueError) as exc:
        error = f"invalid root timestamp: {root_message_ts}"
        print(
            "⚠️ link_love_invalid_root_timestamp "
            f"channel_id={channel_id} root_message_ts={root_message_ts} "
            f"reply_message_ts={reply_message_ts} error={exc}"
        )
        store.mark_reply_check_classified(
            int(reply_check["id"]),
            status="ineligible",
            qualifies=False,
            reason=error,
            raw_response=classification.raw_response,
        )
        return {"status": "ignored", "reason": "invalid_root_message_ts", "classification": classification}

    if root_expired:
        already_notified = store.has_expired_reply_check(
            channel_id=channel_id,
            root_message_ts=root_message_ts,
            slack_user_id=slack_user_id,
        )
        store.mark_reply_check_classified(
            int(reply_check["id"]),
            status="expired",
            qualifies=False,
            reason="root post is older than the link-love award window",
            raw_response=classification.raw_response,
        )
        if not already_notified:
            post_expired_link_love_notice(
                channel_id=channel_id,
                root_message_ts=root_message_ts,
                slack_user_id=slack_user_id,
            )
        return {
            "status": "expired",
            "classification": classification,
            "notice_posted": not already_notified,
        }

    award_created, award = store.create_award(
        channel_id=channel_id,
        root_message_ts=root_message_ts,
        slack_user_id=slack_user_id,
        root_author_slack_id=root_author_slack_id,
        source_reply_message_ts=reply_message_ts,
        locked_by=f"roo-link-love-handler-{reply_message_ts}",
    )
    store.mark_reply_check_classified(
        int(reply_check["id"]),
        status="eligible",
        qualifies=True,
        reason=classification.reason,
        raw_response=classification.raw_response,
        award_id=int(award["id"]),
    )
    if not award_created:
        return {"status": "already_awarded", "award": award}

    result = await process_link_love_award(
        award,
        store=store,
        client=client,
        bot_user_id=bot_user_id,
        notification_delay_seconds=notification_delay_seconds,
    )
    return {**result, "classification": classification}


def post_due_link_love_notifications(
    *,
    store: Optional[LinkLoveAwardStore] = None,
) -> int:
    store = store or get_link_love_store()
    groups = store.get_due_notification_groups()
    posted_count = 0
    if not groups:
        return posted_count

    from .slack_client import post_message

    for group in groups:
        awards = list(group["awards"])
        if not awards:
            continue
        award_ids = [int(award["id"]) for award in awards]
        post_message(
            channel=str(group["channel_id"]),
            thread_ts=str(group["root_message_ts"]),
            text=_build_notification_text(awards),
        )
        store.mark_notified(award_ids)
        posted_count += 1
    return posted_count


async def link_love_retry_loop(
    *,
    store: Optional[LinkLoveAwardStore] = None,
    poll_seconds: Optional[float] = None,
) -> None:
    settings = get_settings()
    store = store or get_link_love_store()
    poll_interval = float(
        poll_seconds
        if poll_seconds is not None
        else getattr(settings, "BOOST_LINK_LOVE_RETRY_POLL_SECONDS", DEFAULT_RETRY_POLL_SECONDS)
    )
    owner = f"roo-link-love-worker-{uuid4().hex}"
    print(f"🔗 Link-love worker started owner={owner} poll_seconds={poll_interval}")

    while True:
        try:
            due = store.claim_due_awards(limit=10, owner=owner)
            for award in due:
                await process_link_love_award(award, store=store)
            post_due_link_love_notifications(store=store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"🔗 link_love_worker_error exc_type={exc.__class__.__name__} exc={exc}")
        await asyncio.sleep(poll_interval)
