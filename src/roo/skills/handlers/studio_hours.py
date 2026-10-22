from __future__ import annotations
import asyncio
from ...config import get_settings
from ...backend_identity import get_backend_actor_context
from ..contracts import SkillRequest

class StudioHoursHandler:
    report_kind = None

    async def execute(self, request: SkillRequest):
        skill, params = request.skill, request.params
        user_id, channel_id, thread_ts = request.user_id, request.channel_id, request.thread_ts
        from ...studio_report_commands import enqueue as enqueue_studio_report

        # The handler fixes the report capability. Model parameters
        # may select a period/client, never another report kind.
        report_options = ({'report_kind': self.report_kind}
                          if self.report_kind else {})
        message = await asyncio.to_thread(
            enqueue_studio_report, get_settings(), get_backend_actor_context(), params,
            user_id=user_id, channel_id=channel_id, thread_ts=thread_ts,
            **report_options,
        )
        result = message
        return result

