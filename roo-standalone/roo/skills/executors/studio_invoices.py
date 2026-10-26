from __future__ import annotations
import asyncio
from ..contracts import SkillRequest, SkillExecutorInterface

class StudioInvoicesExecutor(SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        skill, params = request.skill, request.params
        user_id, channel_id, thread_ts = request.user_id, request.channel_id, request.thread_ts
        from ...invoice_commands import HELP
        result = HELP  # Exact signed ingress owns generation; model params grant no authority.
        return result

