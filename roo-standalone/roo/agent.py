"""
Roo Agent - Core Orchestration Layer

The agent receives user messages, selects appropriate skills,
and executes them to generate responses.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
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


THREAD_CONTEXT_TTL = timedelta(hours=2)
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

        # 1. Try Fast Path (Direct Command Execution)
        fast_result = await self._try_fast_path(clean_text, user_id, channel_id, thread_ts)
        if fast_result:
            print(f"⚡ Fast Path matched!")
            return fast_result
        
        # 2. Select appropriate skill (LLM Routing)
        skill = routing_intent["skill"] if routing_intent else await self._select_skill(
            clean_text,
            thread_history,
            channel_id,
            thread_ts,
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
            if routing_intent and skill.name == routing_intent["skill"].name:
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

    def _looks_like_content_request(self, text: str) -> bool:
        patterns = (
            r'\barticle\b',
            r'\bblog(?:\s+post)?\b',
            r'\bseo\b',
            r'\bkeyword\b',
            r'\btopic\b',
            r'\bwrite\b.*\b(article|blog(?:\s+post)?)\b',
            r'\bresearch\b.*\b(article|topic|keyword)\b',
            r'\bfor my domain\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _looks_like_points_request(self, text: str) -> bool:
        patterns = (
            r'\bpoints?\b',
            r'\bbalance\b',
            r'\bcoworking\b',
            r'\brewards?\b',
            r'\bclaim\s+task\b',
            r'\bcreate\s+(?:a\s+)?task\b',
            r'\btask\s+create\b',
            r'\bworth\s+\d+\s+points?\b',
        )
        return any(re.search(pattern, text) for pattern in patterns)

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
    ) -> Optional[Skill]:
        routing_intent = self._get_routing_intent(text, thread_context)
        if routing_intent:
            return routing_intent["skill"]

        text_lower = text.lower().strip()
        content_skill = self._get_skill_by_name("content-factory")

        if content_skill and self._looks_like_content_request(text_lower):
            return content_skill

        if (
            thread_context
            and thread_context.get("skill_name") == "content-factory"
            and content_skill
            and self._looks_like_content_follow_up(text_lower)
            and not self._looks_like_points_request(text_lower)
        ):
            return content_skill

        skill_scores: Dict[str, int] = {}
        for skill in self.skills:
            matched_keywords = [
                keyword for keyword in skill.trigger_keywords
                if self._keyword_matches(text_lower, keyword)
            ]
            if matched_keywords:
                skill_scores[skill.name] = sum(len(keyword.split()) * 3 + len(keyword) for keyword in matched_keywords)

        if not skill_scores:
            return None

        ranked = sorted(skill_scores.items(), key=lambda item: item[1], reverse=True)
        best_skill_name, best_score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else -1

        if len(ranked) == 1 or best_score >= runner_up_score + 4:
            return self._get_skill_by_name(best_skill_name)

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
        import re
        
        text_lower = text.lower().strip()
        
        # --- Points Skill Fast Paths ---
        
        # 1. Balance Check: "points", "balance", "my points"
        if re.match(r'^(?:points|balance|my points)$', text_lower):
            return await self._execute_fast_points(user_id, "balance")
            
        # 2. Earn/Tasks: "points earn", "earn points", "tasks"
        if re.match(r'^(?:points\s+earn|earn\s+points|tasks|ways\s+to\s+earn)$', text_lower):
            return await self._execute_fast_points(user_id, "list_tasks")

        # 3. Rewards: "points rewards", "rewards"
        if re.match(r'^(?:points\s+rewards|rewards)$', text_lower):
            return await self._execute_fast_points(user_id, "list_rewards")

        # 4. Coworking Book Today: "coworking book today"
        if re.match(r'^coworking\s+book\s+today$', text_lower):
            today = self._get_today().isoformat()
            return await self._execute_fast_points(
                user_id, "book_coworking", 
                date=today, channel_id=channel_id
            )

        # 5. Coworking Cancel: "coworking cancel" (assumes today/upcoming)
        if re.match(r'^coworking\s+cancel$', text_lower):
            # For "cancel", we might need to handle logic in the client or pass a flag
            # The user requirement was "will cancel booking for today"
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
                api_key=settings.MLAI_API_KEY
            )
            
            # Re-use the executor's logic for response formatting to DRY
            # We need to instantiate the executor just to access the helper method
            # Note: This relies on _handle_points_action being available/public-ish
            # Since it's protected, we might duplicate simple formatting here for speed/isolation
            
            if action == "balance":
                data = await client.get_balance(user_id)
                msg = (
                    f"G'day mate! Here's your points summary:\n\n"
                    f"💰 **Current Balance:** {data.get('balance', 0)} points\n"
                    f"📈 **Lifetime Earned:** {data.get('lifetime_earned', 0)} points\n"
                    f"Nice work! Check out `@Roo points earn` to get more! 🦘"
                )
                
            elif action == "list_tasks":
                tasks = await client.list_tasks(status="open")
                if not tasks:
                    msg = "No open tasks at the moment. Check back soon! 🦘"
                else:
                    lines = ["📋 **Open Tasks:**\n"]
                    for t in tasks[:10]:
                        lines.append(f"• **#{t['id']}** - {t['title']} ({t['points']} pts) 📂 {t['portfolio']}")
                    lines.append("\nTo claim one, just say `@Roo claim task <ID>`")
                    msg = "\n".join(lines)
            
            elif action == "list_rewards":
                rewards = await client.list_rewards(user_id)
                if not rewards:
                    msg = "No rewards available right now."
                else:
                    lines = ["🎁 **Rewards Menu:**\n"]
                    for r in rewards:
                        lines.append(f"• **{r['code']}** - {r['name']} ({r['cost_points']} pts)")
                    lines.append("\nAsk me to `buy a sticker` or similar to redeem!")
                    msg = "\n".join(lines)
            
            elif action == "book_coworking":
                booking_date = kwargs.get("date")
                res = await client.book_coworking(user_id, booking_date, kwargs.get("channel_id"))
                msg = f"You beauty! 🎉\nBooked you in for **{booking_date}**. Cost: {res.get('points_cost', 1)} point."
                
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
    ) -> Optional[Skill]:
        """Use LLM to decide which skill to use."""
        if not self.skills:
            return None

        thread_context = self._get_thread_context(channel_id, thread_ts)
        routing_intent = self._get_routing_intent(text, thread_context)
        if routing_intent:
            return routing_intent["skill"]

        trigger_skill = self._select_skill_from_triggers(text, thread_context)
        if trigger_skill:
            return trigger_skill

        # Resolve channel name for priority matching
        channel_priority_hint = ""
        if channel_id:
            from .slack_client import get_channel_name
            channel_name = get_channel_name(channel_id)
            if channel_name:
                for skill in self.skills:
                    if channel_name in skill.priority_channels:
                        channel_priority_hint = (
                            f"\nIMPORTANT: The user is in #{channel_name}. "
                            f"Strongly prefer the '{skill.name}' skill unless "
                            f"the request is clearly about a different skill "
                            f"(e.g. checking points balance, writing content).\n"
                        )
                        break

        # Fall back to LLM classification
        skill_descriptions = "\n".join(
            f"- {s.name}: {s.description}"
            for s in self.skills
        )

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
{channel_priority_hint}
{history_context}
{thread_context_hint}
User message: "{text}"

Routing rules:
- Prefer content-factory for domain-backed repo scans, article/blog writing, SEO research, content planning, scaffolding blog/article pages, and requests like "scan the domain mlai.au" or "scan the repo for the domain mlai.au".
- Prefer github-integration for GitHub auth, reconnecting GitHub, or account/integration management.
- Prefer mlai-points for points, rewards, coworking, and task management.

Examples:
- "please research the best article for me to write" -> content-factory
- "scan the repo for the domain woofya.com.au" -> content-factory
- "scan the domain woofya.com.au" -> content-factory
- "reconnect github for woofya.com.au" -> github-integration
- "write me an article about how to build an ai agent harness for long-running specific tasks" -> content-factory
- "create a task called fix docs worth 5 points" -> mlai-points

Respond with ONLY the skill name (e.g., "connect_users" or "none"):"""

        try:
            settings = get_settings()
            response = await chat([
                {"role": "system", "content": "You are a skill router. Respond with only the skill name."},
                {"role": "user", "content": prompt}
            ], model=settings.ROUTER_MODEL, max_tokens=96, reasoning_effort="low")

            skill_name = response.content.strip().lower()
            # Normalize: both underscores and hyphens should match
            skill_name_normalized = skill_name.replace("_", "-")

            for skill in self.skills:
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
