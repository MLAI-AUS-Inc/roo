"""Deliver Slack confirmations from the backend's durable Roo points queue."""

import asyncio
import logging
from uuid import NAMESPACE_URL, uuid5

from .slack_client import get_thread_messages, post_message

logger = logging.getLogger(__name__)
EVENT_TYPE = "roo_points_approval"


def _post_confirmation(job):
    request_id = job["request_id"]
    channel = job["channel_id"]
    root = job["thread_ts"]
    nonce = str(uuid5(NAMESPACE_URL, f"https://api.mlai.au/roo/points-approval/{request_id}"))
    if job["attempt"] > 1:
        history = get_thread_messages(channel, root, include_metadata=True)
        for message in history:
            metadata = message.get("metadata") or {}
            if (
                message.get("user") == job["author_id"]
                and message.get("thread_ts") == root
                and (
                    message.get("client_msg_id") == nonce
                    or (
                        metadata.get("event_type") == EVENT_TYPE
                        and metadata.get("event_payload", {}).get("request_id") == str(request_id)
                    )
                )
            ):
                return message["ts"]
        if not getattr(history, "complete", False):
            raise RuntimeError("Slack thread history is incomplete; approval receipt remains queued")

    result = post_message(
        channel=channel, thread_ts=root, text=job["text"], client_msg_id=nonce,
        metadata={"event_type": EVENT_TYPE, "event_payload": {"request_id": str(request_id)}},
    )
    if not result or not result.get("ok") or not result.get("ts"):
        raise RuntimeError("Slack approval confirmation did not return a receipt")
    return result["ts"]


async def deliver_next_confirmation(client):
    job = await client.claim_points_confirmation()
    if job is None:
        return False
    message_id = job["existing_message_id"] or await asyncio.to_thread(_post_confirmation, job)
    await client.record_points_confirmation(job["request_id"], job["channel_id"], message_id)
    return True


async def points_confirmation_retry_loop(client):
    while True:
        delay = 10
        try:
            if await deliver_next_confirmation(client):
                delay = 2
        except Exception as exc:
            logger.warning("Roo points confirmation remains queued: %s", type(exc).__name__)
        await asyncio.sleep(delay)
