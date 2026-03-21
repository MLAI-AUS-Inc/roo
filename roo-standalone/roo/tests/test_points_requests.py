import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

backend_module = importlib.import_module("roo.clients.mlai_backend")
main_module = importlib.import_module("roo.main")
slack_client_module = importlib.import_module("roo.slack_client")
executor_module = importlib.import_module("roo.skills.executor")
SkillExecutor = executor_module.SkillExecutor


class FakePointsClient:
    def __init__(self):
        self.created = None
        self.attached = None

    async def create_points_request(
        self,
        requester_slack_id,
        target_slack_id,
        points,
        reason,
        slack_channel_id=None,
        slack_thread_ts=None,
    ):
        self.created = {
            "requester_slack_id": requester_slack_id,
            "target_slack_id": target_slack_id,
            "points": points,
            "reason": reason,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
        }
        return {"id": 42}

    async def attach_points_request_slack_summary(
        self,
        request_id,
        slack_channel_id,
        slack_thread_ts,
        slack_summary_message_ts,
    ):
        self.attached = {
            "request_id": request_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
            "slack_summary_message_ts": slack_summary_message_ts,
        }
        return {"ok": True}


class FakePointsAdminClient:
    def __init__(self):
        self.promote_args = None
        self.allowance_args = None

    def _clean_slack_id(self, user_id):
        return str(user_id).strip("<@>")

    async def promote_points_admin(self, requester_slack_id, target_slack_id):
        self.promote_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id}

    async def set_points_admin_weekly_allowance(
        self,
        requester_slack_id,
        target_slack_id,
        weekly_allowance,
    ):
        self.allowance_args = (requester_slack_id, target_slack_id, weekly_allowance)
        return {
            "target_slack_id": target_slack_id,
            "weekly_allowance": weekly_allowance,
        }


class FakeAlreadyAdminClient(FakePointsAdminClient):
    async def promote_points_admin(self, requester_slack_id, target_slack_id):
        self.promote_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "already_admin": True}


class FakeMissingPointsAdminClient(FakePointsAdminClient):
    async def set_points_admin_weekly_allowance(
        self,
        requester_slack_id,
        target_slack_id,
        weekly_allowance,
    ):
        self.allowance_args = (requester_slack_id, target_slack_id, weekly_allowance)
        return {"error": "Not a points admin"}


def test_resolve_points_action_prefers_request_points():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {"action": "award_points"},
        "request 5 points for helping at the event",
    )

    assert action == "request_points"


def test_resolve_points_action_maps_plain_request_to_request_points():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {"action": "request"},
        "I'm requesting 6 points for volunteering at MedHack",
    )

    assert action == "request_points"


def test_extract_points_request_reason_supports_requesting_phrase():
    executor = SkillExecutor()

    reason = executor._extract_points_request_reason(
        "I'm requesting 6 points for volunteering at MedHack"
    )

    assert reason == "volunteering at MedHack"


def test_extract_points_request_reason_supports_award_me_phrase():
    executor = SkillExecutor()

    reason = executor._extract_points_request_reason(
        "please award me 6 roo points for volunteering at medhack"
    )

    assert reason == "volunteering at medhack"


def test_resolve_points_action_maps_promote_points_admin():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {},
        "please promote <@U123ABC> to roo points admin",
    )

    assert action == "promote_points_admin"


def test_resolve_points_action_maps_points_allowance_change():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {},
        "change <@U123ABC> weekly points allowance to 150",
    )

    assert action == "set_points_admin_allowance"


