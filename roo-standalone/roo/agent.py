"""
Roo Agent - Core Orchestration Layer

The agent receives user messages, selects appropriate skills,
and executes them to generate responses.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path

from .config import get_settings
from .content_intent import (
    extract_content_factory_delegation,
    extract_domain,
    normalize_slack_text,
)
from .llm import chat
from .skills.loader import Skill, load_skills
from .skills.executor import SkillExecutor
from .slack_client import get_thread_messages


# How long a thread stays "sticky" to the last-used skill. Kept short: one
# misroute pinned a thread for 2 hours before this was reduced (2026-06).
THREAD_CONTEXT_TTL = timedelta(minutes=30)
CONTENT_FACTORY_DELEGATION_ADMIN_SLACK_ID = "U05QPB483K9"


class RooAgent:
    """
    Agentic Slack bot that routes requests to skills.
    
    Usage:
        agent = RooAgent()
        result = await agent.handle_mention(
            text="Do you know anyone in AI research?",
            user_id="U12345",
            channel_id="C12345",
            thread_ts="1234567890.123456"
        )
    """
    
    def __init__(self):
        """Initialize the Roo agent with loaded skills."""
        settings = get_settings()
        skills_dir = Path(settings.SKILLS_DIR)

        self.skills = load_skills(skills_dir)
        self.skill_executor = SkillExecutor()
        self._thread_skill_context: Dict[str, Dict[str, Any]] = {}

        print(f"🦘 RooAgent initialized with {len(self.skills)} skills:")
        for skill in self.skills:
            print(f"   - {skill.name}: {skill.description}")
    
    async def handle_mention(
        self,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        param_overrides: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Handle an @Roo mention from Slack.
        
        Args:
            text: The message text (with @Roo mention removed)
            user_id: Slack user ID of the requester
            channel_id: Channel where the mention occurred
            thread_ts: Thread timestamp for replies
            **kwargs: Additional context
        
        Returns:
            Dict with 'message', 'skill_used', and optional 'data'
        """
        # Clean the message
        routing_started_at = time.monotonic()
        clean_text = self._clean_mention(text)
        thread_context = self._get_thread_context(channel_id, thread_ts)
        stripped_text, delegation = extract_content_factory_delegation(clean_text)
        requested_by_slack_user_id = user_id
        effective_slack_user_id = user_id

        if delegation:
            delegated_target = str(delegation.get("effective_slack_user_id") or "").strip()
            if delegated_target and delegated_target != user_id:
                if user_id != CONTENT_FACTORY_DELEGATION_ADMIN_SLACK_ID:
                    return {
                        "message": "Only <@U05QPB483K9> can run Content Factory as another user.",
                        "skill_used": "content-factory",
                        "data": None,
                    }
                effective_slack_user_id = delegated_target
            clean_text = stripped_text
        elif (
            thread_context
            and thread_context.get("skill_name") == "content-factory"
            and str(thread_context.get("requested_by_slack_user_id") or "").strip() == user_id
        ):
            sticky_effective_slack_user_id = str(
                thread_context.get("effective_slack_user_id") or ""
            ).strip()
            sticky_requested_by_slack_user_id = str(
                thread_context.get("requested_by_slack_user_id") or ""
            ).strip()
            if sticky_requested_by_slack_user_id:
                requested_by_slack_user_id = sticky_requested_by_slack_user_id
            if sticky_effective_slack_user_id:
                effective_slack_user_id = sticky_effective_slack_user_id

        is_delegated_content_factory_request = (
            requested_by_slack_user_id != effective_slack_user_id
        )

        print(f"🔍 Processing: {clean_text[:100]}...")
        
        # 0. Fetch Thread Context (if available)
        thread_history = []
        if channel_id and thread_ts:
            try:
                # Fetch last 10 messages for context
                raw_history = get_thread_messages(channel=channel_id, thread_ts=thread_ts)
                # Filter to recent ones and simple format
                # We exclude the current message generally, but get_thread_messages returns all.
                # Let's just pass the raw list to the executor/selector to handle filtering if needed.
                thread_history = raw_history[-10:] if raw_history else []
            except Exception as e:
                print(f"⚠️ Failed to fetch thread history: {e}")

        # 1. Try Fast Path (Direct Command Execution)
        fast_result = await self._try_fast_path(clean_text, user_id, channel_id, thread_ts)
        if fast_result:
            print(f"⚡ Fast Path matched!")
            self._log_routing_decision(
                text=clean_text,
                channel_id=channel_id,
                thread_ts=thread_ts,
                layer="fast",
                skill_name="mlai-points",
                action=(fast_result.get("data") or {}).get("action"),
                started_at=routing_started_at,
            )
            return fast_result

        # 2. Route via the LLM tool-calling router over the SKILL.md catalog.
        # (The legacy regex/keyword funnel was deleted in Phase 3 of the
        # routing redesign — see ROUTING_REDESIGN_PLAN.md. Only the exact-match
        # fast path above and delegation parsing remain deterministic.)
        v2_decision = await self._route_v2(
            clean_text, thread_history, channel_id, thread_ts, kwargs.get("event_files")
        )
        if v2_decision.is_clarification:
            self._log_routing_decision(
                text=clean_text,
                channel_id=channel_id,
                thread_ts=thread_ts,
                layer="v2-clarify",
                skill_name=None,
                started_at=routing_started_at,
            )
            return {
                "message": v2_decision.clarification,
                "skill_used": None,
                "data": {"router": "v2", "clarification": True},
            }

        skill = self._get_skill_by_name(v2_decision.skill) if v2_decision.skill else None
        selection_layer = "v2" if v2_decision.source == "router" else "v2-error"

        self._log_routing_decision(
            text=clean_text,
            channel_id=channel_id,
            thread_ts=thread_ts,
            layer=selection_layer,
            skill_name=skill.name if skill else None,
            action=v2_decision.action,
            params=v2_decision.params,
            started_at=routing_started_at,
        )

        if skill:
            print(f"🎯 Selected skill: {skill.name}")
            self._remember_selected_skill(
                skill.name,
                channel_id,
                thread_ts,
                clean_text,
                workflow=(
                    v2_decision.action if skill.name == "content-factory" else None
                ),
                requested_by_slack_user_id=(
                    requested_by_slack_user_id
                    if skill.name == "content-factory" and is_delegated_content_factory_request
                    else None
                ),
                effective_slack_user_id=(
                    effective_slack_user_id
                    if skill.name == "content-factory" and is_delegated_content_factory_request
                    else None
                ),
            )
            effective_param_overrides = dict(param_overrides or {})
            if v2_decision.skill == skill.name:
                effective_param_overrides = {
                    **self._v2_param_overrides(v2_decision, thread_context),
                    **effective_param_overrides,
                }
            if skill.name == "content-factory":
                effective_param_overrides = {
                    **effective_param_overrides,
                }
                if is_delegated_content_factory_request:
                    effective_param_overrides = {
                        "requested_by_slack_user_id": requested_by_slack_user_id,
                        "effective_slack_user_id": effective_slack_user_id,
                        **effective_param_overrides,
                    }
            execution_kwargs = dict(kwargs)
            execution_thread_history = thread_history
            if skill.name == "linear-meeting-actions":
                from .linear_context import build_linear_slack_context

                settings = get_settings()
                slack_context = await asyncio.to_thread(
                    build_linear_slack_context,
                    text=clean_text,
                    requester_user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    current_message_ts=kwargs.get("current_message_ts"),
                    thread_history=thread_history,
                    workspace_id=kwargs.get("slack_team_id"),
                    event_id=kwargs.get("event_id"),
                    timezone_name=getattr(settings, "TIMEZONE", "Australia/Sydney"),
                    max_messages=int(
                        getattr(settings, "LINEAR_CONTEXT_MAX_MESSAGES", 50) or 50
                    ),
                    lookback_hours=int(
                        getattr(settings, "LINEAR_CONTEXT_LOOKBACK_HOURS", 24) or 24
                    ),
                    max_chars=int(
                        getattr(settings, "LINEAR_CONTEXT_MAX_CHARS", 16000) or 16000
                    ),
                )
                execution_thread_history = slack_context.get("messages") or thread_history
                execution_kwargs["slack_context"] = slack_context
            result = await self.skill_executor.execute(
                skill=skill,
                text=clean_text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                thread_history=execution_thread_history,
                param_overrides=effective_param_overrides or None,
                **execution_kwargs
            )
            return {
                "message": result.message,
                "skill_used": skill.name,
                "data": result.data,
                "blocks": result.blocks,
                "suppress_post": result.suppress_post,
            }
        else:
            print("💬 No skill matched, generating general response")
            response = await self._general_response(clean_text, thread_history)
            return {
                "message": response,
                "skill_used": None,
                "data": None
            }

    def remember_thread_context(
        self,
        skill_name: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        *,
        domain: Optional[str] = None,
        workflow: Optional[str] = None,
        active_job_id: Optional[str] = None,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> None:
        """Persist recent thread routing context so follow-ups stay on the right skill."""
        thread_key = self._thread_key(channel_id, thread_ts)
        if not thread_key or not skill_name:
            return

        existing = self._thread_skill_context.get(thread_key, {})
        self._thread_skill_context[thread_key] = {
            "skill_name": skill_name or existing.get("skill_name"),
            "domain": domain if domain is not None else existing.get("domain"),
            "workflow": workflow if workflow is not None else existing.get("workflow"),
            "active_job_id": (
                active_job_id if active_job_id is not None else existing.get("active_job_id")
            ),
            "requested_by_slack_user_id": (
                requested_by_slack_user_id
                if requested_by_slack_user_id is not None
                else existing.get("requested_by_slack_user_id")
            ),
            "effective_slack_user_id": (
                effective_slack_user_id
                if effective_slack_user_id is not None
                else existing.get("effective_slack_user_id")
            ),
            "updated_at": datetime.now(timezone.utc),
        }

    def _remember_selected_skill(
        self,
        skill_name: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        text: str,
        *,
        workflow: Optional[str] = None,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> None:
        """Store the routing decision as a thread hint for follow-up context.

        The workflow comes straight from the router's decided action — it is a
        hint fed back into the next routing pass, never a routing bypass.
        """
        self.remember_thread_context(
            skill_name,
            channel_id,
            thread_ts,
            domain=self._extract_domain(text),
            workflow=workflow,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )

    def _thread_key(self, channel_id: Optional[str], thread_ts: Optional[str]) -> Optional[str]:
        if not channel_id or not thread_ts:
            return None
        return f"{channel_id}:{thread_ts}"

    def _get_thread_context(
        self,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        thread_key = self._thread_key(channel_id, thread_ts)
        if not thread_key:
            return None

        context = self._thread_skill_context.get(thread_key)
        if not context:
            return None

        updated_at = context.get("updated_at")
        if not updated_at or datetime.now(timezone.utc) - updated_at > THREAD_CONTEXT_TTL:
            self._thread_skill_context.pop(thread_key, None)
            return None

        return context

    def get_thread_context(
        self,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Return remembered thread context for callers outside the agent."""
        return self._get_thread_context(channel_id, thread_ts)

    def _get_skill_by_name(self, skill_name: str) -> Optional[Skill]:
        return next((skill for skill in self.skills if skill.name == skill_name), None)

    def _extract_domain(self, text: str) -> Optional[str]:
        return extract_domain(text)


    def _log_routing_decision(
        self,
        *,
        text: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        layer: Optional[str],
        skill_name: Optional[str],
        action: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        started_at: Optional[float] = None,
    ) -> None:
        """Emit one structured, grep-able log line per routing decision.

        These lines are the raw material for the routing eval set: misroutes
        observed in production get copied into roo/routing_eval/cases/.
        """
        try:
            safe_params = {
                key: value
                for key, value in (params or {}).items()
                if key not in ("requested_by_slack_user_id", "effective_slack_user_id")
            }
            payload = {
                "event": "routing_decision",
                "layer": layer or "none",
                "skill": skill_name,
                "action": action,
                "params": safe_params,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "latency_ms": (
                    round((time.monotonic() - started_at) * 1000)
                    if started_at is not None
                    else None
                ),
                "text": (text or "")[:300],
            }
            print("ROUTING_DECISION " + json.dumps(payload, ensure_ascii=False, default=str))
        except Exception as exc:
            print(f"⚠️ Failed to log routing decision: {exc}")

    def _safe_channel_name(self, channel_id: Optional[str]) -> Optional[str]:
        """Resolve a channel name without ever raising (Slack may be unavailable)."""
        if not channel_id:
            return None
        try:
            from .slack_client import get_channel_name
            return get_channel_name(channel_id)
        except Exception as exc:
            print(f"⚠️ Channel name lookup failed for {channel_id}: {exc}")
            return None


    async def _route_v2(
        self,
        text: str,
        thread_history: Optional[List[dict]],
        channel_id: Optional[str],
        thread_ts: Optional[str],
        event_files: Optional[list] = None,
    ):
        """Run the v2 tool-calling router over the live skill catalog."""
        from . import router as router_v2

        file_names = [
            file.get("name") or file.get("title")
            for file in (event_files or [])
            if isinstance(file, dict)
        ]
        return await router_v2.route(
            text,
            skills=self.skills,
            channel_name=self._safe_channel_name(channel_id),
            thread_history=thread_history,
            thread_hint=self._get_thread_context(channel_id, thread_ts),
            file_names=[name for name in file_names if name] or None,
        )

    def _v2_param_overrides(self, decision, thread_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Decision params -> executor param overrides, with thread post-fill.

        Mirrors what the old intent regexes did for content-factory threads:
        a follow-up inherits the thread's domain/job when the router didn't
        extract them from the message itself.
        """
        params = dict(decision.params or {})
        if decision.action:
            params["action"] = decision.action
        if (
            decision.skill == "content-factory"
            and thread_context
            and thread_context.get("skill_name") == "content-factory"
        ):
            if thread_context.get("domain") and not params.get("domain"):
                params["domain"] = thread_context["domain"]
            if decision.action == "publish_pr" and thread_context.get("active_job_id"):
                params.setdefault("job_id", thread_context["active_job_id"])
        return params


    def _match_fast_path(self, text: str) -> Optional[str]:
        """Match exact-command shortcuts without executing them.

        Returns the points action name, or None. Pure function so the routing
        eval can replay it without hitting the backend.
        """
        text_lower = text.lower().strip()

        # 1. Balance Check: "points", "balance", "my points"
        if re.match(r'^(?:points|balance|my points)$', text_lower):
            return "balance"

        # 2. Earn/Tasks shortcuts
        if re.match(
            r'^(?:points\s+earn|earn\s+points|ways\s+to\s+earn|'
            r'tasks(?:\s+(?:all|mine|review|open))?|'
            r'my\s+tasks|review\s+tasks|open\s+tasks|all\s+tasks)$',
            text_lower,
        ):
            return "list_tasks"

        # 3. Rewards: "points rewards", "rewards"
        if re.match(r'^(?:points\s+rewards|rewards)$', text_lower):
            return "list_rewards"

        # 4. Coworking Book Today: "coworking book today"
        if re.match(r'^coworking\s+book\s+today$', text_lower):
            return "book_coworking"

        # 5. Coworking Cancel: "coworking cancel" (assumes today/upcoming)
        if re.match(r'^coworking\s+cancel$', text_lower):
            return "cancel_coworking"

        return None

    async def _try_fast_path(
        self,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Attempt to execute a direct command without LLM.

        Regex matches specific high-frequency commands.
        """
        action = self._match_fast_path(text)
        if action is None:
            return None

        if action == "balance":
            return await self._execute_fast_points(user_id, "balance")

        if action == "list_tasks":
            return await self._execute_fast_points(
                user_id,
                "list_tasks",
                text=text,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        if action == "list_rewards":
            return await self._execute_fast_points(user_id, "list_rewards")

        if action == "book_coworking":
            today = self._get_today().isoformat()
            return await self._execute_fast_points(
                user_id, "book_coworking",
                date=today, channel_id=channel_id, thread_ts=thread_ts
            )

        if action == "cancel_coworking":
            today = self._get_today().isoformat()
            return await self._execute_fast_points(
                user_id, "cancel_coworking",
                date=today
            )

        return None

    def _get_today(self):
        """Get today's date respecting the configured timezone."""
        from roo.utils import get_current_date
        return get_current_date()

    async def _execute_fast_points(self, user_id: str, action: str, **kwargs) -> Dict[str, Any]:
        """Execute a Points action directly."""
        # Find the skill to get the client class
        skill = next((s for s in self.skills if s.name == "mlai-points"), None)
        if not skill:
            return None
            
        ClientClass = skill.get_client_class("MLAIBackendClient")
        if not ClientClass:
            return None
            
        try:
            settings = get_settings()
            client = ClientClass(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
                internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
            )
            
            # Re-use the executor's logic for response formatting to DRY
            # We need to instantiate the executor just to access the helper method
            # Note: This relies on _handle_points_action being available/public-ish
            # Since it's protected, we might duplicate simple formatting here for speed/isolation
            
            if action == "balance":
                data = await client.get_balance(user_id)
                msg = self.skill_executor._format_points_balance_summary(
                    data,
                    tasks_command="@Roo tasks",
                )
                
            elif action == "list_tasks":
                msg = await self.skill_executor._handle_points_action(
                    client=client,
                    action="list_tasks",
                    params={},
                    text=kwargs.get("text", "tasks"),
                    user_id=user_id,
                    channel_id=kwargs.get("channel_id"),
                    thread_ts=kwargs.get("thread_ts"),
                    skill=skill,
                )
            
            elif action == "list_rewards":
                rewards = await client.list_rewards(user_id)
                balance_summary = await self.skill_executor._get_points_balance_summary_for_rewards(
                    client,
                    user_id,
                )
                msg = self.skill_executor._format_rewards_catalog(
                    rewards,
                    balance_summary=balance_summary,
                )
            
            elif action == "book_coworking":
                booking_date = kwargs.get("date")
                msg = await self.skill_executor._handle_points_action(
                    client=client,
                    action="book_coworking",
                    params={"date": booking_date},
                    text=f"coworking book {booking_date}",
                    user_id=user_id,
                    channel_id=kwargs.get("channel_id"),
                    thread_ts=kwargs.get("thread_ts"),
                    skill=skill,
                )
                
            elif action == "cancel_coworking":
                booking_date = kwargs.get("date")
                res = await client.cancel_coworking(user_id, booking_date=booking_date)
                ref = res.get("refund_amount", 0)
                msg = f"No worries, cancelled your booking for {booking_date}. Refunded {ref} points."
                
            else:
                msg = "Unknown fast action."

            return {
                "message": msg,
                "skill_used": "mlai-points (fast)",
                "data": {"action": action}
            }
            
        except Exception as e:
            print(f"❌ Fast path error: {e}")
            # Fallback to normal flow if fast path fails? Or just return error?
            # Return None to let LLM try? No, if we matched regex, we should probably fail gracefully here.
            return {
                "message": "Sorry mate, having trouble connecting to the points system right now. Try again in a tic!",
                "skill_used": "mlai-points (fast-error)",
                "data": {"error": str(e)}
            }

    def _clean_mention(self, text: str) -> str:
        """Remove only Roo's @mention, preserving other user mentions.
        
        Gets Roo's bot user ID dynamically and removes only that mention,
        regardless of where it appears in the message.
        """
        import re
        from .slack_client import get_bot_user_id
        
        try:
            bot_id = get_bot_user_id()
            # Only remove Roo's specific mention, preserve all others
            cleaned = re.sub(rf'<@{bot_id}>', '', text)
        except Exception:
            # Fallback: remove first mention if we can't get bot ID
            cleaned = re.sub(r'<@[A-Z0-9]+>', '', text, count=1)
        
        cleaned = normalize_slack_text(cleaned)
        # Remove extra whitespace
        cleaned = ' '.join(cleaned.split())
        return cleaned.strip()
    


    
    async def _general_response(self, text: str, history: List[dict] = None) -> str:
        """Generate a general conversational response."""
        # Format history
        history_context = ""
        if history:
            history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in history[:-1]])
            history_context = f"\nRecent Context:\n{history_str}\n"

        skill_lines = []
        for s in self.skills:
            line = f"- {s.name}: {s.description}"
            if s.exclusive_channels:
                channels = ", ".join(f"#{channel}" for channel in s.exclusive_channels)
                line += f" (only available in {channels})"
            skill_lines.append(line)
        skill_list = "\n".join(skill_lines)
        prompt = f"""You are Roo, the friendly AI assistant for the MLAI community.

Your personality:
- Warm and approachable, like a helpful local
- Use casual Australian expressions occasionally (mate, no worries, etc.)
- Helpful and encouraging
- Keep responses concise but friendly

Your Capabilities / Skills:
{skill_list}

If the user asks "what can you do?" or "what are you?", summarize your role and list your skills in a friendly, conversational way. Don't just dump the raw list, explain it naturally.
If the user asks for something a channel-restricted skill does, point them to that skill's home channel instead of attempting it here.

{history_context}
Respond to the user's message in a helpful, conversational way."""

        try:
            response = await chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ])
            return response.content
        except Exception as e:
            print(f"❌ General response failed: {e}")
            return "G'day! Sorry, I'm having a bit of trouble at the moment. Mind trying again? 🦘"


# Singleton agent instance
_agent: Optional[RooAgent] = None


def get_agent() -> RooAgent:
    """Get or create the singleton Roo agent."""
    global _agent
    if _agent is None:
        _agent = RooAgent()
    return _agent
