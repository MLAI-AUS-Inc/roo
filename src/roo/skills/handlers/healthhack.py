"""Healthhack skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from ...clients.mlai_backend import MLAIBackendClient
from typing import Optional
from ..loader import Skill
from ...slack_client import get_bot_user_id, get_channel_name
from ...llm import get_llm_client
import json
import re


class HealthhackHandler:
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_healthhack(
            skill,
            text,
            params,
            user_id,
            channel_id,
            thread_ts,
            kwargs.get("current_message_ts"),
        )
        return result

    async def _execute_healthhack(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        current_message_ts: Optional[str] = None,
    ) -> str:
        """Publish an announcement from #healthhack to the participant app."""
        import json
        import re

        if skill.exclusive_channels:
            from ...slack_client import get_channel_name

            channel_name = get_channel_name(channel_id) if channel_id else None
            if channel_name not in skill.exclusive_channels:
                return "The HealthHack announcement skill is only available in #*healthhack*."

        ann_title = str(params.get("title") or "").strip() or None
        ann_body = str(params.get("body") or "").strip() or None

        if not ann_title or not ann_body:
            extract_prompt = f"""Extract the announcement title and body from this message.
The user wants to publish an announcement to the HealthHack participant app.

User message: "{text}"

Return ONLY valid JSON with two keys: "title" and "body".
If you cannot determine a clear title or body, set the missing field to null.

Example: {{"title": "Lunch is served", "body": "Pizza is in the atrium at 1pm."}}

JSON:"""
            openai_client = get_llm_client("openai")
            extract_response = await openai_client.chat(
                [
                    {
                        "role": "system",
                        "content": "You extract structured data from text. Return valid JSON only.",
                    },
                    {"role": "user", "content": extract_prompt},
                ],
                model="gpt-4o-mini",
                max_tokens=1024,
            )

            try:
                content = extract_response.content.strip()
                if content.startswith("```"):
                    content = re.sub(r'^```\w*\n?', '', content)
                    content = re.sub(r'\n?```$', '', content)
                extracted = json.loads(content)
            except (json.JSONDecodeError, TypeError, AttributeError):
                extracted = {}

            ann_title = ann_title or extracted.get("title")
            ann_body = ann_body or extracted.get("body")

        if not ann_title or not ann_body:
            return (
                f"<@{user_id}> I need both a *title* and *body* for the announcement. "
                "Try: _\"Post an announcement titled 'Lunch is served' with body "
                "'Pizza is in the atrium at 1pm.'\"_"
            )

        if not channel_id or not current_message_ts:
            return (
                f"<@{user_id}> I couldn't identify the source Slack message, so I didn't "
                "publish the announcement. Please try again in #healthhack."
            )

        return await self._publish_healthhack_announcement(
            user_id=user_id,
            ann_title=str(ann_title).strip(),
            ann_body=str(ann_body).strip(),
            channel_id=channel_id,
            source_message_ts=current_message_ts,
        )


    async def _publish_healthhack_announcement(
        self,
        *,
        user_id: str,
        ann_title: str,
        ann_body: str,
        channel_id: str,
        source_message_ts: str,
    ) -> str:
        """Persist one backend-authorised HealthHack announcement."""
        from ...clients.mlai_backend import MLAIBackendClient
        from ...slack_client import get_bot_user_id

        backend = MLAIBackendClient()
        result = await backend.healthhack_create_announcement(
            title=ann_title,
            body=ann_body,
            requester_slack_id=user_id,
            author_slack_id=get_bot_user_id(),
            source_channel_id=channel_id,
            source_message_ts=source_message_ts,
        )

        if result is None:
            return f"<@{user_id}> Something went wrong creating the announcement. Please try again later."

        status_code = result.get("status_code")
        if status_code == 400:
            return f"<@{user_id}> The announcement couldn't be created — {result.get('detail', 'something is missing')}."
        if status_code == 409:
            return (
                f"<@{user_id}> That Slack message is already linked to a different "
                "HealthHack announcement, so I didn't overwrite it."
            )
        if status_code in (401, 403):
            return (
                f"<@{user_id}> Sorry, only authorised HealthHack organisers can "
                "publish announcements to the participant app."
            )
        if status_code is not None:
            return f"<@{user_id}> Unexpected error (HTTP {status_code}): {result.get('detail', 'unknown')}"

        if result.get("created") is False:
            return f"Announcement *\"{ann_title}\"* was already posted to the HealthHack app."
        return f"Announcement *\"{ann_title}\"* has been posted to the HealthHack app."


