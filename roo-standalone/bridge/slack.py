"""
Slack client helpers.

The bridge is a dedicated app installed in each workspace, so every side is a
plain bot token — one WebClient factory covers them all. Plus a channel-name
resolver so pairs can be configured by name instead of opaque IDs.
"""
import re
from typing import Any, Dict, Optional

_ID_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")


def make_bot_client(token: str):
    """WebClient for a bot token (one per workspace install of the Bridge app)."""
    from slack_sdk import WebClient

    client = WebClient(token=token)
    print("🔌 Bridge bot client initialized")
    return client


def resolve_identity(client) -> Dict[str, Any]:
    """auth.test → {team_id, user_id, ...}. Confirms a client actually works."""
    resp = client.auth_test()
    return {
        "user_id": resp.get("user_id"),
        "team_id": resp.get("team_id"),
        "team": resp.get("team"),
        "url": resp.get("url"),
    }


def resolve_channel_id(client, value: str) -> Optional[str]:
    """Return a channel ID for `value`, which may already be an ID or a name.

    Names are matched against conversations.list (falling back to public-only if
    the token lacks the private-channel scope). Returns None if not found — e.g.
    the channel doesn't exist yet or the bot can't see it.
    """
    v = (value or "").strip()
    if _ID_RE.match(v):
        return v
    name = v.lstrip("#")
    for types in ("public_channel,private_channel", "public_channel"):
        try:
            cursor = None
            while True:
                r = client.conversations_list(types=types, limit=1000, cursor=cursor)
                for ch in r.get("channels", []):
                    if ch.get("name") == name:
                        return ch["id"]
                cursor = (r.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return None
        except Exception as e:
            if "missing_scope" in str(e):
                continue  # no groups:read → retry public channels only
            raise
    return None


def is_auth_error(exc: Exception) -> bool:
    """True if a Slack error looks like an auth failure worth alerting on."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "invalid_auth",
            "not_authed",
            "token_revoked",
            "account_inactive",
            "token_expired",
            "no_permission",
            "missing_scope",
        )
    )
