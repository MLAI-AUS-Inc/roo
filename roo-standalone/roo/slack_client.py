"""
Slack Client Utilities

Handles Slack API interactions including posting messages and user lookups.
"""
from typing import Optional, Dict, Any
from functools import lru_cache
import httpx

from .config import get_settings

SLACK_FILES_READ_SCOPE = "files:read"


class SlackIdentityLookupError(RuntimeError):
    """Raised when Slack cannot provide a trustworthy member identity."""


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


def post_ephemeral(
    channel: str,
    user: str,
    text: str,
    thread_ts: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Post a private message visible only to one member in a Slack channel."""
    client = get_slack_client()

    try:
        response = client.chat_postEphemeral(
            channel=channel,
            user=user,
            text=text,
            thread_ts=thread_ts,
            **kwargs,
        )
        if response.get("ok"):
            print(f"✅ Private message posted in {channel} for {user}")
        else:
            print(
                "❌ Failed to post private message: "
                f"error={response.get('error', 'unknown')}"
            )
        return response
    except Exception as exc:
        print(f"❌ Slack private post error: error_type={exc.__class__.__name__}")
        raise


def delete_message(channel: str, message_ts: str) -> Dict[str, Any]:
    """Delete a message previously posted by Roo's bot token."""

    client = get_slack_client()
    try:
        response = client.chat_delete(channel=channel, ts=message_ts)
        if response.get("ok"):
            print("✅ Roo message deleted")
        else:
            print(
                "❌ Failed to delete Roo message: "
                f"error={response.get('error', 'unknown')}"
            )
        return response
    except Exception as exc:
        print(f"❌ Slack delete error: error_type={exc.__class__.__name__}")
        raise


def upload_file(
    channel: str,
    content: str,
    filename: str,
    title: Optional[str] = None,
    thread_ts: Optional[str] = None,
    initial_comment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Upload an in-memory text file to Slack.

    Requires the Slack bot token to have the files:write scope.
    """
    client = get_slack_client()

    try:
        response = client.files_upload_v2(
            channel=channel,
            content=content,
            filename=filename,
            title=title or filename,
            thread_ts=thread_ts,
            initial_comment=initial_comment,
        )

        if response.get("ok"):
            suffix = f" (thread: {thread_ts})" if thread_ts else ""
            print(f"✅ File uploaded to {channel}{suffix}: {filename}")
        else:
            print(f"❌ Failed to upload file: {response}")

        return response

    except Exception as e:
        print(f"❌ Slack file upload error: {e}")
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
                    "thread_ts": msg.get("thread_ts"),
                    "subtype": msg.get("subtype"),
                    "bot_id": msg.get("bot_id"),
                    "is_bot": bool(msg.get("bot_id")),
                    "files": msg.get("files", []),
                })
            print(f"📜 Retrieved {len(messages)} messages from thread")
            return messages
        
        return []
        
    except Exception as e:
        print(f"❌ Thread history error: {e}")
        return []


def get_recent_channel_messages(
    channel: str,
    *,
    before_ts: Optional[str] = None,
    limit: int = 50,
    lookback_hours: int = 24,
) -> list[dict]:
    """Retrieve a bounded slice of channel history before an anchor message.

    Slack returns history newest-first; callers receive chronological order.
    This helper performs one on-demand read and never crawls other channels.
    """
    client = get_slack_client()
    bounded_limit = min(max(int(limit or 50), 1), 100)
    oldest = None
    try:
        if before_ts and lookback_hours > 0:
            oldest = str(max(float(before_ts) - (lookback_hours * 60 * 60), 0.0))
    except (TypeError, ValueError):
        oldest = None

    kwargs: Dict[str, Any] = {
        "channel": channel,
        "limit": bounded_limit,
        "inclusive": False,
        "include_all_metadata": True,
    }
    if before_ts:
        kwargs["latest"] = before_ts
    if oldest:
        kwargs["oldest"] = oldest

    try:
        response = client.conversations_history(**kwargs)
        if not response.get("ok"):
            return []
        messages = [_normalize_slack_message(message) for message in response.get("messages", [])]
        messages.reverse()
        print(f"📚 Retrieved {len(messages)} recent messages from {channel}")
        return messages
    except Exception as exc:
        retry_after = _slack_retry_after(exc)
        if retry_after:
            print(
                f"⚠️ Slack channel history rate limited for {channel}; "
                f"retry after {retry_after}s"
            )
        else:
            print(f"❌ Channel history error for {channel}: {exc}")
        return []


def _normalize_slack_message(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user": message.get("user", ""),
        "text": message.get("text", ""),
        "ts": message.get("ts", ""),
        "thread_ts": message.get("thread_ts"),
        "subtype": message.get("subtype"),
        "bot_id": message.get("bot_id"),
        "is_bot": bool(message.get("bot_id")),
        "files": message.get("files", []),
        "reply_count": int(message.get("reply_count") or 0),
    }


def _slack_retry_after(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value:
            return str(value)
    return None


def get_message(channel: str, message_ts: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single Slack message by timestamp."""
    client = get_slack_client()

    try:
        response = client.conversations_history(
            channel=channel,
            latest=message_ts,
            oldest=message_ts,
            inclusive=True,
            limit=1,
            include_all_metadata=True,
        )

        if response.get("ok"):
            messages = response.get("messages", [])
            if messages:
                return messages[0]

        return None

    except Exception as e:
        print(f"❌ Slack message lookup error for {channel}:{message_ts}: {e}")
        return None


def get_file_info(file_id: str) -> Dict[str, Any]:
    """
    Get full Slack file metadata.

    Requires the Slack bot token to have the files:read scope.
    """
    client = get_slack_client()

    try:
        response = client.files_info(file=file_id)
        if response.get("ok"):
            return response.get("file", {})
        error = response.get("error") or "unknown_error"
        raise RuntimeError(f"Slack files.info failed: {error}")
    except Exception as e:
        print(f"❌ Slack file info error for {file_id}: {e}")
        raise


def download_file_bytes(file: Dict[str, Any]) -> bytes:
    """
    Download a private Slack file using the bot token.

    Requires files:read and a file object with url_private_download/url_private.
    If Slack Connect requires a metadata refresh, pass the file through files.info first.
    """
    resolved_file = dict(file or {})
    file_id = str(resolved_file.get("id") or "").strip()
    if resolved_file.get("file_access") == "check_file_info" and file_id:
        resolved_file = get_file_info(file_id)

    url = (
        resolved_file.get("url_private_download")
        or resolved_file.get("url_private")
        or ""
    )
    if not url and file_id:
        resolved_file = get_file_info(file_id)
        url = (
            resolved_file.get("url_private_download")
            or resolved_file.get("url_private")
            or ""
        )

    if not url:
        raise ValueError("Slack file is missing url_private_download/url_private")

    settings = get_settings()
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"},
            timeout=30.0,
        )
        if _looks_like_slack_file_auth_redirect(response):
            _raise_slack_file_download_permission_error(file_id, response)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        response = exc.response
        if _looks_like_slack_file_auth_redirect(response):
            _raise_slack_file_download_permission_error(file_id, response)
        raise
    return response.content


def _looks_like_slack_file_auth_redirect(response: httpx.Response) -> bool:
    if getattr(response, "status_code", 200) not in {301, 302, 303, 307, 308}:
        return False
    location = str(response.headers.get("location") or "").lower()
    return "slack.com" in location and "redir=" in location


def _raise_slack_file_download_permission_error(file_id: str, response: httpx.Response) -> None:
    location = str(response.headers.get("location") or "").strip()
    scope_hint = ""
    if file_id:
        try:
            get_file_info(file_id)
        except Exception as exc:
            text = str(exc)
            if "missing_scope" in text and SLACK_FILES_READ_SCOPE in text:
                scope_hint = f" Slack reported missing `{SLACK_FILES_READ_SCOPE}`."
    raise RuntimeError(
        "Slack redirected the private file download instead of returning file bytes."
        f"{scope_hint} Add the `{SLACK_FILES_READ_SCOPE}` bot scope to the Roo Slack app, "
        "reinstall it to the workspace, update `SLACK_BOT_TOKEN` if Slack rotates it, "
        "and restart Roo."
        + (f" Redirect location: {location}" if location else "")
    )


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
                "image_192": profile.get("image_192", ""),  # 192x192 avatar
                "image_512": profile.get("image_512", ""),  # 512x512 avatar
            }

        return {"id": user_id, "name": "Unknown"}

    except Exception as e:
        print(f"❌ User lookup error for {user_id}: {e}")
        return {"id": user_id, "name": "Unknown"}


