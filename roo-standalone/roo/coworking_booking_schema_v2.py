from __future__ import annotations

import sqlite3
from pathlib import Path


TABLE_NAME = "coworking_booking_intents"
SCHEMA_VERSION = 2
REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "idempotency_key",
        "slack_user_id",
        "requested_by_slack_id",
        "booking_date",
        "channel_id",
        "thread_ts",
        "request_text",
        "status",
        "attempt_count",
        "next_attempt_at",
        "locked_until",
        "locked_by",
        "last_error",
        "backend_booking_id",
        "backend_result_json",
        "created_at",
        "updated_at",
        "confirmed_at",
        "notification_status",
        "notification_attempt_count",
        "notification_next_attempt_at",
        "notification_locked_until",
        "notification_locked_by",
        "notification_last_error",
        "notification_delivered_at",
    }
)
REQUIRED_INDEXES = frozenset(
    {
        "idx_coworking_intents_due",
        "idx_coworking_intents_user_date",
        "idx_coworking_notifications_due",
    }
)


def migrate_coworking_booking_intents_v2(db_path: str | Path) -> None:
    """Apply the approved, one-shot v2 schema and privacy migration."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
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
                confirmed_at REAL,
                notification_status TEXT NOT NULL DEFAULT 'not_required',
                notification_attempt_count INTEGER NOT NULL DEFAULT 0,
                notification_next_attempt_at REAL,
                notification_locked_until REAL,
                notification_locked_by TEXT,
                notification_last_error TEXT,
                notification_delivered_at REAL
            )
            """
        )
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(coworking_booking_intents)"
            ).fetchall()
        }
        additions = {
            "requested_by_slack_id": "TEXT",
            "notification_status": "TEXT NOT NULL DEFAULT 'not_required'",
            "notification_attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "notification_next_attempt_at": "REAL",
            "notification_locked_until": "REAL",
            "notification_locked_by": "TEXT",
            "notification_last_error": "TEXT",
            "notification_delivered_at": "REAL",
        }
        for column_name, definition in additions.items():
            if column_name not in columns:
                conn.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {definition}"
                )

        # Canonical fields are sufficient for replay. Remove historical raw
        # Slack text once, under the same transaction as the schema upgrade.
        conn.execute(
            "UPDATE coworking_booking_intents "
            "SET request_text = NULL WHERE request_text IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_coworking_intents_due "
            "ON coworking_booking_intents (status, next_attempt_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_coworking_intents_user_date "
            "ON coworking_booking_intents (slack_user_id, booking_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_coworking_notifications_due "
            "ON coworking_booking_intents "
            "(status, notification_status, notification_next_attempt_at)"
        )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
