"""Durable metadata for context-aware Slack conversations.

The store intentionally keeps no Slack message text. It records only enough
metadata to identify recent Roo-owned threads/channel turns and to suppress
logical duplicate deliveries across Slack event subscription types.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ConversationSession:
    team_id: str
    channel_id: str
    session_key: str
    requester_user_id: str
    thread_ts: Optional[str]
    last_bot_ts: str
    state: str
    workflow: str
    reference_id: Optional[str]
    expires_at: float


class ContextualConversationStore:
    """SQLite-backed sessions and logical Slack-message receipts."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._initialised = False
        self._initialise_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
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
                    CREATE TABLE IF NOT EXISTS contextual_message_receipts (
                        logical_key TEXT PRIMARY KEY,
                        received_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS contextual_message_receipts_expiry_idx
                    ON contextual_message_receipts (expires_at)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS contextual_conversation_sessions (
                        team_id TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        session_key TEXT NOT NULL,
                        requester_user_id TEXT NOT NULL,
                        thread_ts TEXT,
                        last_bot_ts TEXT NOT NULL,
                        state TEXT NOT NULL,
                        workflow TEXT NOT NULL DEFAULT '',
                        reference_id TEXT,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        PRIMARY KEY (team_id, channel_id, session_key)
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(contextual_conversation_sessions)"
                    ).fetchall()
                }
                if "workflow" not in columns:
                    connection.execute(
                        "ALTER TABLE contextual_conversation_sessions "
                        "ADD COLUMN workflow TEXT NOT NULL DEFAULT ''"
                    )
                if "reference_id" not in columns:
                    connection.execute(
                        "ALTER TABLE contextual_conversation_sessions "
                        "ADD COLUMN reference_id TEXT"
                    )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS contextual_sessions_expiry_idx
                    ON contextual_conversation_sessions (expires_at)
                    """
                )
            self._initialised = True

    @staticmethod
    def logical_message_key(team_id: str, channel_id: str, message_ts: str) -> str:
        return f"{team_id}:{channel_id}:{message_ts}"

    def claim_message(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        ttl_seconds: int,
        now: Optional[float] = None,
    ) -> bool:
        """Return true once for a logical Slack message, regardless of event type."""

        if not team_id or not channel_id or not message_ts:
            return False
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        self._initialise()
        current_time = time.time() if now is None else float(now)
        logical_key = self.logical_message_key(team_id, channel_id, message_ts)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM contextual_message_receipts WHERE expires_at <= ?",
                (current_time,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO contextual_message_receipts
                    (logical_key, received_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (logical_key, current_time, current_time + ttl_seconds),
            )
            connection.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _thread_key(thread_ts: str) -> str:
        return f"thread:{thread_ts}"

    @staticmethod
    def _channel_user_key(user_id: str) -> str:
        return f"channel-user:{user_id}"

    def record_roo_response(
        self,
        *,
        team_id: str,
        channel_id: str,
        requester_user_id: str,
        thread_ts: Optional[str],
        bot_message_ts: str,
        adjacency_seconds: int,
        thread_ttl_seconds: int,
        state: str = "active",
        workflow: str = "",
        reference_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Open/update thread and same-user adjacency sessions after Roo posts."""

        if not team_id or not channel_id or not requester_user_id or not bot_message_ts:
            return
        self._initialise()
        current_time = time.time() if now is None else float(now)
        rows = [
            (
                team_id,
                channel_id,
                self._channel_user_key(requester_user_id),
                requester_user_id,
                thread_ts,
                bot_message_ts,
                state,
                workflow,
                reference_id,
                current_time,
                current_time + adjacency_seconds,
            )
        ]
        if thread_ts:
            rows.append(
                (
                    team_id,
                    channel_id,
                    self._thread_key(thread_ts),
                    requester_user_id,
                    thread_ts,
                    bot_message_ts,
                    state,
                    workflow,
                    reference_id,
                    current_time,
                    current_time + thread_ttl_seconds,
                )
            )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO contextual_conversation_sessions (
                    team_id, channel_id, session_key, requester_user_id,
                    thread_ts, last_bot_ts, state, workflow, reference_id,
                    updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, session_key) DO UPDATE SET
                    requester_user_id = excluded.requester_user_id,
                    thread_ts = excluded.thread_ts,
                    last_bot_ts = excluded.last_bot_ts,
                    state = excluded.state,
                    workflow = excluded.workflow,
                    reference_id = excluded.reference_id,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                rows,
            )

    def find_session(
        self,
        *,
        team_id: str,
        channel_id: str,
        requester_user_id: str,
        thread_ts: Optional[str],
        now: Optional[float] = None,
    ) -> Optional[ConversationSession]:
        """Find a live same-user thread session, then channel adjacency session."""

        if not team_id or not channel_id or not requester_user_id:
            return None
        self._initialise()
        current_time = time.time() if now is None else float(now)
        keys = []
        if thread_ts:
            keys.append(self._thread_key(thread_ts))
        keys.append(self._channel_user_key(requester_user_id))

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM contextual_conversation_sessions WHERE expires_at <= ?",
                (current_time,),
            )
            for session_key in keys:
                row = connection.execute(
                    """
                    SELECT team_id, channel_id, session_key, requester_user_id,
                           thread_ts, last_bot_ts, state, workflow,
                           reference_id, expires_at
                    FROM contextual_conversation_sessions
                    WHERE team_id = ? AND channel_id = ? AND session_key = ?
                      AND requester_user_id = ? AND expires_at > ?
                    """,
                    (
                        team_id,
                        channel_id,
                        session_key,
                        requester_user_id,
                        current_time,
                    ),
                ).fetchone()
                if row:
                    return ConversationSession(**dict(row))
        return None

    def break_channel_adjacency(self, *, team_id: str, channel_id: str) -> None:
        """Close top-level adjacency after an intervening unhandled human message."""

        if not team_id or not channel_id:
            return
        self._initialise()
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM contextual_conversation_sessions
                WHERE team_id = ? AND channel_id = ?
                  AND session_key LIKE 'channel-user:%'
                """,
                (team_id, channel_id),
            )


@lru_cache(maxsize=8)
def get_contextual_conversation_store(database_path: str) -> ContextualConversationStore:
    return ContextualConversationStore(database_path)
