from __future__ import annotations

import sqlite3
from pathlib import Path

from .coworking_booking_schema_v2 import (
    REQUIRED_COLUMNS as V2_REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    TABLE_NAME,
    migrate_coworking_booking_intents_v2,
)


SCHEMA_VERSION = 3
RECONCILIATION_COLUMNS = {
    "notification_reconciled_at": "REAL",
    "notification_reconciliation_reference": "TEXT",
    "notification_reconciliation_outcome": "TEXT",
}
REQUIRED_COLUMNS = frozenset(V2_REQUIRED_COLUMNS | RECONCILIATION_COLUMNS.keys())


def _schema_version(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    with sqlite3.connect(str(db_path), timeout=30) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate_coworking_booking_intents_v3(db_path: str | Path) -> int:
    """Apply the append-only v3 lineage repair.

    An earlier shared v2 body added ``notification_status`` with a
    ``not_required`` default before it quarantined historical terminal rows.
    Once that migration identity has been recorded, changing the v2 body does
    not help those databases. V3 therefore treats every indistinguishable
    terminal ``not_required`` row as requiring explicit reconciliation.

    Returns the number of rows newly quarantined.
    """

    path = Path(db_path)
    if _schema_version(path) >= SCHEMA_VERSION:
        return 0

    # Preserve a fresh v1 -> v2 -> v3 journey while also repairing databases
    # that already recorded the shared predecessor as version 2.
    migrate_coworking_booking_intents_v2(path)

    connection = sqlite3.connect(str(path), timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        }
        for column_name, definition in RECONCILIATION_COLUMNS.items():
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column_name} {definition}"
                )

        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET notification_status = 'reconciliation_required',
                notification_next_attempt_at = NULL,
                notification_locked_until = NULL,
                notification_locked_by = NULL,
                notification_last_error = 'v2_delivery_provenance_unknown',
                notification_delivered_at = NULL,
                notification_reconciled_at = NULL,
                notification_reconciliation_reference = NULL,
                notification_reconciliation_outcome = NULL
            WHERE status IN ('confirmed', 'blocked')
              AND notification_status = 'not_required'
              AND notification_delivered_at IS NULL
            """
        )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        return int(cursor.rowcount)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
