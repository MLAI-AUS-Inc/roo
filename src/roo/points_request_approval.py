from __future__ import annotations

"""Helpers for Slack-based points request approvals."""

from typing import Any, Optional

POINTS_REQUEST_METADATA_EVENT_TYPE = "roo_points_request"
APPROVAL_REACTION_NAMES = frozenset({"white_check_mark", "heavy_check_mark"})

_pending_points_request_summaries: dict[tuple[str, str], dict[str, Any]] = {}


def _coerce_optional_int(value: Any) -> Any:
    if value in (None, ""):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def normalize_points_request_record(record: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return a consistently shaped points-request record."""
    if not record:
        return None

    normalized = dict(record)
    normalized["id"] = _coerce_optional_int(normalized.get("id"))
    normalized["points"] = _coerce_optional_int(normalized.get("points"))
    return normalized


def build_points_request_record(
    *,
    request_id: int,
    requester_slack_id: str,
    target_slack_id: str,
    points: int,
    reason: str,
    slack_thread_ts: Optional[str],
) -> dict[str, Any]:
    """Build a pending request record from the data Roo already has locally."""
    record: dict[str, Any] = {
        "id": request_id,
        "status": "pending",
        "requester_slack_id": requester_slack_id,
        "target_slack_id": target_slack_id,
        "points": points,
        "reason": reason,
    }
    if slack_thread_ts:
        record["slack_thread_ts"] = slack_thread_ts
    return normalize_points_request_record(record) or record


def build_points_request_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Build Slack message metadata for a pending points request."""
    normalized = normalize_points_request_record(record) or {}
    payload: dict[str, Any] = {
        "request_id": normalized.get("id"),
        "requester_slack_id": normalized.get("requester_slack_id"),
        "target_slack_id": normalized.get("target_slack_id"),
        "points": normalized.get("points"),
        "reason": normalized.get("reason"),
    }
    if normalized.get("slack_thread_ts"):
        payload["slack_thread_ts"] = normalized["slack_thread_ts"]

    return {
        "event_type": POINTS_REQUEST_METADATA_EVENT_TYPE,
        "event_payload": payload,
    }


def remember_points_request_summary(
    channel_id: str,
    summary_message_ts: str,
    record: dict[str, Any],
) -> None:
    """Cache a Slack summary message to pending request mapping in-process."""
    if not channel_id or not summary_message_ts:
        return
    normalized = normalize_points_request_record(record)
    if normalized:
        _pending_points_request_summaries[(channel_id, summary_message_ts)] = normalized


def get_remembered_points_request_summary(
    channel_id: str,
    summary_message_ts: str,
) -> Optional[dict[str, Any]]:
    """Look up an in-process cached points request summary mapping."""
    record = _pending_points_request_summaries.get((channel_id, summary_message_ts))
    return dict(record) if record else None


def forget_points_request_summary(channel_id: str, summary_message_ts: str) -> None:
    """Remove an in-process cached points request summary mapping."""
    _pending_points_request_summaries.pop((channel_id, summary_message_ts), None)


def clear_points_request_summaries() -> None:
    """Clear cached summary mappings. Used by tests."""
    _pending_points_request_summaries.clear()


def get_points_request_record_from_message(message: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Extract a pending points request record from Slack message metadata."""
    if not message:
        return None

    metadata = message.get("metadata") or {}
    if metadata.get("event_type") != POINTS_REQUEST_METADATA_EVENT_TYPE:
        return None

    payload = metadata.get("event_payload") or {}
    request_id = payload.get("request_id")
    if request_id in (None, ""):
        return None

    record: dict[str, Any] = {
        "id": request_id,
        "status": "pending",
        "requester_slack_id": payload.get("requester_slack_id"),
        "target_slack_id": payload.get("target_slack_id"),
        "points": payload.get("points"),
        "reason": payload.get("reason"),
    }
    if payload.get("slack_thread_ts"):
        record["slack_thread_ts"] = payload["slack_thread_ts"]

    return normalize_points_request_record(record)
