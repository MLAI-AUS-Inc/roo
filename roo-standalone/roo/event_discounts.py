"""Private, explicitly confirmed member event discounts.

The backend owns quotes, point accounting and durable redemption recovery. Roo
only supplies verified request context and displays authoritative results.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from .backend_identity import BackendIdentityError, get_backend_actor_context
from .clients.mlai_backend import MLAIBackendUnavailableError

ACTIONS = frozenset({
    "quote_event_discount", "redeem_event_discount",
    "get_event_redemption", "list_event_redemptions",
})
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def confirmation_quote_id(text: str) -> str | None:
    """Extract consent only from the member's complete original command."""
    match = re.fullmatch(
        rf"confirm event discount ({UUID_PATTERN})", str(text or "").strip(),
        flags=re.IGNORECASE,
    )
    return str(UUID(match.group(1))) if match else None



def supplied_event_id(text: str) -> str | None:
    """Use a canonical event ID stated in the member's original Chat request.

    Chat event cards append this marker independently of the event's display
    name. An extracted model parameter must not override that explicit choice.
    Conflicting or malformed markers need a fresh unambiguous request.
    """
    text = str(text or "")
    matches = re.findall(
        r"\((?i:Luma event ID):\s*(evt-[A-Za-z0-9_-]{1,96})\)", text,
    )
    if len(set(matches)) > 1 or (
        re.search(r"\((?i:Luma event ID):", text) and not matches
    ):
        raise ValueError("Supply one canonical event ID for the discount quote.")
    return matches[0] if matches else None



def is_chat_event_discount_request(text: str) -> bool:
    """Recognize only the exact unsent request prepared by Chat's event card."""
    text = str(text or "").strip()
    return bool(
        text.startswith("I want to spend my Roo Points to get a discount to ")
        and text.endswith(
            "). Please check eligibility and quote the points cost and discount before I confirm."
        )
        and supplied_event_id(text)
    )


def redemption_request_id(team_id: str, user_id: str, quote_id: str) -> str:
    """Return the same operation ID for every retry of one member's quote."""
    digest = hashlib.sha256(f"{team_id}:{user_id}:{quote_id}".encode()).hexdigest()
    return f"roo-event-discount-{digest}"


def trusted_actor_payload(actor, *, user_id: str | None = None,
                          channel_id: str | None = None,
                          thread_ts: str | None = None) -> dict:
    """Require a verified private actor; never use model-supplied identity."""
    if not actor or not (
        re.fullmatch(r"T[A-Z0-9]+", actor.slack_team_id)
        and re.fullmatch(r"[UW][A-Z0-9]+", actor.acting_slack_user_id)
        and re.fullmatch(r"D[A-Z0-9]+", actor.slack_channel_id)
        and actor.event_id
        and (user_id is None or actor.acting_slack_user_id == user_id)
        and (channel_id is None or actor.slack_channel_id == channel_id)
        and (thread_ts is None or actor.slack_thread_ts == thread_ts)
    ):
        raise BackendIdentityError("A verified private member conversation is required")
    return {
        "slack_user_id": actor.acting_slack_user_id,
        "slack_team_id": actor.slack_team_id,
        "slack_channel_id": actor.slack_channel_id,
        "slack_thread_ts": actor.slack_thread_ts,
    }


def _uuid(value) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("The event discount reference is invalid. Ask Roo for a fresh quote.") from exc


def _timestamp(value) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("An event discount expiry is missing its timezone")
    return parsed.astimezone(timezone.utc)


def _text(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _canonical_luma_event_url(raw: str) -> str | None:
    """Normalize an exact known Luma host and single event slug."""
    try:
        parsed = urlsplit(str(raw or ""))
        if not (
            parsed.scheme == "https"
            and parsed.hostname in {"luma.com", "lu.ma", "www.luma.com", "www.lu.ma"}
            and not parsed.username and not parsed.password
            and parsed.port in {None, 443}
            and re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path)
            and not parsed.fragment
            and not any(ord(char) < 33 or char in "<>|`\\" for char in str(raw))
        ):
            return None
        return urlunsplit(("https", parsed.hostname.removeprefix("www."), parsed.path, "", ""))
    except ValueError:
        return None


