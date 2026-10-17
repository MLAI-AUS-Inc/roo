"""Minimal event admission after Slack signature verification, before ACK."""

from datetime import datetime, timezone
import re

import httpx

from .config import root_url


class EventAdmissionError(RuntimeError):
    pass


def normalize_slack_event(settings, payload):
    if not getattr(settings, "PROACTIVE_EVENTS_ENABLED", False) or settings.ROO_SURFACE != "public":
        return None
    event = payload.get("event")
    if payload.get("type") != "event_callback" or not isinstance(event, dict):
        return None
    team = str(payload.get("team_id") or "")
    channel = str(event.get("channel") or "")
    allowed = set(str(settings.PROACTIVE_EVENT_CHANNEL_IDS).split(","))
    allowed = {value.strip() for value in allowed if value.strip()}
    if (team != settings.PROACTIVE_EVENT_TEAM_ID or channel not in allowed
            or not re.fullmatch(r"T[A-Z0-9]+", team) or not re.fullmatch(r"C[A-Z0-9]+", channel)):
        return None
    source_id = str(payload.get("event_id") or "")
    if not re.fullmatch(r"Ev[A-Za-z0-9]+", source_id):
        return None
    kind, subject = event.get("type"), event
    if kind == "message":
        subtype = event.get("subtype")
        if subtype == "message_changed":
            subject = event.get("message") or {}
            event_type = "message.updated"
        elif subtype == "message_deleted":
            subject = event.get("previous_message") or {}
            event_type = "message.deleted"
        elif subtype in {None, "file_share"}:
            event_type = "message.created"
        else:
            return None
    elif kind in {"member_joined_channel", "member_left_channel"}:
        event_type = "membership.changed"
    else:
        return None
    if not isinstance(subject, dict) or subject.get("bot_id") or subject.get("subtype") == "bot_message":
        return None
    metadata = subject.get("metadata") or event.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("event_type") == "mlai_agent_action":
        return None
    if event.get("bot_id") or event.get("channel_type") in {"im", "mpim", "group"}:
        return None
    metadata = {"channel_id": channel, "team_id": team}
    message_ts = str(event.get("deleted_ts") or subject.get("ts") or "")
    if event_type.startswith("message."):
        if not re.fullmatch(r"[0-9]{1,16}\.[0-9]{1,10}", message_ts):
            return None
        metadata["message_ts"] = message_ts
    thread_ts = str(subject.get("thread_ts") or "")
    if re.fullmatch(r"[0-9]{1,16}\.[0-9]{1,10}", thread_ts):
        metadata["thread_ts"] = thread_ts
    client_message_id = subject.get("client_msg_id")
    if isinstance(client_message_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", client_message_id):
        metadata["source_client_msg_id"] = client_message_id
    try:
        timestamp = float(event.get("event_ts") or payload.get("event_time"))
        occurred_at = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    # No message text, attachment URLs, identities, or caller-selected grants.
    # Receipt is a wake-up hint; the backend separately authorizes every fetch.
    return {"source": "slack", "source_event_id": f"slack:{team}:{source_id}",
            "resource": f"slack:{team}:{channel}", "event_type": event_type,
            "occurred_at": occurred_at, "payload": metadata, "actor_kind": "human"}


async def admit_slack_event(settings, payload, *, client=None):
    normalized = normalize_slack_event(settings, payload)
    if normalized is None:
        return False
    key = str(settings.PROACTIVE_EVENT_API_KEY or "")
    if len(key) < 32:
        raise EventAdmissionError("event_ingress_not_configured")
    try:
        origin = root_url(settings.PROACTIVE_EVENT_BACKEND_ORIGIN,
                          allow_http=getattr(settings, "PROACTIVE_EVENT_ALLOW_PRIVATE_HTTP", False))
        if client is None:
            async with httpx.AsyncClient(trust_env=False) as transport:
                return await _send(transport, origin, key, normalized)
        return await _send(client, origin, key, normalized)
    except (httpx.HTTPError, ValueError):
        raise EventAdmissionError("event_ingress_unavailable") from None


async def _send(client, origin, key, normalized):
    response = await client.post(origin + "/api/v1/agent-runtime/internal/events/", json=normalized,
                                 headers={"Authorization": "Bearer " + key}, timeout=1.5, follow_redirects=False)
    if response.status_code not in {200, 201, 202}:
        raise EventAdmissionError("event_ingress_rejected")
    return True
