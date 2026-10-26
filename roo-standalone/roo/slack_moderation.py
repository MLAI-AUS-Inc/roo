"""Narrow Slack moderation capability for paid boost root posts.

The ordinary Roo bot token must never be used for member-authored deletions.
This module is the only runtime location that loads the optional Workspace
Admin user token, and every delete is hard-bound to one configured channel.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Settings, get_settings

_moderator_client: Any = None


@dataclass(frozen=True)
class ModeratorDeleteResult:
    ok: bool
    status: str
    channel_id: str
    message_ts: str
    error_code: str | None = None


def get_moderator_slack_client(*, settings: Settings | None = None) -> Any:
    """Return a Slack client authenticated as the configured admin member."""

    global _moderator_client
    resolved = settings or get_settings()
    token = str(resolved.SLACK_MODERATOR_USER_TOKEN or "").strip()
    if not token:
        raise RuntimeError("Slack moderator user token is not configured")
    if _moderator_client is None:
        from slack_sdk import WebClient

        _moderator_client = WebClient(token=token)
    return _moderator_client


def validate_slack_moderator_configuration(
    *,
    settings: Settings | None = None,
    bot_client: Any = None,
    moderator_client: Any = None,
) -> dict[str, str]:
    """Prove that the moderator token is the expected admin in Roo's team."""

    resolved = settings or get_settings()
    if not resolved.BOOST_POST_AUTO_DELETE_ENABLED:
        return {"status": "disabled"}

    if bot_client is None:
        from .slack_client import get_slack_client

        bot_client = get_slack_client()
    moderator_client = moderator_client or get_moderator_slack_client(settings=resolved)

    bot_auth = bot_client.auth_test()
    moderator_auth = moderator_client.auth_test()
    bot_team_id = str(bot_auth.get("team_id") or "")
    moderator_team_id = str(moderator_auth.get("team_id") or "")
    moderator_user_id = str(moderator_auth.get("user_id") or "")
    expected_team_id = str(resolved.SLACK_MODERATOR_TEAM_ID or "")
    expected_user_id = str(resolved.SLACK_MODERATOR_USER_ID or "")

    if not bot_auth.get("ok") or not moderator_auth.get("ok"):
        raise RuntimeError("Slack moderator identity verification failed")
    if bot_team_id != expected_team_id or moderator_team_id != expected_team_id:
        raise RuntimeError("Slack moderator token belongs to the wrong workspace")
    if moderator_user_id != expected_user_id:
        raise RuntimeError("Slack moderator token belongs to the wrong user")

    profile_response = bot_client.users_info(user=moderator_user_id)
    profile = profile_response.get("user") or {}
    if not profile_response.get("ok") or not (
        bool(profile.get("is_admin")) or bool(profile.get("is_owner"))
    ):
        raise RuntimeError("Configured Slack moderator is not a Workspace Admin or Owner")

    return {
        "status": "ready",
        "team_id": expected_team_id,
        "user_id": expected_user_id,
    }


def delete_boost_root_as_moderator(
    *,
    channel_id: str,
    message_ts: str,
    reason_code: str,
    settings: Settings | None = None,
    client: Any = None,
    get_message_fn: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> ModeratorDeleteResult:
    """Delete one verified top-level root in the configured boost channel."""

    resolved = settings or get_settings()
    channel_id = str(channel_id or "").strip()
    message_ts = str(message_ts or "").strip()
    reason_code = str(reason_code or "").strip().lower()

    if not resolved.BOOST_POST_AUTO_DELETE_ENABLED:
        return ModeratorDeleteResult(False, "disabled", channel_id, message_ts, "disabled")
    if channel_id != str(resolved.BOOST_LINK_LOVE_CHANNEL_ID or "").strip():
        return ModeratorDeleteResult(
            False,
            "refused",
            channel_id,
            message_ts,
            "channel_not_allowlisted",
        )
    if not re.fullmatch(r"\d{8,}\.\d+", message_ts):
        return ModeratorDeleteResult(
            False,
            "refused",
            channel_id,
            message_ts,
            "invalid_message_ts",
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_:-]{1,79}", reason_code):
        return ModeratorDeleteResult(
            False,
            "refused",
            channel_id,
            message_ts,
            "invalid_reason_code",
        )

    if get_message_fn is None:
        from .slack_client import get_message

        get_message_fn = get_message
    message = get_message_fn(channel_id, message_ts)
    if not message:
        return ModeratorDeleteResult(
            False,
            "refused",
            channel_id,
            message_ts,
            "message_not_found",
        )
    thread_ts = str(message.get("thread_ts") or "").strip()
    if thread_ts and thread_ts != message_ts:
        return ModeratorDeleteResult(
            False,
            "refused",
            channel_id,
            message_ts,
            "not_root_message",
        )

    try:
        moderator_client = client or get_moderator_slack_client(settings=resolved)
        response = moderator_client.chat_delete(channel=channel_id, ts=message_ts)
        if not response.get("ok"):
            error_code = str(response.get("error") or "delete_failed")
            print(
                "BOOST_POST_DELETE_FAILED "
                f"channel_id={channel_id} message_ts={message_ts} "
                f"reason={reason_code} error_code={error_code}"
            )
            return ModeratorDeleteResult(
                False,
                "failed",
                channel_id,
                message_ts,
                error_code,
            )
        print(
            "BOOST_POST_DELETED "
            f"channel_id={channel_id} message_ts={message_ts} reason={reason_code}"
        )
        return ModeratorDeleteResult(True, "deleted", channel_id, message_ts)
    except Exception as exc:  # noqa: BLE001 - Slack SDK errors vary by transport.
        error_code = exc.__class__.__name__
        print(
            "BOOST_POST_DELETE_FAILED "
            f"channel_id={channel_id} message_ts={message_ts} "
            f"reason={reason_code} error_type={error_code}"
        )
        return ModeratorDeleteResult(
            False,
            "failed",
            channel_id,
            message_ts,
            error_code,
        )
