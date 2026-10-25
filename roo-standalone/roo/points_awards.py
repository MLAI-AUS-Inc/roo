"""Manual awards to canonical, explicitly selected Chat recipients."""

import logging
import re
from typing import Any

import httpx

from .bridge_mentions import VerifiedBridgeContext
from .markdown_code import mask_markdown_code
from .slack_client import send_dm

logger = logging.getLogger(__name__)


class ChatAwardRetryableError(RuntimeError):
    """The original verified command must retry with its existing operation key."""


def _label(value: str) -> str:
    """Render database labels as plain Slack text, never mention syntax."""
    value = " ".join(value.split())[:200]
    value = re.sub(r"[*_~`]", "", value)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _source_for_parsing(text: str, context: VerifiedBridgeContext) -> str:
    """Mask signed mention labels and code while retaining source offsets."""
    labels = context.source_labels
    if labels is None:
        labels = tuple({label for target in context.recipient_targets
                        for label in (target.source_labels or (target.display_name,)) if label})
    visible = text
    for label in sorted(labels, key=len, reverse=True):
        visible = re.sub(
            rf"(?<![\w@])@{re.escape(label)}(?![\w])",
            lambda match: " " * len(match.group()), visible, flags=re.IGNORECASE,
        )
    return mask_markdown_code(visible)


def bridge_award_details(text: str, context: VerifiedBridgeContext) -> tuple[int | None, str, str]:
    """Extract an amount, reason and code-masked segment naming beneficiaries."""
    # Signed source text takes precedence over model-extracted values. Slack
    # IDs and digits in member names are never interpreted as point amounts.
    visible = _source_for_parsing(text, context)
    amounts = list(re.finditer(r"(?<![\w.,+-])([+-]?\d+)\s*(?:points?|pts?)\b", visible, re.IGNORECASE))
    if any(re.search(r"\d\s+$", visible[:amount.start()]) for amount in amounts):
        return None, "", ""  # Unsupported spaced thousands cannot become a smaller award.
    points = int(amounts[0].group(1)) if len(amounts) == 1 else None
    trailing_text = visible[amounts[0].end():] if len(amounts) == 1 else ""
    reason_match = re.search(r"\bfor(?=\s)", trailing_text, re.IGNORECASE)
    reason = text[amounts[0].end() + reason_match.end():].strip() if reason_match else ""
    boundary = amounts[0].end() + reason_match.start() if reason_match else 0
    return points, reason, mask_markdown_code(text)[:boundary]


def _beneficiaries(context: VerifiedBridgeContext, segment: str, bot_user_id: str | None):
    mentions = set(re.findall(r"<@([UW][A-Z0-9]+)>", segment))
    mentions.discard(bot_user_id)
    return tuple(target for target in context.recipient_targets if (
        target.slack_user_id in mentions
        or any(re.search(rf"(?<![\w@])@{re.escape(label)}(?![\w])", segment, re.IGNORECASE)
               for label in (target.source_labels or (target.display_name,)) if label)
    ))


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "The backend rejected this award. Check your Points Admin permission and allowance."
        if status == 409:
            return "This command conflicts with an earlier award. Check the earlier outcome before issuing a new command."
        if status in {400, 404}:
            return "The backend rejected this award. Check the selected member, points and weekly allowance."
    if isinstance(exc, (ValueError, PermissionError)):
        return "The backend rejected this award. Check your permission, selected member and points."
    return "I couldn't confirm this award. Check its outcome before issuing another award."


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code in {408, 429}
    return not isinstance(exc, (ValueError, PermissionError))


async def award_bridge_points(
    *, client: Any, context: VerifiedBridgeContext, text: str, params: dict,
    admin_slack_id: str, channel_id: str | None, thread_ts: str | None,
    bot_user_id: str | None = None,
) -> str:
    """Award only backend-verified targets, using source-bound replay protection."""
    if not isinstance(context, VerifiedBridgeContext):
        return "I couldn't verify the Chat recipients. Select the member again using the @ mention picker."
    if not context.recipient_targets:
        return "Who should receive the points? Select the member using the MLAI Chat @ mention picker, then send a new command."
    # Model parameters are deliberately ignored for both identity and payload;
    # retries of this signed source command must submit the same award.
    points, reason, beneficiary_text = bridge_award_details(text, context)
    if points is None:
        return "How many points should I award? Include one amount, such as `4 points`."
    if points <= 0:
        return "Points awards need a positive number of points."
    if not reason:
        return "What are the points for? Include a reason, such as `for helping at the event`."
    targets = _beneficiaries(context, beneficiary_text, bot_user_id)
    if not targets:
        return "Who should receive the points? Select the beneficiary before the `for` reason, then send a new command."
    actual_slack_mentions = set(re.findall(r"<@([UW][A-Z0-9]+)>", beneficiary_text))
    actual_slack_mentions.discard(bot_user_id)
    known_aliases = {target.slack_user_id for target in targets if target.slack_user_id}
    if actual_slack_mentions - known_aliases:
        return ("I couldn't verify every Slack mention alongside the selected Chat members. "
                "Send separate award commands for the Chat and Slack recipients so everyone receives the right award.")
    if any(target.profile_id == context.actor_profile_id for target in targets):
        return "You can't award points to yourself. Ask another Points Admin to review the award."

    outcomes = []
    for target in sorted(targets, key=lambda item: item.profile_id):
        try:
            result = await client.award_points(
                admin_slack_id, None, points, reason,
                target_profile_id=target.profile_id,
                idempotency_key=f"roo-chat-award:{context.source_event_id}:{target.profile_id}",
            )
            recipient = result.get("recipient") if isinstance(result, dict) else None
            if (not isinstance(recipient, dict)
                    or result.get("success") is not True
                    or recipient.get("kind") != "mlai_profile"
                    or recipient.get("profile_id") != target.profile_id
                    or not result.get("ledger_id")
                    or type(result.get("created")) is not bool
                    or type(result.get("replayed")) is not bool
                    or result["created"] == result["replayed"]):
                raise RuntimeError("Invalid award confirmation")
            outcomes.append(f"✅ Awarded {points} points to {_label(target.display_name) or 'the selected member'}.")
            slack_user_id = recipient.get("slack_user_id")
            if result["created"] and isinstance(slack_user_id, str) and re.fullmatch(r"[UW][A-Z0-9]+", slack_user_id):
                try:
                    send_dm(slack_user_id, f"You received {points} Roo Points.\nReason: {_label(reason)}")
                except Exception as exc:
                    # Notification errors never change the committed award.
                    logger.warning("Award DM failed source=%s error=%s", context.source_event_id, type(exc).__name__)
        except Exception as exc:
            logger.warning("Chat award outcome source=%s error=%s", context.source_event_id, type(exc).__name__)
            if _retryable(exc):
                # A timeout or malformed success can follow a committed award.
                # Keep the original Slack receipt retryable instead of asking
                # for a fresh command, which would have a different source key.
                raise ChatAwardRetryableError("Chat award outcome is pending") from exc
            outcomes.append(f"❌ {_label(target.display_name) or 'The selected member'}: {_failure_message(exc)}")
    return "\n".join(outcomes) + f"\n\nReason: {_label(reason)}"
