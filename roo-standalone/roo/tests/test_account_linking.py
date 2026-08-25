from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from roo.agent import RooAgent
from roo import agent as agent_module
from roo import slack_client
from roo.config import Settings
from roo.skills import executor as executor_module
from roo.skills.executor import SkillExecutor


class LinkClient:
    def __init__(self, linked_user_id=123, error=None):
        self.linked_user_id = linked_user_id
        self.error = error
        self.calls = []

    async def link_slack_user(self, slack_id, email):
        self.calls.append((slack_id, email))
        if self.error:
            raise self.error
        return self.linked_user_id


def _capture_private_delivery(monkeypatch, *, dm_ok=True):
    direct_messages = []
    ephemeral_messages = []

    def fake_send_dm(user_id, text, **kwargs):
        direct_messages.append({"user": user_id, "text": text, **kwargs})
        return {"ok": dm_ok}

    def fake_post_ephemeral(**kwargs):
        ephemeral_messages.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(executor_module, "send_dm", fake_send_dm)
    monkeypatch.setattr(executor_module, "post_ephemeral", fake_post_ephemeral)
    return direct_messages, ephemeral_messages


def test_verified_user_email_requires_the_exact_active_human_member(monkeypatch):
    class FakeSlackClient:
        def users_info(self, *, user):
            assert user == "UREQUESTER"
            return {
                "ok": True,
                "user": {
                    "id": "UREQUESTER",
                    "is_bot": False,
                    "deleted": False,
                    "profile": {"email": "Member@Example.com"},
                },
            }

    monkeypatch.setattr(slack_client, "get_slack_client", lambda: FakeSlackClient())

    assert slack_client.get_verified_user_email("UREQUESTER") == "member@example.com"


@pytest.mark.parametrize(
    "returned_user",
    [
        {
            "id": "UDIFFERENT",
            "is_bot": False,
            "deleted": False,
            "profile": {"email": "member@example.com"},
        },
        {
            "id": "UREQUESTER",
            "is_bot": True,
            "deleted": False,
            "profile": {"email": "member@example.com"},
        },
        {
            "id": "UREQUESTER",
            "is_bot": False,
            "deleted": True,
            "profile": {"email": "member@example.com"},
        },
        {
            "id": "UREQUESTER",
            "is_bot": False,
            "deleted": False,
            "profile": {},
        },
    ],
)
def test_verified_user_email_fails_closed_for_untrusted_profiles(
    monkeypatch,
    returned_user,
):
    class FakeSlackClient:
        def users_info(self, *, user):
            return {"ok": True, "user": returned_user}

    monkeypatch.setattr(slack_client, "get_slack_client", lambda: FakeSlackClient())

    with pytest.raises(slack_client.SlackIdentityLookupError):
        slack_client.get_verified_user_email("UREQUESTER")


def test_verified_user_email_does_not_reuse_a_failed_cached_profile(monkeypatch):
    calls = 0

    class FakeSlackClient:
        def users_info(self, *, user):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary Slack failure")
            return {
                "ok": True,
                "user": {
                    "id": user,
                    "is_bot": False,
                    "deleted": False,
                    "profile": {"email": "retry@example.com"},
                },
            }

    monkeypatch.setattr(slack_client, "get_slack_client", lambda: FakeSlackClient())

    with pytest.raises(slack_client.SlackIdentityLookupError):
        slack_client.get_verified_user_email("URETRY")
    assert slack_client.get_verified_user_email("URETRY") == "retry@example.com"
    assert calls == 2


@pytest.mark.asyncio
async def test_link_command_in_channel_links_verified_requester_and_replies_privately(
    monkeypatch,
):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)
    client = LinkClient(linked_user_id=42)
    monkeypatch.setattr(
        slack_client,
        "get_verified_user_email",
        lambda user_id: "member@example.com",
    )

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="link_account",
        params={"target_slack_id": "UFORGED"},
        text="link",
        user_id="UREQUESTER",
        channel_id="CROO",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [("UREQUESTER", "member@example.com")]
    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert result["data"]["action"] == "link_account"
    assert direct_messages == [
        {
            "user": "UREQUESTER",
            "text": (
                "✅ Your Slack profile is linked to your MLAI account. "
                "You can now try `book me in` again."
            ),
        }
    ]
    assert ephemeral_messages[0]["user"] == "UREQUESTER"
    assert "account-link result privately" in ephemeral_messages[0]["text"]


@pytest.mark.asyncio
async def test_link_command_in_dm_returns_private_result_in_current_dm(monkeypatch):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)
    client = LinkClient(linked_user_id=42)
    monkeypatch.setattr(
        slack_client,
        "get_verified_user_email",
        lambda user_id: "member@example.com",
    )

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="link_account",
        params={},
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "is linked" in result["message"]
    assert result["data"]["delivery"] == "current_direct_message"
    assert direct_messages == []
    assert ephemeral_messages == []


@pytest.mark.asyncio
async def test_link_command_does_not_call_backend_without_verified_slack_email(
    monkeypatch,
):
    client = LinkClient(linked_user_id=42)

    def fail_lookup(user_id):
        raise slack_client.SlackIdentityLookupError("missing email")

    monkeypatch.setattr(slack_client, "get_verified_user_email", fail_lookup)

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="link_account",
        params={},
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == []
    assert "didn't change any account link" in result["message"]


