"""
Bridge persistence (SQLite).

Follows roo/coworking_booking_intents.py conventions: stdlib sqlite3, WAL,
autocommit (isolation_level=None), a threading lock, lazily-created schema.

Store-and-forward design — capture and delivery are decoupled:

  * inbound          — the durable queue. Capture writes every relayable message
                       here exactly once (UNIQUE on the source coordinates); a
                       delivery worker drains it, posting to the other workspace
                       with retries/backoff. A failed post is retried, never lost.
  * posted_registry  — every (team, channel, ts) the bridge itself posted. The
                       authoritative loop guard: a captured message found here is
                       one of our own echoes and is skipped.
  * message_map      — source message → the copy we posted, so threaded replies
                       can be re-threaded on the other side.
  * kv               — small values, e.g. each channel's poll high-water mark.
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class BridgeStore:
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
                    CREATE TABLE IF NOT EXISTS inbound (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        direction TEXT NOT NULL,           -- mlai_to_snc | snc_to_mlai
                        src_team TEXT NOT NULL,
                        src_channel TEXT NOT NULL,
                        src_ts TEXT NOT NULL,
                        payload_json TEXT NOT NULL,        -- raw Slack message object
                        status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE (src_team, src_channel, src_ts)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_inbound_due
                    ON inbound (status, next_attempt_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS posted_registry (
                        team TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        ts TEXT NOT NULL,
                        posted_at REAL NOT NULL,
                        PRIMARY KEY (team, channel, ts)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS message_map (
                        src_team TEXT NOT NULL,
                        src_channel TEXT NOT NULL,
                        src_ts TEXT NOT NULL,
                        dst_team TEXT NOT NULL,
                        dst_channel TEXT NOT NULL,
                        dst_ts TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (src_team, src_channel, src_ts)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kv (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at REAL NOT NULL
                    )
                    """
                )
            self._initialized = True

    # --- inbound queue -------------------------------------------------------

    def enqueue_inbound(
        self, direction: str, src_team: str, src_channel: str, src_ts: str, payload_json: str
    ) -> bool:
        """Capture a message into the queue. Returns True if newly enqueued,
        False if it was already captured (idempotent on the source coordinates)."""
        self._ensure_schema()
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO inbound "
                    "(direction, src_team, src_channel, src_ts, payload_json, status, "
                    " attempt_count, next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)",
                    (direction, src_team, src_channel, src_ts, payload_json, now, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def claim_due_inbound(self, limit: int, now: float) -> List[Dict[str, Any]]:
        """Pending rows whose next_attempt_at has passed, oldest first (FIFO)."""
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inbound WHERE status='pending' AND next_attempt_at<=? "
                "ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_delivered(self, row_id: int) -> None:
        self._update_inbound(row_id, status="delivered")

    def mark_retry(self, row_id: int, error: str, next_attempt_at: float, attempt_count: int) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE inbound SET attempt_count=?, next_attempt_at=?, last_error=?, updated_at=? "
                "WHERE id=?",
                (attempt_count, next_attempt_at, error, time.time(), row_id),
            )

    def mark_failed(self, row_id: int, error: str) -> None:
        self._update_inbound(row_id, status="failed", error=error)

    def _update_inbound(self, row_id: int, *, status: str, error: Optional[str] = None) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE inbound SET status=?, last_error=?, updated_at=? WHERE id=?",
                (status, error, time.time(), row_id),
            )

    # --- loop guard ----------------------------------------------------------

    def record_posted(self, team: str, channel: str, ts: str) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO posted_registry (team, channel, ts, posted_at) "
                "VALUES (?, ?, ?, ?)",
                (team, channel, ts, time.time()),
            )

    def is_self_posted(self, team: str, channel: str, ts: str) -> bool:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted_registry WHERE team=? AND channel=? AND ts=?",
                (team, channel, ts),
            ).fetchone()
            return row is not None

    # --- message correlation -------------------------------------------------

    def map_message(
        self, src_team, src_channel, src_ts, dst_team, dst_channel, dst_ts
    ) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO message_map "
                "(src_team, src_channel, src_ts, dst_team, dst_channel, dst_ts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (src_team, src_channel, src_ts, dst_team, dst_channel, dst_ts, time.time()),
            )

    def dst_for(self, src_team, src_channel, src_ts) -> Optional[Tuple[str, str, str]]:
        """Forward: where did we post the copy of this source message?"""
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT dst_team, dst_channel, dst_ts FROM message_map "
                "WHERE src_team=? AND src_channel=? AND src_ts=?",
                (src_team, src_channel, src_ts),
            ).fetchone()
            return (row["dst_team"], row["dst_channel"], row["dst_ts"]) if row else None

    def src_for(self, dst_team, dst_channel, dst_ts) -> Optional[Tuple[str, str, str]]:
        """Reverse: given a posted copy, what source message produced it? Needed
        to thread a reply that lands on a parent which was *received* on this side."""
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT src_team, src_channel, src_ts FROM message_map "
                "WHERE dst_team=? AND dst_channel=? AND dst_ts=?",
                (dst_team, dst_channel, dst_ts),
            ).fetchone()
            return (row["src_team"], row["src_channel"], row["src_ts"]) if row else None

    # --- kv ------------------------------------------------------------------

    def get_kv(self, key: str) -> Optional[str]:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self._ensure_schema()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, time.time()),
            )

    # --- maintenance ---------------------------------------------------------

    def prune(self, max_age_seconds: float) -> int:
        """Delete old delivered/failed queue rows + registry rows. Returns rows removed."""
        self._ensure_schema()
        cutoff = time.time() - max_age_seconds
        removed = 0
        with self._lock, self._connect() as conn:
            for sql in (
                "DELETE FROM inbound WHERE status IN ('delivered','failed') AND updated_at < ?",
                "DELETE FROM posted_registry WHERE posted_at < ?",
            ):
                cur = conn.execute(sql, (cutoff,))
                removed += cur.rowcount or 0
        return removed


_store: Optional[BridgeStore] = None


def get_store(db_path: Optional[str] = None) -> BridgeStore:
    global _store
    if _store is None:
        from .config import get_bridge_settings

        _store = BridgeStore(db_path or get_bridge_settings().BRIDGE_DB_PATH)
    return _store
