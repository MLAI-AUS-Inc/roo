import asyncio
import importlib
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

backend_module = importlib.import_module("roo.clients.mlai_backend")
coworking_module = importlib.import_module("roo.coworking_booking_intents")
main_module = importlib.import_module("roo.main")
approval_module = importlib.import_module("roo.points_request_approval")
slack_client_module = importlib.import_module("roo.slack_client")
executor_module = importlib.import_module("roo.skills.executor")
SkillExecutor = executor_module.SkillExecutor


@pytest.fixture(autouse=True)
def clear_points_request_summary_cache():
    approval_module.clear_points_request_summaries()
    yield
    approval_module.clear_points_request_summaries()


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


class FailingCreatePointsClient(FakePointsClient):
    async def create_points_request(
        self,
        requester_slack_id,
        target_slack_id,
        points,
        reason,
        slack_channel_id=None,
        slack_thread_ts=None,
    ):
        request = httpx.Request(
            "POST",
            "https://backend.test/api/v1/points/requests/",
        )
        response = httpx.Response(
            404,
            request=request,
            json={"detail": "Not Found"},
        )
        raise httpx.HTTPStatusError("not found", request=request, response=response)


class FailingAttachPointsClient(FakePointsClient):
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
        request = httpx.Request(
            "PATCH",
            f"https://backend.test/api/v1/points/requests/{request_id}/slack-summary/",
        )
        response = httpx.Response(
            404,
            request=request,
            json={"detail": "Not Found"},
        )
        raise httpx.HTTPStatusError("not found", request=request, response=response)


class FakePointsAdminClient:
    def __init__(self):
        self.promote_args = None
        self.revoke_args = None
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

    async def revoke_points_admin(self, requester_slack_id, target_slack_id):
        self.revoke_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "revoked": True}


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


class FakeAlreadyRevokedClient(FakePointsAdminClient):
    async def revoke_points_admin(self, requester_slack_id, target_slack_id):
        self.revoke_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "already_revoked": True}


class FakeAwardGuardClient(FakePointsAdminClient):
    def __init__(self):
        super().__init__()
        self.allowance_lookup_called = False
        self.award_called = False

    async def get_admin_allowance(self, requester_slack_id):
        self.allowance_lookup_called = True
        return {"remaining": 100, "allowance": 100}

    async def award_points(self, requester_slack_id, target_slack_id, points, reason):
        self.award_called = True
        return {"points_awarded": points, "new_balance": 999}


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


def test_resolve_points_action_maps_natural_language_points_allowance_change():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {"action": "award_points"},
        "please increase the number of points <@U123ABC> can give out weekly to 48",
    )

    assert action == "set_points_admin_allowance"


