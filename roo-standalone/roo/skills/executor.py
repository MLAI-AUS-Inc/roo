"""
Skill Executor

Executes skill actions based on the skill definition.
Follows Anthropic's Agent Skills pattern for execution.
"""
import json
import re
import asyncio
from dataclasses import dataclass
from typing import Any, Optional, List
from difflib import SequenceMatcher
import httpx

from .loader import Skill
from ..llm import chat, embed, get_llm_client
from ..slack_client import post_message
from ..config import get_settings


@dataclass
class SkillResult:
    """Result from skill execution."""
    success: bool
    message: str
    data: Optional[Any] = None
    error: Optional[str] = None
    blocks: Optional[list] = None


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
        thread_history: Optional[List[dict]] = None,
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
            params = await self._extract_parameters(skill, text, user_id, thread_history)
            print(f"   Extracted params: {params}")
            
            # Check for skill-specific implementation
            if skill.name == "content-factory":
                result = await self._execute_content_factory(skill, text, params, user_id, channel_id, thread_ts, thread_history)
            elif skill.name == "connect-users":
                result = await self._execute_connect_users(skill, text, params, user_id)
            elif skill.name == "mlai-points":
                result = await self._execute_mlai_points(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "github-integration":
                result = await self._execute_github_integration(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "tone-of-voice":
                result = await self._execute_tone_of_voice(skill, text, params, user_id)
            elif skill.name == "medhack":
                result = await self._execute_medhack(skill, text, params, user_id, channel_id, thread_ts, thread_history)
            else:
                # Generic LLM-based execution
                result = await self._execute_with_llm(skill, text, params, user_id, thread_history)
            
            # Skill handlers can return a dict with "message" + "blocks" for rich responses
            blocks = None
            if isinstance(result, dict) and "message" in result:
                blocks = result.get("blocks")
                result = result["message"]

            return SkillResult(
                success=True,
                message=result,
                data=params,
                blocks=blocks
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
    
    async def _extract_parameters(self, skill: Skill, text: str, user_id: str, history: Optional[List[dict]] = None) -> dict:
        """Extract parameters from user message based on skill definition."""
        from ..utils import get_current_date
        
        # Parse parameter definitions from skill content
        param_section = self._find_section(skill.content, "Parameters")
        
        if not param_section:
            return {}
            
        # Format history context
        history_context = ""
        if history:
            # Simple format: "User: msg"
            context_lines = [f"{msg.get('user')}: {msg.get('text')}" for msg in history[:-1]]
            history_context = "\nConversation Context (use this to fill missing parameters):\n" + "\n".join(context_lines)
        
        current_date_str = get_current_date().isoformat()
        
        prompt = f"""Extract parameters from the user's message based on these definitions:

{param_section}

Current Date: {current_date_str}

{history_context}

User message: "{text}"

Return a JSON object with the extracted parameters. Only include parameters that are clearly present.
Example: {{"query": "machine learning", "limit": 5}}

JSON:"""

        response = await chat([
            {"role": "system", "content": "You extract structured parameters from text. Return valid JSON only."},
            {"role": "user", "content": prompt}
        ])
        
        # Parse JSON from response
        try:
            # Clean up response - extract JSON if wrapped in markdown
            content = response.content.strip()
            if content.startswith("```"):
                content = re.sub(r'^```\w*\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
    
    async def _execute_tone_of_voice(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str
    ) -> str:
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
        openai_client = get_llm_client("openai")
        response = await openai_client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            model="gpt-5.2",
            max_tokens=8192,
            reasoning_effort="high"
        )

        return response.content

    async def _ensure_user_exists(self, user_id: str) -> None:
        """
        Ensure a user exists in the mlai-backend database.
        Uses the /api/v1/users/slack-user/ endpoint which handles both
        new user creation and returning existing users.

        This prevents errors when new users interact with backend-dependent features.
        """
        from ..clients.mlai_backend import MLAIBackendClient
        from ..slack_client import get_user_info

        try:
            backend = MLAIBackendClient()

            # Get Slack user profile
            slack_info = get_user_info(user_id)

            # Email is required by the backend
            email = slack_info.get("email")
            if not email:
                # If no email in Slack profile, generate a fallback
                email = f"{user_id}@slack.generated"
                print(f"⚠️ No email found for {user_id}, using generated: {email}")

            # Parse first_name and last_name from real_name
            real_name = slack_info.get("real_name", "")
            name_parts = real_name.split(" ", 1) if real_name else []
            first_name = name_parts[0] if len(name_parts) > 0 else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Get avatar URL (192x192 size is good for profiles)
            # Note: Slack user info structure varies, handle both formats
            avatar_url = slack_info.get("image_192")  # Direct from our get_user_info

            # Register/fetch user using the new endpoint
            # This endpoint returns existing users or creates new ones
            result = await backend.ensure_slack_user_registered(
                slack_id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url
            )

            if result.get("created"):
                print(f"✅ Created new user: {email} (Slack ID: {user_id})")
            else:
                if result.get("linked"):
                    print(f"✅ Linked Slack ID {user_id} to existing user: {email}")
                # Silently pass for existing users (no need to log on every message)

        except Exception as e:
            # Don't fail the request if user registration fails
            # The MedHack client will handle missing users gracefully with local fallback
            print(f"⚠️ Failed to register user {user_id}: {e}")
            print(f"   MedHack will continue with local JSON fallback")

    async def _execute_medhack(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
    ) -> str:
        """Execute the medhack skill: event Q&A and Guess the Diagnosis game."""
        from ..utils import get_current_date

        # Channel restriction: medhack only works in designated channels
        if skill.exclusive_channels and channel_id:
            from ..slack_client import get_channel_name
            channel_name = get_channel_name(channel_id)
            if channel_name and channel_name not in skill.exclusive_channels:
                channels_list = ", ".join(f"#*{ch}*" for ch in skill.exclusive_channels)
                return (
                    f"The MedHack skill is only available in {channels_list}. "
                    f"Head over there to ask about the event or play Guess the Diagnosis!"
                )

        # Ensure user exists in backend (auto-create if needed)
        await self._ensure_user_exists(user_id)

        # Load the MedHackClient from the skill module
        ClientClass = skill.get_client_class("MedHackClient")
        if not ClientClass:
            return await self._execute_with_llm(skill, text, params, user_id, thread_history)

        client = ClientClass()
        today = get_current_date()
        text_lower = text.lower()

        # --- Admin: manual case start & reset ---
        MEDHACK_ADMIN_ID = "U05QPB483K9"
        import re

        # Check for reset command first
        reset_pattern = r"(?:reset|clear)\s+(?:medhack|game)"
        if re.search(reset_pattern, text_lower):
            if user_id != MEDHACK_ADMIN_ID:
                return f"<@{user_id}> Sorry, only admins can reset the game."

            # Reset local game state
            from pathlib import Path
            skill_dir = Path(__file__).parent.parent.parent / "skills" / "medhack"
            data_dir = Path("/app/data") if Path("/app/data").exists() else skill_dir
            game_state_file = data_dir / "medhack_game_state.json"

            if game_state_file.exists():
                game_state_file.unlink()
                print(f"✅ Deleted local game state: {game_state_file}")
                status_msg = "Local game state file deleted."
            else:
                print(f"ℹ️ No game state file to delete: {game_state_file}")
                status_msg = "No local game state found (already clean)."

            return (
                f"✅ *MedHack game reset complete!*\n\n"
                f"{status_msg}\n\n"
                "_Note: Backend state is NOT automatically cleared. If you need to clear backend guesses/winners, "
                "contact the backend admin or use the backend admin panel._\n\n"
                f"To start case 1, say: `@roo start patient 1`"
            )

        admin_patterns = [
            r"(?:give me|start|begin|launch|lets begin|let's begin)\s+patient\s+(\d+)",
            r"(?:next patient|next case)",
        ]
        admin_match = None
        requested_case_id = None
        for pattern in admin_patterns:
            m = re.search(pattern, text_lower)
            if m:
                admin_match = m
                if m.groups():
                    requested_case_id = int(m.group(1))
                break

        if admin_match:
            if user_id != MEDHACK_ADMIN_ID:
                return f"<@{user_id}> Sorry, only admins can start new cases."

            from ..slack_client import post_message

            if requested_case_id is not None:
                new_case = await client.start_specific_case(requested_case_id, today, admin_slack_id=user_id)
                if not new_case:
                    available = client.get_all_case_ids()
                    return f"Case #{requested_case_id} not found. Available case IDs: {available}"
            else:
                # "next patient" — pick the next unplayed case
                new_case = await client.start_new_case(today, admin_slack_id=user_id)
                if not new_case:
                    return "All cases have been played! No new cases available."

            # Format and post as new top-level message
            title = new_case.get("title", "Daily Case")
            difficulty = new_case.get("difficulty", "medium").upper()
            header = f"*GUESS THE DIAGNOSIS* - Daily Challenge [{difficulty}] - _{title}_"
            complaint = (new_case.get("ed_first_look") or new_case["presenting_complaint"]).strip()
            triage = new_case["presenting_complaint"].strip()

            if new_case.get("ed_first_look"):
                message = (
                    f"{header}\n\n"
                    f"{complaint}\n\n"
                    f"*Triage note:* {triage}\n\n"
                    f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                    f"Ask me anything you'd ask them and I'll relay their answer. "
                    f"You can also request examinations and investigations, but be specific — "
                    f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                    f"When you're ready, tell me your diagnosis!\n\n"
                    f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                    f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                )
            else:
                message = (
                    f"{header}\n\n"
                    f"{complaint}\n\n"
                    f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                    f"Ask me anything you'd ask them and I'll relay their answer. "
                    f"You can also request examinations and investigations, but be specific — "
                    f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                    f"When you're ready, tell me your diagnosis!\n\n"
                    f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                    f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                )

            # Post as new top-level message (not in thread)
            image_url = new_case.get("image_url", "")
            if image_url and channel_id:
                blocks = [
                    {"type": "image", "image_url": image_url, "alt_text": f"Guess the Diagnosis - {title}"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": message}},
                ]
                post_message(channel=channel_id, text=message, blocks=blocks)
            elif channel_id:
                post_message(channel=channel_id, text=message)

            return f"Patient #{new_case['id']} ({title}) is now live!"

        # --- Announcement creation ---
        MEDHACK_ANNOUNCE_ADMIN_IDS = ["U08DD0DCL4D", "U05QPB483K9", "U08CWAPMQH0", "U07QJ5L0EHY"]
        announce_keywords = ["announce", "announcement", "post announcement", "create announcement"]
        is_announcement = any(k in text_lower for k in announce_keywords)

        if is_announcement:
            if user_id not in MEDHACK_ANNOUNCE_ADMIN_IDS:
                return f"<@{user_id}> Sorry, only authorized MedHack admins can create announcements."

            # Use LLM to extract title and body
            extract_prompt = f"""Extract the announcement title and body from this message.
The user wants to create an announcement for the MedHack: Frontiers website.

User message: "{text}"

Return ONLY valid JSON with two keys: "title" and "body".
If you cannot determine a clear title or body from the message, set the missing field to null.

Example: {{"title": "Workshop Schedule Update", "body": "The AI workshop has been moved to Room 3B at 2pm."}}

JSON:"""
            openai_client = get_llm_client("openai")
            extract_response = await openai_client.chat([
                {"role": "system", "content": "You extract structured data from text. Return valid JSON only."},
                {"role": "user", "content": extract_prompt}
            ], model="gpt-4o-mini", max_tokens=1024)

            try:
                content = extract_response.content.strip()
                if content.startswith("```"):
                    content = re.sub(r'^```\w*\n?', '', content)
                    content = re.sub(r'\n?```$', '', content)
                extracted = json.loads(content)
            except json.JSONDecodeError:
                extracted = {}

            ann_title = extracted.get("title")
            ann_body = extracted.get("body")

            if not ann_title or not ann_body:
                return (
                    f"<@{user_id}> I need both a *title* and *body* for the announcement. "
                    f"Please try again with something like:\n"
                    f"_\"Create an announcement titled 'Workshop Update' with body 'The AI workshop is moved to 2pm.'\"_"
                )

            # Call the backend — use Roo's bot user ID so the announcement
            # author avatar is always Roo's, not the requesting human's.
            from ..clients.mlai_backend import MLAIBackendClient
            from ..slack_client import get_bot_user_id
            backend = MLAIBackendClient()
            bot_id = get_bot_user_id()
            result = await backend.medhack_create_announcement(ann_title, ann_body, bot_id or user_id)

            if result is None:
                return f"<@{user_id}> Something went wrong creating the announcement. Please try again later."

            status_code = result.get("status_code")
            if status_code == 400:
                return f"<@{user_id}> The announcement couldn't be created — the server said something is missing. Details: {result.get('detail', 'unknown')}"
            if status_code in (401, 403):
                return f"<@{user_id}> Authorization error creating the announcement. Please contact an admin."
            if status_code is not None:
                return f"<@{user_id}> Unexpected error (HTTP {status_code}): {result.get('detail', 'unknown')}"

            # Success — confirm and post to channel
            confirm_msg = f"Announcement *\"{ann_title}\"* has been posted to the MedHack: Frontiers website."
            if channel_id:
                from ..slack_client import post_message
                post_message(channel=channel_id, text=confirm_msg, thread_ts=thread_ts)

            return confirm_msg

        # --- Determine mode: event info vs diagnosis game ---
        event_keywords = ["when", "where", "ticket", "register", "schedule", "speaker",
                          "venue", "price", "event", "medhack", "frontiers", "sign up"]
        game_keywords = ["patient", "diagnos", "symptom", "exam", "blood", "ecg",
                         "x-ray", "xray", "ct", "mri", "imaging", "investig",
                         "history", "vitals", "murmur", "case", "present",
                         "i think", "my guess", "is it", "could it be"]

        is_event_q = any(k in text_lower for k in event_keywords)
        is_game_q = any(k in text_lower for k in game_keywords)

        # Patterns for confirmation/lock-in only
        confirm_only_patterns = ["lock in", "lock it in", "final answer"]
        is_lock_in = any(p in text_lower for p in confirm_only_patterns)

        # Confirmation patterns for locking in a pending guess
        confirm_patterns = ["yes", "yeah", "yep", "yup", "lock in", "lock it in",
                            "final answer", "confirm", "do it", "go for it",
                            "that's my guess", "sure", "absolutely"]
        cancel_patterns = ["no", "nah", "nope", "cancel", "never mind", "keep going",
                           "not yet", "wait", "hold on", "keep digging"]

        current_case = await client.get_current_case(today)

        # --- Check for pending guess confirmation/cancellation ---
        pending_guess = (await client.get_pending_guess(user_id)) if current_case else None
        if pending_guess and current_case and not current_case.get("solved"):
            is_confirm = any(p in text_lower for p in confirm_patterns)
            is_cancel = any(p in text_lower for p in cancel_patterns)

            if is_confirm:
                # Lock in the pending guess
                await client.clear_pending_guess(user_id)
                result = await client.check_guess(user_id, pending_guess, today)
                return await self._handle_guess_result(
                    result, user_id, skill, text, client, today,
                    thread_history, channel_id, pending_guess
                )

            elif is_cancel:
                await client.clear_pending_guess(user_id)
                return (
                    f"<@{user_id}> No worries — guess cancelled. "
                    f"Keep investigating and lock in your diagnosis when you're ready. "
                    f"Remember, you only get *one guess* per case!"
                )

            # If they said something else while having a pending guess,
            # remind them (but also let the LLM respond to their question)
            # Clear the pending guess so it doesn't block future interactions
            await client.clear_pending_guess(user_id)

        # --- "Lock it in" with no pending guess ---
        if is_lock_in and not pending_guess and current_case and not current_case.get("solved"):
            if await client.is_user_locked_out(user_id, today):
                return (
                    f"<@{user_id}> Sorry mate, you've already used your guess for today's case. "
                    "Come back tomorrow for a new one!"
                )
            return (
                f"<@{user_id}> I'm not sure what diagnosis you want to lock in. "
                f"Tell me your guess and I'll ask you to confirm before locking it in."
            )

        # --- Repost the daily case (with image) ---
        repost_patterns = ["post the", "show the case", "show me the case",
                           "post again", "start again", "from the start",
                           "show the patient", "post the patient",
                           "present the case", "daily patient"]
        if current_case and any(p in text_lower for p in repost_patterns):
            complaint = current_case.get("ed_first_look") or current_case.get("presenting_complaint", "")
            title = current_case.get("title", "Daily Case")
            header = f"*Guess the Diagnosis — {title}*"
            message = (
                f"{header}\n\n"
                f"{complaint}\n\n"
                f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                f"Ask me anything you'd ask them and I'll relay their answer. "
                f"You can also request examinations and investigations, but be specific — "
                f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                f"When you're ready, tell me your diagnosis!\n\n"
                f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
            )
            return self._medhack_game_response(message, current_case.get("image_url", ""))

        # --- Solved case ---
        if current_case and current_case.get("solved"):
            diagnosis_name = "already revealed"
            cases_data = client._load_cases()
            solved_case = next((c for c in cases_data if c["id"] == current_case["id"]), None)
            if solved_case:
                diagnosis_name = solved_case["diagnosis"]
            winners = current_case.get("winners", [])
            winner_mentions = ", ".join(f"<@{w}>" for w in winners)
            return (
                f"Today's case has been solved! The diagnosis was *{diagnosis_name}*.\n\n"
                f"Solved by: {winner_mentions}\n\n"
                f"Come back tomorrow for a new case!"
            )

        # --- Locked out ---
        if current_case and await client.is_user_locked_out(user_id, today):
            return (
                f"<@{user_id}> Sorry mate, you've already used your guess for today's case "
                "so you can no longer interact with it. Come back tomorrow for a new one!"
            )

        # --- Active unsolved case: use LLM to classify intent ---
        if current_case and not current_case.get("solved") and not is_event_q:
            classification = await self._classify_medhack_intent(text)

            if classification.get("is_guess") and classification.get("diagnosis"):
                guess_text = classification["diagnosis"]
                await client.set_pending_guess(user_id, guess_text)
                return (
                    f"<@{user_id}> You want to lock in *{guess_text}* as your final diagnosis?\n\n"
                    f"_Remember: you only get *one guess* per case. "
                    f"Reply *yes* to confirm or *no* to keep investigating._"
                )

            # Not a guess — respond as PQM narrator
            case_data = await client.get_case_for_llm(today)
            llm_response = await self._medhack_llm_response(skill, text, case_data, thread_history)
            return f"<@{user_id}> {llm_response}"

        if not current_case and is_game_q:
            return (
                "No active case right now! A new clinical case is posted each day. "
                "Keep an eye on this channel for the next one."
            )

        # --- Event info mode ---
        if is_event_q or (not is_game_q):
            event_info = client.load_event_info()
            import yaml
            event_info_str = yaml.dump(event_info, default_flow_style=False)

            system_prompt = """You are Roo, acting as the "MedHack Frontiers 2026 Info Pack Q&A Assistant".

Rules:
- Use ONLY the event information provided below. Do not guess. Do not invent details that are not present.
- If a question cannot be answered from the event information, say so and ask a clarifying question or suggest contacting the organisers (email: info@mymi.org.au or hi@mlai.au).
- When you answer, cite where you found it using "(Source: <section name>)" based on the data sections.
- Prefer short, direct answers. Use bullet points for schedules, lists, and criteria.
- Keep names, dates, and times exactly as written in the event information.
- If the user asks for sponsor details, include them.

Answer format:
- Start with the answer.
- Then add "Sources: <section>".

Be friendly, enthusiastic, and helpful. If information is listed as "TBD", say the details haven't been announced yet and suggest they keep an eye on the channel for updates."""

            prompt = f"""Here is the complete event information:
{event_info_str}

{skill.content}

User's question: "{text}"

Previous conversation (if any):
{thread_history if thread_history else 'None'}

Answer the question using ONLY the event data above. Keep your answer concise."""

            openai_client = get_llm_client("openai")
            response = await openai_client.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ], model="gpt-5", max_tokens=4096, reasoning_effort="high")
            return response.content

        return await self._execute_with_llm(skill, text, params, user_id, thread_history)

    async def _classify_medhack_intent(self, text: str) -> dict:
        """Use LLM to classify if a message is a diagnosis guess.

        Returns dict with:
            is_guess (bool): whether the user is guessing a diagnosis
            diagnosis (str|None): the extracted diagnosis if is_guess
        """
        prompt = f"""You are classifying messages in a medical diagnosis guessing game. Players interact with a simulated patient and can ask questions or guess the diagnosis.

A message is a DIAGNOSIS GUESS if the player is proposing what they think the medical diagnosis is. Examples:
- "I think it's pneumonia" → guess: "pneumonia"
- "Is it gastroenteritis?" → guess: "gastroenteritis"
- "I guess Addison's disease" → guess: "Addison's disease"
- "She has COPD" → guess: "COPD"
- "Could it be lupus?" → guess: "lupus"
- "My diagnosis is acute appendicitis" → guess: "acute appendicitis"
- "gastroenteritis!" → guess: "gastroenteritis"

NOT a guess (these are clinical questions or requests):
- "What are her vitals?"
- "Can I see the blood results?"
- "Does she have any allergies?" (asking about patient history, not guessing)
- "Order a chest X-ray"
- "Tell me about her symptoms"
- "What medications is she on?"

Classify this message: "{text}"

Respond with ONLY valid JSON, no markdown:
{{"is_guess": true, "diagnosis": "the diagnosis"}} or {{"is_guess": false, "diagnosis": null}}"""

        openai_client = get_llm_client("openai")
        response = await openai_client.chat([
            {"role": "user", "content": prompt}
        ], model="gpt-4o-mini", max_tokens=100)

        import json as _json
        try:
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return _json.loads(content)
        except (ValueError, KeyError, IndexError):
            return {"is_guess": False, "diagnosis": None}

    def _medhack_game_response(self, text: str, image_url: str = "") -> dict | str:
        """Wrap a game response with image blocks when the case has an image_url."""
        if image_url:
            return {
                "message": text,
                "blocks": [
                    {
                        "type": "image",
                        "image_url": image_url,
                        "alt_text": "Patient case image",
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    },
                ],
            }
        return text

    async def _handle_guess_result(
        self, result: dict, user_id: str, skill, text: str,
        client, today, thread_history, channel_id: str, guess_text: str,
    ) -> str:
        """Process the result of a locked-in guess. NOTE: Currently disabled."""
        return f"<@{user_id}> The diagnosis game is currently disabled. Stay tuned for updates!"
        # --- DISABLED: original implementation below ---
        from ..slack_client import post_message

        if result["correct"]:
            diagnosis = result["diagnosis"]
            thread_reply = f"<@{user_id}> *CORRECT!* The diagnosis is *{diagnosis}*! Well done!"

            try:
                announcement_parts = [
                    f"*DIAGNOSIS SOLVED!*\n\n"
                    f"<@{user_id}> correctly diagnosed today's case: *{diagnosis}*!"
                ]

                if result.get("is_first_solver"):
                    announcement_parts.append(
                        "They're the first to crack it! DM Dr Sam for a free ticket code to MedHack: Frontiers!"
                    )

                points_msg = ""
                try:
                    from ..config import get_settings
                    from ..slack_client import get_bot_user_id
                    settings = get_settings()

                    if settings.MLAI_BACKEND_URL and settings.MLAI_API_KEY:
                        from ..clients.mlai_backend import MLAIBackendClient
                        points_client = MLAIBackendClient(
                            base_url=settings.MLAI_BACKEND_URL,
                            api_key=settings.MLAI_API_KEY,
                            internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
                        )
                        bot_id = get_bot_user_id()
                        diagnosis_points = 12
                        await points_client.system_award_points(
                            admin_slack_id=bot_id,
                            target_slack_id=user_id,
                            points=diagnosis_points,
                            reason="Correct diagnosis in Guess the Diagnosis game"
                        )
                        points_msg = f"\n\n+{diagnosis_points} MLAI points awarded!"
                except Exception as e:
                    print(f"⚠️ Failed to award diagnosis points: {e}")

                announcement_text = "\n\n".join(announcement_parts) + points_msg

                if channel_id:
                    post_message(channel=channel_id, text=announcement_text)
            except Exception as e:
                print(f"⚠️ Failed to post win announcement: {e}")

            return thread_reply

        elif result.get("already_solved"):
            return f"<@{user_id}> You've already solved today's case! Nice work earlier. Come back tomorrow for a new one."

        elif result.get("message") == "no_guesses_remaining":
            return (
                f"<@{user_id}> Sorry mate, you've already used your guess for today's case. "
                "Come back tomorrow for a new one!"
            )

        else:
            # Wrong guess
            case_data = await client.get_case_for_llm(today)
            llm_response = await self._medhack_llm_response(
                skill, text, case_data, thread_history,
                extra_instruction="The user just locked in an INCORRECT diagnosis guess. "
                "Respond clinically: suggest they review the findings again. "
                "Do NOT reveal the correct diagnosis or hint at it. "
                "Let them know their guess was wrong and they're out for today's case."
            )
            return f"<@{user_id}> {llm_response}\n\n_That was your one guess for this case. Better luck tomorrow!_"

    async def _medhack_llm_response(
        self,
        skill: Skill,
        text: str,
        case_data: Optional[dict],
        thread_history: Optional[List[dict]] = None,
        extra_instruction: str = "",
    ) -> str:
        """Generate an in-character clinical response for the diagnosis game.
        NOTE: Currently disabled.
        """
        return "The patient simulator is currently disabled. Stay tuned for updates!"
        # --- DISABLED: original implementation below ---
        #
        # Thread history is included so the LLM can follow the conversation flow
        # within a thread. Each patient case lives in its own Slack thread, so
        # thread history is safe to pass and provides useful conversational context.
        #
        # import yaml
        #
        # case_str = yaml.dump(case_data, default_flow_style=False) if case_data else "No case data available."
        # hints_str = ""
        # if case_data and case_data.get("revealed_hints"):
        #     hints_str = "\n\nHints already given:\n" + "\n".join(
        #         f"- {h}" for h in case_data["revealed_hints"]
        #     )
        #
        # system_prompt = (long prompt omitted)
        #
        # thread_context = ""
        # if thread_history:
        #     thread_context = f"\n\nPrevious conversation in this thread:\n{thread_history}"
        #
        # prompt = (long prompt omitted)
        #
        # openai_client = get_llm_client("openai")
        # response = await openai_client.chat([
        #     {"role": "system", "content": system_prompt},
        #     {"role": "user", "content": prompt}
        # ], model="gpt-5", max_tokens=4096, reasoning_effort="high")
        # return response.content
        # --- END DISABLED ---

    async def _execute_with_llm(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        history: Optional[List[dict]] = None
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

Previous Conversation Context (if any):
{history if history else 'None'}

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
        thread_ts: Optional[str],
        thread_history: Optional[List[dict]] = None
    ) -> str:
        """Execute the content factory generation workflow."""
        
        # 0. Access Control (Private Beta)
        ALLOWED_USER_ID = "U05QPB483K9"
        if user_id != ALLOWED_USER_ID:
            return "Sorry mate, this skill is currently in private beta. 🔒"
        
        # Get a MLAIBackendClient for API calls
        settings = get_settings()
        from roo.clients.mlai_backend import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
        )
        domain = params.get("domain")
        org_config_cached = None

        # Check Status of GitHub Integration
        integration = await api_client.get_integration(user_id)
        
        # 1. New User Disclaimer & Education
        # If user has no integration AND hasn't confirmed the disclaimer yet
        if not integration and not params.get("confirmed"):
            # Save pending intent so we don't lose the original params (domain/topic)
            intent_data = json.dumps({
                "skill": "content-factory",
                "params": params,
                "text": text,
                "channel": channel_id,
                "ts": thread_ts
            })
            await api_client.save_pending_intent(user_id, intent_data)
            
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ Content Factory Requirements",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Before we start, a quick heads-up! This skill works best with **Next.js & Tailwind CSS** projects "
                            "and requires a connected **GitHub repository**.\n\n"
                            "*How it works:*\n"
                            "1. 🏗️ **Scans** your repo for blog structure (creates it if missing)\n"
                            "2. 🧩 **Checks** for reusable components (Hero, CTAs) or creates them\n"
                            "3. 🎨 **Creates** a design guide to match your site's style\n"
                            "4. 🚀 **Publishes** a Pull Request for your review"
                        )
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "I'm ready to proceed",
                                "emoji": True
                            },
                            "action_id": "confirm_content_factory",
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please review the requirements above.", thread_ts=thread_ts, blocks=blocks)
                return "Please review the requirements above to get started! 👆"
            return "Please confirm you have a Next.js/Tailwind project and are ready to connect GitHub."

        # Check for Expired Token or Other Errors
        if integration and integration.get("error"):
            auth_url = integration.get("auth_url")
            error_msg = integration.get("error")
            
            if not auth_url:
                # Fallback if auth_url missing in error response
                auth_url_resp = await api_client.get_github_auth_url(user_id, domain=domain)
                auth_url = auth_url_resp.get("auth_url")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ **Connection Issue**: {error_msg}\nI need you to re-connect your GitHub account to continue."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Re-connect GitHub",
                                "emoji": True
                            },
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "danger"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🚀 I've Connected - Resume",
                                "emoji": True
                            },
                            "action_id": "resume_scan",
                            "value": "resume_scan",
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return "Please re-connect your GitHub account using the button above. 🔌"
            return f"GitHub connection issue ({error_msg}). Please re-connect here: {auth_url}"

        if not integration:
             # Get Auth URL from Backend
            auth_response = await api_client.get_github_auth_url(user_id, domain=domain)
            auth_url = auth_response.get("auth_url")
            
            if not auth_url:
                return "Sorry mate, I couldn't get the authorization URL from the backend. Try again strictly?"
            
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
            # Critically: Do NOT overwrite the intent if we are just confirming requirements (which has no domain/topic)
            if not params.get("confirmed"):
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

        # 2. Resolve domain from connected_domains
        connected_domains = integration.get("connected_domains", [])

        if not domain:
            if len(connected_domains) == 0:
                # No domains connected — fall back to org config lookup
                org_config_cached = await api_client.get_content_org_config(
                    slack_user_id=user_id
                )
                if org_config_cached:
                    domain = org_config_cached.get("domain")
            elif len(connected_domains) == 1:
                # Single domain — use it automatically
                domain = connected_domains[0].get("domain")
            else:
                # Multiple domains — ask user to choose
                domain_list = "\n".join(
                    f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                    for d in connected_domains
                )
                msg = f"You have multiple connected codebases. Which one should I work with?\n\n{domain_list}\n\nTry: `@Roo scan <domain>` or `@Roo write an article for <domain>`"
                if channel_id:
                    post_message(channel_id, msg, thread_ts)
                return msg

        # Look up repo from connected_domains
        repo_name = None
        domain_info = None
        if domain and connected_domains:
            domain_info = next(
                (d for d in connected_domains if d.get("domain") == domain), None
            )
            if domain_info:
                repo_name = domain_info.get("github_repo")

        # Fallback to top-level github_repo if domain not in connected_domains
        if not repo_name:
            repo_name = integration.get("github_repo")

        # No repo at all — prompt to connect
        if not repo_name:
            auth_response = await api_client.get_github_auth_url(user_id, domain=domain)
            auth_url = auth_response.get("auth_url")

            if not auth_url:
                return "Sorry mate, I couldn't get the authorization URL. Try again?"

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "I see you're connected, but no repository is selected. Click below to choose one."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Re-connect & Select Repo",
                                "emoji": True
                            },
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "primary"
                        }
                    ]
                }
            ]

            if channel_id:
                post_message(channel_id, "Please select a repository", thread_ts=thread_ts, blocks=blocks)
                return "Please choose a repository to use with the Content Factory. 🔌"
            return f"Please select a repository here: {auth_url}"

        # 3. Handle explicit scaffold action
        action = params.get("action")
        if action == "scaffold":
            if not domain:
                return "I need a domain to scaffold the articles directory. Try: `@Roo scaffold articles for <domain>`"

            # Check scan prerequisite
            if domain_info and not domain_info.get("scanned"):
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "prerequisite_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                "style": "primary",
                                "value": json.dumps({
                                    "domain": domain,
                                    "slack_user_id": user_id,
                                    "channel_id": channel_id,
                                    "thread_ts": thread_ts,
                                    "original_intent": {"action": "scaffold"}
                                }),
                                "action_id": "prerequisite_scan"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                "value": json.dumps({"domain": domain}),
                                "action_id": "prerequisite_cancel"
                            }
                        ]
                    }
                ]
                if channel_id:
                    post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                return "I need to scan your codebase first before I can scaffold the articles directory."

            if channel_id:
                post_message(
                    channel_id,
                    f"📁 Creating articles directory for *{domain}*...",
                    thread_ts
                )

            try:
                result = await api_client.scaffold_articles(
                    domain=domain,
                    slack_user_id=user_id,
                    slack_channel_id=channel_id or "",
                    slack_thread_ts=thread_ts or ""
                )

                status_code = result.get("status_code")
                data = result.get("data", {})

                if status_code == 200:
                    pr_url = data.get("pr_url", "")
                    pr_text = f" <{pr_url}|View PR>" if pr_url else ""
                    return f"📁 Articles directory already exists for *{domain}*.{pr_text}"
                elif status_code == 202:
                    return "Scaffolding is underway! I'll reply here when it's done. 🏗️"
                elif status_code == 412:
                    missing_step = data.get("missing_step", "scan")
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps({
                                        "domain": domain,
                                        "slack_user_id": user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "original_intent": {"action": "scaffold"}
                                    }),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps({"domain": domain}),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif status_code == 400:
                    if data.get("needs_github_auth"):
                        oauth_url = data.get("oauth_url", "")
                        return f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {oauth_url}"
                    return f"❌ Could not start scaffolding: {data.get('error', 'Unknown error')}"
                elif status_code == 404:
                    return f"❌ No configuration found for *{domain}*."
                else:
                    return f"❌ Unexpected response from backend (status {status_code})"
            except Exception as e:
                print(f"❌ Failed to trigger scaffold: {e}")
                return f"❌ Error creating articles directory: {e}"

        # 4. Check scan status
        needs_scan = False
        scan_reason = ""

        if domain_info:
            # Use domain-specific scan status
            if not domain_info.get("scanned"):
                needs_scan = True
                scan_reason = "Initial scan required"
        elif not integration.get("project_scanned"):
            needs_scan = True
            scan_reason = "Initial scan required"

        if integration.get("has_updates"):
            needs_scan = True
            scan_reason = "🔄 Updates detected in repository"
            
        # Compile Status Report
        last_scanned = integration.get("last_scanned_at", "Never")
        last_article = (integration.get("last_article") or {}).get("title", "None")

        if channel_id:
            if domain:
                connected_msg = f"👋 G'day! Connected to `{repo_name}` for *{domain}*.\n\n"
            else:
                connected_msg = f"👋 G'day! I see you're connected to `{repo_name}`.\n\n"
            status_msg = (
                connected_msg +
                f"📊 **Status Report:**\n"
                f"• Last scanned: {last_scanned}\n"
                f"• Last article: {last_article}\n"
            )
            if needs_scan:
                status_msg += f"\n{scan_reason}. Scanning updates now... 🕵️"
            else:
                status_msg += "• Repository: ✅ Up to date"

            post_message(channel_id, status_msg, thread_ts)

        if needs_scan:
            scan_result = await api_client.trigger_repo_scan(
                user_id, 
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain
            )
            
            if scan_result.get("status") == "accepted":
                return "updates are being processed in the background! 🏃\nI'll reply here when the scan is complete."
                
            # Handle sync failures or other errors
            if scan_result.get("error"):
                error_msg = scan_result.get("message", "Unknown error")

                # Multiple domains — backend needs user to choose
                if scan_result.get("error") == "multiple_domains":
                    available = scan_result.get("available_domains", [])
                    domain_list = "\n".join(
                        f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                        for d in available
                    )
                    msg = f"{error_msg}\n\n{domain_list}\n\nTry: `@Roo scan <domain>`"
                    if channel_id:
                        post_message(channel_id, msg, thread_ts)
                    return msg

                # If backend says GitHub isn't connected for this domain
                if scan_result.get("needs_github_auth"):
                    oauth_url = scan_result.get("oauth_url")
                    domain_name = scan_result.get("domain", domain)
                    if oauth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                            return f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌"
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account and try again."

                # If error indicates auth failure or repo not found (404/403/401)
                if any(code in str(error_msg) for code in ["404", "401", "403", "Not Found", "Bad credentials"]):
                    # Fetch Auth URL to allow reconnect
                    auth_response = await api_client.get_github_auth_url(user_id, domain=domain)
                    auth_url = auth_response.get("auth_url")
                    
                    if auth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ **Connection Lost**: It looks like I can't access your repository anymore ({error_msg})."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Re-connect GitHub App",
                                            "emoji": True
                                        },
                                        "url": auth_url,
                                        "action_id": "connect_github",
                                        "style": "danger"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                            return "Please re-connect your GitHub App using the button above. 🔌"
                
                return f"Had some trouble scanning your repository: {error_msg}"


            # Legacy Sync Behavior (if backend returns 200 immediately)
            # Scan succeeded - refresh integration status
            integration = await api_client.get_integration(user_id, domain=domain)
            if not integration or not integration.get("project_scanned"):
                return "Scanning is taking a bit longer than expected. Please wait for the notification! 🦘"
            
            if channel_id:
                post_message(channel_id, "✅ Repository analysis complete! Ready to write.", thread_ts)

        # 3. Validation: Check parameters (Domain/Topic)
        topic = params.get("topic")
        target_keyword = params.get("target_keyword", "")

        if not domain:
            return "I can help write that article! To get started, I just need to know the domain name (e.g., mlai.au)."

        # Check scaffold prerequisite before article generation
        if domain_info and not domain_info.get("articles_scaffolded"):
            original_intent = {"action": "write"}
            if topic:
                original_intent["topic"] = topic
            if target_keyword:
                original_intent["target_keyword"] = target_keyword

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"I need to set up your articles directory before I can write articles for *{domain}*.\n\nThis will create a PR with all the reusable components and a demo article so you can see how everything looks."
                    }
                },
                {
                    "type": "actions",
                    "block_id": "prerequisite_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                            "style": "primary",
                            "value": json.dumps({
                                "domain": domain,
                                "slack_user_id": user_id,
                                "channel_id": channel_id,
                                "thread_ts": thread_ts,
                                "original_intent": original_intent
                            }),
                            "action_id": "prerequisite_scaffold"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                            "value": json.dumps({"domain": domain}),
                            "action_id": "prerequisite_cancel"
                        }
                    ]
                }
            ]
            if channel_id:
                post_message(channel_id, f"Articles directory needed for {domain}", thread_ts=thread_ts, blocks=blocks)
            return "I need to set up your articles directory before I can write articles."

        try:
            # Start generation via MLAI Backend
            # Enhance context with thread history if available
            full_context = text
            if thread_history:
                history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in thread_history[:-1]])
                full_context = f"Context from Thread:\n{history_str}\n\nCurrent Request: {text}"
            
            # Note: topic can be None (triggers Auto-Write / Research Mode)
            response = await api_client.trigger_article_generation(
                slack_user_id=user_id,
                domain=domain,
                topic=topic,
                target_keyword=target_keyword,
                context=full_context,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts
            )
            
            job_id = response.get("job_id")
            if not job_id:
                return "Failed to start generation: No job ID returned from backend."

            # Launch background monitoring task
            if channel_id:
                asyncio.create_task(
                    self._monitor_generation(api_client, job_id, channel_id, thread_ts, user_id)
                )
            
            if topic:
                return f"You beauty! I've started writing the article '{topic}' for {domain}. (Job ID: {job_id})\nI'll keep you posted on the progress right here! 🚀"
            else:
                return f"You beauty! I've started researching the best article for {domain}. (Job ID: {job_id})\nI'll keep you posted on the progress right here! 🕵️"
            
        except httpx.HTTPStatusError as e:
            print(f"Content Generation HTTP Error: {e}")
            if e.response.status_code == 412:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                missing_step = error_data.get("missing_step", "")
                if missing_step == "scan":
                    original_intent = {"action": "write"}
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps({
                                        "domain": domain,
                                        "slack_user_id": user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "original_intent": original_intent
                                    }),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps({"domain": domain}),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif missing_step == "scaffold":
                    original_intent = {"action": "write"}
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to set up your articles directory before I can write articles for *{domain}*.\n\nThis will create a PR with all the reusable components and a demo article so you can see how everything looks."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps({
                                        "domain": domain,
                                        "slack_user_id": user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "original_intent": original_intent
                                    }),
                                    "action_id": "prerequisite_scaffold"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps({"domain": domain}),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scaffold required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to set up your articles directory first."
                else:
                    return f"A prerequisite step is missing: {error_data.get('error', 'Unknown')}. Please try again."
            if e.response.status_code == 400:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                # Structured error: GitHub not connected for this domain
                if error_data.get("needs_github_auth"):
                    oauth_url = error_data.get("oauth_url")
                    domain_name = error_data.get("domain", domain)
                    if oauth_url and channel_id:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain, then try again."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                        return f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌"
                    elif oauth_url:
                        return f"GitHub isn't connected for {domain_name}. Connect here: {oauth_url}"
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account."
                # Other 400 errors - show the error message
                error_msg = error_data.get("error", str(e))
                return f"Sorry mate, I had trouble starting the article generation: {error_msg}"
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"
        except Exception as e:
            print(f"Content Generation Error: {e}")
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"

    async def _monitor_generation(
        self,
        client,  # This is now MLAIBackendClient
        job_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        slack_user_id: str
    ):
        """Monitor job progress and post updates to Slack."""
        last_progress = -1
        last_step = ""
        
        try:
            consecutive_failures = 0

            # Poll until completion
            while True:
                try:
                    status_data = await client.check_generation_status(job_id)
                    consecutive_failures = 0 # Reset on success
                    
                    state = status_data.get("status")
                    progress = status_data.get("progress", 0)
                    step = status_data.get("current_step", "unknown")
                    
                    # Update progress
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

                    if state == "completed":
                        break
                    elif state == "failed":
                        raise Exception(f"Job failed: {status_data.get('error', 'Unknown')}")
                        
                except Exception as loop_error:
                    # If it's the "Job failed" exception raised above, re-raise it to exit
                    if "Job failed" in str(loop_error):
                        raise loop_error
                        
                    consecutive_failures += 1
                    print(f"⚠️ Monitor polling failed ({consecutive_failures}/5): {loop_error}")
                    
                    if consecutive_failures >= 5:
                        raise Exception(f"Lost connection to backend after 5 attempts. Last error: {loop_error}")
                
                await asyncio.sleep(5.0)
            
            # Publish
            post_message(channel_id, "✨ Article generated! Publishing now...", thread_ts)
            
            publish_result = await client.publish_article(job_id, slack_user_id)
            
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
        # ClientClass = skill.get_client_class("MLAIBackendClient")
        from roo.clients.mlai_backend import MLAIBackendClient
        ClientClass = MLAIBackendClient
        
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
            return await self._execute_with_llm(skill, text, params, user_id, thread_history)

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
        from skills.mlai_points.client import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
        )
        
        # 1. Check for valid integration & handle errors
        integration = await api_client.get_integration(user_id)
        
        # Check for Expired Token or Other Errors (Same as content-factory)
        if integration and integration.get("error"):
            auth_url = integration.get("auth_url")
            error_msg = integration.get("error")
            
            if not auth_url:
                auth_url_resp = await api_client.get_github_auth_url(user_id)
                auth_url = auth_url_resp.get("auth_url")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ **Connection Issue**: {error_msg}\nI need you to re-connect your GitHub account to continue."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Re-connect GitHub",
                                "emoji": True
                            },
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "danger"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🚀 I've Connected - Resume",
                                "emoji": True
                            },
                            "action_id": "resume_scan",
                            "value": "resume_scan",
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return "Please re-connect your GitHub account using the button above. 🔌"
            return f"GitHub connection issue ({error_msg}). Please re-connect here: {auth_url}"

        if not integration:
            # Send Auth Button
            auth_response = await api_client.get_github_auth_url(user_id)
            auth_url = auth_response.get("auth_url")
            
            if not auth_url:
                return "Sorry mate, I couldn't communicate with the backend to get the auth URL."
            
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

        # 2. Determine Repo Name
        # Prefer integration repo
        repo_name = integration.get("github_repo")
        
        if not repo_name:
             # Logic C: Connected but no repo selected
             auth_response = await api_client.get_github_auth_url(user_id)
             auth_url = auth_response.get("auth_url")
             if channel_id:
                 # Reuse connection block but maybe change text logic if we wanted, for now simplistic
                 post_message(channel_id, f"You are connected but I don't see a repository linked. Please select one: {auth_url}", thread_ts=thread_ts)
                 return "Please select a repository to scan."
             return f"Please select a repository here: {auth_url}"

        # 3. Trigger Scan via Backend
        domain = params.get("domain")
        if channel_id:
            scan_msg = f"🔍 Requesting scan for `{repo_name}`..."
            post_message(channel_id, scan_msg, thread_ts=thread_ts)

        try:
            # Trigger via Backend Client
            scan_result = await api_client.trigger_repo_scan(
                user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain
            )
            
            if scan_result.get("error"):
                 return f"❌ Scan failed: {scan_result.get('message')}"
            
            # Backend might return status: 'started' or 'queued'
            return f"✅ Scan started for `{repo_name}`! I'll let you know when the backend updates."
            
        except Exception as e:
            print(f"GitHub Integration Scan Error: {e}")
            return f"Sorry mate, I had trouble triggering the scan: {str(e)}"
