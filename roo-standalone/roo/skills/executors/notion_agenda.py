"""Notion agenda skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from ... import notion_agenda as agenda
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from ...config import get_settings
from ...slack_client import get_user_info
from typing import Optional
import asyncio


class NotionAgendaExecutor(SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        kwargs = request.metadata
        return await self._execute_notion_agenda(
            text=request.text,
            params=request.params,
            user_id=request.user_id,
            channel_id=request.channel_id,
            slack_team_id=kwargs.get("slack_team_id"),
            request_message_ts=kwargs.get("current_message_ts"),
        )

    async def _execute_notion_agenda(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        slack_team_id: Optional[str],
        request_message_ts: Optional[str],
    ) -> str:
        """Add one Notion agenda bullet through the backend; reply only from its answer."""
        settings = get_settings()
        if not settings.MLAI_BACKEND_URL or not settings.ROO_API_KEY or not request_message_ts:
            return agenda.NOT_CONFIGURED
        item = str(params.get("item") or "").strip()
        if not item:
            return agenda.ITEM_QUESTION
        try:
            meeting_date = agenda.resolve_meeting_date(
                params.get("date"), text, item, message_ts=request_message_ts
            )
        except agenda.AgendaInputError as exc:
            return str(exc)

        # D5: the owner tag is the Slack display name only, never a fallback.
        # Uncached: get_user_info is lru_cached per process, which would keep
        # refusing someone who just set a display name, or a failed lookup.
        profile = await asyncio.to_thread(get_user_info.__wrapped__, user_id)
        if "display_name" not in profile:
            return agenda.PROFILE_UNAVAILABLE
        owner_name = str(profile.get("display_name") or "").strip()
        if not owner_name:
            return agenda.NO_DISPLAY_NAME

        # The strict endpoint accepts only ROO_API_KEY, never MLAI_API_KEY.
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY,
            internal_api_key=settings.ROO_API_KEY,
        )
        request = dict(
            idempotency_key=agenda.idempotency_key(slack_team_id, channel_id, request_message_ts),
            requester_slack_id=user_id,
            owner_name=owner_name,
            meeting_date=meeting_date,
            item=item,
            page_id=agenda.page_id_from_text(text),
        )

        async def send() -> Optional[dict]:
            try:
                return await client.add_notion_agenda_item(**request)
            except MLAIBackendUnavailableError:
                return None  # The write may have landed: treated as uncertain.

        result = await send()
        if result is not None and result.get("code") == "notion_rate_limited":
            await asyncio.sleep(min(int(result.get("retry_after") or 1), 10))
            result = await send()
        elif result is None or agenda.is_uncertain(result):
            # Same key: the backend replays a stored result or reads the page
            # before writing, so a landed first attempt answers "duplicate".
            result = await send()
        if result is None or agenda.is_uncertain(result):
            return agenda.UNCONFIRMED
        return agenda.reply_for(result, escape=agenda.slack_escape)
