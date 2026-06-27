"""
Slack client helpers.

The bridge is a dedicated app installed in both workspaces, so each side is a
plain bot token — one WebClient factory covers both.
"""
from typing import Any, Dict


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
