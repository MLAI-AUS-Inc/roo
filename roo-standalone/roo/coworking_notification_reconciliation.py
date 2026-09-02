from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .coworking_booking_schema_v3 import SCHEMA_VERSION, TABLE_NAME


VALID_OUTCOMES = frozenset({"delivered", "not_required", "retry"})
_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")


def reconcile_coworking_notification(
    db_path: str | Path,
    *,
    intent_id: int,
    outcome: str,
    operator_reference: str,
    now: Optional[float] = None,
) -> dict:
    """Resolve one quarantined notification using explicit operator evidence."""

    normalized_outcome = str(outcome or "").strip().lower().replace("-", "_")
    if normalized_outcome not in VALID_OUTCOMES:
        raise ValueError("outcome must be delivered, not_required, or retry")
    reference = str(operator_reference or "").strip()
    if not _REFERENCE_PATTERN.fullmatch(reference):
        raise ValueError("operator_reference must be 1-120 safe audit characters")
    current_time = time.time() if now is None else float(now)
    path = Path(db_path)
    if not path.is_file():
        raise ValueError("coworking booking database was not found")

    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if schema_version < SCHEMA_VERSION:
            raise RuntimeError(
                "coworking booking schema v3 must be applied before reconciliation"
            )
        row = connection.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE id = ?",
            (int(intent_id),),
        ).fetchone()
        if row is None:
            raise ValueError("coworking booking intent was not found")
        if row["status"] not in {"confirmed", "blocked"}:
            raise ValueError("only terminal booking intents can be reconciled")
        if row["notification_status"] != "reconciliation_required":
            raise ValueError("notification does not require reconciliation")

        notification_status = (
            "pending" if normalized_outcome == "retry" else normalized_outcome
        )
        delivered_at = current_time if normalized_outcome == "delivered" else None
        next_attempt_at = current_time if normalized_outcome == "retry" else None
        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET notification_status = ?,
                notification_next_attempt_at = ?,
                notification_locked_until = NULL,
                notification_locked_by = NULL,
                notification_last_error = NULL,
                notification_delivered_at = ?,
                notification_reconciled_at = ?,
                notification_reconciliation_reference = ?,
                notification_reconciliation_outcome = ?,
                updated_at = ?
            WHERE id = ?
              AND notification_status = 'reconciliation_required'
            """,
            (
                notification_status,
                next_attempt_at,
                delivered_at,
                current_time,
                reference,
                normalized_outcome,
                current_time,
                int(intent_id),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("notification reconciliation lost its state fence")
        updated = connection.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE id = ?",
            (int(intent_id),),
        ).fetchone()
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
