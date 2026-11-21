"""Roo's signed Slack entry points and retryable delivery for volunteer time."""

import asyncio
import hashlib
import hmac
import logging
from time import time
from uuid import uuid4

import httpx
from slack_sdk.errors import SlackApiError

from .manual_time_modal import build_view, read_state, review_submission
from .slack_client import get_slack_client

logger = logging.getLogger(__name__)
CALLBACK_PREFIX = "roo_volunteer_time_"
REVIEW_STAGE = "time_slack_review"
METADATA_EVENT = "roo_manual_time_delivery"


def enabled(settings):
    return bool(
        getattr(settings, "ROO_MANUAL_TIME_SLACK_WORKSPACE_ID", "") and settings.ROO_SURFACE == "public"
        and settings.ROO_MANUAL_TIME_SLACK_REVIEW_CHANNEL_ID and settings.ROO_API_KEY
        and settings.SLACK_SIGNING_SECRET
    )


async def backend(settings, operation="", **data):
    url = f"{settings.MLAI_BACKEND_URL.rstrip('/')}/api/v1/points/manual-time/"
    headers = {"X-API-Key": settings.ROO_API_KEY}
    async with httpx.AsyncClient(timeout=2.2) as client:
        if operation:
            response = await client.post(
                url + operation + "/", headers=headers,
                json={"workspace": settings.ROO_MANUAL_TIME_SLACK_WORKSPACE_ID, **data},
            )
        else:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def signature(settings, draft, actor, workspace, mode):
    return hmac.new(
        settings.SLACK_SIGNING_SECRET.encode(),
        f"manual-time:{draft}:{actor}:{workspace}:{mode}".encode(), hashlib.sha256,
    ).hexdigest()


def validate_draft(settings, metadata, actor, workspace, mode):
    parts = metadata.split(".")
    if len(parts) != 3 or len(parts[1]) > 12 or not parts[1].isdigit():
        return None
    key, expires, mac = parts
    draft = f"{key}.{expires}"
    if int(expires) < time() or not hmac.compare_digest(mac, signature(settings, draft, actor, workspace, mode)):
        return None
    return key


def ephemeral(text):
    return {"response_type": "ephemeral", "text": text}


async def open_modal(settings, form, mode):
    actor, workspace = form.get("user_id"), form.get("team_id")
    if workspace != settings.ROO_MANUAL_TIME_SLACK_WORKSPACE_ID or not actor or not form.get("trigger_id"):
        return ephemeral("Volunteer time is unavailable in this workspace.")
    try:
        if mode == "admin":
            catalogue, _ = await asyncio.gather(backend(settings), backend(settings, "check-admin", actor=actor))
        else:
            catalogue = await backend(settings)
        draft = f"{uuid4()}.{int(time()) + 3600}"
        metadata = f"{draft}.{signature(settings, draft, actor, workspace, mode)}"
        await asyncio.to_thread(
            get_slack_client().views_open, trigger_id=form["trigger_id"],
            view=build_view(mode, catalogue, draft_id=metadata),
        )
    except (httpx.HTTPError, SlackApiError):
        return ephemeral("Roo couldn't open that form. Check your access and try again.")
    return {}


async def modal_action(settings, payload):
    try:
        async with asyncio.timeout(2.7):
            return await _modal_action(settings, payload)
    except TimeoutError:
        if payload.get("type") == "view_submission":
            return {"response_action": "errors", "errors": {"duration": "Roo ran out of time. Try again."}}
        return {}


