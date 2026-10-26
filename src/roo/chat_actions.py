"""First-party Chat actions reuse Slack's handler and requester identity checks."""

import asyncio
import hashlib
import json
import re

from fastapi import HTTPException
from slack_sdk.errors import SlackApiError


def action_digest(message, action):
    material = {
        "blocks": message.get("blocks") or [],
        "action_id": action.get("action_id"),
        "value": action.get("value"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fetch_message(client, channel, message_ts, thread_ts):
    if thread_ts != message_ts:
        response = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            oldest=message_ts,
            latest=message_ts,
            inclusive=True,
            limit=2,
        )
    else:
        response = client.conversations_history(
            channel=channel,
            oldest=message_ts,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
    return next(
        (item for item in response.get("messages", []) if item.get("ts") == message_ts),
        None,
    )


async def handle_chat_action(payload, *, client, dispatch):
    """Fetch the current bot-owned card; never accept action values from Chat."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid action request")
    values = {
        key: str(payload.get(key) or "")
        for key in (
            "workspace_id",
            "channel_id",
            "message_ts",
            "thread_ts",
            "user_id",
            "action_id",
            "action_hash",
        )
    }
    for key, pattern in (
        ("workspace_id", r"T[A-Z0-9]+"),
        ("channel_id", r"[CDG][A-Z0-9]+"),
        ("message_ts", r"\d+\.\d+"),
        ("thread_ts", r"\d+\.\d+"),
        ("user_id", r"[UW][A-Z0-9]+"),
    ):
        if not re.fullmatch(pattern, values[key]):
            raise HTTPException(400, "Invalid action reference")
    perform = payload.get("perform") is True
    if perform and (
        not re.fullmatch(
            r"confirm_topic_btn_\d+|cancel_topic_btn|select_article_delivery_mode",
            values["action_id"],
        )
        or not re.fullmatch(r"[a-f0-9]{64}", values["action_hash"])
    ):
        raise HTTPException(400, "Unsupported action")
    try:
        identity = await asyncio.to_thread(client.auth_test)
        if identity.get("team_id") != values["workspace_id"]:
            raise HTTPException(403, "Workspace mismatch")
        message = await asyncio.to_thread(
            _fetch_message,
            client,
            values["channel_id"],
            values["message_ts"],
            values["thread_ts"],
        )
        if not message or message.get("user") != identity.get("user_id"):
            raise HTTPException(404, "Roo message is unavailable")
        if values["channel_id"].startswith("D"):
            conversation = await asyncio.to_thread(
                client.conversations_info, channel=values["channel_id"]
            )
            if (conversation.get("channel") or {}).get("user") != values["user_id"]:
                raise HTTPException(403, "This is another member's conversation")
        if perform:
            candidates = [
                element
                for block in message.get("blocks", [])
                if isinstance(block, dict)
                for element in [*(block.get("elements") or []), block.get("accessory")]
                if isinstance(element, dict)
                and element.get("type") == "button"
                and element.get("action_id") == values["action_id"]
                and not element.get("url")
                and not element.get("confirm")
                and action_digest(message, element) == values["action_hash"]
            ]
            if len(candidates) != 1:
                raise HTTPException(
                    409, "This action has changed or is no longer available"
                )
            action_payload = {
                "type": "block_actions",
                "team": {"id": values["workspace_id"]},
                "user": {"id": values["user_id"]},
                "channel": {"id": values["channel_id"]},
                "message": message,
                "actions": [candidates[0]],
                "container": {
                    "type": "message",
                    "channel_id": values["channel_id"],
                    "message_ts": values["message_ts"],
                },
            }
            response = await dispatch(action_payload)
            if response.status_code != 200:
                raise HTTPException(
                    response.status_code, "Roo could not accept this action"
                )
            # Existing handlers can return a private denial with HTTP 200.
            result = json.loads(response.body or b"{}")
            if result.get("response_type") == "ephemeral" or result.get("error"):
                raise HTTPException(403, "Roo did not accept this action")
            message = await asyncio.to_thread(
                _fetch_message,
                client,
                values["channel_id"],
                values["message_ts"],
                values["thread_ts"],
            )
            if not message:
                raise HTTPException(409, "The message changed; refresh it")
            remaining = [
                element
                for block in message.get("blocks", [])
                if isinstance(block, dict)
                for element in [*(block.get("elements") or []), block.get("accessory")]
                if isinstance(element, dict) and element.get("type") == "button"
            ]
            if any(
                action_digest(message, item) == values["action_hash"]
                for item in remaining
            ):
                raise HTTPException(
                    409, "Roo has not confirmed this action; refresh the message"
                )
        return {
            "message": {
                key: message[key]
                for key in ("text", "blocks", "attachments", "user", "ts", "thread_ts")
                if key in message
            }
        }
    except SlackApiError as error:
        raise HTTPException(
            503, "Slack could not verify the current message"
        ) from error
