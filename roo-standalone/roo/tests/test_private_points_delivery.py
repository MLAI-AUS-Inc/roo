import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-private-points-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "private-points-signing-test")

from roo import agent as agent_module
from roo import coworking_booking_intents as coworking_module
from roo.coworking_booking_schema_v2 import migrate_coworking_booking_intents_v2
from roo.agent import RooAgent
from roo.skills import executor as executor_module
from roo.skills.executor import SkillExecutor


def coworking_intent_store(db_path):
    migrate_coworking_booking_intents_v2(db_path)
    return coworking_module.CoworkingBookingIntentStore(db_path)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (
            "One or more users have insufficient Roo Points",
            "One or more users have insufficient Roo Points",
        ),
        (
            "Insufficient balance: 4 < 8 required",
            "There are not enough Roo Points for this action.",
        ),
        (
            "You have 4 Roo Points but need 8",
            "There are not enough Roo Points for this action.",
        ),
    ],
)
def test_points_error_redaction_preserves_reason_without_exposing_balance(
    detail,
    expected,
):
    assert SkillExecutor._redact_points_balance_error(detail) == expected


class PersonalPointsClient:
    def __init__(self):
        self.balance_users = []
        self.history_users = []
        self.reward_users = []

    async def get_balance(self, slack_user_id):
        self.balance_users.append(slack_user_id)
        return {
            "balance": 37,
            "lifetime_earned": 140,
            "lifetime_spent": 103,
            "lifetime_purchased": 0,
        }

    async def get_history(self, slack_user_id, limit):
        self.history_users.append((slack_user_id, limit))
        return [{"delta": -4, "description": "Coworking booking"}]

    async def list_rewards(self, slack_user_id):
        self.reward_users.append(slack_user_id)
        return [
            {
                "code": "STICKER",
                "name": "Sticker",
                "cost_points": 1,
                "can_afford": True,
                "user_balance": 37,
            }
        ]


def _capture_private_delivery(monkeypatch, *, dm_ok=True, ephemeral_ok=True):
    direct_messages = []
    ephemeral_messages = []

    def fake_send_dm(user_id, text, **kwargs):
        direct_messages.append({"user": user_id, "text": text, **kwargs})
        if isinstance(dm_ok, Exception):
            raise dm_ok
        return {"ok": dm_ok}

    def fake_post_ephemeral(**kwargs):
        ephemeral_messages.append(kwargs)
        if isinstance(ephemeral_ok, Exception):
            raise ephemeral_ok
        return {"ok": ephemeral_ok}

    monkeypatch.setattr(executor_module, "send_dm", fake_send_dm)
    monkeypatch.setattr(executor_module, "post_ephemeral", fake_post_ephemeral)
    return direct_messages, ephemeral_messages


@pytest.mark.asyncio
async def test_routed_balance_in_channel_uses_verified_actor_and_private_delivery(monkeypatch):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)
    client = PersonalPointsClient()

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="balance",
        params={"target_slack_id": "UFORGED"},
        text="show <@UFORGED>'s balance",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.balance_users == ["UVERIFIED"]
    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert direct_messages[0]["user"] == "UVERIFIED"
    assert "Current Balance:** 37 points" in direct_messages[0]["text"]
    assert ephemeral_messages == [
        {
            "channel": "CPOINTS",
            "user": "UVERIFIED",
            "text": "I've sent your Roo Points summary privately.",
            "thread_ts": "111.222",
        }
    ]


