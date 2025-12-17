"""
Slack Client Utilities

Handles Slack API interactions including posting messages and user lookups.
"""
from typing import Optional, Dict, Any
from functools import lru_cache

from .config import get_settings


# Lazy-loaded Slack client
_slack_client = None


def get_slack_client():
    """Get the Slack WebClient instance."""
    global _slack_client
    if _slack_client is None:
        from slack_sdk import WebClient
        
        settings = get_settings()
        _slack_client = WebClient(token=settings.SLACK_BOT_TOKEN)
        print("🔌 Slack client initialized")
    
    return _slack_client


# Cache for bot user ID
_bot_user_id = None


def get_bot_user_id() -> str:
    """Get Roo's own Slack user ID via auth.test.
    
    This is cached to avoid repeated API calls.
    """
    global _bot_user_id
    if _bot_user_id is None:
        client = get_slack_client()
        response = client.auth_test()
        _bot_user_id = response["user_id"]
        print(f"🤖 Bot user ID: {_bot_user_id}")
    return _bot_user_id


def post_message(
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Post a message to a Slack channel or thread.
    
    Args:
        channel: Channel ID
        text: Message text
        thread_ts: Thread timestamp (for replies)
        **kwargs: Additional Slack API parameters
    
    Returns:
        Slack API response
    """
    client = get_slack_client()
    
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
            **kwargs
        )
        
        if response.get("ok"):
            suffix = f" (thread: {thread_ts})" if thread_ts else ""
            print(f"✅ Message posted to {channel}{suffix}")
        else:
            print(f"❌ Failed to post message: {response}")
        
        return response
        
    except Exception as e:
        print(f"❌ Slack post error: {e}")
        raise


def get_thread_messages(channel: str, thread_ts: str) -> list[dict]:
    """
    Retrieve all messages in a Slack thread for context.
    
    Args:
        channel: Channel ID
        thread_ts: Thread timestamp (parent message ts)
    
    Returns:
        List of message dicts with 'user', 'text', 'ts', and 'bot_id'
    """
    client = get_slack_client()
    
    try:
        response = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=50
        )
        
        if response.get("ok"):
            messages = []
            for msg in response.get("messages", []):
                messages.append({
                    "user": msg.get("user", ""),
                    "text": msg.get("text", ""),
                    "ts": msg.get("ts", ""),
                    "bot_id": msg.get("bot_id"),
                    "is_bot": bool(msg.get("bot_id"))
                })
            print(f"📜 Retrieved {len(messages)} messages from thread")
            return messages
        
        return []
        
    except Exception as e:
        print(f"❌ Thread history error: {e}")
        return []


@lru_cache(maxsize=100)
def get_user_info(user_id: str) -> Dict[str, Any]:
    """
    Get user information from Slack.
    
    Results are cached to avoid repeated API calls.
    """
    client = get_slack_client()
    
    try:
        response = client.users_info(user=user_id)
        
        if response.get("ok"):
            user = response["user"]
            profile = user.get("profile", {})
            
            return {
                "id": user_id,
                "name": user.get("name", ""),
                "real_name": user.get("real_name", profile.get("real_name", "")),
                "display_name": profile.get("display_name", ""),
                "email": profile.get("email", ""),
            }
        
        return {"id": user_id, "name": "Unknown"}
        
    except Exception as e:
        print(f"❌ User lookup error for {user_id}: {e}")
        return {"id": user_id, "name": "Unknown"}


def get_display_name(user_id: str) -> str:
    """Get the best display name for a user."""
    info = get_user_info(user_id)
    return (
        info.get("display_name") or 
        info.get("real_name") or 
        info.get("name") or 
        "Unknown"
    )


def open_dm(user_id: str) -> Optional[str]:
    """Open a DM channel with a user."""
    client = get_slack_client()
    
    try:
        response = client.conversations_open(users=user_id)
        if response.get("ok"):
            return response["channel"]["id"]
        return None
    except Exception as e:
        print(f"❌ Failed to open DM with {user_id}: {e}")
        return None


def send_dm(user_id: str, text: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Send a direct message to a user."""
    dm_channel = open_dm(user_id)
    if dm_channel:
        return post_message(dm_channel, text, **kwargs)
    return None
