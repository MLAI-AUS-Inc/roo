"""Optional threaded replies for authenticated requests from MLAI Chat."""

import asyncio
import re
from uuid import UUID

from fastapi import HTTPException

from .slack_client import post_message


async def handle_internal_mention(payload, agent):
    """Run the normal Public Roo workflow and reuse a stable Slack reply ID."""
    post_reply = payload.get("post_reply") is True
    request_id = None
    if post_reply:
        try:
            request_id = str(UUID(str(payload.get("request_id", ""))))
        except ValueError:
            raise HTTPException(status_code=400, detail="A valid request_id is required")
        if (not re.fullmatch(r"C[A-Z0-9]+", str(payload.get("channel_id", "")))
                or not re.fullmatch(r"[UW][A-Z0-9]+", str(payload.get("user_id", "")))
                or not re.fullmatch(r"[0-9]+\.[0-9]+", str(payload.get("thread_ts", "")))):
            raise HTTPException(status_code=400, detail="A Slack member, channel and thread are required")
    result = await agent.handle_mention(
        text=payload.get("text", ""), user_id=payload.get("user_id", ""),
        channel_id=payload.get("channel_id"), thread_ts=payload.get("thread_ts"),
    )
    if not post_reply:
        return result
    if not result.get("message") and not result.get("suppress_post"):
        raise HTTPException(status_code=503, detail="Roo did not return a reply")
    if result.get("message") and not result.get("suppress_post"):
        response = await asyncio.to_thread(
            post_message, channel=payload["channel_id"], thread_ts=payload["thread_ts"],
            text=result["message"], client_msg_id=request_id,
            **({"blocks": result["blocks"]} if result.get("blocks") else {}),
        )
        if not response.get("ok"):
            raise HTTPException(status_code=503, detail="Roo could not post its reply")
    return {**result, "reply_delivered": True}


async def handle_private_command(payload, agent, receipts):
    """Dispatch once, with no room, thread, files or transcript available to the agent."""
    from .backend_identity import BackendActorContext, use_backend_actor_context
    from .bridge_mentions import _verified_context

    fields = {
        "text",
        "user_id",
        "workspace_id",
        "source_event_id",
        "actor_profile_id",
        "version",
        "posting_mode",
        "recipient_targets",
        "source_labels",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise HTTPException(status_code=400, detail="Invalid private command")
    actor, workspace, source, text = (
        payload[k] for k in ("user_id", "workspace_id", "source_event_id", "text")
    )
    if (
        not isinstance(actor, str)
        or not re.fullmatch(r"[UW][A-Z0-9]+", actor)
        or not isinstance(workspace, str)
        or not re.fullmatch(r"T[A-Z0-9]+", workspace)
        or not isinstance(source, str)
        or not re.fullmatch(r"[a-f0-9]{64}", source)
        or not isinstance(text, str)
        or not 0 < len(text.strip()) <= 10000
        or payload["posting_mode"] != "member"
    ):
        raise HTTPException(status_code=400, detail="Invalid private command")
    try:
        verified = _verified_context(payload)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid selected member") from exc
    # Admission on the backend expires after 15 minutes. Hold this receipt
    # beyond that window even if a skill succeeds and its reply is lost.
    if not await asyncio.to_thread(receipts.claim, source, ttl_seconds=3600):
        raise HTTPException(status_code=409, detail="Command already received")
    context = BackendActorContext(workspace, actor, "", "", f"mlai-chat:{source}")
    with use_backend_actor_context(context):
        result = await agent.handle_mention(
            text=text,
            user_id=actor,
            slack_team_id=workspace,
            event_id=context.event_id,
            verified_bridge_context=verified,
        )
    message = result.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=502, detail="Roo could not confirm the result")
    return {"message": message}