def get_verified_user_email(user_id: str) -> str:
    """Fetch an uncached email for the exact active human Slack member.

    Account linking is an identity mutation, so it must not use the permissive
    cached profile helper above or treat a stale/failed lookup as authoritative.
    """
    requested_user_id = str(user_id or "").strip()
    if not requested_user_id:
        raise SlackIdentityLookupError("Slack member ID is missing")

    try:
        response = get_slack_client().users_info(user=requested_user_id)
    except Exception as exc:
        raise SlackIdentityLookupError(
            "Slack could not verify this member right now"
        ) from exc

    if not response.get("ok") or not isinstance(response.get("user"), dict):
        raise SlackIdentityLookupError("Slack could not verify this member right now")

    user = response["user"]
    returned_user_id = str(user.get("id") or "").strip()
    if returned_user_id != requested_user_id:
        raise SlackIdentityLookupError("Slack returned a different member identity")
    if user.get("is_bot") or user.get("deleted"):
        raise SlackIdentityLookupError("This Slack profile cannot be linked")

    profile = user.get("profile") or {}
    email = str(profile.get("email") or "").strip().lower()
    if not email:
        raise SlackIdentityLookupError(
            "Slack did not provide an email for this member"
        )
    return email


def get_display_name(user_id: str) -> str:
    """Get the best display name for a user."""
    info = get_user_info(user_id)
    return (
        info.get("display_name") or 
        info.get("real_name") or 
        info.get("name") or 
        "Unknown"
    )