def test_resolve_points_action_maps_revoke_points_admin():
    executor = SkillExecutor()

    action = executor._resolve_points_action(
        {},
        "remove <@U123ABC> as roo points admin",
    )

    assert action == "revoke_points_admin"


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
    assert "Points Admins can approve this by reacting with a green tick" in posted_messages[0]["text"]
    assert posted_messages[0]["metadata"] == {
        "event_type": "roo_points_request",
        "event_payload": {
            "request_id": 42,
            "requester_slack_id": "U123",
            "target_slack_id": "U123",
            "points": 5,
            "reason": "helping at the event",
            "slack_thread_ts": "111.222",
        },
    }


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
async def test_request_points_404_returns_integration_error(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=FailingCreatePointsClient(),
        action="request_points",
        params={"points": 12, "reason": "running the 21st x MLAI event"},
        text="I'm requesting 12 points for running the 21st x MLAI event",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "queue that points request for admin approval" in result
    assert "Double-check the ID or date" not in result


@pytest.mark.asyncio
async def test_request_points_attach_failure_keeps_slack_approval_active(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=FailingAttachPointsClient(),
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
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert posted_messages[0]["metadata"]["event_payload"]["request_id"] == 42
    assert (
        approval_module.get_remembered_points_request_summary("C123", "222.333")["id"] == 42
    )


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
async def test_execute_mlai_points_prefers_roo_api_key(monkeypatch):
    executor = SkillExecutor()
    captured = {}

    async def fake_handle_points_action(
        self,
        client,
        action,
        params,
        text,
        user_id,
        channel_id,
        thread_ts,
        skill,
    ):
        captured["client"] = client
        captured["action"] = action
        return {"ok": True}

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="mlai-api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", InitCapturingBackendClient)
    monkeypatch.setattr(
        SkillExecutor,
        "_handle_points_action",
        fake_handle_points_action,
    )

    result = await executor._execute_mlai_points(
        skill=SimpleNamespace(name="mlai-points"),
        text="request 5 points for helping at the event",
        params={"action": "request_points", "points": 5, "reason": "helping at the event"},
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result == {"ok": True}
    assert captured["action"] == "request_points"
    assert isinstance(captured["client"], InitCapturingBackendClient)
    assert InitCapturingBackendClient.last_init["kwargs"] == {
        "base_url": "https://backend.test",
        "api_key": "roo-api-key",
        "internal_api_key": "internal-key",
    }


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
async def test_revoke_points_admin_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="remove <@UTARGET> as roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.revoke_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "✅ Removed Roo Points Admin access from <@UTARGET>." == result


@pytest.mark.asyncio
async def test_revoke_points_admin_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="revoke <@UTARGET> as roo points admin",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.revoke_args is None


@pytest.mark.asyncio
async def test_revoke_points_admin_handles_already_revoked_idempotently(monkeypatch):
    executor = SkillExecutor()
    client = FakeAlreadyRevokedClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="revoke <@UTARGET> as roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.revoke_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> already doesn't have Roo Points Admin access." == result


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


@pytest.mark.asyncio
async def test_award_flow_reroutes_management_phrase_to_allowance_update(monkeypatch):
    executor = SkillExecutor()
    client = FakeAwardGuardClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points"},
        text="please increase the number of points <@UTARGET> can give out weekly to 48",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        48,
    )
    assert client.allowance_lookup_called is False
    assert client.award_called is False
    assert "✅ Set <@UTARGET>'s weekly points allowance to 48 points." == result


@pytest.mark.asyncio
async def test_award_flow_reroutes_missing_tag_management_phrase_to_clarification(monkeypatch):
    executor = SkillExecutor()
    client = FakeAwardGuardClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points"},
        text="please increase the number of points sam can give out weekly to 48",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args is None
    assert client.allowance_lookup_called is False
    assert client.award_called is False
    assert "Tag exactly one user to change their weekly points allowance." == result


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


class LookupMissApprovalClient(FakeApprovalClient):
    async def get_points_request_by_slack_message(self, slack_channel_id, slack_message_ts):
        self.lookup_args = (slack_channel_id, slack_message_ts)
        return None


class InitCapturingBackendClient:
    last_init = None

    def __init__(self, *args, **kwargs):
        InitCapturingBackendClient.last_init = {
            "args": args,
            "kwargs": kwargs,
        }


class RecordingAsyncClient:
    def __init__(self, *, status_code=200, json_data=None):
        self.status_code = status_code
        self.json_data = {} if json_data is None else json_data
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def _response(self, method, url, *, json=None, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return httpx.Response(
            self.status_code,
            request=httpx.Request(method, url),
            json=self.json_data,
        )

    async def request(self, method, url, **kwargs):
        return self._response(
            method,
            url,
            json=kwargs.get("json"),
            params=kwargs.get("params"),
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout"),
        )

    async def post(self, url, *, json=None, headers=None, timeout=None):
        return self._response("POST", url, json=json, headers=headers, timeout=timeout)

    async def patch(self, url, *, json=None, headers=None, timeout=None):
        return self._response("PATCH", url, json=json, headers=headers, timeout=timeout)

    async def get(self, url, *, params=None, headers=None, timeout=None):
        return self._response("GET", url, params=params, headers=headers, timeout=timeout)


class FakeStartHereAwardClient:
    last_instance = None
    response = {"awarded": True, "new_balance": 2}

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.award_args = None
        FakeStartHereAwardClient.last_instance = self

    async def award_first_channel_post(self, slack_user_id, channel_id):
        self.award_args = (slack_user_id, channel_id)
        return dict(self.response)


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
            ROO_API_KEY="roo-api-key",
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
            ROO_API_KEY="roo-api-key",
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
async def test_reaction_approval_uses_cached_summary_mapping_when_backend_lookup_misses(monkeypatch):
    posted_messages = []
    direct_messages = []

    approval_module.remember_points_request_summary(
        "C123",
        "222.333",
        {
            "id": 9,
            "status": "pending",
            "requester_slack_id": "UREQUESTER",
            "target_slack_id": "UREQUESTER",
            "points": 18,
            "reason": "helping out at the openclaw event last night",
            "slack_thread_ts": "111.222",
        },
    )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", LookupMissApprovalClient)
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

    assert LookupMissApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert LookupMissApprovalClient.last_instance.approve_args == (9, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert approval_module.get_remembered_points_request_summary("C123", "222.333") is None
    assert direct_messages == []


@pytest.mark.asyncio
async def test_reaction_approval_uses_message_metadata_for_heavy_check_mark(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", LookupMissApprovalClient)
    monkeypatch.setattr(
        main_module,
        "get_message",
        lambda channel, message_ts: {
            "metadata": {
                "event_type": "roo_points_request",
                "event_payload": {
                    "request_id": 12,
                    "requester_slack_id": "UREQUESTER",
                    "target_slack_id": "UREQUESTER",
                    "points": 18,
                    "reason": "helping out at the openclaw event last night",
                    "slack_thread_ts": "111.222",
                },
            }
        },
    )
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
            "reaction": "heavy_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert LookupMissApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert LookupMissApprovalClient.last_instance.approve_args == (12, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert direct_messages == []


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


@pytest.mark.asyncio
async def test_slack_events_start_here_message_triggers_intro_handler(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_handle_start_here_intro(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module, "_handle_start_here_intro", fake_handle_start_here_intro)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]


@pytest.mark.asyncio
async def test_slack_events_ignores_start_here_thread_reply(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
            "thread_ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_non_start_here_message_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UOTHER",
            "channel": "COTHER",
            "ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_bot_message_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UBOT",
            "channel": "CSTART",
            "ts": "111.222",
            "bot_id": "B123",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_message_subtype_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
            "subtype": "message_changed",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_dedupes_retried_app_mention(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task
    main_module._recent_app_mention_events.clear()

    async def fake_handle_mention(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "team_id": "T123",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "ts": "111.222",
            "text": "<@UROO> book me in today",
        },
    }

    class FakeRequest:
        async def json(self):
            return payload

    response_1 = await main_module.slack_events(FakeRequest())
    response_2 = await main_module.slack_events(FakeRequest())

    assert response_1.status_code == 200
    assert response_2.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]
    main_module._recent_app_mention_events.clear()


@pytest.mark.asyncio
async def test_slack_events_allows_new_app_mention_timestamp(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task
    main_module._recent_app_mention_events.clear()

    async def fake_handle_mention(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    first_event = {
        "type": "app_mention",
        "user": "U123",
        "channel": "C123",
        "ts": "111.222",
        "text": "<@UROO> book me in today",
    }
    second_event = dict(first_event, ts="111.333")

    class FakeRequest:
        def __init__(self, event):
            self._payload = {"team_id": "T123", "event": event}

        async def json(self):
            return self._payload

    response_1 = await main_module.slack_events(FakeRequest(first_event))
    response_2 = await main_module.slack_events(FakeRequest(second_event))

    assert response_1.status_code == 200
    assert response_2.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [first_event, second_event]
    main_module._recent_app_mention_events.clear()


def test_app_mention_dedupe_logs_ttl_expiry(capsys):
    main_module._recent_app_mention_events.clear()
    event = {
        "type": "app_mention",
        "user": "U123",
        "channel": "C123",
        "ts": "111.222",
    }
    payload = {"team_id": "T123", "event": event}

    assert main_module._mark_app_mention_event_seen(payload, event, now=1000.0)
    assert main_module._mark_app_mention_event_seen(payload, dict(event, ts="111.333"), now=2000.0)

    captured = capsys.readouterr()
    assert "Slack app_mention dedupe TTL expired count=1" in captured.out
    main_module._recent_app_mention_events.clear()


@pytest.mark.asyncio
async def test_handle_start_here_intro_awards_and_posts_thread_reply(monkeypatch):
    posted_messages = []
    FakeStartHereAwardClient.response = {"awarded": True, "new_balance": 2}

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeStartHereAwardClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs),
    )

    await main_module._handle_start_here_intro(
        {
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
        }
    )

    assert FakeStartHereAwardClient.last_instance.award_args == ("UINTRO", "CSTART")
    assert posted_messages == [
        {
            "channel": "CSTART",
            "thread_ts": "111.222",
            "text": "Welcome <@UINTRO>! You've earned 2 Roo points for introducing yourself here.",
        }
    ]


@pytest.mark.asyncio
async def test_handle_start_here_intro_noops_when_award_already_exists(monkeypatch):
    posted_messages = []
    FakeStartHereAwardClient.response = {"awarded": False}

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeStartHereAwardClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs),
    )

    await main_module._handle_start_here_intro(
        {
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
        }
    )

    assert FakeStartHereAwardClient.last_instance.award_args == ("UINTRO", "CSTART")
    assert posted_messages == []


@pytest.mark.asyncio
async def test_backend_client_create_points_request_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"id": 42})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.create_points_request(
        requester_slack_id="UREQUESTER",
        target_slack_id="<@UTARGET>",
        points=12,
        reason="running the 21st x MLAI event",
        slack_channel_id="C123",
        slack_thread_ts="111.222",
    )

    assert result == {"id": 42}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/"
    assert recorder.calls[0]["json"] == {
        "requester_slack_id": "UREQUESTER",
        "target_slack_id": "UTARGET",
        "points": 12,
        "reason": "running the 21st x MLAI event",
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "api-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_attach_points_request_summary_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"ok": True})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.attach_points_request_slack_summary(
        request_id=42,
        slack_channel_id="C123",
        slack_thread_ts="111.222",
        slack_summary_message_ts="222.333",
    )

    assert result == {"ok": True}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "PATCH"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/42/slack-summary/"
    assert recorder.calls[0]["json"] == {
        "slack_channel_id": "C123",
        "slack_summary_message_ts": "222.333",
        "slack_thread_ts": "111.222",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_lookup_points_request_by_slack_message_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"id": 42, "status": "pending"})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.get_points_request_by_slack_message(
        slack_channel_id="C123",
        slack_message_ts="222.333",
    )

    assert result == {"id": 42, "status": "pending"}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "GET"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/by-slack-message/"
    assert recorder.calls[0]["json"] is None
    assert recorder.calls[0]["params"] == {
        "slack_channel_id": "C123",
        "slack_message_ts": "222.333",
    }
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_approve_points_request_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"points_awarded": 12, "new_balance": 17})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.approve_points_request(
        request_id=42,
        admin_slack_id="UADMIN",
    )

    assert result == {"points_awarded": 12, "new_balance": 17}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/42/approve/"
    assert recorder.calls[0]["json"] == {"admin_slack_id": "UADMIN"}
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 15.0


