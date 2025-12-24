"""
Skill Executor

Executes skill actions based on the skill definition.
Follows Anthropic's Agent Skills pattern for execution.
"""
import re
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from difflib import SequenceMatcher

from .loader import Skill
from ..llm import chat, embed
from ..slack_client import post_message
from ..config import get_settings


@dataclass
class SkillResult:
    """Result from skill execution."""
    success: bool
    message: str
    data: Optional[Any] = None
    error: Optional[str] = None


class SkillExecutor:
    """
    Executes skills based on their SKILL.md definitions.
    
    The executor:
    1. Extracts parameters from the user's message using LLM
    2. Routes to skill-specific handlers if available
    3. Falls back to generic LLM execution with skill instructions
    """
    
    async def execute(
        self,
        skill: Skill,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
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
            # Extract parameters using LLM
            params = await self._extract_parameters(skill, text)
            print(f"   Extracted params: {params}")
            
            # Check for skill-specific implementation
            if skill.name == "content-factory":
                result = await self._execute_content_factory(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "connect-users":
                result = await self._execute_connect_users(skill, text, params, user_id)
            elif skill.name == "mlai-points":
                result = await self._execute_mlai_points(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "github-integration":
                result = await self._execute_github_integration(skill, text, params, user_id, channel_id, thread_ts)
            else:
                # Generic LLM-based execution
                result = await self._execute_with_llm(skill, text, params, user_id)
            
            return SkillResult(
                success=True,
                message=result,
                data=params
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
    
    async def _extract_parameters(self, skill: Skill, text: str) -> dict:
        """Extract parameters from user message based on skill definition."""
        # Parse parameter definitions from skill content
        param_section = self._find_section(skill.content, "Parameters")
        
        if not param_section:
            return {}
        
        prompt = f"""Extract parameters from the user's message based on these definitions:

{param_section}

User message: "{text}"

Return a JSON object with the extracted parameters. Only include parameters that are clearly present.
Example: {{"query": "machine learning", "limit": 5}}

JSON:"""

        response = await chat([
            {"role": "system", "content": "You extract structured parameters from text. Return valid JSON only."},
            {"role": "user", "content": prompt}
        ])
        
        # Parse JSON from response
        import json
        try:
            # Clean up response - extract JSON if wrapped in markdown
            content = response.content.strip()
            if content.startswith("```"):
                content = re.sub(r'^```\w*\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
    
    async def _execute_with_llm(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str
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

Follow the skill instructions to generate an appropriate response.
Be helpful, friendly, and use casual Australian expressions occasionally.
Keep the response concise but informative."""

        response = await chat([
            {"role": "system", "content": "You are Roo, a friendly AI assistant for the MLAI community."},
            {"role": "user", "content": prompt}
        ])
        
        return response.content
    
    async def _execute_connect_users(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str
    ) -> str:
        """Execute the connect_users skill with vector search."""
        query = params.get("query", "")
        
        if not query:
            # Try to extract from the text directly
            query = text
        
        # Note: Vector search is disabled until API endpoint is implemented
        # For now, fall back to LLM-based execution
        return await self._execute_with_llm(skill, text, params, user_id)
    
    async def _execute_content_factory(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str]
    ) -> str:
        """Execute the content factory generation workflow."""
        domain = params.get("domain")
        topic = params.get("topic")
        target_keyword = params.get("target_keyword", "")
        
        if not domain or not topic:
            missing = []
            if not domain: missing.append("domain name (e.g., mlai.au)")
            if not topic: missing.append("topic for the article")
            
            return f"I can help write that article! To get started, I just need to know the {' and '.join(missing)}."
        
        # Get a PointsClient for API calls
        settings = get_settings()
        from .mlai_points.client import PointsClient
        api_client = PointsClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
        )
        
        # Check for GitHub Token (required for publishing updates)
        github_token = await api_client.get_github_token(user_id)
        
        if not github_token:
             # Send Auth Button
            auth_url = f"{settings.SLACK_APP_URL}/auth/github/login?state={user_id}"
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "I need permission to access your GitHub to publish articles. Click the button below to connect your account."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Connect GitHub Account",
                                "emoji": True
                            },
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            # Save pending intent before asking for auth
            import json
            intent_data = json.dumps({
                "skill": "content-factory",
                "params": params,
                "text": text,
                "channel": channel_id,
                "ts": thread_ts
            })
            await api_client.save_pending_intent(user_id, intent_data)
            
            if channel_id:
                post_message(channel_id, "Please connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return "I've sent a button to connect your GitHub account. 🔌"
            return f"Please connect your GitHub account here: {auth_url}"

        # 2. Check for Project Scanned status
        integration = await api_client.get_integration(user_id)
        if not integration or not integration.get("project_scanned"):
            # Only allow if user specifically requested a scan or we can infer it? 
            # Ideally we redirect them to scan first.
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"I see you're connected, but I need to scan your repository **{github_token}** (placeholder - strictly we need repo name) to understand the structure first."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Scan Repository",
                                "emoji": True
                            },
                            "action_id": "scan_repo_trigger", 
                            "value": "scan_repo", # We need a way to trigger this logic
                            "style": "primary"
                        }
                    ]
                }
            ]
            # Ideally we'd just automatically trigger the scan if we knew the repo name.
            # But we might lack the repo name in the params here if they just said "generate article".
            # For now, let's just ask them to run the scan command.
            
            return "Hold your horses! 🐎 I need to scan your repository first to understand your project structure.\n\nPlease run: `@Roo scan repo <owner>/<repo>`"

        # Get client from skill's implementation module
        ClientClass = skill.get_client_class("ContentFactoryClient")
        
        if ClientClass is None:
            return "Sorry mate, the Content Factory skill isn't properly configured. Missing implementation."
        
        try:
            settings = get_settings()
            client = ClientClass(
                base_url=settings.CONTENT_FACTORY_URL,
                api_key=settings.CONTENT_FACTORY_API_KEY
            )
            
            # Start generation
            job_id = await client.generate_article(
                domain=domain,
                topic=topic,
                target_keyword=target_keyword,
                context=text,
                github_token=github_token
            )
            
            # Launch background monitoring task
            if channel_id:
                asyncio.create_task(
                    self._monitor_generation(client, job_id, channel_id, thread_ts, github_token)
                )
            
            return f"You beauty! I've started writing the article '{topic}' for {domain}. (Job ID: {job_id})\nI'll keep you posted on the progress right here! 🚀"
            
        except Exception as e:
            print(f"Content Factory Error: {e}")
            return f"Sorry mate, I had trouble connecting to the Content Factory: {str(e)}"

    async def _monitor_generation(
        self,
        client,
        job_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        github_token: str
    ):
        """Monitor job progress and post updates to Slack."""
        last_progress = -1
        last_step = ""
        
        def on_progress(status):
            nonlocal last_progress, last_step
            progress = status.get("progress", 0)
            step = status.get("current_step", "")
            
            # Only update on meaningful change (every 20% or step change)
            should_update = (
                progress >= last_progress + 20 or 
                (step != last_step and step in ["researching", "writing", "optimizing", "publishing"])
            )
            
            if should_update:
                msg = f"📝 *Status Update*: {step.title()}... ({progress}%)"
                try:
                    post_message(channel_id, msg, thread_ts)
                    last_progress = progress
                    last_step = step
                except Exception as e:
                    print(f"Failed to post progress: {e}")

        try:
            # Poll until completion
            result = await client.poll_and_wait(job_id, on_progress)
            
            # Publish
            post_message(channel_id, "✨ Article generated! Publishing now...", thread_ts)
            
            publish_result = await client.publish_article(job_id, github_token)
            
            preview_url = publish_result.get("preview_url")
            pr_url = publish_result.get("pr_url")
            
            final_msg = (
                f"🎉 *Article Published!* \n\n"
                f"👀 *Preview:* {preview_url}\n"
                f"💻 *Pull Request:* {pr_url}\n\n"
                f"Review the content and merge the PR when you're ready!"
            )
            
            post_message(channel_id, final_msg, thread_ts)
            
        except Exception as e:
            error_msg = f"❌ Something went wrong with the article generation: {str(e)}"
            post_message(channel_id, error_msg, thread_ts)
    
    def _find_section(self, content: str, section_name: str) -> Optional[str]:
        """Find a section in the markdown content."""
        pattern = rf'##\s*{section_name}\s*\n(.*?)(?=\n##|\Z)'
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None
    
    async def _execute_mlai_points(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str]
    ) -> str:
        """Execute the MLAI Points skill."""
        import httpx
        
        # Get client from skill's implementation module
        ClientClass = skill.get_client_class("PointsClient")
        
        if ClientClass is None:
            return "Sorry mate, the Points skill isn't properly configured. Missing implementation."
        
        try:
            settings = get_settings()
            if not settings.MLAI_BACKEND_URL:
                return "Sorry mate, the Points API isn't configured. Ask the team to set MLAI_BACKEND_URL."
            
            client = ClientClass(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.MLAI_API_KEY,
                internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
            )
            
            # Determine action from params or text
            action = params.get("action", "").lower()
            text_lower = text.lower()
            
            # Alias Handling for Common Mis-Extractions
            if action == "book":
                # LLM often extracts "book" instead of "book_coworking"
                action = "book_coworking"
            elif action in ["create", "task", "create_task"]:
                # "task" or "create" often extracted for "create task"
                if params.get("task_title") or "create" in text_lower:
                    action = "create_task"
            
            # Fallback action detection from text
            if not action or action == "task":
                if any(w in text_lower for w in ["balance", "how many points", "my points"]):
                    action = "balance"
                elif "history" in text_lower:
                    action = "history"
                elif any(w in text_lower for w in ["tasks open", "open tasks", "available tasks", "tasks"]):
                    action = "list_tasks"
                elif "claim" in text_lower:
                    action = "claim_task"
                elif "submit" in text_lower:
                    action = "submit_task"
                elif any(w in text_lower for w in ["coworking check", "check coworking", "availability"]):
                    action = "check_coworking"
                elif any(w in text_lower for w in ["coworking book", "book coworking", "book me"]):
                    action = "book_coworking"
                elif "cancel" in text_lower and "coworking" in text_lower:
                    action = "cancel_coworking"
                elif any(w in text_lower for w in ["rate card", "point values", "how much is"]):
                     action = "view_rate_card"
                elif any(w in text_lower for w in ["rewards", "perks"]):
                    action = "list_rewards"
                elif "reward" in text_lower and "request" in text_lower:
                    action = "request_reward"
                elif "task" in text_lower and "create" in text_lower:
                    action = "create_task"
                elif "approve" in text_lower:
                    action = "approve_task"
                elif "reject" in text_lower:
                    action = "reject_task"
                elif any(w in text_lower for w in ["award", "give points", "reward"]):
                    action = "award_points"
                elif any(w in text_lower for w in ["deduct", "remove points"]):
                    action = "deduct_points"
            
            # Execute the appropriate action
            return await self._handle_points_action(
                client=client,
                action=action,
                params=params,
                text=text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                skill=skill
            )
            
        except PermissionError:
            return "Sorry mate, you're not authorized to do that. Only Points Admins can perform that action. 🔒"
        except ValueError as e:
            return str(e)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return "Sorry mate, you're not authorized to do that. Only Points Admins can perform that action. 🔒"
            elif e.response.status_code == 404:
                return "Hmm, couldn't find that. Double-check the ID or date and try again? 🤔"
            elif e.response.status_code == 400:
                # Handle bad requests (e.g. insufficient funds)
                try:
                    error_detail = e.response.json().get("error", "")
                    
                    # If it's a balance issue, fetch current balance to be helpful
                    if "balance" in error_detail.lower() or "insufficient" in error_detail.lower():
                        try:
                            balance_data = await client.get_balance(user_id)
                            current_balance = balance_data.get("balance", 0)
                            return f"🛑 Computer says no: {error_detail}\n\nYour current balance is **{current_balance} points**."
                        except Exception:
                            pass
                            
                    return f"🛑 {error_detail}"
                except Exception:
                    return f"Ran into a snag with that request (400 Bad Request)."
            else:
                error_detail = ""
                try:
                    error_detail = e.response.json().get("error", "")
                except Exception:
                    pass
                return f"Ran into a snag: {error_detail or str(e)}"
        except Exception as e:
            print(f"Points skill error: {e}")
            import traceback
            traceback.print_exc()
            return f"Had some trouble with the points system: {str(e)}"
    
    async def _handle_points_action(
        self,
        client,
        action: str,
        params: dict,
        text: str,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        skill
    ) -> str:
        """Handle individual points actions."""
        
        # =====================================================================
        # Member Actions
        # =====================================================================
        
        if action == "balance":
            data = await client.get_balance(user_id)
            balance = data.get("balance", 0)
            earned = data.get("lifetime_earned", 0)
            spent = data.get("lifetime_spent", 0)
            
            return (
                f"G'day mate! Here's your points summary:\n\n"
                f"💰 **Current Balance:** {balance} points\n"
                f"📈 **Lifetime Earned:** {earned} points\n"
                f"📉 **Lifetime Spent:** {spent} points\n\n"
                f"Nice work! Check out open tasks to earn more 🦘"
            )
        
        elif action == "history":
            limit = params.get("limit", 10)
            entries = await client.get_history(user_id, limit)
            
            if not entries:
                return "No transactions yet! Start earning points by claiming some tasks 💪"
            
            lines = ["📜 **Your Recent Transactions:**\n"]
            for entry in entries[:10]:
                delta = entry.get("delta", 0)
                emoji = "➕" if delta > 0 else "➖"
                desc = entry.get("description", "")[:50]
                lines.append(f"{emoji} {delta:+d} pts - {desc}")
            
            return "\n".join(lines)
        
        elif action == "list_tasks":
            status = params.get("status", "open")
            portfolio = params.get("portfolio")
            tasks = await client.list_tasks(status, portfolio)
            
            if not tasks:
                return f"No {status} tasks at the moment. Check back soon! 🦘"
            
            lines = [f"📋 **{status.title()} Tasks:**\n"]
            for task in tasks[:10]:
                tid = task.get("id")
                title = task.get("title", "Untitled")[:40]
                pts = task.get("points", 0)
                port = task.get("portfolio", "")
                lines.append(f"• **#{tid}** - {title} ({pts} pts) 📂 {port}")
            
            lines.append("\nKeen to help? Just say \"claim task <id>\" to get started!")
            return "\n".join(lines)
        
        elif action == "claim_task":
            task_id = params.get("task_id")
            if not task_id:
                # Try to extract from text
                import re
                match = re.search(r'(?:task|#)\s*(\d+)', text, re.IGNORECASE)
                if match:
                    task_id = int(match.group(1))
                else:
                    return "Which task do you want to claim? Give me the task ID (e.g., \"claim task 42\")"
            
            result = await client.claim_task(int(task_id), user_id)
            title = result.get("title", "")
            pts = result.get("points", 0)
            
            return f"Ripper! 🎉 You've claimed **#{task_id} - {title}** ({pts} pts).\n\nWhen you're done, submit your work with \"task submit {task_id} <description>\""
        
        elif action == "submit_task":
            task_id = params.get("task_id")
            submission_text = params.get("submission_text", "")
            submission_url = params.get("submission_url")
            
            if not task_id:
                import re
                match = re.search(r'(?:task|#)\s*(\d+)', text, re.IGNORECASE)
                if match:
                    task_id = int(match.group(1))
                else:
                    return "Which task are you submitting? Give me the task ID (e.g., \"submit task 42 done!\")"
            
            if not submission_text:
                # Extract text after the task ID
                import re
                match = re.search(r'(?:task|#)\s*\d+\s+(.+)', text, re.IGNORECASE)
                if match:
                    submission_text = match.group(1)
                else:
                    submission_text = "Submitted via Slack"
            
            result = await client.submit_task(int(task_id), user_id, submission_text, submission_url)
            
            return f"Submitted! 📬 Task #{task_id} is now pending approval.\n\nA Points Admin will review your work soon. Legend! 🦘"
        
        elif action == "check_coworking":
            check_date = params.get("date")
            days = params.get("days", 7)
            
            availability = await client.check_coworking(check_date, days)
            
            if not availability:
                return "Couldn't check availability right now. Try again in a tick?"
            
            lines = ["🏢 **Coworking Availability:**\n"]
            for slot in availability[:7]:
                date_str = slot.get("date", "")
                avail = slot.get("available_slots", 0)
                cost = slot.get("cost_points", 1)
                emoji = "✅" if avail > 0 else "❌"
                lines.append(f"{emoji} **{date_str}**: {avail} slots ({cost} pt)")
            
            lines.append("\nBook a day with \"coworking book <date>\"")
            return "\n".join(lines)
        
        elif action == "book_coworking":
            booking_date = params.get("date")
            
            # Normalize date aliases
            if booking_date:
                from datetime import timedelta
                from roo.utils import get_current_date
                today = get_current_date()

                if booking_date.lower() == "today":
                    booking_date = today.isoformat()
                elif booking_date.lower() == "tomorrow":
                    booking_date = (today + timedelta(days=1)).isoformat()
            
            if not booking_date:
                import re
                match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                if match:
                    booking_date = match.group(1)
                else:
                    return "What date would you like to book? Use format YYYY-MM-DD (e.g., \"book 2025-12-20\")"
            
            result = await client.book_coworking(user_id, booking_date, channel_id)
            cost = result.get("points_cost", 1)
            
            # Get new balance
            balance_data = await client.get_balance(user_id)
            new_balance = balance_data.get("balance", 0)
            
            return (
                f"You beauty! 🎉\n\n"
                f"Booked you in for **{booking_date}** at the coworking space.\n"
                f"Cost: {cost} point (Balance remaining: {new_balance} points)\n\n"
                f"See you there, legend!"
            )
        
        elif action == "cancel_coworking":
            booking_date = params.get("date")
            booking_id = params.get("booking_id")
            
            # Normalize date aliases
            if booking_date:
                from datetime import timedelta
                from roo.utils import get_current_date
                today = get_current_date()

                if booking_date.lower() == "today":
                    booking_date = today.isoformat()
                elif booking_date.lower() == "tomorrow":
                    booking_date = (today + timedelta(days=1)).isoformat()
            
            if not booking_date and not booking_id:
                import re
                match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                if match:
                    booking_date = match.group(1)
                else:
                    return "Which booking do you want to cancel? Give me the date (e.g., \"cancel coworking 2025-12-20\")"
            
            result = await client.cancel_coworking(user_id, booking_id, booking_date)
            refunded = result.get("refunded", False)
            refund_amount = result.get("refund_amount", 0)
            
            if refunded:
                return f"No worries! Cancelled your booking. {refund_amount} point refunded to your balance. 👍"
            else:
                return f"Booking cancelled. (No refund - cancellation after cutoff)"
        
        elif action == "list_rewards":
            rewards = await client.list_rewards(user_id)
            
            if not rewards:
                return "No rewards available at the moment. Check back soon! 🦘"
            
            lines = ["🎁 **Available Rewards:**\n"]
            for reward in rewards:
                code = reward.get("code", "")
                name = reward.get("name", "")
                cost = reward.get("cost_points", 0)
                lines.append(f"• **{code}** - {name} ({cost} pts)")
            
            lines.append("\nRequest a reward with \"reward request <CODE>\"")
            return "\n".join(lines)
        
        elif action == "request_reward":
            reward_code = params.get("reward_code", "").upper()
            quantity = params.get("quantity", 1)
            
            if not reward_code:
                import re
                match = re.search(r'request\s+(\w+)', text, re.IGNORECASE)
                if match:
                    reward_code = match.group(1).upper()
                else:
                    return "Which reward would you like? Give me the code (e.g., \"reward request HOTDESK_DAY\")"
            
            result = await client.request_reward(
                user_id, reward_code, quantity,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts
            )
            
            return f"Request submitted! 🎉 Your request for **{reward_code}** is pending approval.\n\nAn admin will review it shortly."
        
        # =====================================================================
        # Admin Actions
        # =====================================================================
        
        elif action == "create_task":
            # 1. Parameter Aliases
            title = params.get("task_title") or params.get("title") or params.get("submission_text")
            points = params.get("points")
            description = params.get("description", "")
            
            # Default portfolio logic: Param > Admin's Portfolio > "events"
            portfolio = params.get("portfolio")
            if not portfolio:
                try:
                    admin_details = await client.get_admin_details(user_id)
                    if admin_details:
                        portfolio = admin_details.get("portfolio")
                except Exception as e:
                    print(f"⚠️ Failed to lookup admin portfolio: {e}")
            
            if not portfolio:
                portfolio = "events" # Fallback if lookup fails

            due_date = params.get("due_date")
            assigned_to = params.get("assigned_to_user_id") or params.get("target_user")
            
            # 2. Validation
            if not title:
                return "G'day! I need a task title to create the task, mate. (e.g., \"create task 'Fix docs' 5 points\")"
            
            if not points:
                return "Crikey! You need to specify how many points this task is worth."
            
            # 3. Execution
            result = await client.create_task(
                admin_slack_id=user_id,
                title=title,
                points=int(points),
                description=description,
                portfolio=portfolio,
                due_date=due_date,
                assigned_to_user_id=assigned_to,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts
            )
            
            # 4. Response Handling
            if result.get("error") == "forbidden":
                return "Sorry mate, but I can't create tasks. You need to be a Points Admin for that! If you reckon you should have access, have a chat with the committee. 🤔"
            
            task_id = result.get("id")
            pts = result.get("points", points)
            port = result.get("portfolio", portfolio)
            
            assigned_msg = ""
            if result.get("assigned_to_user_id"):
                assigned_msg = f" and assigned to <@{result.get('assigned_to_user_id')}>"
            elif assigned_to:
                 assigned_msg = f" and assigned to <@{client._clean_slack_id(assigned_to)}>"
            
            return f"✅ Beauty! Created task **{title}** worth **{pts} points**{assigned_msg}. Task ID: #{task_id}"
        
        elif action == "view_rate_card":
             card = await client.get_rate_card()
             if not card:
                 return "Rate card is empty or unavailable."
             
             lines = ["📋 **Standard Point Rates:**\n"]
             for item in card:
                 name = item.get("name", "Unknown")
                 pts = item.get("points", 0)
                 desc = item.get("description", "")
                 lines.append(f"• **{name}** ({pts} pts) - {desc}")
             
             return "\n".join(lines)
        
        elif action == "approve_task":
            task_id = params.get("task_id")
            
            if not task_id:
                import re
                match = re.search(r'(?:task|#)\s*(\d+)', text, re.IGNORECASE)
                if match:
                    task_id = int(match.group(1))
                else:
                    return "Which task are you approving? Give me the task ID (e.g., \"approve task 42\")"
            
            result = await client.approve_task(int(task_id), user_id)
            points_awarded = result.get("points_awarded", 0)
            
            return f"Approved! ✅ Task #{task_id} completed. {points_awarded} points awarded. 🎉"
        
        elif action == "reject_task":
            task_id = params.get("task_id")
            reason = params.get("reason", "")
            
            if not task_id:
                import re
                match = re.search(r'(?:task|#)\s*(\d+)', text, re.IGNORECASE)
                if match:
                    task_id = int(match.group(1))
                else:
                    return "Which task are you rejecting? Give me the task ID."
            
            result = await client.reject_task(int(task_id), user_id, reason)
            
            return f"Task #{task_id} rejected. The volunteer can resubmit if needed."
        
        elif action in ["deduct_points", "deduct"]:
            return "Sorry mate, I can only award points, not deduct them! 🚫"
            
        elif action in ["award_points", "award"]:
            # Early allowance check for award actions (before LLM/rate card lookup)
            if action in ["award_points", "award"]:
                try:
                    allowance_status = await client.get_admin_allowance(user_id)
                    if 'error' in allowance_status:
                        return "Sorry mate, you're not authorized to award points. Only Points Admins can do that. 🔒"
                    remaining = allowance_status.get('remaining', 0)
                    if remaining <= 0:
                        weekly_allowance = allowance_status.get('allowance', 0)
                        return (
                            f"You've used your full weekly allowance ({weekly_allowance} pts). "
                            "It resets on Monday. ⏰"
                        )
                    # Store for later use in messages
                    params['_admin_remaining_allowance'] = remaining
                    params['_admin_weekly_allowance'] = allowance_status.get('allowance', 0)
                except Exception as e:
                    print(f"⚠️ Allowance pre-check failed: {e}")
                    # Continue anyway - the actual award will fail if not authorized

            points = params.get("points", 0)
            reason = params.get("reason", "Manual adjustment")

            
            # Get Roo's bot ID to filter it from target users
            from ..slack_client import get_bot_user_id
            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None
            
            # Extract ALL user mentions from the text (excluding Roo)
            import re
            all_mentions = re.findall(r'<@([A-Z0-9]+)>', text)
            target_slack_ids = [uid for uid in all_mentions if uid != bot_id]
            
            # Fallback to params if no mentions found in text
            if not target_slack_ids:
                target_users_param = params.get("target_users", [])
                target_user_param = params.get("target_user", "")
                target_slack_id_param = params.get("target_slack_id", "")
                
                if target_users_param:
                    # Clean each ID
                    for tu in target_users_param:
                        cleaned = re.sub(r'[<@>]', '', str(tu))
                        if cleaned and cleaned != bot_id:
                            target_slack_ids.append(cleaned)
                elif target_user_param:
                    cleaned = re.sub(r'[<@>]', '', str(target_user_param))
                    if cleaned and cleaned != bot_id:
                        target_slack_ids.append(cleaned)
                elif target_slack_id_param:
                    cleaned = re.sub(r'[<@>]', '', str(target_slack_id_param))
                    if cleaned and cleaned != bot_id:
                        target_slack_ids.append(cleaned)
            
            # Validate we have valid targets (not prepositions)
            invalid_words = ["for", "to", "reason", "because", "points", "award", "give", "and"]
            target_slack_ids = [uid for uid in target_slack_ids if uid.lower() not in invalid_words]
            
            if not target_slack_ids:
                return "Who should I award points to? Mention them like @user (e.g., 'award 5 points to @Jasmine')"
            
            # Extract points amount if not in params
            # Extract points amount if not in params
            if not points:
                # 1. Try Regex fallback first (in case params missed explicit points)
                pts_match = re.search(r'(?<![a-zA-Z])([+-]?\d+)\s*(?:points?|pts?)?', text, re.IGNORECASE)
                if pts_match:
                    found_val = int(pts_match.group(1))
                    has_keyword = "point" in pts_match.group(0).lower() or "pts" in pts_match.group(0).lower()
                    if has_keyword or abs(found_val) < 1000:
                        points = found_val
            
            # 2. Smart Awards Logic (Rate Card) - Only if points still missing
            if not points:
                if reason:
                    print(f"🕵️ No points specified. Checking Rate Card for '{reason}'...")
                    try:
                        rate_card = await client.get_rate_card()
                        matches = []
                        reason_lower = reason.lower()
                        
                        for item in rate_card:
                            name = item.get("name", "")
                            desc = item.get("description", "") or ""
                            # Enhanced scoring
                            score = 0
                            if reason_lower in name.lower(): score += 50
                            if reason_lower in desc.lower(): score += 30
                            
                            seq_score = SequenceMatcher(None, reason_lower, name.lower()).ratio() * 100
                            if seq_score > 60: score += seq_score
                            
                            if score > 40:
                                matches.append((score, item))
                        
                        matches.sort(key=lambda x: x[0], reverse=True)
                        
                        if matches:
                            top_match = matches[0][1]
                            top_pts = top_match.get("points")
                            top_name = top_match.get("name")
                            cleanup_target = client._clean_slack_id(target_slack_ids[0]) if target_slack_ids else "the user"
                            
                            # Include remaining allowance context if available
                            remaining_info = ""
                            if params.get('_admin_remaining_allowance'):
                                remaining_info = f" (You have {params['_admin_remaining_allowance']} pts left this week.)"
                            
                            if len(matches) == 1 or matches[0][0] > 80:
                                return f"I found a match in the Rate Card: '{top_name}' is worth {top_pts} points. Should I award {top_pts} points to <@{cleanup_target}>?{remaining_info}"
                            else:
                                options = [f"'{m[1].get('name')}' ({m[1].get('points')} pts)" for m in matches[:3]]
                                return f"That sounds like it could be {options[0]} or {options[1] if len(options)>1 else ''}. Which one is it?{remaining_info}"
                                
                    except Exception as e:
                        print(f"⚠️ Smart award lookup failed: {e}")

                return "How many points should I award? (e.g., \"award @user 5 points\")"
            
            # Validate positive points
            if points < 0:
                return "Crikey! I can only award positive points. 🚫"
            
            # Award points to each target user
            results = []
            errors = []
            for target_id in target_slack_ids:
                try:
                    # Deduplication: Link Slack ID to existing email user if needed
                    try:
                        # Check if this Slack ID is already known
                        existing_user_id = await client.get_user_by_slack_id(target_id)
                        
                        if not existing_user_id:
                            # Not found by Slack ID -> Check if we know this user by email
                            from ..slack_client import get_user_info
                            u_info = get_user_info(target_id)
                            u_email = u_info.get("email")
                            
                            if u_email:
                                linked_user_id = await client.link_slack_user(target_id, u_email)
                                if linked_user_id:
                                    print(f"🔗 Linked Slack ID {target_id} to existing user {linked_user_id} via email {u_email}")
                    except Exception as e:
                        print(f"⚠️ User linking failed (continuing to award): {e}")

                    result = await client.award_points(user_id, target_id, int(points), reason)
                    new_balance = result.get("new_balance", 0)
                    results.append({"user": target_id, "new_balance": new_balance})
                except Exception as e:
                    errors.append({"user": target_id, "error": str(e)})
            
            # Build response
            emoji = "🎉" if points > 0 else "📉"
            verb = "Awarded" if points > 0 else "Deducted"
            
            if len(results) == 1 and not errors:
                r = results[0]
                return f"{emoji} {verb} {abs(points)} points to <@{r['user']}>.\n\nReason: {reason}\nTheir new balance: {r['new_balance']} pts"
            
            lines = [f"{emoji} {verb} {abs(points)} points each!\n\nReason: {reason}\n"]
            for r in results:
                lines.append(f"✅ <@{r['user']}>: now has {r['new_balance']} pts")
            for e in errors:
                lines.append(f"❌ <@{e['user']}>: {e['error']}")
            
            return "\n".join(lines)
        
        else:
            # Fall back to LLM for unrecognized actions
            return await self._execute_with_llm(skill, text, params, user_id)

    async def _execute_github_integration(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str]
    ) -> str:
        """Execute the GitHub Integration skill."""
        
        # Get API client for GitHub token operations
        settings = get_settings()
        from .mlai_points.client import PointsClient
        api_client = PointsClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
        )
        
        # 1. Check for token
        token = await api_client.get_github_token(user_id)
        
        if not token:
            # Send Auth Button
            auth_url = f"{settings.SLACK_APP_URL}/auth/github/login?state={user_id}"
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "I need permission to access your GitHub repository first. Click the button below to connect your account."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Connect GitHub Account",
                                "emoji": True
                            },
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            # Post interactive message
            if channel_id:
                post_message(channel_id, "Please connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return "I've sent a button to connect your GitHub account. 🔌"
            return f"Please connect your GitHub account here: {auth_url}"

        # 2. Token exists, assume action is scan_repo (main use case for now)
        repo_name = params.get("repo_name")
        domain = params.get("domain")
        
        if not repo_name:
            return "Which repository should I scan? (format: owner/repo)"
            
        # Get client
        ClientClass = skill.get_client_class("GitHubIntegrationClient")
        if not ClientClass:
            return "Skill configuration error: Client not found."
            
        client = ClientClass(
            content_factory_url=settings.CONTENT_FACTORY_URL,
            api_key=settings.CONTENT_FACTORY_API_KEY
        )
        
        try:
            result = await client.scan_repo(repo_name, token, domain)
            
            job_id = result.get("job_id")
            
            # Mark project as scanned on success
            await api_client.mark_project_scanned(user_id, True)
            
            return f"Started scanning **{repo_name}**! (Job ID: {job_id})\nI'll let you know when it's done."
            
        except Exception as e:
            print(f"GitHub Integration Error: {e}")
            return f"Sorry mate, I had trouble connecting to your repository: {str(e)}"
