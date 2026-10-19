"""Short-lived article confirmations. A service key is never price consent."""
from collections import OrderedDict
from copy import deepcopy
import secrets
import time


PRICE_CONFIRMATION_STATUS = "awaiting_price_confirmation"
PRICE_CONFIRMATION_ACTION = "confirm_article_price"
_confirmations: OrderedDict[str, dict] = OrderedDict()
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
    for key, record in list(_confirmations.items()):
        if record["expires_at"] <= now:
            del _confirmations[key]
    while len(_confirmations) >= _MAX_CONFIRMATIONS:
        _confirmations.popitem(last=False)
    token = secrets.token_urlsafe(24)
    confirmed_payload = deepcopy(payload)
    confirmed_payload["expected_cost_points"] = cost_points
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


def get_confirmation(token: str, actor: str) -> dict:
    record = _confirmations.get(token)
    if not record or record["expires_at"] <= time.monotonic():
        raise ValueError("This price confirmation expired. Request the article again to review a fresh quote.")
    payload = record["payload"]
    owner = payload.get("requested_by_slack_user_id") or payload.get("slack_user_id")
    if not actor or actor != owner:
        raise ValueError("Only the requesting user can confirm this article price.")
    return deepcopy(record)


def invalidate_confirmation(token: str) -> None:
    _confirmations.pop(token, None)
