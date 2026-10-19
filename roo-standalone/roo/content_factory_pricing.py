"""Short-lived article confirmations. A service key is never price consent."""
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
import secrets
import time


PRICE_CONFIRMATION_STATUS = "awaiting_price_confirmation"
PRICE_CONFIRMATION_ACTION = "confirm_article_price"
_confirmations: OrderedDict[str, dict] = OrderedDict()
_confirmation_lock = RLock()
_TTL_SECONDS = 1800
_MAX_CONFIRMATIONS = 1000


class ArticleQuoteUnavailableError(RuntimeError):
    """No article action may proceed without a usable authoritative quote."""


def valid_cost(value) -> bool:
    return type(value) is int and value >= 0


def create_confirmation(endpoint: str, payload: dict, cost_points: int, *, changed=False) -> dict:
    if not valid_cost(cost_points):
        raise ValueError("The article quote is unavailable. Please try again.")
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    confirmed_payload = deepcopy(payload)
    confirmed_payload["expected_cost_points"] = cost_points
    with _confirmation_lock:
        for key, record in list(_confirmations.items()):
            if record["expires_at"] <= now:
                del _confirmations[key]
        while len(_confirmations) >= _MAX_CONFIRMATIONS:
            _confirmations.popitem(last=False)
        _confirmations[token] = {
            "endpoint": endpoint, "payload": confirmed_payload, "expires_at": now + _TTL_SECONDS,
        }
    domain = str(payload.get("domain") or "your website")
    topic = str(payload.get("topic") or payload.get("keyword") or "the selected topic")
    prefix = "The article price changed. " if changed else ""
    message = f"{prefix}Generate an article for {domain} about {topic} for {cost_points} Roo points?"
    return {
        "status": PRICE_CONFIRMATION_STATUS,
        "message": message,
        "blocks": [
            {"type": "section", "text": {"type": "plain_text", "text": message[:2900]}},
            {"type": "actions", "elements": [{
                "type": "button", "action_id": PRICE_CONFIRMATION_ACTION,
                "text": {"type": "plain_text", "text": f"Confirm {cost_points} Roo points"[:75]},
                "value": token, "style": "primary",
            }]},
        ],
    }


def _confirmation_record(token: str) -> dict:
    record = _confirmations.get(token)
    if not record or record["expires_at"] <= time.monotonic():
        raise ValueError("This price confirmation expired. Request the article again to review a fresh quote.")
    return record


def _card_origin(*, workspace_id: str, channel_id: str, thread_ts: str, message_ts: str) -> dict:
    origin = {
        "workspace_id": workspace_id, "channel_id": channel_id,
        "thread_ts": thread_ts, "message_ts": message_ts,
    }
    if any(not isinstance(value, str) or not value for value in origin.values()):
        raise ValueError("Reopen the article price confirmation in its original conversation.")
    return origin


def confirmation_tokens(blocks: list) -> list[str]:
    return [
        str(element.get("value") or "")
        for block in blocks if isinstance(block, dict)
        for element in (block.get("elements") or []) if isinstance(element, dict)
        if element.get("action_id") == PRICE_CONFIRMATION_ACTION
    ]


def bind_confirmation_card(blocks: list, **origin_fields: str) -> None:
    """Bind once, after Slack confirms delivery, to the actual displayed card."""
    tokens = confirmation_tokens(blocks)
    if len(tokens) != 1:
        raise ValueError("The article price confirmation could not be verified.")
    origin = _card_origin(**origin_fields)
    # Chat posts run in worker threads. Check and bind atomically so two
    # concurrent deliveries cannot replace each other's established origin.
    with _confirmation_lock:
        record = _confirmation_record(tokens[0])
        if record.get("origin"):
            if record["origin"] != origin:
                raise ValueError("The article price confirmation is already bound to another message.")
            return
        saved = record["payload"]
        for field, origin_field in (("slack_channel_id", "channel_id"), ("slack_thread_ts", "thread_ts")):
            if saved.get(field) and saved[field] != origin[origin_field]:
                raise ValueError("The article price confirmation has a different destination.")
        record["origin"] = origin
        # Only the successful delivery boundary can establish the destination.
        # Clicks may verify it, but cannot fill in or replace any part of it.
        saved.update(
            slack_channel_id=origin["channel_id"], slack_thread_ts=origin["thread_ts"],
            progress_message_ts=origin["message_ts"],
        )


def get_confirmation(token: str, actor: str, **origin_fields: str) -> dict:
    with _confirmation_lock:
        record = _confirmation_record(token)
        payload = record["payload"]
        owner = payload.get("requested_by_slack_user_id") or payload.get("slack_user_id")
        if not actor or actor != owner:
            raise ValueError("Only the requesting user can confirm this article price.")
        if not record.get("origin") or record["origin"] != _card_origin(**origin_fields):
            raise ValueError("Confirm this price on its original message in the original conversation.")
        return deepcopy(record)


def invalidate_confirmation(token: str) -> None:
    with _confirmation_lock:
        _confirmations.pop(token, None)
