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
