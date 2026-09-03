from __future__ import annotations

import re
from typing import Any


_URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_SLACK_MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>", re.IGNORECASE)
_SLACK_ID_PATTERN = re.compile(r"\b[ETUWCDG][A-Z0-9]{8,}\b")
_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_CONTROL_PATTERN = re.compile(r"[\r\n\t]+")


def redact_log_text(value: Any, *, max_length: int = 300) -> str:
    text = str(value or "")
    text = _URL_PATTERN.sub("[url]", text)
    text = _EMAIL_PATTERN.sub("[email]", text)
    text = _SLACK_MENTION_PATTERN.sub("[slack-user]", text)
    text = _SLACK_ID_PATTERN.sub("[slack-id]", text)
    text = _TOKEN_PATTERN.sub("[token]", text)
    text = _CONTROL_PATTERN.sub(" ", text)
    return text[:max_length]


def sanitize_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            redact_log_text(key): sanitize_log_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, str):
        return redact_log_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_text(value)


def slack_destination_type(channel_id: str | None) -> str:
    value = str(channel_id or "").strip().upper()
    if value.startswith("D"):
        return "dm"
    if value.startswith(("C", "G")):
        return "channel"
    return "unknown"
