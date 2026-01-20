"""
MLAI Quests System

This module implements simple quests for user engagement.
"""
import asyncio
import re
from datetime import datetime
from typing import Dict, Optional
try:
    import zoneinfo
except ImportError:
    # Backport for python < 3.9
    from backports import zoneinfo

from .config import get_settings
from skills.mlai_points.client import MLAIBackendClient
from .slack_client import (
    get_bot_user_id,
    post_message,
    get_channel_id,
    get_thread_messages,
)

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
        "points": 2,
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

# Link Love tracking (reset on restart)
LINK_LOVE_CHANNEL_NAME = "link-love"
LINK_LOVE_POINTS = 1
LINK_LOVE_KEYWORDS = (
    "like",
    "liked",
    "share",
    "shared",
    "support",
    "supported",
    "supporting",
)
_LINK_LOVE_KEYWORD_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(keyword) for keyword in LINK_LOVE_KEYWORDS),
    re.IGNORECASE
)
_link_love_threads: Dict[str, Dict[str, object]] = {}
_link_love_daily_posts: Dict[str, set] = {}
_link_love_thread_rewards: Dict[str, set] = {}

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

        await _handle_link_love_event(event)

        # 5. Helper (Thread replies)
        if is_thread and event.get("thread_ts") != ts:
             await _update_progress(user_id, "helper")

        # 6. First Contact (#_start-here post, no thread)
        if not is_thread:
             await _check_start_here_quest(event)

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


def _get_melbourne_date(ts: str) -> str:
    """Return the Melbourne date (YYYY-MM-DD) for a Slack timestamp."""
    from zoneinfo import ZoneInfo
    timestamp = float(ts)
    dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo("Australia/Melbourne"))
    return dt.strftime("%Y-%m-%d")


def _contains_link_love_keyword(text: str) -> bool:
    """Check if a message includes a Link Love keyword."""
    if not text:
        return False
    return bool(_LINK_LOVE_KEYWORD_RE.search(text))


async def _handle_link_love_event(event: dict) -> None:
    """Handle Link Love rewards in #link-love."""
    channel_id = event.get("channel")
    link_love_id = get_channel_id(LINK_LOVE_CHANNEL_NAME)
    if not link_love_id or channel_id != link_love_id:
        return

    user_id = event.get("user")
    ts = event.get("ts")
    thread_ts = event.get("thread_ts")

    if not user_id or not ts:
        return

    if not thread_ts or thread_ts == ts:
        _register_link_love_post(user_id, ts)
        return

    if not _contains_link_love_keyword(event.get("text", "")):
        return

    await _maybe_reward_link_love_reply(user_id, channel_id, thread_ts)


def _register_link_love_post(user_id: str, ts: str) -> None:
    """Record a Link Love post and enforce one eligible post per day."""
    date_key = _get_melbourne_date(ts)
    daily_posters = _link_love_daily_posts.setdefault(date_key, set())
    is_first_post_today = user_id not in daily_posters
    if is_first_post_today:
        daily_posters.add(user_id)

    _link_love_threads[ts] = {
        "author": user_id,
        "eligible": is_first_post_today,
        "date": date_key,
    }


def _get_or_fetch_link_love_thread(
    channel_id: str,
    thread_ts: str
) -> Optional[Dict[str, object]]:
    """Retrieve cached thread info or fetch from Slack."""
    if thread_ts in _link_love_threads:
        return _link_love_threads[thread_ts]

    messages = get_thread_messages(channel_id, thread_ts)
    if not messages:
        return None

    root = messages[0]
    author = root.get("user")
    root_ts = root.get("ts") or thread_ts
    if not author or not root_ts:
        return None

    date_key = _get_melbourne_date(root_ts)
    daily_posters = _link_love_daily_posts.setdefault(date_key, set())
    is_first_post_today = author not in daily_posters
    if is_first_post_today:
        daily_posters.add(author)

    _link_love_threads[thread_ts] = {
        "author": author,
        "eligible": is_first_post_today,
        "date": date_key,
    }
    return _link_love_threads[thread_ts]


async def _maybe_reward_link_love_reply(
    user_id: str,
    channel_id: str,
    thread_ts: str
) -> None:
    """Reward a user for supporting a Link Love post in a thread."""
    thread_info = _get_or_fetch_link_love_thread(channel_id, thread_ts)
    if not thread_info or not thread_info.get("eligible"):
        return

    author_id = thread_info.get("author")
    if not author_id or user_id == author_id:
        return

    rewarded_users = _link_love_thread_rewards.setdefault(thread_ts, set())
    if user_id in rewarded_users:
        return

    if await _award_link_love_points(user_id, author_id, channel_id, thread_ts):
        rewarded_users.add(user_id)


async def _award_link_love_points(
    supporter_id: str,
    author_id: str,
    channel_id: str,
    thread_ts: str
) -> bool:
    """Award Link Love points and post a confirmation message in the thread."""
    settings = get_settings()
    points_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
    )

    try:
        bot_id = get_bot_user_id()
        if not bot_id:
            print("⚠️ Cannot award Link Love points: Bot ID not found")
            return False

        await points_client.system_award_points(
            admin_slack_id=bot_id,
            target_slack_id=supporter_id,
            points=LINK_LOVE_POINTS,
            reason="Link Love: supported a community post"
        )

        post_message(
            channel=channel_id,
            text=(
                f"Thanks <@{supporter_id}> for supporting <@{author_id}>'s post! "
                f"You earned {LINK_LOVE_POINTS} MLAI point."
            ),
            thread_ts=thread_ts
        )
        return True
    except Exception as e:
        print(f"❌ Failed to award Link Love points: {e}")
        return False

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

async def _check_start_here_quest(event: dict):
    """Special handling for the First Contact quest."""
    channel_id = event.get("channel")
    user_id = event.get("user")

    # Resolve channel name
    target_channel_id = get_channel_id("_start-here")

    # Fallback for testing/mocking if get_channel_id returns None but we want to simulate match
    # (In real run, get_channel_id should work or return None)

    if channel_id != target_channel_id:
        return

    # Use MLAIBackendClient to check if they've posted before
    settings = get_settings()
    points_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.MLAI_API_KEY
    )

    try:
        has_posted = await points_client.has_posted_in_channel(user_id, channel_id)
        if has_posted:
            return

        # Record it
        await points_client.record_channel_post(user_id, channel_id)

        # Complete the quest directly
        await _complete_quest(user_id, "first_contact")
    except Exception as e:
        print(f"❌ Failed First Contact check: {e}")


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
