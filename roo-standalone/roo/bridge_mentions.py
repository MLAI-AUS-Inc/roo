"""Authenticate a Chat bridge mention and its explicitly selected recipients."""

import asyncio
import logging
import re
from typing import Any
from dataclasses import dataclass
from uuid import UUID

from .clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError

logger = logging.getLogger(__name__)

_TERMINAL_ERRORS = {
    "invalid_bridge_context", "unknown_bridge_sender", "bridge_context_revoked",
    "bridge_actor_unverified", "bridge_actor_consent_required", "roo_not_explicitly_mentioned",
    "invalid_native_mentions",
}


@dataclass(frozen=True)
class BridgeRecipient:
    """A canonical recipient supplied only by the verified backend resolver."""

    profile_id: str
    display_name: str
    source_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifiedBridgeContext:
    """Immutable authority kept outside model-extracted skill parameters."""

    source_event_id: str
    actor_profile_id: str
    posting_mode: str
    recipient_targets: tuple[BridgeRecipient, ...]


def _profile_id(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid canonical profile ID")
    return value


def _verified_context(actor: dict[str, Any]) -> VerifiedBridgeContext | None:
    if "version" not in actor:
        return None  # Rolling deployment: legacy bot actor responses still work.
    if actor.get("version") != 2 or actor.get("posting_mode") not in {"member", "bot"}:
        raise ValueError("Unsupported bridge context")
    targets = actor.get("recipient_targets")
    if not isinstance(targets, list) or len(targets) > 20:
        raise ValueError("Invalid bridge recipients")
    recipients = {}
    for target in targets:
        if (not isinstance(target, dict) or target.get("kind") != "mlai_profile"
                or not isinstance(target.get("display_name"), str)
                or len(target["display_name"]) > 200):
            raise ValueError("Invalid bridge recipient")
        profile_id = _profile_id(target.get("profile_id"))
        labels = target.get("source_labels", [])
        if (not isinstance(labels, list) or len(labels) > 20
                or any(not isinstance(label, str) or not label or len(label) > 80
                       or re.search(r"[\x00-\x1f\x7f-\x9f`<>@]", label) for label in labels)):
            raise ValueError("Invalid bridge source labels")
        previous = recipients.get(profile_id)
        combined_labels = tuple(dict.fromkeys((previous.source_labels if previous else ()) + tuple(labels)))
        if len(combined_labels) > 20:
            raise ValueError("Too many bridge source labels")
        recipients[profile_id] = BridgeRecipient(profile_id, target["display_name"], combined_labels)
    return VerifiedBridgeContext(
        source_event_id=actor["source_event_id"],
        actor_profile_id=_profile_id(actor.get("actor_profile_id")),
        posting_mode=actor["posting_mode"],
        recipient_targets=tuple(recipients.values()),
    )


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
            or (actor["user_id"] == context["bridge_user_id"]
                and not (actor.get("version") == 2 and actor.get("posting_mode") == "member"))
            or not isinstance(actor.get("source_event_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", actor["source_event_id"])
            or not isinstance(actor.get("text"), str) or not actor["text"].strip()):
        raise MLAIBackendUnavailableError("Bridge actor response has mismatched scope")
    try:
        verified_context = _verified_context(actor)
    except (TypeError, ValueError, AttributeError) as exc:
        raise MLAIBackendUnavailableError("Invalid bridge recipient context") from exc
    return {
        **event,
        "user": actor["user_id"],
        "text": actor["text"],
        # Keep mutations stable if Slack redelivers the same bridged source.
        "_slack_event_id": f"mlai-chat:{actor['source_event_id']}",
        "_verified_bridge_context": verified_context,
    }
