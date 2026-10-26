"""Bounded Slack context for contextual Linear task commands.

This module deliberately reads only the conversation in which Roo was invoked.
It does not subscribe to or cache workspace-wide Slack traffic.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .slack_client import (
    get_channel_context,
    get_recent_channel_messages,
    get_user_info,
)


CONTEXTUAL_LINEAR_REFERENCE_RE = re.compile(
    r"\b(?:add|create|make|put|send|sync|turn|log|file|raise)\s+"
    r"(?:the\s+)?(?:this|that|above|previous|last|conversation|discussion|thread|message)\b"
    r"|\b(?:request|item|task|message)\s+above\b"
    r"|\b(?:using|from|based\s+on)\s+(?:this|these|the)\s+"
    r"(?:transcript|meeting\s+notes?|notes?|summary|conversation|discussion|"
    r"thread|message|file|document)\b",
    flags=re.IGNORECASE,
)


def is_contextual_linear_reference(text: str) -> bool:
    """Return whether a Linear command refers to surrounding Slack context."""
    value = str(text or "")
    return bool(
        re.search(r"\blinear\b", value, flags=re.IGNORECASE)
        and CONTEXTUAL_LINEAR_REFERENCE_RE.search(value)
    )


def build_linear_slack_context(
    *,
    text: str,
    requester_user_id: str,
    channel_id: Optional[str],
    thread_ts: Optional[str],
    current_message_ts: Optional[str],
    thread_history: Optional[list[dict[str, Any]]] = None,
    workspace_id: Optional[str] = None,
    event_id: Optional[str] = None,
    timezone_name: str = "Australia/Sydney",
    max_messages: int = 50,
    lookback_hours: int = 24,
    max_chars: int = 16000,
) -> dict[str, Any]:
    """Build a normalized, bounded context packet for the Linear executor.

    Top-level contextual requests read recent messages before the command.
    Threaded requests retain the existing thread history. The current command
    is appended so downstream code can explicitly exclude it by timestamp.
    """
    contextual_reference = is_contextual_linear_reference(text)
    current_ts = str(current_message_ts or thread_ts or "").strip()
    resolved_thread_ts = str(thread_ts or "").strip()
    is_top_level = bool(current_ts and resolved_thread_ts == current_ts)
    mode = "thread"

    if channel_id and contextual_reference and is_top_level:
        messages = get_recent_channel_messages(
            channel=channel_id,
            before_ts=current_ts,
            limit=max_messages,
            lookback_hours=lookback_hours,
        )
        mode = "recent_channel"
    else:
        messages = [dict(message) for message in (thread_history or []) if isinstance(message, dict)]

    if current_ts and not any(str(message.get("ts") or "") == current_ts for message in messages):
        messages.append(
            {
                "user": requester_user_id,
                "text": text,
                "ts": current_ts,
                "thread_ts": None if is_top_level else resolved_thread_ts,
                "is_bot": False,
                "bot_id": None,
                "files": [],
            }
        )

    messages = _dedupe_and_sort_messages(messages)
    messages = _enrich_messages(messages, timezone_name=timezone_name)
    messages, truncated = _cap_context_messages(messages, max_messages=max_messages, max_chars=max_chars)

    requester = _safe_user_info(requester_user_id)
    requester_local_datetime = _local_datetime(current_ts, timezone_name)
    channel = get_channel_context(channel_id) if channel_id else {}

    return {
        "workspace_id": str(workspace_id or ""),
        "channel": {
            "id": str(channel_id or ""),
            "name": str(channel.get("name") or ""),
            "topic": str(channel.get("topic") or ""),
            "purpose": str(channel.get("purpose") or ""),
            "is_private": bool(channel.get("is_private")),
        },
        "request": {
            "user_id": requester_user_id,
            "display_name": _display_name(requester, requester_user_id),
            "email": str(requester.get("email") or ""),
            "message_ts": current_ts,
            "local_datetime": requester_local_datetime,
            "timezone": timezone_name,
            "event_id": str(event_id or ""),
        },
        "messages": messages,
        "selection": {
            "mode": mode,
            "anchor_ts": current_ts,
            "lookback_hours": lookback_hours if mode == "recent_channel" else None,
            "truncated": truncated,
            "contextual_reference": contextual_reference,
        },
    }


def _dedupe_and_sort_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    without_ts: list[dict[str, Any]] = []
    for message in messages:
        ts = str(message.get("ts") or "").strip()
        if ts:
            deduped[ts] = dict(message)
        else:
            without_ts.append(dict(message))

    def sort_key(message: dict[str, Any]) -> float:
        try:
            return float(message.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted([*deduped.values(), *without_ts], key=sort_key)


def _enrich_messages(
    messages: list[dict[str, Any]],
    *,
    timezone_name: str,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        user_id = str(message.get("user") or "").strip()
        info = _safe_user_info(user_id) if user_id and not message.get("is_bot") else {}
        message["display_name"] = _display_name(info, user_id or "Unknown")
        message["email"] = str(info.get("email") or "")
        message["local_datetime"] = _local_datetime(str(message.get("ts") or ""), timezone_name)
        enriched.append(message)
    return enriched


def _cap_context_messages(
    messages: list[dict[str, Any]],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = messages[-max(max_messages, 1):]
    selected_reversed: list[dict[str, Any]] = []
    char_count = 0
    for message in reversed(bounded):
        text = str(message.get("text") or "")
        if selected_reversed and char_count + len(text) > max(max_chars, 1000):
            break
        selected_reversed.append(message)
        char_count += len(text)
    selected = list(reversed(selected_reversed))
    return selected, len(selected) < len(messages)


def _safe_user_info(user_id: str) -> dict[str, Any]:
    if not user_id:
        return {}
    try:
        return get_user_info(user_id) or {}
    except Exception:
        return {"id": user_id}


def _display_name(info: dict[str, Any], fallback: str) -> str:
    return str(
        info.get("display_name")
        or info.get("real_name")
        or info.get("name")
        or fallback
    )


def _local_datetime(timestamp: str, timezone_name: str) -> str:
    try:
        instant = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        return instant.astimezone(ZoneInfo(timezone_name)).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""
    except Exception:
        return ""
