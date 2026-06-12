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
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

from .config import get_settings
from .content_intent import (
    extract_content_factory_delegation,
    extract_domain,
    normalize_slack_text,
    parse_routing_intent,
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
        routing_intent = self._get_routing_intent(clean_text, thread_context)
        
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
        has_prior_thread_context = self._has_prior_slack_thread_context(
            thread_history,
            current_message_ts=kwargs.get("current_message_ts"),
        )

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

        # 2. Select appropriate skill
        router_mode = self._router_v2_mode()
        v2_decision = None

        if router_mode == "on":
            # Router v2 decides (fast path and delegation parsing stayed in front).
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
            if v2_decision.source == "error":
                # Provider/transport failure — fall back to the legacy funnel
                # while it still exists (once Phase 3 removes it, this becomes
                # general chat). Keeps the bot useful through LLM outages.
                print("⚠️ Router v2 errored; falling back to legacy routing")
                if routing_intent:
                    skill, selection_layer = routing_intent["skill"], "v2-error-intent-regex"
                else:
                    skill, fallback_layer = await self._select_skill_detail(
                        clean_text,
                        thread_history,
                        channel_id,
                        thread_ts,
                        has_file_context=bool(kwargs.get("event_files")),
                        has_thread_context=has_prior_thread_context,
                    )
                    selection_layer = f"v2-error-{fallback_layer or 'none'}"
                v2_decision = None  # legacy params/overrides apply downstream
            else:
                skill = self._get_skill_by_name(v2_decision.skill) if v2_decision.skill else None
                selection_layer = "v2"
        elif routing_intent:
            skill, selection_layer = routing_intent["skill"], "intent-regex"
        else:
            skill, selection_layer = await self._select_skill_detail(
                clean_text,
                thread_history,
                channel_id,
                thread_ts,
                has_file_context=bool(kwargs.get("event_files")),
                has_thread_context=has_prior_thread_context,
            )

        if router_mode == "shadow":
            try:
                asyncio.create_task(
                    self._shadow_route_v2(
                        clean_text,
                        thread_history,
                        channel_id,
                        thread_ts,
                        kwargs.get("event_files"),
                        skill.name if skill else None,
                        selection_layer,
                    )
                )
            except RuntimeError:
                pass  # no running event loop (sync contexts)

        log_action = (routing_intent or {}).get("params", {}).get("action")
        log_params = (routing_intent or {}).get("params")
        if v2_decision is not None:
            log_action = v2_decision.action
            log_params = v2_decision.params
        self._log_routing_decision(
            text=clean_text,
            channel_id=channel_id,
            thread_ts=thread_ts,
            layer=selection_layer,
            skill_name=skill.name if skill else None,
            action=log_action,
            params=log_params,
            started_at=routing_started_at,
        )

        if skill:
            print(f"🎯 Selected skill: {skill.name}")
            self._remember_selected_skill(
                skill.name,
                channel_id,
                thread_ts,
                clean_text,
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
            if v2_decision is not None and v2_decision.skill == skill.name:
                effective_param_overrides = {
                    **self._v2_param_overrides(v2_decision, thread_context),
                    **effective_param_overrides,
                }
            elif routing_intent and skill.name == routing_intent["skill"].name:
                effective_param_overrides = {
                    **routing_intent.get("params", {}),
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
            result = await self.skill_executor.execute(
                skill=skill,
                text=clean_text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                thread_history=thread_history,
                param_overrides=effective_param_overrides or None,
                **kwargs
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
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> None:
        workflow = None
        text_lower = text.lower()

        if skill_name == "content-factory":
            if any(term in text_lower for term in ("scan", "codebase", "repository", "repo")):
                workflow = "scan"
            elif any(term in text_lower for term in ("scaffold", "articles directory", "blog page")):
                workflow = "scaffold"
            elif any(term in text_lower for term in ("research", "keyword", "topic")):
                workflow = "research"
            elif any(term in text_lower for term in ("write", "article", "blog")):
                workflow = "write"

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

    def _get_routing_intent(
        self,
        text: str,
        thread_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        route = parse_routing_intent(
            text,
            thread_skill_name=(thread_context or {}).get("skill_name"),
            thread_domain=(thread_context or {}).get("domain"),
            thread_job_id=(thread_context or {}).get("active_job_id"),
        )
        if not route:
            return None

        skill = self._get_skill_by_name(route["skill_name"])
        if not skill:
            return None

        return {
            "skill": skill,
            "params": dict(route.get("params") or {}),
        }

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

    def _router_v2_mode(self) -> str:
        """Read ROUTER_V2 without ever raising (tests run without env)."""
        try:
            return str(getattr(get_settings(), "ROUTER_V2", "off") or "off").strip().lower()
        except Exception:
            return "off"

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

    async def _shadow_route_v2(
        self,
        text: str,
        thread_history: Optional[List[dict]],
        channel_id: Optional[str],
        thread_ts: Optional[str],
        event_files: Optional[list],
        v1_skill: Optional[str],
        v1_layer: Optional[str],
    ) -> None:
        """Shadow mode: run v2 after the fact and log (dis)agreement. Never raises."""
        try:
            decision = await self._route_v2(text, thread_history, channel_id, thread_ts, event_files)
            v2_skill = decision.skill
            payload = {
                "event": "routing_decision_v2",
                "mode": "shadow",
                "skill": v2_skill,
                "action": decision.action,
                "params": decision.params,
                "clarification": decision.clarification,
                "source": decision.source,
                "v1_skill": v1_skill,
                "v1_layer": v1_layer,
                "disagree": (v2_skill or None) != (v1_skill or None),
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "text": (text or "")[:300],
            }
            print("ROUTING_DECISION_V2 " + json.dumps(payload, ensure_ascii=False, default=str))
        except Exception as exc:
            print(f"⚠️ Shadow router v2 failed: {exc}")

    def _looks_like_content_request(self, text: str) -> bool:
        # Verb + object only. Bare nouns ("article", "blog", "topic", "keyword")
        # fire on vocabulary, not intent — "summarise this article" and "what's
        # the topic for the meetup?" must NOT land here.
        patterns = (
            r'\b(?:write|draft|generate|create)\b.*\b(article|blog(?:\s+post)?|content)\b',
            r'\bresearch\b(?!\s+papers?\b).*\b(article|topic|keyword|blog(?:\s+post)?)\b',
            r'\b(?:article|blog)\s+(?:idea|topic|draft)s?\b',
            r'\bseo\b',
            r'\bfor my domain\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _looks_like_points_request(self, text: str) -> bool:
        text = self._normalize_points_routing_text(text)
        patterns = (
            r'\bpoints?\b',
            r'\btop\s*up\b',
            r'\btopup\b',
            r'\btop-up\b',
            r'\bbalance\b',
            r'\bcoworking\b',
            r'\boffice\b.*\b(?:attendance|attended|usage|used|report|summary)\b',
            r'\b(?:attendance|attended|usage|used)\b.*\boffice\b',
            r'\bbook\s+me\s+in\b',
            r'\bbook\b.*<@[a-z0-9]+>.*\bin\b',
            r'\bcheck\b.*<@[a-z0-9]+>.*\bin\b',
            r'\brewards?\b',
            r'\bclaim\s+task\b',
            r'\bcreate\s+(?:a\s+)?task\b',
            r'\btask\s+create\b',
            r'\bworth\s+\d+\s+points?\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _normalize_points_routing_text(self, text: str) -> str:
        """Normalize common typos before points-skill routing checks."""
        text_lower = str(text or "").lower()
        replacements = {
            "coworkign": "coworking",
            "cowokrking": "coworking",
            "cowokring": "coworking",
            "co working": "coworking",
            "co-working": "coworking",
        }
        for typo, replacement in replacements.items():
            text_lower = text_lower.replace(typo, replacement)
        return text_lower

    def _looks_like_luma_request(self, text: str) -> bool:
        patterns = (
            r'\bluma\b',
            r'\battendees?\b',
            r'\bguest\s+lists?\b',
            r'\bguests?\b.*\bcsv\b',
            r'\bcsv\b.*\bguests?\b',
            r'\bcsv\b.*\bmlai\s+events?\b',
            r'\bmlai\s+events?\b.*\bcsv\b',
            r'\bpast\s+csv\s+documents?\b',
            r'\bregistered\b.*\bevents?\b',
            r'\bregistrations?\b.*\bevents?\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _has_prior_slack_thread_context(
        self,
        history: Optional[List[dict]],
        current_message_ts: Optional[str] = None,
    ) -> bool:
        current_ts = str(current_message_ts or "").strip()
        for message in history or []:
            if message.get("is_bot") or message.get("bot_id"):
                continue
            if current_ts and str(message.get("ts") or "").strip() == current_ts:
                continue
            if str(message.get("text") or "").strip() or message.get("files"):
                return True
        return False

    def _looks_like_linear_thread_reference_request(self, text: str) -> bool:
        if not re.search(r'\blinear\b', text):
            return False
        reference = r'(?:this|that|above|thread|conversation|message|discussion)'
        return bool(
            re.search(rf'\b(?:add|put|send|sync|create)\b.*\b{reference}\b.*\blinear\b', text)
            or re.search(rf'\blinear\b.*\b(?:add|put|send|sync|create)\b.*\b{reference}\b', text)
        )

    def _looks_like_linear_meeting_request(
        self,
        text: str,
        has_file_context: bool = False,
        has_thread_context: bool = False,
    ) -> bool:
        has_linear = bool(re.search(r'\blinear\b', text))
        has_meeting_source = bool(
            re.search(
                r'\b(meeting|transcript|summary|notes?|action\s+items?|to-?dos?|file|pdf|docx?|document|image|screenshot)\b',
                text,
            )
        ) or has_file_context
        has_creation_intent = bool(
            re.search(r'\b(extract|sync|turn|send|put|create|add|do|write|post|generate|summari[sz]e|tickets?|issues?|tasks?)\b', text)
        )
        if self._looks_like_linear_direct_issue_request(text):
            return True
        if self._looks_like_linear_project_update_request(text, has_file_context):
            return True
        if has_linear and has_meeting_source and has_creation_intent:
            return True
        return (
            has_creation_intent
            and (has_file_context or has_thread_context)
            and self._looks_like_linear_thread_reference_request(text)
        )

    def _looks_like_linear_project_update_request(self, text: str, has_file_context: bool = False) -> bool:
        if not re.search(r'\bproject\s+updates?\b', text):
            return False
        has_update_intent = bool(
            re.search(r'\b(create|do|write|post|generate|draft|summari[sz]e|make)\b', text)
        )
        has_source_context = has_file_context or bool(
            re.search(r'\b(linear|meeting|transcript|summary|notes?|file|pdf|docx?|document|image|screenshot)\b', text)
        )
        return has_update_intent and has_source_context

    def _looks_like_linear_direct_issue_request(self, text: str) -> bool:
        if not re.search(r'\blinear\b', text):
            return False
        if re.search(r'\b(points?|rewards?|coworking|allowance|worth\s+\d+\s+points?)\b', text):
            return False
        if re.search(r'\bproject\s+updates?\b', text):
            return False
        has_creation_intent = bool(re.search(r'\b(create|add|open|file|make)\b', text))
        has_issue_noun = bool(
            re.search(r'\b(?:to\s*do\s+items?|todo\s+items?|tasks?|issues?|tickets?)\b', text)
        )
        has_linear_project = bool(
            re.search(
                r'\blinear\s+project\b|\bproject\s+(?:called|named)\b|'
                r'\blinear\s+(?:tasks?|issues?|tickets?|to\s*do\s+items?)\s+(?:in|to|under)\b',
                text,
            )
        )
        return has_creation_intent and has_issue_noun and has_linear_project

    def _looks_like_data_query_request(self, text: str) -> bool:
        if re.search(r'\b(?:data|database|db)\s+(?:catalog|resources?|tables?|schema)\b', text):
            return True
        if re.search(r'\b(?:what|which|show|list)\b.*\b(?:tables?|resources?)\b.*\b(?:query|available|access)\b', text):
            return True

        has_query_intent = bool(
            re.search(
                r'\b(?:how\s+many|count|show|list|which|what|query|find|search|give\s+me|display|report)\b',
                text,
            )
        )
        if not has_query_intent:
            return False

        data_subject_patterns = (
            r'\bvibe\s*raising\b',
            r'\bstartup\s+(?:updates?|drafts?|profiles?|bindings?|metrics?|events?)\b',
            r'\bmonthly\s+update\s+drafts?\b',
            r'\bupdates?\s+drafts?\b',
            r'\bcontent\s+factory\s+(?:jobs?|runs?|steps?|attempts?|articles?)\b',
            r'\blinear\s+(?:issues?|projects?|project\s+updates?)\b',
            r'\bgmail\s+(?:messages?|threads?|attachments?)\b',
            r'\bslack\s+(?:messages?|threads?|channel\s+selections?)\b',
            r'\bgithub\s+integrations?\b',
            r'\bfinancial\s+(?:records?|accounts?)\b',
            r'\borganizations?\b',
            r'\bstartup\s+data\b',
        )
        return any(re.search(pattern, text) for pattern in data_subject_patterns)

    def _looks_like_content_follow_up(self, text: str) -> bool:
        patterns = (
            r'\bwrite\b',
            r'\bresearch\b',
            r'\barticle\b',
            r'\bblog\b',
            r'\bkeyword\b',
            r'\btopic\b',
            r'\bdraft\b',
            r'\boutline\b',
            r'\bfor my domain\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _skill_available_in_channel(self, skill: Skill, channel_name: Optional[str]) -> bool:
        """Channel scoping as a routing constraint.

        A skill restricted to exclusive_channels must not capture messages from
        other channels (its keywords used to grab e.g. "patient"/"announcement"
        workspace-wide, then the executor refused to run). Mirrors the executor
        rule: an unknown channel (None) is allowed through.
        """
        if not skill.exclusive_channels:
            return True
        if not channel_name:
            return True
        return channel_name in skill.exclusive_channels

    def _keyword_matches(self, text: str, keyword: str) -> bool:
        keyword = keyword.lower().strip()
        if not keyword:
            return False

        escaped = re.escape(keyword)
        pattern = rf'(?<!\w){escaped}(?!\w)'
        return re.search(pattern, text) is not None

    def _select_skill_from_triggers(
        self,
        text: str,
        thread_context: Optional[Dict[str, Any]] = None,
        has_file_context: bool = False,
        has_thread_context: bool = False,
        channel_name: Optional[str] = None,
    ) -> Optional[Skill]:
        skill, _layer = self._select_skill_from_triggers_detail(
            text,
            thread_context,
            has_file_context=has_file_context,
            has_thread_context=has_thread_context,
            channel_name=channel_name,
        )
        return skill

    def _select_skill_from_triggers_detail(
        self,
        text: str,
        thread_context: Optional[Dict[str, Any]] = None,
        has_file_context: bool = False,
        has_thread_context: bool = False,
        channel_name: Optional[str] = None,
    ) -> Tuple[Optional[Skill], Optional[str]]:
        """Deterministic (pre-LLM) skill selection, reporting which layer decided."""
        routing_intent = self._get_routing_intent(text, thread_context)
        if routing_intent:
            return routing_intent["skill"], "intent-regex"

        text_lower = text.lower().strip()
        content_skill = self._get_skill_by_name("content-factory")
        luma_skill = self._get_skill_by_name("luma-events")
        points_skill = self._get_skill_by_name("mlai-points")
        linear_meeting_skill = self._get_skill_by_name("linear-meeting-actions")
        data_query_skill = self._get_skill_by_name("mlai-data-query")

        if luma_skill and self._looks_like_luma_request(text_lower):
            return luma_skill, "looks-like-luma"

        if content_skill and self._looks_like_content_request(text_lower):
            return content_skill, "looks-like-content"

        if points_skill and self._looks_like_points_request(text_lower):
            return points_skill, "looks-like-points"

        if linear_meeting_skill and self._looks_like_linear_meeting_request(
            text_lower,
            has_file_context,
            has_thread_context,
        ):
            return linear_meeting_skill, "looks-like-linear"

        if data_query_skill and self._looks_like_data_query_request(text_lower):
            return data_query_skill, "looks-like-data-query"

        if (
            thread_context
            and thread_context.get("skill_name") == "content-factory"
            and content_skill
            and self._looks_like_content_follow_up(text_lower)
            and not self._looks_like_points_request(text_lower)
        ):
            return content_skill, "content-follow-up"

        skill_scores: Dict[str, int] = {}
        for skill in self.skills:
            if not self._skill_available_in_channel(skill, channel_name):
                continue
            matched_keywords = [
                keyword for keyword in skill.trigger_keywords
                if self._keyword_matches(text_lower, keyword)
            ]
            if matched_keywords:
                skill_scores[skill.name] = sum(len(keyword.split()) * 3 + len(keyword) for keyword in matched_keywords)

        if not skill_scores:
            return None, None

        ranked = sorted(skill_scores.items(), key=lambda item: item[1], reverse=True)
        best_skill_name, best_score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else -1

        if len(ranked) == 1 or best_score >= runner_up_score + 4:
            return self._get_skill_by_name(best_skill_name), "keywords"

        return None, "keyword-tie"

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
    
    async def _select_skill(
        self,
        text: str,
        history: List[dict] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        has_file_context: bool = False,
        has_thread_context: bool = False,
    ) -> Optional[Skill]:
        """Use LLM to decide which skill to use."""
        skill, _layer = await self._select_skill_detail(
            text,
            history,
            channel_id,
            thread_ts,
            has_file_context=has_file_context,
            has_thread_context=has_thread_context,
        )
        return skill

    async def _select_skill_detail(
        self,
        text: str,
        history: List[dict] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        has_file_context: bool = False,
        has_thread_context: bool = False,
        channel_name: Optional[str] = None,
    ) -> Tuple[Optional[Skill], Optional[str]]:
        """Select a skill, reporting which layer decided ("intent-regex", "looks-like-*",
        "keywords", "llm", …). `channel_name` may be passed pre-resolved (eval harness);
        otherwise it is resolved from `channel_id` without ever raising."""
        if not self.skills:
            return None, "no-skills"

        thread_context = self._get_thread_context(channel_id, thread_ts)
        routing_intent = self._get_routing_intent(text, thread_context)
        if routing_intent:
            return routing_intent["skill"], "intent-regex"

        if channel_name is None:
            channel_name = self._safe_channel_name(channel_id)

        has_slack_files = has_file_context or any(message.get("files") for message in history or [])
        trigger_skill, trigger_layer = self._select_skill_from_triggers_detail(
            text,
            thread_context,
            has_file_context=has_slack_files,
            has_thread_context=has_thread_context,
            channel_name=channel_name,
        )
        if trigger_skill:
            return trigger_skill, trigger_layer

        llm_skill = await self._llm_select_skill(
            text,
            history,
            channel_name=channel_name,
            thread_context=thread_context,
        )
        if llm_skill:
            return llm_skill, "llm"
        return None, "llm-none"

    # Routing examples shown to the LLM fallback, keyed by skill name so that
    # skills filtered out (channel scoping / not loaded) drop their examples too.
    # Interim home until Phase 2 moves routing examples into SKILL.md frontmatter.
    LLM_ROUTER_EXAMPLES: Dict[str, Tuple[str, ...]] = {
        "content-factory": (
            '"please research the best article for me to write" -> content-factory',
            '"scan the repo for the domain woofya.com.au" -> content-factory',
            '"write me an article about how to build an ai agent harness" -> content-factory',
            '"set up a blog section on my site" -> content-factory',
        ),
        "github-integration": (
            '"reconnect github for woofya.com.au" -> github-integration',
            '"I need to reauthorise github" -> github-integration',
        ),
        "linear-meeting-actions": (
            '"turn this meeting summary into Linear tasks" -> linear-meeting-actions',
            '"extract action items from this transcript and add them to Linear" -> linear-meeting-actions',
            '"send this attached PDF to Linear as tasks" -> linear-meeting-actions',
            '"add a task to linear to fix the login bug" -> linear-meeting-actions',
        ),
        "mlai-points": (
            '"create a task called fix docs worth 5 points" -> mlai-points',
            '"what tasks are open?" -> mlai-points',
            '"how do I earn points?" -> mlai-points',
            '"I\'d like to claim the docs task" -> mlai-points',
        ),
        "luma-events": (
            '"give me CSVs for the past 3 MLAI events" -> luma-events',
            '"how many people registered for the april 29 event" -> luma-events',
            '"who\'s coming to the patient-data workshop?" -> luma-events',
            '"how many signed up for thursday\'s event?" -> luma-events',
        ),
        "mlai-data-query": (
            '"How many Vibe Raising companies do we have?" -> mlai-data-query',
            '"What data resources can Roo query?" -> mlai-data-query',
            '"Which Content Factory jobs failed?" -> mlai-data-query',
        ),
        "connect-users": (
            '"do you know anyone in AI research?" -> connect-users',
            '"anyone in the community working with medical imaging?" -> connect-users',
            '"connect me with someone who writes blog content" -> connect-users',
            '"looking for a mentor in data engineering, any suggestions?" -> connect-users',
        ),
        "tone-of-voice": (
            '"rewrite this announcement in our tone of voice" -> tone-of-voice',
            '"can you make this sound more like mlai?" -> tone-of-voice',
        ),
        "medhack": (
            '"give me a clinical case to diagnose" -> medhack',
            '"what time does medhack kick off on saturday?" -> medhack',
        ),
        "watt-the-hack": (
            '"announce that judging starts at 5pm" -> watt-the-hack',
            '"post an announcement: pizza in the atrium" -> watt-the-hack',
        ),
        "none": (
            '"can you summarise this article for me?" -> none',
            '"what did you think of the blog post I shared yesterday?" -> none',
            '"what\'s the topic for this week\'s meetup?" -> none',
            '"research the best time to post on linkedin" -> none',
            '"please analyse this project proposal" -> none',
        ),
    }

    async def _llm_select_skill(
        self,
        text: str,
        history: List[dict] = None,
        channel_name: Optional[str] = None,
        thread_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Skill]:
        """LLM fallback classification over the skill catalog."""
        # Channel scoping: skills locked to other channels are not candidates.
        available_skills = [
            skill for skill in self.skills
            if self._skill_available_in_channel(skill, channel_name)
        ]
        if not available_skills:
            return None

        channel_hint = f"\nChannel: #{channel_name}\n" if channel_name else ""
        channel_priority_hint = ""
        if channel_name:
            for skill in available_skills:
                if channel_name in skill.priority_channels:
                    channel_priority_hint = (
                        f"IMPORTANT: The user is in #{channel_name}. "
                        f"Strongly prefer the '{skill.name}' skill unless "
                        f"the request is clearly about a different skill "
                        f"(e.g. checking points balance, writing content).\n"
                    )
                    break

        skill_descriptions = "\n".join(
            f"- {s.name}: {s.description}"
            for s in available_skills
        )

        example_lines: List[str] = []
        for skill in available_skills:
            example_lines.extend(self.LLM_ROUTER_EXAMPLES.get(skill.name, ()))
        example_lines.extend(self.LLM_ROUTER_EXAMPLES["none"])
        examples = "\n".join(f"- {line}" for line in example_lines)

        # Format history for context
        history_context = ""
        if history:
            trimmed_history = history[:-1][-4:]
            history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in trimmed_history]) # Skip last as it's the current request usually
            history_context = f"Conversation History:\n{history_str}\n"

        thread_context_hint = ""
        if thread_context:
            thread_context_hint = (
                "Active Thread Context:\n"
                f"- last skill: {thread_context.get('skill_name')}\n"
                f"- domain: {thread_context.get('domain') or 'unknown'}\n"
                f"- workflow: {thread_context.get('workflow') or 'unknown'}\n"
            )

        prompt = f"""Choose the best skill for the user's message.

Available skills:
{skill_descriptions}
- none: Use this if no skill is appropriate (general conversation)
{channel_hint}{channel_priority_hint}
{history_context}
{thread_context_hint}
User message: "{text}"

Routing rules:
- Route on what the user wants DONE, not on vocabulary. A message mentioning "article", "task" or "csv" is not automatically a skill request.
- Prefer content-factory for domain-backed repo scans, writing NEW articles/blog posts, SEO research, content planning, and scaffolding blog/article pages.
- Do NOT use content-factory for summarising, reviewing, or giving opinions on existing content — that is none.
- Prefer github-integration for GitHub auth, reconnecting GitHub, or account/integration management.
- Prefer linear-meeting-actions for creating Linear issues/tickets/tasks, including from meeting notes, transcripts, action items, or attached files.
- Prefer mlai-points for the Roo points system: balances, claimable community tasks, coworking bookings, rewards, top-ups.
- Prefer luma-events for event registration counts, attendee lists/reports, and attendee CSV exports.
- Prefer mlai-data-query for read-only questions over backend data (Vibe Raising companies, startup/monthly update drafts, Content Factory jobs/runs, synced Linear issues, integration status, the data catalog).
- Prefer connect-users when the user wants to FIND or MEET community members with some expertise ("anyone who…", "who knows…", "connect me with…").
- Prefer tone-of-voice for rewriting/rephrasing existing text in MLAI's tone or brand voice.
- Questions about meetups/events that are not Luma data requests, file questions with no skill action, summaries, and chit-chat -> none.
- When genuinely unsure between a skill and none, answer none.

Examples:
{examples}

Respond with ONLY the skill name (e.g., "connect-users" or "none"):"""

        try:
            settings = get_settings()
            response = await chat([
                {"role": "system", "content": "You are a skill router. Respond with only the skill name."},
                {"role": "user", "content": prompt}
            ], model=settings.ROUTER_MODEL, max_tokens=128, reasoning_effort="medium")

            skill_name = response.content.strip().lower()
            # Normalize: both underscores and hyphens should match
            skill_name_normalized = skill_name.replace("_", "-")

            for skill in available_skills:
                skill_normalized = skill.name.lower().replace("_", "-")
                if skill_normalized == skill_name_normalized:
                    return skill

            return None

        except Exception as e:
            print(f"❌ Skill selection failed: {e}")
            return None
    
    async def _general_response(self, text: str, history: List[dict] = None) -> str:
        """Generate a general conversational response."""
        # Format history
        history_context = ""
        if history:
            history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in history[:-1]])
            history_context = f"\nRecent Context:\n{history_str}\n"

        skill_list = "\n".join(f"- {s.name}: {s.description}" for s in self.skills)
        prompt = f"""You are Roo, the friendly AI assistant for the MLAI community.
        
Your personality:
- Warm and approachable, like a helpful local
- Use casual Australian expressions occasionally (mate, no worries, etc.)
- Helpful and encouraging
- Keep responses concise but friendly

Your Capabilities / Skills:
{skill_list}

If the user asks "what can you do?" or "what are you?", summarize your role and list your skills in a friendly, conversational way. Don't just dump the raw list, explain it naturally.

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
