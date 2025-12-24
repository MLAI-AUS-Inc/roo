"""
Roo Agent - Core Orchestration Layer

The agent receives user messages, selects appropriate skills,
and executes them to generate responses.
"""
from typing import Optional, Dict, Any, List
from pathlib import Path

from .config import get_settings
from .llm import chat
from .skills.loader import Skill, load_skills
from .skills.executor import SkillExecutor


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
        
        print(f"🦘 RooAgent initialized with {len(self.skills)} skills:")
        for skill in self.skills:
            print(f"   - {skill.name}: {skill.description}")
    
    async def handle_mention(
        self,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
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
        
        print(f"🔍 Processing: {clean_text[:100]}...")
        
        # 1. Try Fast Path (Direct Command Execution)
        fast_result = await self._try_fast_path(clean_text, user_id, channel_id, thread_ts)
        if fast_result:
            print(f"⚡ Fast Path matched!")
            return fast_result
        
        # 2. Select appropriate skill (LLM Routing)
        skill = await self._select_skill(clean_text)
        
        if skill:
            print(f"🎯 Selected skill: {skill.name}")
            result = await self.skill_executor.execute(
                skill=skill,
                text=clean_text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                **kwargs
            )
            return {
                "message": result.message,
                "skill_used": skill.name,
                "data": result.data
            }
        else:
            print("💬 No skill matched, generating general response")
            response = await self._general_response(clean_text)
            return {
                "message": response,
                "skill_used": None,
                "data": None
            }
    
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
            
        ClientClass = skill.get_client_class("PointsClient")
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
        
        # Remove extra whitespace
        cleaned = ' '.join(cleaned.split())
        return cleaned.strip()
    
    async def _select_skill(self, text: str) -> Optional[Skill]:
        """Use LLM to decide which skill to use."""
        if not self.skills:
            return None
        
        # First check trigger keywords for quick matching
        text_lower = text.lower()
        for skill in self.skills:
            for keyword in skill.trigger_keywords:
                if keyword.lower() in text_lower:
                    return skill
        
        # Fall back to LLM classification
        skill_descriptions = "\n".join(
            f"- {s.name}: {s.description}" 
            for s in self.skills
        )
        
        prompt = f"""You are a skill router. Given the user's message, decide which skill to use.

Available skills:
{skill_descriptions}
- none: Use this if no skill is appropriate (general conversation)

User message: "{text}"

Respond with ONLY the skill name (e.g., "connect_users" or "none"):"""

        try:
            response = await chat([
                {"role": "system", "content": "You are a skill router. Respond with only the skill name."},
                {"role": "user", "content": prompt}
            ])
            
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
    
    async def _general_response(self, text: str) -> str:
        """Generate a general conversational response."""
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