@pytest.mark.asyncio
async def test_link_command_distinguishes_no_matching_account_from_unknown_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        slack_client,
        "get_verified_user_email",
        lambda user_id: "member@example.com",
    )
    no_match = await SkillExecutor()._handle_points_action(
        client=LinkClient(linked_user_id=None),
        action="link_account",
        params={},
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )
    uncertain = await SkillExecutor()._handle_points_action(
        client=LinkClient(error=RuntimeError("response lost after request")),
        action="link_account",
        params={},
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "couldn't find an existing MLAI account" in no_match["message"]
    assert "Nothing was confirmed" in uncertain["message"]
    assert "not found" not in uncertain["message"].lower()


@pytest.mark.asyncio
async def test_link_command_reports_definitive_identity_conflict(monkeypatch):
    monkeypatch.setattr(
        slack_client,
        "get_verified_user_email",
        lambda user_id: "member@example.com",
    )
    request = httpx.Request("POST", "https://backend.test/api/v1/users/link-slack/")
    response = httpx.Response(
        409,
        request=request,
        json={"code": "slack_identity_conflict"},
    )

    result = await SkillExecutor()._handle_points_action(
        client=LinkClient(
            error=httpx.HTTPStatusError(
                "conflict",
                request=request,
                response=response,
            )
        ),
        action="link_account",
        params={},
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "already linked to another Slack profile" in result["message"]
    assert "try `link` again" not in result["message"]


def test_link_fast_path_is_exact_and_does_not_capture_other_integrations():
    agent = object.__new__(RooAgent)

    assert agent._match_fast_path("link") == "link_account"
    assert agent._match_fast_path("link my account") == "link_account"
    assert agent._match_fast_path("link my MLAI account") == "link_account"
    assert agent._match_fast_path("link my slack") == "link_account"
    assert agent._match_fast_path("link my github account") is None
    assert agent._match_fast_path("send me that link") is None


def test_default_implicit_allowlist_permits_the_self_only_link_command():
    settings = Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-test",
        SLACK_SIGNING_SECRET="test-secret",
        OPENAI_API_KEY="test-key",
    )

    assert "mlai-points:link_account" in settings.implicit_action_allowlist


@pytest.mark.asyncio
async def test_link_mention_bypasses_the_model_router(monkeypatch):
    agent = object.__new__(RooAgent)
    agent.skills = [
        SimpleNamespace(name="mlai-points", path=Path(".")),
    ]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    calls = []

    async def fake_execute_fast_points(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {
            "message": "",
            "suppress_post": True,
            "skill_used": "mlai-points (fast)",
            "data": {"action": action},
        }

    async def router_must_not_run(*args, **kwargs):
        raise AssertionError("the exact link command must not reach the model router")

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute_fast_points)
    monkeypatch.setattr(agent, "_route_v2", router_must_not_run)
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")

    result = await agent.handle_mention(
        text="<@UROO> link",
        user_id="UREQUESTER",
        channel_id="CROO",
        thread_ts="111.222",
    )

    assert result["skill_used"] == "mlai-points (fast)"
    assert calls == [
        (
            "UREQUESTER",
            "link_account",
            {"channel_id": "CROO", "thread_ts": "111.222"},
        )
    ]


@pytest.mark.asyncio
async def test_bare_link_in_roo_dm_is_allowed_and_bypasses_the_model(monkeypatch):
    agent = object.__new__(RooAgent)
    agent.skills = [SimpleNamespace(name="mlai-points", path=Path("."))]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    calls = []

    async def fake_execute_fast_points(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {
            "message": "linked",
            "skill_used": "mlai-points (fast)",
            "data": {"action": action},
        }

    async def router_must_not_run(*args, **kwargs):
        raise AssertionError("the bare DM link command must not reach the model router")

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute_fast_points)
    monkeypatch.setattr(agent, "_route_v2", router_must_not_run)
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda **kwargs: [])
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset({"mlai-points:link_account"})
        ),
    )

    result = await agent.handle_mention(
        text="link",
        user_id="UREQUESTER",
        channel_id="DROODM",
        thread_ts="111.222",
        implicit_addressing=True,
    )

    assert result["message"] == "linked"
    assert calls == [
        (
            "UREQUESTER",
            "link_account",
            {"channel_id": "DROODM", "thread_ts": "111.222"},
        )
    ]


@pytest.mark.asyncio
async def test_link_backend_client_only_treats_not_found_as_no_match(monkeypatch):
    from roo.clients.mlai_backend import MLAIBackendClient

    request = httpx.Request("POST", "https://backend.test/api/v1/users/link-slack/")
    responses = [
        httpx.Response(404, request=request, json={"error": "not found"}),
        httpx.Response(500, request=request, json={"error": "backend failed"}),
    ]

    async def fake_request(*args, **kwargs):
        return responses.pop(0)

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-key",
        internal_api_key="internal-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    assert await client.link_slack_user("UREQUESTER", "member@example.com") is None
    with pytest.raises(httpx.HTTPStatusError):
        await client.link_slack_user("UREQUESTER", "member@example.com")