def open_dm(user_id: str, *, raise_on_error: bool = False) -> Optional[str]:
    """Open a DM channel with a user."""
    client = get_slack_client()
    
    try:
        response = client.conversations_open(users=user_id)
        if response.get("ok"):
            return response["channel"]["id"]
        return None
    except Exception as e:
        print(f"❌ Failed to open DM with {user_id}: {e}")
        if raise_on_error:
            raise
        return None


def send_dm(
    user_id: str,
    text: str,
    *,
    raise_on_error: bool = False,
    **kwargs,
) -> Optional[Dict[str, Any]]:
    """Send a direct message to a user."""
    dm_channel = open_dm(user_id, raise_on_error=raise_on_error)
    if dm_channel:
        return post_message(dm_channel, text, **kwargs)
    if raise_on_error:
        raise RuntimeError("Slack did not return a direct-message channel")
    return None


_channel_name_cache: Dict[str, str] = {}
_channel_context_cache: Dict[str, Dict[str, Any]] = {}


def get_channel_context(channel_id: str) -> Dict[str, Any]:
    """Return cached channel name/topic/purpose metadata."""
    cached = _channel_context_cache.get(channel_id)
    if cached is not None:
        return dict(cached)

    client = get_slack_client()
    try:
        response = client.conversations_info(channel=channel_id)
        if not response.get("ok"):
            return {}
        channel = response.get("channel") or {}
        context = {
            "id": channel_id,
            "name": channel.get("name") or "",
            "topic": (channel.get("topic") or {}).get("value") or "",
            "purpose": (channel.get("purpose") or {}).get("value") or "",
            "is_private": bool(channel.get("is_private")),
        }
        _channel_context_cache[channel_id] = context
        if context["name"]:
            _channel_name_cache[channel_id] = str(context["name"])
        return dict(context)
    except Exception as exc:
        print(f"❌ Channel context lookup error for {channel_id}: {exc}")
        return {}


def get_message_permalink(channel_id: str, message_ts: str) -> Optional[str]:
    """Resolve a stable Slack link for source provenance."""
    if not channel_id or not message_ts:
        return None
    client = get_slack_client()
    try:
        response = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        if response.get("ok"):
            return str(response.get("permalink") or "") or None
    except Exception as exc:
        print(f"⚠️ Slack permalink lookup failed for {channel_id}:{message_ts}: {exc}")
    return None


def get_channel_name(channel_id: str) -> Optional[str]:
    """Get channel name by ID using conversations.info.

    Successful lookups are cached for the process lifetime (channel renames are
    rare); failures are not cached so transient Slack errors can recover.
    """
    cached = _channel_name_cache.get(channel_id)
    if cached:
        return cached

    return str(get_channel_context(channel_id).get("name") or "") or None


@lru_cache(maxsize=10)
def get_channel_id(channel_name: str) -> Optional[str]:
    """Get channel ID by name."""
    client = get_slack_client()
    target_name = channel_name.lstrip('#')
    
    try:
        # Check public and private channels, with pagination
        cursor = None
        while True:
            result = client.conversations_list(
                types="public_channel,private_channel",
                limit=1000,
                cursor=cursor
            )
            
            for channel in result["channels"]:
                if channel["name"] == target_name:
                    print(f"✅ Found channel #{target_name}: {channel['id']}")
                    return channel["id"]
            
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
                
        print(f"⚠️ Channel #{target_name} not found")
        return None
        
    except Exception as e:
        print(f"❌ Failed to lookup channel {channel_name}: {e}")
        return None
