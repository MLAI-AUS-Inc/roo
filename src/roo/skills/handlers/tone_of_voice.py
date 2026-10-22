"""Tone-of-voice prompt skill; no backend actions."""
from __future__ import annotations
from ..contracts import SkillRequest
from .prompt import PromptExecutor

class ToneOfVoiceHandler(PromptExecutor):
    async def execute(self, request: SkillRequest):
        return await self._execute_tone_of_voice(request.skill, request.text, request.params, request.user_id)

    async def _execute_tone_of_voice(self, skill, text, params, user_id):
        """Execute the tone-of-voice skill with a dedicated prompt structure."""

        # Extract the user's original text to rewrite
        # Strip common prefixes so the LLM gets just the raw content
        raw_text = params.get("text", text)

        system_prompt = skill.content

        user_prompt = f"""Here is the original text to rewrite. Follow these steps:

    1. First, identify the key points and core message in the original text.
    2. Then COMPLETELY rewrite it from scratch using the MLAI tone of voice described in your instructions. Do not lightly edit or rephrase. Write it fresh as if you were the MLAI writer producing this content for the first time.
    3. Before returning your result, review it against these HARD RULES and fix any violations:
       - ZERO em dash characters (\u2014) or en dash characters (\u2013). Use a comma, period, or hyphen instead.
       - ZERO emoji characters of any kind.
       - ZERO corporate filler language.
       - Short paragraphs, punchy lines, high specificity.

    Return ONLY the final rewritten text. No preamble, no explanation.

    Original text:
    {raw_text}"""

        # Use GPT-5.2 with thinking mode for higher quality tone rewrites
        response = await self.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            provider="openai",
            model="gpt-5.2",
            max_tokens=8192,
            reasoning_effort="high"
        )

        return response.content