@pytest.mark.asyncio
async def test_backend_client_award_first_channel_post_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"awarded": True, "new_balance": 2})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.award_first_channel_post(
        slack_user_id="<@UINTRO>",
        channel_id="CSTART",
    )

    assert result == {"awarded": True, "new_balance": 2}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/activity/first-post-award/"
    assert recorder.calls[0]["json"] == {
        "slack_user_id": "UINTRO",
        "channel_id": "CSTART",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 15.0


@pytest.mark.asyncio
async def test_execute_mlai_points_returns_backend_unavailable_message(monkeypatch):
    executor = SkillExecutor()
    skill = SimpleNamespace(name="mlai-points")

    async def fake_handle_points_action(**kwargs):
        raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    monkeypatch.setattr(
        executor,
        "_handle_points_action",
        fake_handle_points_action,
    )
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await executor._execute_mlai_points(
        skill=skill,
        text="book coworking today",
        params={"action": "book_coworking", "date": "today"},
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "couldn't confirm whether your coworking booking went through" in result.lower()


@pytest.mark.asyncio
async def test_book_coworking_still_succeeds_when_balance_refresh_times_out(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class FakeCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            return {"points_cost": 4}

        async def get_balance(self, slack_user_id):
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: __import__("datetime").date(2026, 4, 9))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    try:
        result = await executor._handle_points_action(
            client=FakeCoworkingClient(),
            action="book_coworking",
            params={"date": "2026-04-09"},
            text="book coworking today",
            user_id="U123",
            channel_id="C123",
            thread_ts="111.222",
            skill=SimpleNamespace(name="mlai-points"),
        )
    finally:
        monkeypatch.undo()

    assert "Booked you in for **2026-04-09**" in result
    assert "Balance remaining" not in result


class FakeCoworkingReportClient:
    def __init__(self):
        self.calls = []

    async def get_coworking_report(self, slack_user_id, start_date, end_date):
        self.calls.append((slack_user_id, start_date, end_date))
        return {
            "range": {
                "start_date": start_date,
                "end_date": end_date,
                "source": "active_coworking_bookings",
            },
            "totals": {
                "booked_user_days": 3,
                "unique_users": 2,
                "active_days": 2,
                "range_days": 31,
                "average_per_day": 0.1,
                "busiest_days": [{"date": start_date, "booked_users": 2}],
            },
            "monthly": [
                {"month": start_date[:7], "booked_user_days": 3, "active_days": 2},
            ],
            "weekly": [
                {"week_start": start_date, "booked_user_days": 3, "active_days": 2},
            ],
            "daily": [
                {"date": start_date, "booked_users": 2},
                {"date": end_date, "booked_users": 1},
            ],
        }


@pytest.mark.asyncio
async def test_coworking_report_exact_range_formats_slack_report():
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report from 2026-01-01 to 2026-01-31",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-01", "2026-01-31")
    ]
    assert "Source: Active coworking bookings" in result
    assert "Booked user-days: 3" in result
    assert "Unique users: 2" in result
    assert "*Monthly*" in result
    assert "*Weekly*" in result
    assert "*Daily*" in result
    assert "2026-01" in result
    assert "2026-01-01" in result
    assert "```" in result


