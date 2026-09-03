from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional
from uuid import NAMESPACE_URL, UUID, uuid5

from .coworking_booking_schema_v3 import SCHEMA_VERSION, TABLE_NAME


VALID_OUTCOMES = frozenset({"delivered", "not_required", "retry"})
_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}\Z")


def _normalized_retry_payload(row: sqlite3.Row) -> str:
    """Upgrade a historical confirmation into today's delivery contract."""
    try:
        payload = json.loads(str(row["backend_result_json"] or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "retry requires a recorded booking result; choose delivered or not_required"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            "retry requires a recorded booking result; choose delivered or not_required"
        )
    raw_id = str(payload.get("id") or "").strip()
    booking_date = str(payload.get("date") or "").strip()
    points_cost = payload.get("points_cost")
    if (
        not raw_id
        or booking_date != str(row["booking_date"])
        or payload.get("status") != "booked"
        or not isinstance(points_cost, int)
        or isinstance(points_cost, bool)
        or points_cost < 0
    ):
        raise ValueError(
            "retry requires a complete historical booking result; "
            "choose delivered or not_required"
        )
    try:
        normalized_id = str(UUID(raw_id))
    except ValueError:
        normalized_id = str(uuid5(NAMESPACE_URL, f"roo:legacy-booking:{raw_id}"))
        payload["legacy_booking_reference"] = raw_id
    standard_cost = payload.get("standard_points_cost")
    recorded_discount = payload.get("monthly_update_discount_applied")
    pricing_state_known = bool(
        isinstance(standard_cost, int)
        and not isinstance(standard_cost, bool)
        and standard_cost >= points_cost
        and isinstance(recorded_discount, bool)
        and recorded_discount is (points_cost < standard_cost)
    )
    if not pricing_state_known:
        standard_cost = points_cost
    connection_type = payload.get("founder_tools_connection_type")
    account_linked = payload.get("founder_tools_account_linked")
    explicitly_linked = payload.get("founder_tools_explicitly_linked")
    link_state_known = (
        connection_type in {None, "direct", "explicit"}
        and isinstance(account_linked, bool)
        and isinstance(explicitly_linked, bool)
        and account_linked is (connection_type is not None)
        and explicitly_linked is (connection_type == "explicit")
    )
    if not link_state_known:
        connection_type = None
    payload.update(
        {
            "id": normalized_id,
            "standard_points_cost": standard_cost,
            "monthly_update_discount_applied": points_cost < standard_cost,
            "pricing_state_historical_unknown": not pricing_state_known,
            "founder_tools_connection_type": connection_type,
            "founder_tools_account_linked": connection_type is not None,
            "founder_tools_explicitly_linked": connection_type == "explicit",
            "founder_tools_link_state_historical_unknown": not link_state_known,
        }
    )
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


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

        normalized_backend_result = row["backend_result_json"]
        if normalized_outcome == "retry" and row["status"] == "confirmed":
            normalized_backend_result = _normalized_retry_payload(row)

        notification_status = (
            "pending" if normalized_outcome == "retry" else normalized_outcome
        )
        delivered_at = current_time if normalized_outcome == "delivered" else None
        next_attempt_at = current_time if normalized_outcome == "retry" else None
        cursor = connection.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET notification_status = ?,
                backend_result_json = ?,
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
                normalized_backend_result,
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
