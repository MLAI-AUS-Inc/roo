import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.slack_moderation import (
    delete_boost_root_as_moderator,
    validate_slack_moderator_configuration,
)


def settings(**overrides):
    values = {
        "BOOST_POST_AUTO_DELETE_ENABLED": True,
        "BOOST_LINK_LOVE_CHANNEL_ID": "CBOOST123",
        "SLACK_MODERATOR_USER_TOKEN": "xoxp-secret-never-log-this",
        "SLACK_MODERATOR_TEAM_ID": "TTEAM123",
        "SLACK_MODERATOR_USER_ID": "UADMIN123",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DeleteClient:
    def __init__(self):
        self.calls = []

    def chat_delete(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def test_delete_is_hard_scoped_to_configured_channel_and_root_message():
    client = DeleteClient()

    wrong_channel = delete_boost_root_as_moderator(
        channel_id="COTHER123",
        message_ts="1800000000.123456",
        reason_code="insufficient_points",
        settings=settings(),
        client=client,
        get_message_fn=lambda *args: {"ts": "1800000000.123456"},
    )
    reply = delete_boost_root_as_moderator(
        channel_id="CBOOST123",
        message_ts="1800000000.123456",
        reason_code="insufficient_points",
        settings=settings(),
        client=client,
        get_message_fn=lambda *args: {
            "ts": "1800000000.123456",
            "thread_ts": "1700000000.999999",
        },
    )

    assert wrong_channel.error_code == "channel_not_allowlisted"
    assert reply.error_code == "not_root_message"
    assert client.calls == []


def test_delete_uses_moderator_client_only_after_root_is_verified():
    client = DeleteClient()
    result = delete_boost_root_as_moderator(
        channel_id="CBOOST123",
        message_ts="1800000000.123456",
        reason_code="rejected_insufficient_points",
        settings=settings(),
        client=client,
        get_message_fn=lambda *args: {
            "ts": "1800000000.123456",
            "thread_ts": "1800000000.123456",
        },
    )

    assert result.ok
    assert client.calls == [{"channel": "CBOOST123", "ts": "1800000000.123456"}]


def test_startup_validation_proves_workspace_user_and_admin_role():
    class BotClient:
        def auth_test(self):
            return {"ok": True, "team_id": "TTEAM123"}

        def users_info(self, **kwargs):
            assert kwargs == {"user": "UADMIN123"}
            return {"ok": True, "user": {"is_admin": True}}

    class ModeratorClient:
        def auth_test(self):
            return {
                "ok": True,
                "team_id": "TTEAM123",
                "user_id": "UADMIN123",
            }

    result = validate_slack_moderator_configuration(
        settings=settings(),
        bot_client=BotClient(),
        moderator_client=ModeratorClient(),
    )

    assert result == {
        "status": "ready",
        "team_id": "TTEAM123",
        "user_id": "UADMIN123",
    }
