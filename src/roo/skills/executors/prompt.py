"""Prompt skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from typing import Optional, List
from ..loader import Skill
from ...llm import chat, get_llm_client


class PromptExecutor(SkillExecutorInterface):
    """Execute prompt instructions with injectable model dependencies."""
    def __init__(self, *, chat_fn=None, client_factory=None):
        self._chat_fn = chat_fn
        self._client_factory = client_factory

    async def complete(self, messages, *, provider=None, **options):
        if provider is not None:
            client = (self._client_factory or get_llm_client)(provider)
            return await client.chat(messages, **options)
        return await (self._chat_fn or chat)(messages, **options)

    async def execute(self, request: SkillRequest):
        return await self._execute_with_llm(request.skill, request.text, request.params, request.user_id, request.thread_history)

    async def _execute_with_llm(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        history: Optional[List[dict]] = None
    ) -> str:
        """Execute the skill using LLM to follow the skill's instructions."""
        
        # Check if skill has vector search action
        has_vector_search = "vector" in skill.content.lower() or "embedding" in skill.content.lower()
        
        context = ""
        # Note: Vector search is disabled until API endpoint is implemented
        # if has_vector_search and params.get("query"):
        #     try:
        #         search_results = await api_client.search_user_expertise(params["query"])
        #         if search_results:
        #             context = f"\n\nVector search results:\n{search_results}"
        #     except Exception as e:
        #         print(f"   Vector search failed: {e}")
        
        prompt = f"""You are Roo, executing the "{skill.name}" skill.

Skill description: {skill.description}

Skill instructions:
{skill.content}

User's original request: "{text}"
Extracted parameters: {params}
Requesting user ID: {user_id}
{context}

Previous Conversation Context (if any):
{history if history else 'None'}

Follow the skill instructions to generate an appropriate response.
Be helpful, friendly, and use casual Australian expressions occasionally.
Keep the response concise but informative."""

        response = await self.complete([
            {"role": "system", "content": "You are Roo, a friendly AI assistant for the MLAI community."},
            {"role": "user", "content": prompt}
        ])
        
        return response.content


