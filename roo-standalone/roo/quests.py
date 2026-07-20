"""
MLAI Quests System

This module implements simple quests for user engagement.
"""
import asyncio
import re
from datetime import datetime
from typing import Dict, List, Optional
try:
    import zoneinfo
except ImportError:
    # Backport for python < 3.9
    from backports import zoneinfo

from .config import get_settings
from .clients.mlai_backend import MLAIBackendClient
from .slack_client import get_bot_user_id, post_message, get_channel_id

# Configuration for quests
QUESTS = {
    # Existing
    "connector": {
        "name": "Connector",
        "description": "React to 5 messages",
        "target_count": 5,
        "points": 2,
        "event_type": "reaction_added"
    },
    "helper": {
        "name": "Helper",
        "description": "Reply to 3 threads",
        "target_count": 3,
        "points": 2,
        "event_type": "message"
    },
    "first_contact": {
        "name": "First Contact",
        "description": "First post in #_start-here",
        "target_count": 1,
        "points": 4,
        "event_type": "message",
        "channel_name": "_start-here"
    },
    # New Quests
    "paper_trail": {
        "name": "Paper Trail",
        "points": 2,
        "target_count": 1,
        "pattern": r"arxiv\.org",
    },
    "git_pusher": {
        "name": "Git Pusher",
        "points": 2,
        "target_count": 1,
        "pattern": r"github\.com",
    },
    "model_citizen": {
        "name": "Model Citizen",
        "points": 2,
        "target_count": 1,
        "pattern": r"huggingface\.co",
    },
    "code_blooded": {
        "name": "Code Blooded",
        "points": 1,
        "target_count": 1,
        "pattern": r"```",
    },
    "show_off": {
        "name": "Show Off",
        "points": 4,
        "target_count": 1,
        "channel_name": "showcase"
    },
    "bug_basher": {
        "name": "Bug Basher",
        "points": 1,
        "target_count": 1,
        "channel_name": "bugs"
    },
    "melb_coffee": {
        "name": "Melb Coffee",
        "points": 1,
        "target_count": 1,
        "emojis": ["coffee", "flat_white", "espresso"]
    },
    "kangaroo_court": {
        "name": "Kangaroo Court",
        "points": 1,
        "target_count": 1,
        "emojis": ["kangaroo"]
    },
    "warm_welcome": {
        "name": "Warm Welcome",
        "points": 1,
        "target_count": 1,
        "reaction_channel": "_start-here"
    },
    "night_owl": {
        "name": "Night Owl",
        "points": 2,
        "target_count": 1,
        "time_start": 1, # 1 AM
        "time_end": 5    # 5 AM
    }
}

# In-memory tracking for simplicity (note: this resets on restart)
_quest_progress: Dict[str, Dict[str, int]] = {}
# Track completed quests (reset on restart for now)
_completed_quests: Dict[str, set] = {}

async def handle_quests(event: dict):
    """
    Main entry point for quest processing.
    Call this from main.py's slack_events.
    """
    event_type = event.get("type")
    user_id = event.get("user")

    if not user_id:
        return

    # --- Reaction Events ---
    if event_type == "reaction_added":
        # 1. Connector (Any reaction)
        await _update_progress(user_id, "connector")

        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel = item.get("channel")

        # 2. Melb Coffee
        if reaction in QUESTS["melb_coffee"]["emojis"]:
             await _update_progress(user_id, "melb_coffee")

        # 3. Kangaroo Court
        if reaction in QUESTS["kangaroo_court"]["emojis"]:
             await _update_progress(user_id, "kangaroo_court")

        # 4. Warm Welcome (React in #_start-here)
        # Note: In real app, check if message author != user_id
        start_here_id = get_channel_id("_start-here")
        if start_here_id and channel == start_here_id:
            await _update_progress(user_id, "warm_welcome")

    # --- Message Events ---
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "")
        channel = event.get("channel")
        ts = event.get("ts")
        is_thread = event.get("thread_ts") is not None

        # 5. Helper (Thread replies)
        if is_thread and event.get("thread_ts") != ts:
             await _update_progress(user_id, "helper")

        # 6. First Contact is handled by the durable start-here-introductions skill.

        # 7. Pattern Match Quests (Paper Trail, Git Pusher, etc)
        for q_id, q_data in QUESTS.items():
            if "pattern" in q_data:
                if re.search(q_data["pattern"], text, re.IGNORECASE):
                    await _update_progress(user_id, q_id)

        # 8. Channel Specific Quests (Show Off, Bug Basher)
        for q_id, q_data in QUESTS.items():
            if "channel_name" in q_data and q_id != "first_contact": # First contact handled separately
                target_id = get_channel_id(q_data["channel_name"])
                if target_id and channel == target_id:
                    # For showcase/bugs, we assume any post counts
                    if not is_thread: # usually top-level
                        await _update_progress(user_id, q_id)

        # 9. Night Owl
        if QUESTS["night_owl"].get("time_start"):
            try:
                # Use float ts to get datetime
                timestamp = float(ts)
                # Convert to Melbourne time
                from zoneinfo import ZoneInfo
                dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo("Australia/Melbourne"))
                hour = dt.hour
                if QUESTS["night_owl"]["time_start"] <= hour < QUESTS["night_owl"]["time_end"]:
                    await _update_progress(user_id, "night_owl")
            except Exception as e:
                print(f"⚠️ Night Owl check failed: {e}")

async def _update_progress(user_id: str, quest_id: str):
    """Update progress for a user on a specific quest."""
    if user_id not in _quest_progress:
        _quest_progress[user_id] = {}
    if user_id not in _completed_quests:
        _completed_quests[user_id] = set()

    # If already completed this session, skip
    if quest_id in _completed_quests[user_id]:
        return

    current = _quest_progress[user_id].get(quest_id, 0)
    target = QUESTS[quest_id]["target_count"]

    current += 1
    _quest_progress[user_id][quest_id] = current

    print(f"📊 Quest Progress: {user_id} - {quest_id}: {current}/{target}")

    if current >= target:
        _completed_quests[user_id].add(quest_id)
        await _complete_quest(user_id, quest_id)

async def _complete_quest(user_id: str, quest_id: str):
    """Award points and notify user of quest completion."""
    quest = QUESTS[quest_id]
    points = quest["points"]
    name = quest["name"]

    print(f"🎉 Quest Complete: {user_id} completed {name}!")

    settings = get_settings()
    points_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
    )

    try:
        bot_id = get_bot_user_id()
        if not bot_id:
            print("⚠️ Cannot award quest points: Bot ID not found")
            return

        # Award points
        await points_client.system_award_points(
            admin_slack_id=bot_id,
            target_slack_id=user_id,
            points=points,
            reason=f"Completed quest: {name}"
        )

        # Send DM to user
        from .slack_client import send_dm
        send_dm(
            user_id,
            f"🏆 *Quest Complete!* \n\nYou've completed the *{name}* quest and earned {points} points! 🌟"
        )

    except Exception as e:
        print(f"❌ Failed to award quest points: {e}")
