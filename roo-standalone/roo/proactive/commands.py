"""Explicit Slack commands; only a verified Events envelope may call this module."""

import asyncio
import json
import re
from uuid import UUID

import httpx

from .config import root_url
from .ingress import EventAdmissionError

REQUEST_TIMEOUT = 1.5


def normalize_task_command(settings, payload):
    if not getattr(settings, "PROACTIVE_TASKS_ENABLED", False) or settings.ROO_SURFACE != "public":
        return None
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if payload.get("type") != "event_callback" or not isinstance(event, dict):
        return None
    if (event.get("type") not in {"app_mention", "message"} or event.get("subtype")
            or event.get("bot_id") or event.get("channel_type") in {"im", "mpim", "group"}):
        return None
    team, channel, user = (str(payload.get("team_id") or ""), str(event.get("channel") or ""), str(event.get("user") or ""))
    bot = str(getattr(settings, "PROACTIVE_TASK_BOT_USER_ID", ""))
    allowed = {item.strip() for item in str(settings.PROACTIVE_EVENT_CHANNEL_IDS).split(",")}
    if (team != settings.PROACTIVE_EVENT_TEAM_ID or channel not in allowed
            or not re.fullmatch(r"T[A-Z0-9]{1,29}", team) or not re.fullmatch(r"C[A-Z0-9]{1,29}", channel)
            or not re.fullmatch(r"[UW][A-Z0-9]{1,29}", user) or not re.fullmatch(r"[UW][A-Z0-9]{1,29}", bot)):
        return None
    text, source_id = event.get("text"), str(payload.get("event_id") or "")
    if not isinstance(text, str) or not text.startswith(f"<@{bot}> run: ") or not re.fullmatch(r"Ev[A-Za-z0-9]{1,98}", source_id):
        return None
    instruction = text[len(f"<@{bot}> run: "):].strip()
    try:
        instruction_bytes = len(instruction.encode())
    except UnicodeEncodeError:
        return None
    if not instruction or "\x00" in instruction or instruction_bytes > 16_000:
        return None
    return {"team_id": team, "user_id": user, "channel_id": channel,
            "source_event_id": f"slack:{team}:{source_id}", "instruction": instruction}


async def admit_task_command(settings, payload, *, client=None):
    normalized = normalize_task_command(settings, payload)
    if normalized is None:
        return None
    # Slack also emits a message copy; app_mention is the sole admission owner.
    if payload["event"]["type"] == "message":
        return {"handled": True, "new": False}
    key = str(getattr(settings, "PROACTIVE_TASK_API_KEY", "") or "")
    if not 32 <= len(key) <= 8192 or any(c.isspace() or ord(c) < 32 for c in key):
        raise EventAdmissionError("task_admission_not_configured")
    try:
        origin = root_url(settings.PROACTIVE_EVENT_BACKEND_ORIGIN,
                          allow_http=getattr(settings, "PROACTIVE_EVENT_ALLOW_PRIVATE_HTTP", False))
        if client is None:
            async with httpx.AsyncClient(trust_env=False) as transport:
                return await _send(transport, origin, key, normalized)
        return await _send(client, origin, key, normalized)
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise EventAdmissionError("task_admission_unavailable") from None


async def _send(client, origin, key, payload):
    # Slack admission must finish within its ACK window even if the backend
    # trickles response bytes faster than an individual read timeout.
    async with asyncio.timeout(REQUEST_TIMEOUT):
        return await _send_once(client, origin, key, payload)


async def _send_once(client, origin, key, payload):
    async with client.stream("POST", origin + "/api/v1/agent-runtime/internal/tasks/admit-slack/",
                             json=payload, headers={"Authorization": "Bearer " + key},
                             timeout=REQUEST_TIMEOUT, follow_redirects=False) as response:
        if response.status_code in {400, 403, 409}:
            return {"handled": True, "new": True, "text": "Roo could not start this task. Check your connected Roo agent and its access to this channel in your MLAI account."}
        if response.status_code != 202:
            raise EventAdmissionError("task_admission_rejected")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > 16_384:
                raise EventAdmissionError("task_admission_invalid_response")
        try:
            result = json.loads(body)
            if not isinstance(result, dict) or type(result.get("new")) is not bool:
                raise ValueError()
            task_id = str(UUID(result["task_id"]))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise EventAdmissionError("task_admission_invalid_response") from None
        return {"handled": True, "new": result.get("new") is True,
                "text": f"Roo queued task {task_id}. Follow its progress and review drafts in Connected agents in your MLAI account."}