@pytest.mark.asyncio
async def test_coworking_report_denies_non_super_admin_without_backend_call():
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        text="coworking report 2026-01-01 2026-01-31",
        user_id="UOTHER",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "only <@U05QPB483K9> can generate coworking reports" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_coworking_report_last_three_months_uses_current_date(monkeypatch):
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))

    await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report last 3 months",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-29", "2026-04-28")
    ]


@pytest.mark.asyncio
async def test_coworking_report_this_week_uses_monday_through_today(monkeypatch):
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))

    await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="how many people used the coworking space this week",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-27", "2026-04-28")
    ]


def test_extract_http_error_detail_suppresses_html_500_body():
    executor = SkillExecutor()
    request = httpx.Request("GET", "https://backend.test/api/v1/points/coworking/report/")
    response = httpx.Response(
        500,
        request=request,
        text="<!doctype html><html><body><h1>Server Error (500)</h1></body></html>",
    )
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    detail = executor._extract_http_error_detail(exc)

    assert "<!doctype html>" not in detail
    assert "temporarily unavailable" in detail


@pytest.mark.asyncio
async def test_execute_mlai_points_html_500_uses_backend_unavailable_message(monkeypatch):
    executor = SkillExecutor()
    skill = SimpleNamespace(name="mlai-points")

    async def fake_handle_points_action(**kwargs):
        request = httpx.Request(
            "GET",
            "https://backend.test/api/v1/points/coworking/report/",
        )
        response = httpx.Response(
            500,
            request=request,
            text="<!doctype html><html><body><h1>Server Error (500)</h1></body></html>",
        )
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    monkeypatch.setattr(executor, "_handle_points_action", fake_handle_points_action)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await executor._execute_mlai_points(
        skill=skill,
        text="how many people used the coworking space this week",
        params={},
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "<!doctype html>" not in result
    assert "couldn't reach the MLAI points backend" in result


def test_resolve_points_action_detects_coworking_report_wording():
    executor = SkillExecutor()

    assert executor._resolve_points_action({}, "coworking summary last 6 months") == "coworking_report"
    assert executor._resolve_points_action({"action": "report"}, "coworking report last year") == "coworking_report"
    assert (
        executor._resolve_points_action(
            {},
            "how many people used the coworking space this week",
        )
        == "coworking_report"
    )


@pytest.mark.asyncio
async def test_book_coworking_persists_intent_and_queues_backend_timeout(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class TimeoutCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await executor._handle_points_action(
        client=TimeoutCoworkingClient(),
        action="book_coworking",
        params={"date": "2026-04-22"},
        text="book coworking today",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:U123:2026-04-22")
    assert "queued it and will keep retrying automatically" in result
    assert intent["status"] == "pending_retry"
    assert intent["slack_user_id"] == "U123"
    assert intent["booking_date"] == "2026-04-22"
    assert intent["channel_id"] == "C123"
    assert intent["thread_ts"] == "111.222"
    assert "backend unavailable" in intent["last_error"]


@pytest.mark.asyncio
async def test_dependency_health_check_reports_degraded_backend(monkeypatch):
    class FakeBackendClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_backend_readiness(self):
            return {"status": "ok"}

        async def get_points_health(self):
            raise backend_module.MLAIBackendUnavailableError("points probe failed")

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeBackendClient)
    main_module.app.state.startup_complete = True

    result = await main_module.dependency_health_check()

    assert result["status"] == "degraded"
    assert result["dependencies"]["mlai_backend"]["status"] == "degraded"
    assert result["dependencies"]["mlai_backend"]["readiness"]["status"] == "ok"
    assert "points_error" in result["dependencies"]["mlai_backend"]
