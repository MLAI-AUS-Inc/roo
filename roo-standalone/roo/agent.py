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
        
        # Select appropriate skill
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
        prompt = """You are Roo, the friendly AI assistant for the MLAI community.
        
Your personality:
- Warm and approachable, like a helpful local
- Use casual Australian expressions occasionally (mate, no worries, etc.)
- Helpful and encouraging
- Keep responses concise but friendly

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
