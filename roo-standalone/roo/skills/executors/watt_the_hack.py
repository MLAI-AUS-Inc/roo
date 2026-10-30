"""Watt the hack skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from typing import Optional, List
from ...clients.mlai_backend import MLAIBackendClient
from ..loader import Skill
from ...slack_client import get_bot_user_id, get_channel_name
from ...llm import get_llm_client
import json
import re
from .prompt import PromptExecutor


class WattTheHackExecutor(PromptExecutor):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_watt_the_hack(skill, text, params, user_id, channel_id, thread_ts, thread_history)
        return result

    async def _execute_watt_the_hack(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
    ) -> str:
        """Execute the Watt The Hack skill: publish announcements to the site.

        Authorisation is delegated to the backend — only Slack users who map to
        an MLAI Django superuser may publish. Announcements are authored by Roo
        (the bot identity), matching the HealthHack behaviour.
        """
        import json
        import re

        # Channel restriction: this skill only operates in the Watt channel.
        if skill.exclusive_channels and channel_id:
            from ...slack_client import get_channel_name
            channel_name = get_channel_name(channel_id)
            if channel_name and channel_name not in skill.exclusive_channels:
                channels_list = ", ".join(f"#*{ch}*" for ch in skill.exclusive_channels)
                return f"The Watt The Hack skill is only available in {channels_list}."

        text_lower = text.lower()
        # The router's validated action wins; keyword sniffing only covers
        # callers that did not route (e.g. direct invocations without params).
        requested_action = str(params.get("action") or "").strip().lower()
        if requested_action in ("announce", "event_qa"):
            is_announcement = requested_action == "announce"
        else:
            announce_keywords = ["announce", "announcement", "post announcement", "create announcement"]
            is_announcement = any(k in text_lower for k in announce_keywords)

        if not is_announcement:
            # Event Q&A: answer questions about Watt The Hack from the skill's
            # knowledge base. Falls back to the generic skill content only if the
            # knowledge file is missing.
            knowledge = ""
            try:
                knowledge_file = skill.path / "knowledge.md"
                if knowledge_file.exists():
                    knowledge = knowledge_file.read_text(encoding="utf-8")
            except Exception as e:
                print(f"⚠️ Watt The Hack: failed to load knowledge.md: {e}")

            if not knowledge:
                return await self._execute_with_llm(skill, text, params, user_id, thread_history)

            qa_system_prompt = (
                "You are Roo, the friendly assistant for the Watt The Hack hackathon, "
                "answering questions in the #watt-the-hack Slack channel.\n\n"
                "Answer the user's question using ONLY the Watt The Hack information below. "
                "Be concise, warm and helpful, and use simple Slack-friendly formatting. "
                "If the answer is not in the information, say you're not sure and suggest they "
                "check watt-the-hack.com or ask an organiser — do NOT invent facts, dates, "
                "prizes, names, times or venues.\n\n"
                "=== WATT THE HACK INFORMATION ===\n"
                f"{knowledge}\n"
                "=== END INFORMATION ==="
            )
            openai_client = get_llm_client("openai")
            qa_response = await openai_client.chat([
                {"role": "system", "content": qa_system_prompt},
                {"role": "user", "content": text},
            ], model="gpt-4o-mini", max_tokens=700)
            return qa_response.content.strip()

        # Prefer title/body the router already extracted from the message.
        ann_title = str(params.get("title") or "").strip() or None
        ann_body = str(params.get("body") or "").strip() or None
        if ann_title and ann_body:
            return await self._publish_watt_announcement(user_id, ann_title, ann_body)

        # Use an LLM to extract a clear title and body from the request.
        extract_prompt = f"""Extract the announcement title and body from this message.
The user wants to create an announcement for the Watt The Hack hackathon website.

User message: "{text}"

Return ONLY valid JSON with two keys: "title" and "body".
If you cannot determine a clear title or body from the message, set the missing field to null.

Example: {{"title": "Lunch is served", "body": "Pizza is in the atrium at 1pm."}}

JSON:"""
        openai_client = get_llm_client("openai")
        extract_response = await openai_client.chat([
            {"role": "system", "content": "You extract structured data from text. Return valid JSON only."},
            {"role": "user", "content": extract_prompt}
        ], model="gpt-4o-mini", max_tokens=1024)

        try:
            content = extract_response.content.strip()
            if content.startswith("```"):
                content = re.sub(r'^```\w*\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            extracted = json.loads(content)
        except json.JSONDecodeError:
            extracted = {}

        ann_title = ann_title or extracted.get("title")
        ann_body = ann_body or extracted.get("body")

        if not ann_title or not ann_body:
            return (
                f"<@{user_id}> I need both a *title* and *body* for the announcement. "
                f"Try something like:\n"
                f"_\"Post an announcement titled 'Lunch is served' with body 'Pizza is in the atrium at 1pm.'\"_"
            )

        return await self._publish_watt_announcement(user_id, ann_title, ann_body)


    async def _publish_watt_announcement(self, user_id: str, ann_title: str, ann_body: str) -> str:
        """Publish a Watt The Hack announcement via the backend.

        The requesting human (user_id) is checked against Django superusers;
        authorship is attributed to Roo's bot id so the website shows Roo as
        the author.
        """
        from ...clients.mlai_backend import MLAIBackendClient
        from ...slack_client import get_bot_user_id
        backend = MLAIBackendClient()
        bot_id = get_bot_user_id()
        result = await backend.generic_hackathon_create_announcement(
            slug="watt-the-hack",
            title=ann_title,
            body=ann_body,
            requester_slack_id=user_id,
            author_slack_id=bot_id,
        )

        if result is None:
            return f"<@{user_id}> Something went wrong creating the announcement. Please try again later."

        status_code = result.get("status_code")
        if status_code == 400:
            return f"<@{user_id}> The announcement couldn't be created — {result.get('detail', 'something is missing')}."
        if status_code in (401, 403):
            return (
                f"<@{user_id}> Sorry, only MLAI superusers can post announcements "
                f"to the Watt The Hack site."
            )
        if status_code is not None:
            return f"<@{user_id}> Unexpected error (HTTP {status_code}): {result.get('detail', 'unknown')}"

        # Success — the framework posts this confirmation back in-thread.
        return f"Announcement *\"{ann_title}\"* has been posted to the Watt The Hack website."


