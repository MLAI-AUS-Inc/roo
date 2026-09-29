"""Authenticate the human behind a Chat bridge bot's public Slack mention."""

import asyncio
import logging
import re
from typing import Any

from .clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError

logger = logging.getLogger(__name__)

_TERMINAL_ERRORS = {
    "invalid_bridge_context", "unknown_bridge_sender", "bridge_context_revoked",
    "bridge_actor_unverified", "bridge_actor_consent_required", "roo_not_explicitly_mentioned",
}


async def resolve_bridge_mention(
    event: dict[str, Any], *, slack_team_id: str, surface: str,
    backend: MLAIBackendClient,
) -> dict[str, Any] | None:
    """Replace bot identity only with a scope-matched backend message record.

    Resolution happens before agent execution. Transient failures propagate to
    the Slack delivery wrapper, which releases its receipt for a safe retry.
    """
    if not event.get("bot_id") and event.get("subtype") != "bot_message":
        return event
    if surface != "public":
        return None
    context = {
        "workspace_id": slack_team_id,
        "channel_id": str(event.get("channel") or ""),
        "message_id": str(event.get("ts") or ""),
        "thread_ts": str(event.get("thread_ts") or event.get("ts") or ""),
        "bridge_user_id": str(event.get("user") or ""),
    }
    # A Slack callback can beat the posting worker's link commit by milliseconds.
    # Bound local retries, then leave the durable Slack receipt retryable.
    for attempt in range(4):
        response = await backend.resolve_chat_bridge_actor(context)
        if response.status_code == 409 and attempt < 3:
            await asyncio.sleep(0.2 * (2 ** attempt))
            continue
        break
    try:
        actor = response.json()
    except ValueError as exc:
        raise MLAIBackendUnavailableError("Invalid bridge actor response") from exc
    if response.status_code != 200:
        if isinstance(actor, dict) and response.status_code in {400, 403, 404} and actor.get("error") in _TERMINAL_ERRORS:
            logger.info("Ignoring unauthorized bridge mention: %s", actor["error"])
            return None
        raise MLAIBackendUnavailableError("Bridge actor lookup is not ready")
    if (not isinstance(actor, dict)
            or any(actor.get(key) != value for key, value in context.items())
            or not isinstance(actor.get("user_id"), str)
            or not re.fullmatch(r"[UW][A-Z0-9]+", actor["user_id"])
            or actor["user_id"] == context["bridge_user_id"]
            or not isinstance(actor.get("source_event_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", actor["source_event_id"])
            or not isinstance(actor.get("text"), str) or not actor["text"].strip()):
        raise MLAIBackendUnavailableError("Bridge actor response has mismatched scope")
    return {
        **event,
        "user": actor["user_id"],
        "text": actor["text"],
        # Keep mutations stable if Slack redelivers the same bridged source.
        "_slack_event_id": f"mlai-chat:{actor['source_event_id']}",
    }
