"""
MLAI Quests System

This module implements simple quests for user engagement.
"""
import asyncio
from typing import Dict, List, Optional
from .config import get_settings
from skills.mlai_points.client import PointsClient
from .slack_client import get_bot_user_id, post_message

# Configuration for quests
QUESTS = {
    "connector": {
        "name": "Connector",
        "description": "React to 5 messages",
        "target_count": 5,
        "points": 5,
        "event_type": "reaction_added"
    },
    "helper": {
        "name": "Helper",
        "description": "Reply to 3 threads",
        "target_count": 3,
        "points": 5,
        "event_type": "message" # specifically thread replies
    },
    "first_contact": {
        "name": "First Contact",
        "description": "First post in #_start-here",
        "target_count": 1,
        "points": 2,
        "event_type": "message",
        "channel_name": "_start-here"
    }
}

# In-memory tracking for simplicity (note: this resets on restart)
# In production, this should use a DB or Redis
_quest_progress: Dict[str, Dict[str, int]] = {}

async def handle_quests(event: dict):
    """
    Main entry point for quest processing.
    Call this from main.py's slack_events.
    """
    event_type = event.get("type")
    user_id = event.get("user")

    if not user_id:
        return

    # 1. Handle "Connector" Quest (Reactions)
    if event_type == "reaction_added":
        await _update_progress(user_id, "connector")

    # 2. Handle "Helper" Quest (Thread Replies)
    if event_type == "message" and event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        # Ensure it's not a bot message
        if not event.get("bot_id") and not event.get("subtype"):
             await _update_progress(user_id, "helper")

    # 3. Handle "First Contact" Quest (#_start-here post)
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        # Ignore thread replies for this specific quest
        if not event.get("thread_ts"):
            await _check_start_here_quest(event)

async def _update_progress(user_id: str, quest_id: str):
    """Update progress for a user on a specific quest."""
    if user_id not in _quest_progress:
        _quest_progress[user_id] = {}

    current = _quest_progress[user_id].get(quest_id, 0)
    target = QUESTS[quest_id]["target_count"]

    # If already completed, ignore (simple dedup)
    # Ideally we'd check a persistent "completed_quests" store
    if current >= target:
        return

    current += 1
    _quest_progress[user_id][quest_id] = current

    print(f"📊 Quest Progress: {user_id} - {quest_id}: {current}/{target}")

    if current >= target:
        await _complete_quest(user_id, quest_id)

async def _check_start_here_quest(event: dict):
    """Special handling for the First Contact quest."""
    channel_id = event.get("channel")
    user_id = event.get("user")

    # Resolve channel name
    from .slack_client import get_channel_id
    target_channel_id = get_channel_id("_start-here")

    # In tests, mock_get_channel returns "C12345".
    # But if not mocked properly, it might be None or different.

    if channel_id != target_channel_id:
        return

    # Use PointsClient to check if they've posted before
    settings = get_settings()
    points_client = PointsClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
    )

    has_posted = await points_client.has_posted_in_channel(user_id, channel_id)
    if has_posted:
        return

    # Record it
    await points_client.record_channel_post(user_id, channel_id)

    # Complete the quest directly
    await _complete_quest(user_id, "first_contact")


async def _complete_quest(user_id: str, quest_id: str):
    """Award points and notify user of quest completion."""
    quest = QUESTS[quest_id]
    points = quest["points"]
    name = quest["name"]

    print(f"🎉 Quest Complete: {user_id} completed {name}!")

    settings = get_settings()
    points_client = PointsClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
    )

    try:
        from .slack_client import get_bot_user_id
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
