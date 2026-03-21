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
