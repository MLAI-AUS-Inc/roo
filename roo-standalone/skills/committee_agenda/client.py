"""Committee meeting agenda skill client.

Posts agenda submissions to a dedicated Slack channel. Slack itself is the
source of truth — seconds are tallied from emoji reactions on the posted card.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from roo.config import get_settings
from roo.slack_client import (
    get_channel_id,
    get_display_name,
    get_slack_client,
    post_message,
)


VALID_URGENCIES = ("low", "normal", "high")
URGENCY_LABEL = {"low": "Low", "normal": "Normal", "high": "High"}
URGENCY_EMOJI = {"low": "🟢", "normal": "🟡", "high": "🔴"}
MAX_TITLE_LEN = 120

# Stable marker prefixes embedded in the message fallback text so cleanup can
# find agenda items written by Roo without relying on per-item metadata.
AGENDA_ITEM_MARKER = "[roo-agenda-item]"
AGENDA_COMPLETED_MARKER = "[roo-agenda-item][completed]"
AGENDA_CLEANUP_DEFAULT_LIMIT = 200


@dataclass
class AgendaSubmission:
    ok: bool
    message: str
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    posted_ts: Optional[str] = None
    permalink: Optional[str] = None
    error_code: Optional[str] = None


@dataclass
class AgendaCompletion:
    ok: bool
    message: str
    item_ts: Optional[str] = None
    removed: bool = False
    error_code: Optional[str] = None


@dataclass
class AgendaCleanup:
    ok: bool
    message: str
    removed_count: int = 0
    error_code: Optional[str] = None


class CommitteeAgendaClient:
    """Slack-only persistence: each agenda item is a message in #committee-agenda."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def _resolve_channel_id(self) -> Optional[str]:
        configured = (self._settings.COMMITTEE_AGENDA_CHANNEL_ID or "").strip()
        if configured:
            return configured
        name = (self._settings.COMMITTEE_AGENDA_CHANNEL_NAME or "").strip()
        if not name:
            return None
        return get_channel_id(name)

    @staticmethod
    def _normalize_urgency(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in VALID_URGENCIES:
            return text
        return "normal"

    @staticmethod
    def _normalize_title(value: Any) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > MAX_TITLE_LEN:
            text = text[: MAX_TITLE_LEN - 1].rstrip() + "…"
        return text

    @staticmethod
    def _build_blocks(
        *,
        title: str,
        description: str,
        urgency: str,
        proposer_user_id: str,
        source_permalink: Optional[str],
        second_emoji: str,
    ) -> list[dict]:
        urgency_text = f"{URGENCY_EMOJI.get(urgency, '🟡')} {URGENCY_LABEL.get(urgency, 'Normal')}"
        proposer_mention = f"<@{proposer_user_id}>" if proposer_user_id else "_unknown_"
        context_elements: list[dict] = [
            {"type": "mrkdwn", "text": f"*Proposed by:* {proposer_mention}"},
            {"type": "mrkdwn", "text": f"*Urgency:* {urgency_text}"},
        ]
        if source_permalink:
            context_elements.append(
                {"type": "mrkdwn", "text": f"<{source_permalink}|Source message>"}
            )

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📋 New agenda item", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"},
            },
        ]
        if description:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": description},
                }
            )
        blocks.append({"type": "context", "elements": context_elements})
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"React with :{second_emoji}: to second this item.",
                    }
                ],
            }
        )
        return blocks

    def _resolve_source_permalink(
        self,
        source_channel_id: Optional[str],
        source_message_ts: Optional[str],
    ) -> Optional[str]:
        if not source_channel_id or not source_message_ts:
            return None
        try:
            client = get_slack_client()
            response = client.chat_getPermalink(
                channel=source_channel_id,
                message_ts=source_message_ts,
            )
            if response.get("ok"):
                return response.get("permalink")
        except Exception as exc:
            print(f"⚠️ chat.getPermalink failed for {source_channel_id}/{source_message_ts}: {exc}")
        return None

    def _resolve_posted_permalink(
        self, channel_id: str, posted_ts: str
    ) -> Optional[str]:
        try:
            client = get_slack_client()
            response = client.chat_getPermalink(channel=channel_id, message_ts=posted_ts)
            if response.get("ok"):
                return response.get("permalink")
        except Exception as exc:
            print(f"⚠️ chat.getPermalink failed for posted item {channel_id}/{posted_ts}: {exc}")
        return None

    def submit_item(
        self,
        *,
        title: str,
        description: str = "",
        urgency: str = "normal",
        proposer_user_id: str = "",
        source_channel_id: Optional[str] = None,
        source_message_ts: Optional[str] = None,
    ) -> AgendaSubmission:
        clean_title = self._normalize_title(title)
        if not clean_title:
            return AgendaSubmission(
                ok=False,
                message=(
                    "I need a short title for the agenda item. "
                    "Try something like `@Roo add to agenda: budget for new whiteboards`."
                ),
                error_code="missing_title",
            )

        channel_id = self._resolve_channel_id()
        if not channel_id:
            return AgendaSubmission(
                ok=False,
                message=(
                    "The committee agenda channel isn't configured yet. "
                    "An admin needs to set `COMMITTEE_AGENDA_CHANNEL_ID` (or make sure "
                    f"I'm a member of `#{self._settings.COMMITTEE_AGENDA_CHANNEL_NAME}`)."
                ),
                error_code="channel_unconfigured",
            )

        clean_description = " ".join(str(description or "").split())
        clean_urgency = self._normalize_urgency(urgency)
        second_emoji = (self._settings.COMMITTEE_AGENDA_SECOND_EMOJI or "+1").strip(":") or "+1"
        source_permalink = self._resolve_source_permalink(source_channel_id, source_message_ts)

        blocks = self._build_blocks(
            title=clean_title,
            description=clean_description,
            urgency=clean_urgency,
            proposer_user_id=proposer_user_id,
            source_permalink=source_permalink,
            second_emoji=second_emoji,
        )
        proposer_label = get_display_name(proposer_user_id) if proposer_user_id else "a member"
        fallback_text = (
            f"{AGENDA_ITEM_MARKER} New agenda item from {proposer_label}: {clean_title}"
        )

        try:
            response = post_message(channel_id, text=fallback_text, blocks=blocks)
        except Exception as exc:
            return AgendaSubmission(
                ok=False,
                message=(
                    "Sorry, I couldn't post to the committee agenda channel. "
                    "Double check that I'm a member and try again."
                ),
                error_code="post_failed",
                channel_id=channel_id,
            )

        if not response.get("ok"):
            slack_error = response.get("error") or "unknown_error"
            print(f"❌ Failed to post agenda item: {slack_error}")
            return AgendaSubmission(
                ok=False,
                message=(
                    f"Slack rejected the agenda post (`{slack_error}`). "
                    "Make sure I'm a member of the agenda channel and try again."
                ),
                error_code=slack_error,
                channel_id=channel_id,
            )

        posted_ts = response.get("ts")
        posted_channel = response.get("channel") or channel_id
        permalink = self._resolve_posted_permalink(posted_channel, posted_ts) if posted_ts else None
        channel_label = f"<#{posted_channel}>"
        link_clause = f" → {permalink}" if permalink else ""

        return AgendaSubmission(
            ok=True,
            message=(
                f"Done! I've added *{clean_title}* to the committee agenda in {channel_label}."
                f"{link_clause}\n"
                f"React with :{second_emoji}: on that card to second it. 👍"
            ),
            channel_id=posted_channel,
            channel_name=self._settings.COMMITTEE_AGENDA_CHANNEL_NAME,
            posted_ts=posted_ts,
            permalink=permalink,
        )

    def submit_from_params(
        self,
        params: Dict[str, Any],
        *,
        proposer_user_id: str,
        source_channel_id: Optional[str],
        source_message_ts: Optional[str],
    ) -> AgendaSubmission:
        return self.submit_item(
            title=params.get("title", ""),
            description=params.get("description", ""),
            urgency=params.get("urgency", "normal"),
            proposer_user_id=proposer_user_id,
            source_channel_id=source_channel_id,
            source_message_ts=source_message_ts,
        )

    # ------------------------------------------------------------------
    # Completion + cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _is_agenda_item_message(message: Dict[str, Any]) -> bool:
        text = str(message.get("text") or "")
        return AGENDA_ITEM_MARKER in text

    @staticmethod
    def _is_completed_agenda_item(message: Dict[str, Any]) -> bool:
        text = str(message.get("text") or "")
        return AGENDA_COMPLETED_MARKER in text

    def _agenda_channel_or_error(self) -> tuple[Optional[str], Optional[AgendaCompletion]]:
        channel_id = self._resolve_channel_id()
        if channel_id:
            return channel_id, None
        return None, AgendaCompletion(
            ok=False,
            message=(
                "The committee agenda channel isn't configured. "
                "Ask an admin to set `COMMITTEE_AGENDA_CHANNEL_ID`."
            ),
            error_code="channel_unconfigured",
        )

    def _fetch_agenda_item(
        self, channel_id: str, item_ts: str
    ) -> Optional[Dict[str, Any]]:
        from roo.slack_client import get_message  # local import to avoid cycle

        return get_message(channel_id, item_ts)

    def complete_item(
        self,
        *,
        item_channel_id: Optional[str],
        item_ts: Optional[str],
        completed_by_user_id: str = "",
        remove: bool = False,
        reason: str = "",
    ) -> AgendaCompletion:
        agenda_channel_id, error = self._agenda_channel_or_error()
        if error:
            return error
        if not item_channel_id or item_channel_id != agenda_channel_id or not item_ts:
            return AgendaCompletion(
                ok=False,
                message=(
                    "Reply in the thread of the agenda item in "
                    f"<#{agenda_channel_id}> and say something like "
                    "`@Roo agenda complete` so I know which item to close."
                ),
                error_code="not_in_agenda_thread",
            )

        message = self._fetch_agenda_item(agenda_channel_id, item_ts)
        if not message:
            return AgendaCompletion(
                ok=False,
                message="I couldn't find that agenda item to mark complete.",
                error_code="item_not_found",
            )
        if not self._is_agenda_item_message(message):
            return AgendaCompletion(
                ok=False,
                message=(
                    "That message doesn't look like an agenda item I posted. "
                    "Reply in the thread of an agenda card instead."
                ),
                error_code="not_agenda_item",
            )

        client = get_slack_client()
        completer_mention = (
            f"<@{completed_by_user_id}>" if completed_by_user_id else "a committee member"
        )
        reason_clause = f" — {reason.strip()}" if reason and reason.strip() else ""

        if remove:
            try:
                response = client.chat_delete(channel=agenda_channel_id, ts=item_ts)
            except Exception as exc:
                return AgendaCompletion(
                    ok=False,
                    message=f"Couldn't delete that agenda item: {exc}",
                    error_code="delete_failed",
                )
            if not response.get("ok"):
                slack_error = response.get("error") or "unknown_error"
                return AgendaCompletion(
                    ok=False,
                    message=(
                        f"Slack rejected the delete (`{slack_error}`). "
                        "Roo can only delete its own messages, so make sure the "
                        "item was posted by me."
                    ),
                    error_code=slack_error,
                )
            return AgendaCompletion(
                ok=True,
                message=f"Agenda item removed by {completer_mention}.{reason_clause}",
                item_ts=item_ts,
                removed=True,
            )

        existing_text = str(message.get("text") or "")
        existing_blocks = message.get("blocks") or []
        if self._is_completed_agenda_item(message):
            return AgendaCompletion(
                ok=True,
                message="That agenda item is already marked complete.",
                item_ts=item_ts,
            )

        updated_text = existing_text.replace(
            AGENDA_ITEM_MARKER, AGENDA_COMPLETED_MARKER, 1
        )
        if AGENDA_COMPLETED_MARKER not in updated_text:
            updated_text = f"{AGENDA_COMPLETED_MARKER} {existing_text}"
        if not updated_text.startswith("✅ Completed"):
            updated_text = f"✅ Completed — {updated_text}"

        updated_blocks = self._mark_blocks_completed(existing_blocks, completer_mention, reason_clause)

        try:
            response = client.chat_update(
                channel=agenda_channel_id,
                ts=item_ts,
                text=updated_text,
                blocks=updated_blocks if updated_blocks else None,
            )
        except Exception as exc:
            return AgendaCompletion(
                ok=False,
                message=f"Couldn't update that agenda item: {exc}",
                error_code="update_failed",
            )
        if not response.get("ok"):
            slack_error = response.get("error") or "unknown_error"
            return AgendaCompletion(
                ok=False,
                message=(
                    f"Slack rejected the update (`{slack_error}`). "
                    "Roo can only edit its own messages."
                ),
                error_code=slack_error,
            )

        try:
            post_message(
                agenda_channel_id,
                text=f"✅ Marked complete by {completer_mention}.{reason_clause}",
                thread_ts=item_ts,
            )
        except Exception as exc:
            print(f"⚠️ Failed to post completion thread reply: {exc}")

        return AgendaCompletion(
            ok=True,
            message=f"Agenda item marked complete by {completer_mention}.{reason_clause}",
            item_ts=item_ts,
        )

    @staticmethod
    def _mark_blocks_completed(
        blocks: list,
        completer_mention: str,
        reason_clause: str,
    ) -> list:
        if not isinstance(blocks, list) or not blocks:
            return []
        out: list = []
        replaced_header = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            cloned = dict(block)
            if not replaced_header and cloned.get("type") == "header":
                cloned = {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Completed agenda item",
                        "emoji": True,
                    },
                }
                replaced_header = True
            out.append(cloned)
        out.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"✅ Marked complete by {completer_mention}.{reason_clause}",
                    }
                ],
            }
        )
        return out

    def cleanup_completed_items(
        self,
        *,
        initiator_user_id: str = "",
        limit: int = AGENDA_CLEANUP_DEFAULT_LIMIT,
    ) -> AgendaCleanup:
        channel_id = self._resolve_channel_id()
        if not channel_id:
            return AgendaCleanup(
                ok=False,
                message=(
                    "The committee agenda channel isn't configured. "
                    "Ask an admin to set `COMMITTEE_AGENDA_CHANNEL_ID`."
                ),
                error_code="channel_unconfigured",
            )

        client = get_slack_client()
        removed = 0
        scanned = 0
        cursor: Optional[str] = None
        try:
            while scanned < limit:
                page = client.conversations_history(
                    channel=channel_id,
                    limit=min(100, limit - scanned),
                    cursor=cursor,
                )
                if not page.get("ok"):
                    slack_error = page.get("error") or "unknown_error"
                    return AgendaCleanup(
                        ok=False,
                        message=(
                            f"Couldn't read the agenda channel (`{slack_error}`). "
                            "Make sure Roo is a member."
                        ),
                        error_code=slack_error,
                    )
                for msg in page.get("messages", []):
                    scanned += 1
                    if not self._is_completed_agenda_item(msg):
                        continue
                    ts = msg.get("ts")
                    if not ts:
                        continue
                    delete_resp = client.chat_delete(channel=channel_id, ts=ts)
                    if delete_resp.get("ok"):
                        removed += 1
                    else:
                        print(
                            f"⚠️ Failed to delete completed agenda item ts={ts}: "
                            f"{delete_resp.get('error')}"
                        )
                cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        except Exception as exc:
            return AgendaCleanup(
                ok=False,
                message=f"Cleanup failed: {exc}",
                error_code="cleanup_failed",
            )

        initiator_clause = (
            f" (initiated by <@{initiator_user_id}>)" if initiator_user_id else ""
        )
        if removed == 0:
            return AgendaCleanup(
                ok=True,
                message=f"No completed agenda items found to remove{initiator_clause}.",
            )
        return AgendaCleanup(
            ok=True,
            message=(
                f"Removed {removed} completed agenda item"
                f"{'s' if removed != 1 else ''} from <#{channel_id}>{initiator_clause}."
            ),
            removed_count=removed,
        )