@pytest.mark.asyncio
async def test_routed_balance_in_roo_dm_replies_without_opening_another_dm(monkeypatch):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)

    result = await SkillExecutor()._handle_points_action(
        client=PersonalPointsClient(),
        action="balance",
        params={},
        text="points",
        user_id="UVERIFIED",
        channel_id="DPRIVATE",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Current Balance:** 37 points" in result["message"]
    assert result.get("suppress_post") is None
    assert result["data"]["delivery"] == "current_direct_message"
    assert direct_messages == []
    assert ephemeral_messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dm_outcome", "ephemeral_outcome"),
    [
        (False, True),
        (RuntimeError("dm unavailable"), RuntimeError("ephemeral unavailable")),
    ],
)
async def test_balance_delivery_failures_never_fall_back_to_public_message(
    monkeypatch,
    dm_outcome,
    ephemeral_outcome,
):
    _, ephemeral_messages = _capture_private_delivery(
        monkeypatch,
        dm_ok=dm_outcome,
        ephemeral_ok=ephemeral_outcome,
    )

    result = await SkillExecutor()._handle_points_action(
        client=PersonalPointsClient(),
        action="balance",
        params={},
        text="points",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert result["data"]["delivery_failed"] is True
    if ephemeral_messages:
        assert "DM Roo `points`" in ephemeral_messages[0]["text"]
        assert "37" not in ephemeral_messages[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "private_fragment"),
    [
        ("history", "Coworking booking"),
        ("list_rewards", "Your balance: **37 points**"),
    ],
)
async def test_history_and_personalised_rewards_are_private(
    monkeypatch,
    action,
    private_fragment,
):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)

    result = await SkillExecutor()._handle_points_action(
        client=PersonalPointsClient(),
        action=action,
        params={"limit": 5},
        text=action,
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert private_fragment in direct_messages[0]["text"]
    assert ephemeral_messages[0]["user"] == "UVERIFIED"


@pytest.mark.asyncio
async def test_fast_path_forwards_channel_context_and_preserves_private_result(monkeypatch):
    agent = object.__new__(RooAgent)
    calls = []

    async def fake_execute_fast_points(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {
            "message": "",
            "skill_used": "mlai-points (fast)",
            "data": {"action": action},
            "suppress_post": True,
        }

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute_fast_points)

    result = await agent._try_fast_path(
        "points",
        "UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert calls == [
        (
            "UVERIFIED",
            "balance",
            {"channel_id": "CPOINTS", "thread_ts": "111.222"},
        )
    ]
    assert result["suppress_post"] is True


@pytest.mark.asyncio
async def test_fast_executor_keeps_private_delivery_metadata(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_init"] = kwargs

    class FakeSkill:
        name = "mlai-points"

        @staticmethod
        def get_client_class(name):
            assert name == "MLAIBackendClient"
            return FakeClient

    class FakeExecutor:
        async def _handle_points_action(self, **kwargs):
            captured["handler"] = kwargs
            return {
                "message": "",
                "data": {"delivery": "direct_message"},
                "suppress_post": True,
            }

    agent = object.__new__(RooAgent)
    agent.skills = [FakeSkill()]
    agent.skill_executor = FakeExecutor()
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await agent._execute_fast_points(
        "UVERIFIED",
        "balance",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert captured["handler"]["user_id"] == "UVERIFIED"
    assert captured["handler"]["channel_id"] == "CPOINTS"
    assert captured["handler"]["thread_ts"] == "111.222"
    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert result["data"] == {"delivery": "direct_message", "action": "balance"}


@pytest.mark.asyncio
async def test_balance_backend_failure_returns_no_personal_data(monkeypatch):
    direct_messages, ephemeral_messages = _capture_private_delivery(monkeypatch)

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_balance(self, slack_user_id):
            raise RuntimeError("balance=37 for UVERIFIED")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FailingClient)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await SkillExecutor()._execute_mlai_points(
        skill=SimpleNamespace(name="mlai-points"),
        text="points",
        params={"action": "balance"},
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert "trouble with the points system" in result
    assert "37" not in result
    assert "UVERIFIED" not in result
    assert direct_messages == []
    assert ephemeral_messages == []


@pytest.mark.asyncio
async def test_fast_balance_failure_does_not_return_raw_backend_error(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_balance(self, slack_user_id):
            raise RuntimeError("balance=37 for UVERIFIED")

    class FakeSkill:
        name = "mlai-points"

        @staticmethod
        def get_client_class(name):
            assert name == "MLAIBackendClient"
            return FailingClient

    agent = object.__new__(RooAgent)
    agent.skills = [FakeSkill()]
    agent.skill_executor = SkillExecutor()
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await agent._execute_fast_points(
        "UVERIFIED",
        "balance",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert result["data"] == {"error": "points_backend_unavailable"}
    assert "37" not in result["message"]
    assert "UVERIFIED" not in result["message"]


@pytest.mark.asyncio
async def test_fast_coworking_booking_surfaces_terminal_backend_reason(
    monkeypatch,
    tmp_path,
):
    store = coworking_intent_store(tmp_path / "coworking.db")

    class TerminalCoworkingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            request = httpx.Request(
                "POST",
                "https://backend.test/api/v1/points/coworking/book/",
            )
            response = httpx.Response(
                400,
                request=request,
                json={"error": "Please link your Slack account first"},
            )
            raise httpx.HTTPStatusError(
                "bad request",
                request=request,
                response=response,
            )

    class FakeSkill:
        name = "mlai-points"

        @staticmethod
        def get_client_class(name):
            assert name == "MLAIBackendClient"
            return TerminalCoworkingClient

    agent = object.__new__(RooAgent)
    agent.skills = [FakeSkill()]
    agent.skill_executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await agent._execute_fast_points(
        "U123",
        "book_coworking",
        date="2026-08-25",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result["message"] == (
        "🛑 I couldn't book you in for **2026-08-25**: "
        "Please link your Slack account first"
    )
    assert result["data"] == {"action": "book_coworking"}
    assert "connecting" not in result["message"]


class CoworkingClient:
    def __init__(self, *, balance=9):
        self.balance = balance

    async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
        return {
            "id": "booking-private-delivery",
            "date": booking_date,
            "status": "booked",
            "points_cost": 4,
            "standard_points_cost": 8,
            "monthly_update_discount_applied": True,
            "founder_tools_explicitly_linked": False,
        }

    async def get_balance(self, slack_user_id):
        return {"balance": self.balance}


@pytest.mark.asyncio
async def test_channel_coworking_confirmation_keeps_remaining_balance_in_member_dm(
    monkeypatch,
    tmp_path,
):
    direct_messages, _ = _capture_private_delivery(monkeypatch)
    store = coworking_intent_store(tmp_path / "coworking.db")
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await SkillExecutor()._book_coworking_with_intent(
        client=CoworkingClient(balance=9),
        target_user_id="UMEMBER",
        requested_by_user_id="UMEMBER",
        booking_date="2026-08-14",
        text="book coworking tomorrow",
        channel_id="CPOINTS",
        thread_ts="111.222",
        admin_checkin=False,
    )

    assert "Booked you in" in result["message"]
    assert "Balance remaining" not in result["message"]
    assert direct_messages[0]["user"] == "UMEMBER"
    assert "Balance remaining: 9 points" in direct_messages[0]["text"]


@pytest.mark.asyncio
async def test_dm_coworking_confirmation_returns_remaining_balance_directly(
    monkeypatch,
    tmp_path,
):
    direct_messages, _ = _capture_private_delivery(monkeypatch)
    current_dm_messages = []

    def post_current_dm(**kwargs):
        current_dm_messages.append(kwargs)
        return {"ok": True, "ts": "333.444"}

    monkeypatch.setattr("roo.slack_client.post_message", post_current_dm)
    store = coworking_intent_store(tmp_path / "coworking.db")
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await SkillExecutor()._book_coworking_with_intent(
        client=CoworkingClient(balance=9),
        target_user_id="UMEMBER",
        requested_by_user_id="UMEMBER",
        booking_date="2026-08-14",
        text="book coworking tomorrow",
        channel_id="DPRIVATE",
        thread_ts="111.222",
        admin_checkin=False,
    )

    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert "Balance remaining: 9 points" in current_dm_messages[0]["text"]
    assert current_dm_messages[0]["client_msg_id"]
    assert direct_messages == []


@pytest.mark.asyncio
async def test_admin_coworking_confirmation_never_exposes_target_balance(
    monkeypatch,
    tmp_path,
):
    direct_messages, _ = _capture_private_delivery(monkeypatch)
    store = coworking_intent_store(tmp_path / "coworking.db")
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await SkillExecutor()._book_coworking_with_intent(
        client=CoworkingClient(balance=13),
        target_user_id="UTARGET",
        requested_by_user_id="UADMIN",
        booking_date="2026-08-14",
        text="check <@UTARGET> in tomorrow",
        channel_id="CADMIN",
        thread_ts="111.222",
        admin_checkin=True,
    )

    assert "Checked <@UTARGET> in" in result["message"]
    assert "13" not in result["message"]
    assert "Their balance" not in result["message"]
    assert direct_messages[0]["user"] == "UTARGET"
    assert "Balance remaining: 13 points" in direct_messages[0]["text"]


class AwardClient:
    async def get_admin_details(self, slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "admin"}

    async def get_admin_allowance(self, slack_user_id):
        return {"remaining": 100, "allowance": 100}

    async def get_user_by_slack_id(self, slack_user_id):
        return "user-id"

    async def award_points(self, requester_slack_id, target_slack_id, points, reason):
        return {"points_awarded": points, "new_balance": 88}


@pytest.mark.asyncio
async def test_manual_award_keeps_resulting_balance_out_of_public_confirmation(monkeypatch):
    direct_messages, _ = _capture_private_delivery(monkeypatch)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")

    result = await SkillExecutor()._handle_points_action(
        client=AwardClient(),
        action="award_points",
        params={"points": 5, "reason": "helping at the event"},
        text="award <@UTARGET> 5 points for helping at the event",
        user_id="UADMIN",
        channel_id="CADMIN",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Awarded 5 points to <@UTARGET>" in result
    assert "88" not in result
    assert "new balance" not in result.lower()
    assert direct_messages[0]["user"] == "UTARGET"
    assert "new balance is 88 points" in direct_messages[0]["text"]