async def _modal_action(settings, payload):
    view = payload.get("view") or {}
    mode = view.get("callback_id", "").removeprefix(CALLBACK_PREFIX)
    metadata = view.get("private_metadata", "")
    actor = (payload.get("user") or {}).get("id")
    workspace = (payload.get("team") or {}).get("id")
    if mode not in {"member", "admin"} or not actor or workspace != settings.ROO_MANUAL_TIME_SLACK_WORKSPACE_ID:
        return {"response_action": "errors", "errors": {"duration": "This form is unavailable."}}
    draft = validate_draft(settings, metadata, actor, workspace, mode)
    if not draft:
        return {"response_action": "errors", "errors": {"duration": "Open a new Roo form and try again."}}
    kind = payload.get("type")
    state = read_state(((payload.get("state") or view.get("state") or {}).get("values")) or {})
    try:
        catalogue = await backend(settings)
        if kind == "block_actions":
            await asyncio.to_thread(
                get_slack_client().views_update, view_id=view["id"], hash=view["hash"],
                view=build_view(mode, catalogue, state, draft_id=metadata),
            )
            return {}
        if kind != "view_submission":
            return {}
        values = review_submission(mode, state, catalogue)
        if values.get("errors"):
            return values
        row = await backend(
            settings, "award" if mode == "admin" else "request",
            actor=actor, target=state.get("recipient"), submission_key=draft,
            catalog_version=catalogue["version"], **values,
        )
    except SlackApiError as exc:
        if exc.response.get("error") != "hash_conflict":
            logger.warning("Manual time modal update failed")
        return {}
    except httpx.HTTPError as exc:
        message = "Roo couldn't save this yet. Try again."
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            try:
                message = exc.response.json().get("error") or message
            except ValueError:
                pass
        return {"response_action": "errors", "errors": {"duration": message}}
    status = f"Recorded {row['roo']} Roo." if row["status"] == "approved" else f"Requested {row['roo']} Roo. A points admin will review it."
    return {"response_action": "update", "view": {
        "type": "modal", "title": {"type": "plain_text", "text": "Volunteer time"},
        "close": {"type": "plain_text", "text": "Done"},
        "blocks": [{"type": "section", "text": {"type": "plain_text", "text": status}}],
    }}


async def approve_reaction(settings, event):
    item = event["item"]
    channel, ts = item["channel"], item["ts"]
    slack = get_slack_client()
    history = await asyncio.to_thread(
        slack.conversations_history, channel=channel, oldest=ts, latest=ts,
        inclusive=True, limit=1, include_all_metadata=True,
    )
    for message in history.get("messages", []):
        metadata = message.get("metadata") or {}
        payload = metadata.get("event_payload") or {}
        if message.get("ts") == ts and metadata.get("event_type") == METADATA_EVENT and payload.get("stage") == REVIEW_STAGE:
            await backend(settings, "approve", actor=event["user"], request_id=payload.get("request_id"), channel=channel, ts=ts)
            return True
    return False


def deliver(settings, delivery):
    slack = get_slack_client()
    row = delivery["request"]
    if delivery["stage"] == REVIEW_STAGE:
        channel = settings.ROO_MANUAL_TIME_SLACK_REVIEW_CHANNEL_ID
        info = slack.conversations_info(channel=channel)["channel"]
        if not info.get("is_private") or info.get("is_shared") or info.get("is_ext_shared"):
            raise ValueError("Review requires a private workspace channel")
        text = f"{row['display_name']} requested {row['roo']} Roo for {row['volunteer_minutes']} minutes.\n{row['activity']}\nPoints Admins can approve with ✅."
        link = f"https://chat.mlai.au/volunteer?rooRequest={row['id']}"
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": text},
                   "accessory": {"type": "button", "text": {"type": "plain_text", "text": "Review in Chat"},
                                 "action_id": "roo_manual_time_review_link", "url": link}}]
    else:
        blocks = []
        channel = slack.conversations_open(users=delivery["target"])["channel"]["id"]
        text = f"You earned {row['roo']} Roo for {row['volunteer_minutes']} minutes.\n{row['activity']}"
    metadata = {"event_type": METADATA_EVENT, "event_payload": {
        "delivery_id": delivery["id"], "stage": delivery["stage"], "request_id": row["id"],
    }}
    if delivery["attempts"] > 1:
        cursor = ""
        while True:
            history = slack.conversations_history(
                channel=channel, oldest=str(delivery["created"]),
                include_all_metadata=True, cursor=cursor, limit=100,
            )
            for message in history.get("messages", []):
                if message.get("metadata") == metadata:
                    return channel, message["ts"]
            cursor = (history.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    response = slack.chat_postMessage(channel=channel, text=text, metadata=metadata, mrkdwn=False, blocks=blocks or None)
    return channel, response["ts"]


async def delivery_loop(settings):
    while True:
        try:
            delivery = await backend(settings, "claim")
            if delivery:
                channel, ts = await asyncio.to_thread(deliver, settings, delivery)
                await backend(settings, "finish", id=delivery["id"], lease=delivery["lease"], channel=channel, ts=ts)
        except (httpx.HTTPError, SlackApiError, ValueError):
            logger.warning("Volunteer time Slack delivery will retry")
        await asyncio.sleep(5)
