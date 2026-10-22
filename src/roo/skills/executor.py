"""Skill dispatch and result normalization. Domain logic lives in executors/."""
from __future__ import annotations
from typing import Optional, List
from .loader import Skill
from .contracts import SkillRequest, SkillResult
from .registry import EXECUTORS

class SkillExecutor:
    def __init__(self, executors=None):
        self.executors = (dict(executors) if executors is not None else
                         {name: executor_class() for name, executor_class in EXECUTORS.items()})

    def owner_of(self, method: str):
        """Resolve legacy callback helpers during incremental caller migration."""
        for executor in self.executors.values():
            if hasattr(executor, method):
                return executor
        raise AttributeError(method)

    def __getattr__(self, name):
        # Existing Slack callbacks still call domain helpers through this facade.
        # Executors are independent: they never call back into the facade.
        if name.startswith("_") or name == "complete_meeting_room_room_choice":
            return getattr(self.owner_of(name), name)
        raise AttributeError(name)

    async def execute(
        self,
        skill: Skill,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
        param_overrides: Optional[dict] = None,
        **kwargs
    ) -> SkillResult:
        """
        Execute a skill with the given context.
        
        Args:
            skill: The skill to execute
            text: User's message
            user_id: Slack user ID
            channel_id: Channel ID
            thread_ts: Thread timestamp
            **kwargs: Additional context
        
        Returns:
            SkillResult with message and optional data
        """
        print(f"🎯 Executing skill: {skill.name}")
        
        try:
            # Router v2 supplies structured params (action + declared fields);
            # interactive callbacks pass explicit param_overrides. The free-form
            # LLM parameter-extraction call was removed in Phase 4 of the
            # routing redesign — executors parse remaining details from the text.
            params = dict(param_overrides or {})
            if skill.name == "studio-client-balance":
                params = {key: value for key, value in params.items()
                          if key in {"action", "month", "months", "client"}}
            if params:
                print(f"   Routed params: {params}")
            
            # Check for skill-specific implementation
            executor = self.executors.get(skill.name)
            if executor is None:
                return SkillResult(
                    success=False,
                    message="This skill has no registered executor and is unavailable.",
                    error="unregistered_skill_handler",
                )
            result = await executor.execute(SkillRequest(
                skill=skill, text=text, params=params, user_id=user_id,
                channel_id=channel_id, thread_ts=thread_ts,
                thread_history=thread_history, metadata=kwargs,
            ))
            
            # Skill executors can return a dict with "message" + "blocks" for rich responses
            blocks = None
            result_data = params
            suppress_post = False
            if isinstance(result, dict) and "message" in result:
                blocks = result.get("blocks")
                result_data = result.get("data", params)
                suppress_post = bool(result.get("suppress_post", False))
                result = result["message"]

            return SkillResult(
                success=True,
                message=result,
                data=result_data,
                blocks=blocks,
                suppress_post=suppress_post
            )
            
        except Exception as e:
            print(f"❌ Skill execution failed: {e}")
            import traceback
            traceback.print_exc()
            
            return SkillResult(
                success=False,
                message="Sorry, I ran into a problem executing that skill. Can you try again?",
                error=str(e)
            )
