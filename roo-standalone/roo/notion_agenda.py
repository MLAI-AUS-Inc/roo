"""Pure helpers for adding Notion meeting agenda items through mlai-backend.

The backend owns Notion, the committee check, the write and its verification
(contract: notion-agenda-spec §10). Roo resolves the meeting date, builds the
idempotency key and turns the backend's answer into a thread reply. Nothing
here touches the network or the system clock.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .coworking_dates import CoworkingDateError, resolve_coworking_date

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")

DATE_QUESTION = (
    "Which meeting should I add it to? Give me the date, like `tomorrow`, "
    "`Monday` or `27 October`."
)
ITEM_QUESTION = "What should I add to the agenda?"
NO_DISPLAY_NAME = (
    "I couldn't find your Slack display name, which I use as the owner tag on "
    "the agenda. Set one in your Slack profile and ask me again. Nothing was added."
)
PROFILE_UNAVAILABLE = "I couldn't look up your Slack profile just now. Please try again. Nothing was added."
NOT_CONFIGURED = "Notion agenda updates aren't set up right now."
UNCONFIRMED = (
    "Notion didn't confirm the change. Please check the meeting page before "
    "asking again."
)

# Codes whose write may have landed when `uncertain` is true (§10).
UNCERTAIN_CODES = {"notion_unavailable", "notion_invalid_response", "agenda_verify_failed"}

ERROR_REPLIES = {
    "notion_agenda_disabled": "Agenda updates are switched off right now.",
    "agenda_requester_not_allowed": (
        "Only committee members can add agenda items. Nothing was added."
    ),
    "invalid_meeting_date": (
        "I can only add items to meetings from yesterday up to a year ahead. "
        "Which meeting date did you mean?"
    ),
    "invalid_item": "I couldn't read the agenda item. Could you rephrase it?",
    "invalid_owner": "I couldn't use your Slack display name as the owner tag. Nothing was added.",
    "invalid_request": "I couldn't turn that into an agenda item. Could you rephrase it with the meeting date?",
    "meeting_page_mismatch": (
        "That link isn't a meeting page for that date. Nothing was added. "
        "Ask again with one of the links I listed."
    ),
    "agenda_busy": "Another update for that meeting is running. Ask me again in a minute.",
    "agenda_request_in_progress": "I'm still working on that request. Ask me again in a minute.",
    "idempotency_key_reused": "Something went wrong on my side, so nothing was added. Please ask again.",
    "agenda_section_not_found": (
        "That meeting page doesn't have an *Agenda* heading I can find, so I "
        "didn't change it. Please add the item by hand."
    ),
    "agenda_section_ambiguous": (
        "That meeting page has more than one *Agenda* heading, so I didn't change "
        "it. Please add the item by hand."
    ),
    "agenda_section_unsupported": (
        "That meeting page's agenda isn't laid out the way I expect, so I didn't "
        "change it. Please add the item by hand."
    ),
    "notion_page_too_large": (
        "That meeting page is too long for me to check safely, so I didn't change "
        "it. Please add the item by hand."
    ),
    "notion_rate_limited": "Notion is busy right now. Nothing was added. Ask me again in a minute.",
    "notion_not_configured": NOT_CONFIGURED,
    "notion_access_denied": "I couldn't reach the Meeting Notes in Notion. Nothing was added.",
    "notion_not_found": "I couldn't reach the Meeting Notes in Notion. Nothing was added.",
    "notion_rejected": "I couldn't reach the Meeting Notes in Notion. Nothing was added.",
}

_NOTION_ID = re.compile(
    r"notion\.(?:so|site)/[^\s|>]*?([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)


class AgendaInputError(ValueError):
    """The request needs a clarification before anything is sent."""


def melbourne_date_of(message_ts: str) -> date:
    """The Melbourne calendar date of a Slack message timestamp."""
    return datetime.fromtimestamp(float(message_ts), tz=MELBOURNE_TZ).date()


def resolve_meeting_date(raw_date: object, text: str, item: str, *, message_ts: str) -> str:
    """Resolve the meeting date relative to the message's Melbourne date.

    The date resolver asks for clarification when it sees stray numbers or
    words like "between", and agenda items often contain them ("Q4 goals",
    "check-ins between presidents…"). So a router-supplied date phrase is
    resolved on its own, and the full message is only scanned (with the item
    text removed) when there is no phrase.
    """
    raw = re.sub(r"'s\b|\b(?:the|for|on|at|meeting|meetings|agenda)\b", " ", str(raw_date or ""), flags=re.IGNORECASE)
    raw = " ".join(raw.split())
    date_text = ""
    if not raw:
        date_text = str(text or "")
        item = str(item or "").strip()
        if item:
            date_text = re.sub(re.escape(item), " ", date_text, flags=re.IGNORECASE)
    try:
        resolved = resolve_coworking_date(
            raw,
            date_text,
            today=melbourne_date_of(message_ts),
            default_to_today=False,
        )
    except CoworkingDateError as exc:
        raise AgendaInputError(str(exc) or DATE_QUESTION) from exc
    if not resolved:
        raise AgendaInputError(DATE_QUESTION)
    return resolved


def idempotency_key(workspace_id: str, channel_id: str, message_ts: str) -> str:
    """sha256 hex of workspace:channel:source message ts (the item text is left out)."""
    source = f"{workspace_id or ''}:{channel_id or ''}:{message_ts}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def page_id_from_text(text: str) -> Optional[str]:
    """The Notion page ID in a pasted meeting link, as Notion's dashed form.

    Used when the user answers "which meeting?" by asking again with a link.
    More than one different link is ambiguous, so none is used.
    """
    ids = {match.group(1).replace("-", "").lower() for match in _NOTION_ID.finditer(str(text or ""))}
    if len(ids) != 1:
        return None
    raw = ids.pop()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def slack_escape(value: Any) -> str:
    """Escape Slack control characters in text taken from Notion (page titles)."""
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_uncertain(result: dict[str, Any]) -> bool:
    return bool(result.get("uncertain")) and result.get("code") in UNCERTAIN_CODES


def reply_for(result: dict[str, Any], *, escape=lambda value: str(value)) -> str:
    """The thread reply for one backend answer (§10 Results and Errors)."""
    status = result.get("status")
    title = escape(result.get("page_title") or "the meeting page")
    url = str(result.get("page_url") or "")
    if status == "added":
        created = " I created the page from the template." if result.get("created_page") else ""
        return f"Added to {title}: {url}{created}"
    if status == "duplicate":
        where = "already under *If time:*" if result.get("duplicate_location") == "if_time" else "already on the agenda"
        return f"That's {where} for {title}: {url}"
    if status == "ambiguous":
        choices = [c for c in result.get("choices") or [] if isinstance(c, dict) and c.get("page_url")]
        lines = "\n".join(f"{i}. {c['page_url']}" for i, c in enumerate(choices, 1))
        return (
            "More than one meeting page matches that date, so I didn't add anything. "
            "Which meeting? Ask me again with the link of the right one:\n" + lines
        )
    if status == "template_pending":
        return (
            f"I created {title} but the template is still loading, so I didn't add "
            f"the item yet. Ask me again in a minute: {url}"
        )
    if is_uncertain(result):
        return UNCONFIRMED
    return ERROR_REPLIES.get(str(result.get("code") or ""), "I couldn't add that to the agenda. Nothing was added.")