@pytest.mark.asyncio
async def test_request_points_creates_request_and_suppresses_auto_post(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="request_points",
        params={"points": 5, "reason": "helping at the event"},
        text="request 5 points for helping at the event",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert result["data"]["request_id"] == 42
    assert client.created == {
        "requester_slack_id": "U123",
        "target_slack_id": "U123",
        "points": 5,
        "reason": "helping at the event",
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
    }
    assert client.attached == {
        "request_id": 42,
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
        "slack_summary_message_ts": "222.333",
    }
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "Points Admins can approve this by reacting with ✅" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_request_points_handles_im_requesting_phrase(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="request_points",
        params={"action": "request", "points": 6, "reason": ""},
        text="I'm requesting 6 points for volunteering at MedHack",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert client.created["points"] == 6
    assert client.created["reason"] == "volunteering at MedHack"
    assert "requested *6 points* for: volunteering at MedHack" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_award_me_phrase_is_converted_into_points_request(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points", "points": 6, "reason": ""},
        text="please award me 6 roo points for volunteering at medhack",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert client.created["target_slack_id"] == "U123"
    assert client.created["reason"] == "volunteering at medhack"
    assert "requested *6 points* for: volunteering at medhack" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_request_points_rejected_in_dm():
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=FakePointsClient(),
        action="request_points",
        params={"points": 5, "reason": "helping at the event"},
        text="request 5 points for helping at the event",
        user_id="U123",
        channel_id="D123",
        thread_ts="111.222",
        skill=None,
    )

    assert "shared channel or thread" in result


@pytest.mark.asyncio
async def test_promote_points_admin_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()
    ensured_users = []

    async def fake_ensure_user_exists(user_id):
        ensured_users.append(user_id)

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UTARGET> to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert ensured_users == ["UTARGET"]
    assert client.promote_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> is now a Roo Points Admin" in result


@pytest.mark.asyncio
async def test_promote_points_admin_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()
    ensured_users = []

    async def fake_ensure_user_exists(user_id):
        ensured_users.append(user_id)

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UTARGET> to roo points admin",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.promote_args is None
    assert ensured_users == []


@pytest.mark.asyncio
async def test_promote_points_admin_requires_exactly_one_tagged_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    missing_result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote sam to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    multiple_result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UONE> and <@UTWO> to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "Tag exactly one user to promote them to Roo Points Admin." in missing_result
    assert "I found multiple mentions there" in multiple_result


@pytest.mark.asyncio
async def test_promote_points_admin_handles_already_admin_idempotently(monkeypatch):
    executor = SkillExecutor()
    client = FakeAlreadyAdminClient()

    async def fake_ensure_user_exists(_user_id):
        return None

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="make <@UTARGET> a roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.promote_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> is already a Roo Points Admin." == result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="change <@UTARGET> weekly points allowance to 150",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        150,
    )
    assert "✅ Set <@UTARGET>'s weekly points allowance to 150 points." == result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 150",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.allowance_args is None


@pytest.mark.asyncio
async def test_set_points_admin_allowance_requires_numeric_positive_value(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    missing_result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    invalid_result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 0",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "What weekly allowance should I set?" in missing_result
    assert "Weekly points allowance has to be a positive number." == invalid_result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_surfaces_target_not_admin(monkeypatch):
    executor = SkillExecutor()
    client = FakeMissingPointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 150",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        150,
    )
    assert "<@UTARGET> isn't a Points Admin yet, so I can't update their allowance." == result


class FakeApprovalClient:
    last_instance = None

    def __init__(self, *args, **kwargs):
        self.lookup_args = None
        self.approve_args = None
        FakeApprovalClient.last_instance = self

    async def get_points_request_by_slack_message(self, slack_channel_id, slack_message_ts):
        self.lookup_args = (slack_channel_id, slack_message_ts)
        return {
            "id": 7,
            "status": "pending",
            "requester_slack_id": "UREQUESTER",
            "target_slack_id": "UREQUESTER",
            "points": 5,
            "reason": "helping at the event",
            "slack_thread_ts": "111.222",
        }

    async def approve_points_request(self, request_id, admin_slack_id):
        self.approve_args = (request_id, admin_slack_id)
        return {"points_awarded": 5, "new_balance": 17}


class FailingApprovalClient(FakeApprovalClient):
    async def approve_points_request(self, request_id, admin_slack_id):
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(403, request=request, json={"error": "Not a points admin"})
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)


@pytest.mark.asyncio
async def test_reaction_approval_posts_confirmation(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert FakeApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert FakeApprovalClient.last_instance.approve_args == (7, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "approved by <@UADMIN>" in posted_messages[0]["text"]
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert direct_messages == []


@pytest.mark.asyncio
async def test_reaction_approval_failure_dms_reactor(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FailingApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UNOTADMIN",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert posted_messages == []
    assert direct_messages
    assert direct_messages[0][0] == "UNOTADMIN"
    assert "Not a points admin" in direct_messages[0][1]


@pytest.mark.asyncio
async def test_reaction_approval_ignores_other_emoji(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "thumbsup",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert posted_messages == []
    assert direct_messages == []