def _normalized_checkout_url(url, coupon_code) -> str | None:
    canonical = _canonical_luma_event_url(url)
    if not canonical or not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", str(coupon_code or "")):
        return None
    try:
        query = parse_qsl(urlsplit(str(url)).query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if query != [("coupon", coupon_code)]:
        return None
    return canonical + "?" + urlencode({"coupon": coupon_code})


def _points(value, *, positive=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid points amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid points amount") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount <= 0):
        raise ValueError("Invalid points amount")
    return amount


def _quote_message(result: dict, *, expected_event_id: str | None = None) -> str:
    quote_id = _uuid(result.get("quote_id"))
    expiry = _timestamp(result.get("expires_at"))
    cost = result.get("points_cost")
    event = result.get("event") or {}
    if (
        expiry <= datetime.now(timezone.utc)
        or not isinstance(event, dict)
        or not re.fullmatch(r"evt-[A-Za-z0-9_-]{1,96}", str(event.get("id") or ""))
        or (expected_event_id is not None and event["id"] != expected_event_id)
        or not isinstance(event.get("name"), str) or not event["name"].strip()
        or len(event["name"]) > 500
        or not _canonical_luma_event_url(event.get("url"))
        or urlsplit(str(event.get("url"))).query
        or _timestamp(event.get("starts_at")) < expiry
        or not isinstance(result.get("discount_description"), str)
        or not result["discount_description"].strip()
        or len(result["discount_description"]) > 1000
        or _points(result.get("eligible_balance")) < _points(cost, positive=True)
    ):
        raise ValueError("That event quote is unavailable or expired. Ask Roo for a fresh quote.")
    lines = [
        f"{_text(event['name'])}: {_text(result['discount_description'])}.",
        f"Cost: {cost} Roo Points.",
    ]
    if event.get("starts_at"):
        lines.append(f"Event starts: {_text(event['starts_at'])}.")
    if result.get("eligible_balance") is not None:
        lines.append(f"Your eligible balance: {_text(result['eligible_balance'])} Roo Points.")
    lines.extend([
        f"Quote expires: {expiry.isoformat()}.",
        f"To spend your points, reply in this same private thread:\n`confirm event discount {quote_id}`",
        "No points have been spent yet.",
    ])
    return "\n".join(lines)


def _redemption_message(result: dict) -> str:
    redemption_id = _uuid(result.get("id"))
    status = result.get("status")
    event = result.get("event") or {}
    name = _text(event.get("name") or result.get("event_name") or "Your event")
    reference = f"Redemption: {redemption_id}."
    if status == "processing":
        return (f"{name}: your discount is still being created. Check `my event discounts` "
                f"for the result. {reference}")
    if status == "fulfilled":
        checkout_url = _normalized_checkout_url(result.get("checkout_url"), result.get("coupon_code"))
        if not checkout_url:
            raise ValueError("I couldn't verify the coupon link. Check `my event discounts` again shortly.")
        if result.get("expires_at") and _timestamp(result["expires_at"]) <= datetime.now(timezone.utc):
            return f"{name}: this coupon has expired. {reference}"
        lines = [
            f"{name}: your event discount is ready.",
            f"{_text(result.get('points_cost'))} Roo Points spent.",
            f"Code: `{result['coupon_code']}`",
            f"Booking link: {checkout_url}",
        ]
        if result.get("expires_at"):
            lines.append(f"Coupon expires: {_text(result['expires_at'])}.")
        if result.get("remaining_balance") is not None:
            lines.append(f"Your remaining balance: {_text(result['remaining_balance'])} Roo Points.")
        lines.append(reference)
        return "\n".join(lines)
    if status == "cancelled":
        return f"{name}: this redemption was cancelled. {reference}"
    if status == "failed":
        return f"{name}: the discount could not be created; your points reservation was released. {reference}"
    raise ValueError("The event discount status is unavailable. Check `my event discounts` again shortly.")


async def handle_event_discount(*, client, action: str, params: dict, text: str,
                                user_id: str, channel_id: str | None,
                                thread_ts: str | None, settings) -> dict | str:
    """Handle member offers with consent independent of any LLM tool arguments."""
    if action not in ACTIONS:
        return "That event discount action is unavailable."
    if (action in {"quote_event_discount", "redeem_event_discount"}
            and not getattr(settings, "ROO_EVENT_DISCOUNTS_ENABLED", False)):
        return "Event discounts with Roo Points are not available yet."
    if getattr(settings, "ROO_SURFACE", "public") != "public":
        return "Please use Public Roo in a private conversation for your event discounts."
    if not channel_id or not channel_id.startswith("D"):
        return "DM Roo your event discount request so your quote and coupon stay private."
    try:
        actor = get_backend_actor_context()
        trusted_actor_payload(actor, user_id=user_id, channel_id=channel_id,
                              thread_ts=thread_ts or "")
        if actor.slack_team_id != getattr(settings, "ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID", ""):
            return "Event discounts are not enabled for this workspace."
        if not getattr(settings, "ROO_API_KEY", None):
            return "Event discounts are not configured yet. Please contact the MLAI team."
        if action == "quote_event_discount":
            original_event_id = supplied_event_id(text)
            query = original_event_id or str(params.get("event_query") or "").strip()
            if not query:
                return "Which event would you like a discount for? Send its name or canonical event ID."
            result = await client.quote_event_discount(query)
            message = _quote_message(result, expected_event_id=original_event_id)
        elif action == "redeem_event_discount":
            quote_id = confirmation_quote_id(text)
            if not quote_id:
                return ("To confirm spending, copy the `confirm event discount <quote_id>` "
                        "command from your quote into this same private thread.")
            result = await client.redeem_event_discount(quote_id)
            message = _redemption_message(result)
        elif action == "get_event_redemption":
            result = await client.get_event_redemption(_uuid(params.get("redemption_id")))
            message = _redemption_message(result)
        else:
            result = await client.list_event_redemptions()
            rows = result.get("redemptions")
            if not isinstance(rows, list):
                raise ValueError("Your event discounts are unavailable. Please try again shortly.")
            message = "\n\n".join(_redemption_message(row) for row in rows)
            message = message or "You have no event discount redemptions yet."
        return {"message": message, "data": {"action": action, "private_points": True,
                                              "delivery": "current_direct_message"}}
    except BackendIdentityError:
        return "I couldn't verify your private member conversation. Please start a new DM with Roo."
    except MLAIBackendUnavailableError:
        return ("I couldn't confirm the event discount result. Check `my event discounts` "
                "before starting another redemption; repeating your confirmation safely checks the same one.")
    except httpx.HTTPStatusError as exc:
        try:
            error = exc.response.json()
            code = error.get("code", "")
        except (ValueError, AttributeError):
            error, code = {}, ""
        if code == "ambiguous_event":
            candidates = error.get("candidates") or []
            if not isinstance(candidates, list):
                candidates = []
            choices = [
                f"• {_text(row.get('event_name'))} — `{row['event_id']}`"
                for row in candidates[:10]
                if isinstance(row, dict) and row.get("event_name")
                and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(row.get("event_id") or ""))
            ]
            if choices:
                return ("Which event would you like? Ask for a fresh quote using one of these event IDs:\n"
                        + "\n".join(choices) + "\nNo points have been spent.")
        if code == "insufficient_points":
            return "You don't have enough eligible Roo Points for that event discount."
        if exc.response.status_code in {400, 404, 409}:
            return ("That event offer or quote is unavailable, expired, or changed. "
                    "Ask Roo for a fresh quote, or check `my event discounts` for an existing coupon.")
        return "The event discount service is unavailable. Check `my event discounts` again shortly."
    except (ValueError, TypeError, KeyError, AttributeError):
        return "I couldn't verify the event discount result. Check `my event discounts` again shortly."
