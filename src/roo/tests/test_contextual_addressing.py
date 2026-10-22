import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import addressing
from roo import agent as agent_module
from roo import main as main_module
from roo.addressing import AddressDecision
from roo.conversation_sessions import ContextualConversationStore
from roo.config import Settings
from roo.llm import ToolCall
from roo.router import RouteDecision
from roo.skills.loader import Skill


BASE_SETTINGS = {
    "_env_file": None,
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "test-secret",
    "OPENAI_API_KEY": "test-key",
}


def test_contextual_settings_require_pilot_channels():
    with pytest.raises(ValidationError, match="ROO_CONTEXTUAL_CHANNEL_IDS"):
        Settings(**BASE_SETTINGS, ROO_CONTEXTUAL_RESPONSES_ENABLED=True)

    configured = Settings(
        **BASE_SETTINGS,
        ROO_CONTEXTUAL_RESPONSES_ENABLED=True,
        ROO_CONTEXTUAL_CHANNEL_IDS="C123 G456",
    )
    assert configured.contextual_channel_ids == frozenset({"C123", "G456"})

def test_addressing_eval_cases_are_well_formed():
    cases_path = Path(__file__).parents[1] / "addressing_eval" / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert {case["expect"] for case in cases} == {"respond", "ignore"}
    assert "indirect-teaching-command" in ids
    assert "same-user-pack-followup" in ids


def test_candidate_prefilter_requires_a_strong_context_signal():
    assert addressing.candidate_reason_for_message(
        text="Anyone up for lunch?",
        explicit_mention=False,
        thread_ts=None,
        session=None,
    ) is None
    assert addressing.candidate_reason_for_message(
        text="Roo, can you help?",
        explicit_mention=False,
        thread_ts=None,
        session=None,
    ) == "plain_roo_name"
    assert addressing.candidate_reason_for_message(
        text="20 please",
        explicit_mention=False,
        thread_ts="111.000",
        session=SimpleNamespace(session_key="thread:111.000"),
    ) == "same_user_thread_continuation"


def test_exact_indirect_mention_from_conversation_is_detected_without_llm():
    assert addressing.obvious_indirect_mention(
        "lol sorry sam you'll need to say <@UROO> topup 20 points",
        "UROO",
    )
    assert not addressing.obvious_indirect_mention(
        "<@UROO> topup 20 points",
        "UROO",
    )


@pytest.mark.asyncio
async def test_implicit_followup_requires_high_confidence(monkeypatch):
    async def fake_chat_tools(messages, tools, **kwargs):
        return ToolCall(
            name="decide_addressing",
            arguments={
                "decision": "respond",
                "confidence": 0.89,
                "reason": "thread_continuation",
            },
        )

    monkeypatch.setattr(addressing, "chat_tools", fake_chat_tools)
    decision = await addressing.decide_addressing(
        text="Then top up 20 points please",
        user_id="USAM",
        bot_user_id="UROO",
        history=[{"user": "UROO", "text": "Choose 10, 20, or 50", "is_bot": True}],
        current_message_ts="2.0",
        candidate_reason="same_user_thread_continuation",
        explicit_mention=False,
        min_implicit_confidence=0.90,
        indirect_mention_confidence=0.90,
    )
    assert not decision.should_respond
    assert decision.confidence == 0.89


@pytest.mark.asyncio
async def test_implicit_classifier_failure_is_silent_but_direct_mention_survives(monkeypatch):
    async def failing_chat_tools(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(addressing, "chat_tools", failing_chat_tools)
    common = {
        "text": "topup 20 points",
        "user_id": "USAM",
        "bot_user_id": "UROO",
        "history": [],
        "current_message_ts": "2.0",
        "min_implicit_confidence": 0.90,
        "indirect_mention_confidence": 0.90,
    }
    implicit = await addressing.decide_addressing(
        **common,
        candidate_reason="same_user_channel_adjacency",
        explicit_mention=False,
    )
    direct = await addressing.decide_addressing(
        **common,
        candidate_reason="explicit_mention",
        explicit_mention=True,
    )
    assert not implicit.should_respond
    assert direct.should_respond


def test_contextual_store_dedupes_and_tracks_same_user_sessions(tmp_path):
    store = ContextualConversationStore(tmp_path / "context.db")
    assert store.claim_message(
        team_id="T1",
        channel_id="C1",
        message_ts="1.0",
        ttl_seconds=60,
        now=100,
    )
    assert not store.claim_message(
        team_id="T1",
        channel_id="C1",
        message_ts="1.0",
        ttl_seconds=60,
        now=101,
    )
    assert store.claim_message(
        team_id="T1",
        channel_id="C1",
        message_ts="1.0",
        ttl_seconds=60,
        now=161,
    )

    store.record_roo_response(
        team_id="T1",
        channel_id="C1",
        requester_user_id="USAM",
        thread_ts="1.0",
        bot_message_ts="1.1",
        adjacency_seconds=180,
        thread_ttl_seconds=1800,
        state="awaiting_linear_approval",
        workflow="linear-meeting-actions",
        reference_id="batch-1",
        now=200,
    )
    thread = store.find_session(
        team_id="T1",
        channel_id="C1",
        requester_user_id="USAM",
        thread_ts="1.0",
        now=300,
    )
    assert thread is not None
    assert thread.session_key == "thread:1.0"
    assert thread.state == "awaiting_linear_approval"
    assert thread.workflow == "linear-meeting-actions"
    assert thread.reference_id == "batch-1"
    assert store.find_session(
        team_id="T1",
        channel_id="C1",
        requester_user_id="UOTHER",
        thread_ts="1.0",
        now=300,
    ) is None

    store.break_channel_adjacency(team_id="T1", channel_id="C1")
    # Breaking top-level adjacency does not destroy the Roo-owned thread.
    assert store.find_session(
        team_id="T1",
        channel_id="C1",
        requester_user_id="USAM",
        thread_ts="1.0",
        now=300,
    ) is not None


def test_contextual_store_upgrades_existing_session_schema(tmp_path):
    database_path = tmp_path / "legacy-context.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE contextual_conversation_sessions (
                team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                requester_user_id TEXT NOT NULL,
                thread_ts TEXT,
                last_bot_ts TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (team_id, channel_id, session_key)
            )
            """
        )

    store = ContextualConversationStore(database_path)
    store.record_roo_response(
        team_id="T1",
        channel_id="C1",
        requester_user_id="USAM",
        thread_ts="1.0",
        bot_message_ts="1.1",
        adjacency_seconds=180,
        thread_ttl_seconds=1800,
        workflow="linear-meeting-actions",
        reference_id="batch-legacy",
        now=200,
    )

    session = store.find_session(
        team_id="T1",
        channel_id="C1",
        requester_user_id="USAM",
        thread_ts="1.0",
        now=201,
    )
    assert session is not None
    assert session.workflow == "linear-meeting-actions"
    assert session.reference_id == "batch-legacy"


class FakeContextualStore:
    def __init__(self, session):
        self.session = session
        self.recorded = []
        self.broken = []

    def claim_message(self, **kwargs):
        return True

    def find_session(self, **kwargs):
        return self.session

    def record_roo_response(self, **kwargs):
        self.recorded.append(kwargs)

    def break_channel_adjacency(self, **kwargs):
        self.broken.append(kwargs)


@pytest.mark.asyncio
async def test_contextual_handler_accepts_requester_untagged_batch_approval(monkeypatch):
    session = SimpleNamespace(
        session_key="thread:1.0",
        requester_user_id="USAM",
        workflow="linear-meeting-actions",
        state="awaiting_linear_approval",
        reference_id="batch-1",
    )
    store = FakeContextualStore(session)
    decisions = []
    posts = []

    async def fake_history(event):
        return [{"user": "UROO", "text": "Review these tasks", "is_bot": True}]

    async def fake_decision(**kwargs):
        return AddressDecision(
            should_respond=True,
            confidence=0.99,
            reason="answer_to_roo",
            source="llm",
            candidate_reason=kwargs["candidate_reason"],
        )

    class FakeClient:
        async def decide_action_batch(self, **kwargs):
            decisions.append(kwargs)
            return {
                "status": "completed",
                "counts": {"approved": 2, "rejected": 0, "failed": 0},
                "items": [],
            }

    skill = SimpleNamespace(
        get_client_class=lambda name: FakeClient,
    )
    agent = SimpleNamespace(
        _get_skill_by_name=lambda name: skill,
    )

    monkeypatch.setattr(main_module, "get_settings", contextual_settings)
    monkeypatch.setattr(main_module, "get_contextual_conversation_store", lambda path: store)
    monkeypatch.setattr(main_module, "get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(main_module, "_get_addressing_history", fake_history)
    monkeypatch.setattr(main_module, "decide_addressing", fake_decision)
    monkeypatch.setattr(main_module, "get_agent", lambda: agent)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posts.append(kwargs) or {"ts": "2.1"},
    )

    await main_module._handle_contextual_slack_message(
        {
            "type": "message",
            "user": "USAM",
            "channel": "C1",
            "thread_ts": "1.0",
            "ts": "2.0",
            "text": "approve all",
        },
        slack_team_id="T1",
        trigger_source="channel_message",
    )

    assert decisions == [{
        "batch_id": "batch-1",
        "requested_by_slack_user_id": "USAM",
        "decision": "approve",
        "item_ids": None,
    }]
    assert "2 created" in posts[0]["text"]
    assert store.recorded[0]["state"] == "completed"
    assert store.recorded[0]["workflow"] == "linear-meeting-actions"
    assert store.recorded[0]["reference_id"] == "batch-1"


@pytest.mark.asyncio
async def test_linear_batch_followup_rejects_cross_user_and_ambiguous_text():
    session = SimpleNamespace(
        requester_user_id="USAM",
        workflow="linear-meeting-actions",
        state="awaiting_linear_approval",
        reference_id="batch-1",
    )
    assert await main_module._maybe_handle_linear_meeting_followup(
        {"user": "UOTHER", "text": "approve all"},
        session=session,
        explicit_mention=False,
    ) is None
    assert await main_module._maybe_handle_linear_meeting_followup(
        {"user": "USAM", "text": "looks good but change the owner"},
        session=session,
        explicit_mention=False,
    ) is None


def contextual_settings(**overrides):
    values = {
        "SLACK_CONTEXTUAL_STATE_DB_PATH": "unused.db",
        "ROO_CONTEXTUAL_MESSAGE_RECEIPT_TTL_SECONDS": 600,
        "ROO_CONTEXTUAL_SHADOW_MODE": False,
        "ROO_CONTEXTUAL_MIN_CONFIDENCE": 0.90,
        "ROO_CONTEXTUAL_INDIRECT_MENTION_CONFIDENCE": 0.90,
        "ROO_CONTEXTUAL_MODEL": None,
        "ROO_CONTEXTUAL_CLASSIFIER_TIMEOUT_SECONDS": 5.0,
        "ROO_CONTEXTUAL_ADJACENCY_SECONDS": 180,
        "ROO_CONTEXTUAL_THREAD_TTL_SECONDS": 1800,
    }
    return SimpleNamespace(**{**values, **overrides})


@pytest.mark.asyncio
async def test_contextual_handler_routes_same_user_untagged_followup(monkeypatch):
    store = FakeContextualStore(SimpleNamespace(session_key="thread:1.0"))
    handled = []

    async def fake_history(event):
        return [{"user": "UROO", "text": "Choose 10, 20, or 50", "is_bot": True}]

    async def fake_decision(**kwargs):
        return AddressDecision(
            should_respond=True,
            confidence=0.99,
            reason="answer_to_roo",
            source="llm",
            candidate_reason=kwargs["candidate_reason"],
        )

    async def fake_handle(event, **kwargs):
        handled.append(event)
        return {"post_response": {"ts": "2.1"}, "thread_ts": "1.0", "result": {}}

    monkeypatch.setattr(main_module, "get_settings", contextual_settings)
    monkeypatch.setattr(main_module, "get_contextual_conversation_store", lambda path: store)
    monkeypatch.setattr(main_module, "get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(main_module, "_get_addressing_history", fake_history)
    monkeypatch.setattr(main_module, "decide_addressing", fake_decision)
    monkeypatch.setattr(main_module, "_handle_mention", fake_handle)

    await main_module._handle_contextual_slack_message(
        {
            "type": "message",
            "user": "USAM",
            "channel": "C1",
            "thread_ts": "1.0",
            "ts": "2.0",
            "text": "Then top up 20 points please",
        },
        slack_team_id="T1",
        trigger_source="channel_message",
    )

    assert len(handled) == 1
    assert handled[0]["implicit_addressing"] is True
    assert store.recorded[0]["requester_user_id"] == "USAM"


@pytest.mark.asyncio
async def test_contextual_handler_ignores_person_teaching_command_to_another_user(monkeypatch):
    store = FakeContextualStore(None)
    handled = []

    async def fake_history(event):
        return []

    async def fake_handle(event, **kwargs):
        handled.append(event)

    monkeypatch.setattr(main_module, "get_settings", contextual_settings)
    monkeypatch.setattr(main_module, "get_contextual_conversation_store", lambda path: store)
    monkeypatch.setattr(main_module, "get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(main_module, "_get_addressing_history", fake_history)
    monkeypatch.setattr(main_module, "_handle_mention", fake_handle)

    await main_module._handle_contextual_slack_message(
        {
            "type": "app_mention",
            "user": "UDRSAM",
            "channel": "C1",
            "ts": "3.0",
            "text": "lol sorry sam you'll need to say <@UROO> topup 20 points",
        },
        slack_team_id="T1",
        trigger_source="app_mention",
    )

    assert handled == []


def test_implicit_action_guard_defaults_to_narrow_self_service(monkeypatch):
    agent = object.__new__(agent_module.RooAgent)
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset(
                {
                    "respond_in_chat",
                    "mlai-points:balance",
                    "mlai-points:topup_points",
                }
            )
        ),
    )
    assert agent._is_implicit_action_allowed("respond_in_chat", None)
    assert agent._is_implicit_action_allowed("mlai-points", "topup_points")
    assert not agent._is_implicit_action_allowed("mlai-points", "award_points")


@pytest.mark.asyncio
async def test_implicit_admin_action_is_blocked_before_executor(monkeypatch):
    agent = object.__new__(agent_module.RooAgent)
    agent.skills = [
        Skill(
            name="mlai-points",
            description="points",
            content="",
            path=Path("."),
        )
    ]
    agent._thread_skill_context = {}

    class FailingExecutor:
        async def execute(self, **kwargs):
            raise AssertionError("blocked implicit action must not execute")

    agent.skill_executor = FailingExecutor()

    async def fake_route(self, *args, **kwargs):
        return RouteDecision(skill="mlai-points", action="award_points", params={})

    monkeypatch.setattr(agent_module.RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset(
                {"respond_in_chat", "mlai-points:topup_points"}
            )
        ),
    )

    result = await agent.handle_mention(
        text="give Sam 20 points",
        user_id="UADMIN",
        channel_id="C1",
        thread_ts="1.0",
        implicit_addressing=True,
    )

    assert result["suppress_post"] is True
    assert result["data"]["implicit_action_blocked"] is True


@pytest.mark.asyncio
async def test_implicit_topup_continuation_reaches_existing_executor(monkeypatch):
    agent = object.__new__(agent_module.RooAgent)
    agent.skills = [
        Skill(
            name="mlai-points",
            description="points",
            content="",
            path=Path("."),
        )
    ]
    agent._thread_skill_context = {}
    captured = {}

    class CaptureExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="checkout ready",
                data=None,
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = CaptureExecutor()

    async def fake_route(self, *args, **kwargs):
        return RouteDecision(skill="mlai-points", action="topup_points", params={})

    monkeypatch.setattr(agent_module.RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset(
                {"respond_in_chat", "mlai-points:topup_points"}
            )
        ),
    )

    result = await agent.handle_mention(
        text="Then top up 20 points please",
        user_id="USAM",
        channel_id="C1",
        thread_ts="1.0",
        implicit_addressing=True,
    )

    assert result["message"] == "checkout ready"
    assert captured["param_overrides"]["action"] == "topup_points"
    assert captured["user_id"] == "USAM"


@pytest.mark.asyncio
async def test_slack_events_dispatches_ordinary_pilot_channel_message(monkeypatch):
    handled = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    settings = SimpleNamespace(
        ROO_SURFACE="public",
        ROO_CONTEXTUAL_RESPONSES_ENABLED=True,
        contextual_channel_ids=frozenset({"C1"}),
        START_HERE_INTRO_ENABLED=False,
        START_HERE_INTRO_CHANNEL_NAME="_start-here",
        BOOST_LINK_LOVE_ENABLED=False,
        BOOST_LINK_LOVE_CHANNEL_NAME="boost-my-startup",
    )

    async def fake_contextual(event, **kwargs):
        handled.append((event, kwargs))

    def capture_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "_handle_contextual_slack_message", fake_contextual)
    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)
    monkeypatch.setattr("roo.slack_client.get_channel_id", lambda name: None)

    payload = {
        "team_id": "T1",
        "event_id": "EV5",
        "event": {
            "type": "message",
            "channel_type": "channel",
            "user": "USAM",
            "channel": "C1",
            "ts": "5.0",
            "text": "Then top up 20 points please",
        },
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())
    await asyncio.gather(*scheduled_tasks)

    assert response.status_code == 200
    assert len(handled) == 1
    assert handled[0][1]["trigger_source"] == "channel_message"


@pytest.mark.asyncio
async def test_shadow_mode_never_sends_an_implicit_candidate(monkeypatch):
    store = FakeContextualStore(SimpleNamespace(session_key="channel-user:USAM"))
    handled = []

    async def fake_history(event):
        return []

    async def fake_decision(**kwargs):
        return AddressDecision(True, 0.99, "continuation", "llm", kwargs["candidate_reason"])

    async def fake_handle(event, **kwargs):
        handled.append(event)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: contextual_settings(ROO_CONTEXTUAL_SHADOW_MODE=True),
    )
    monkeypatch.setattr(main_module, "get_contextual_conversation_store", lambda path: store)
    monkeypatch.setattr(main_module, "get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(main_module, "_get_addressing_history", fake_history)
    monkeypatch.setattr(main_module, "decide_addressing", fake_decision)
    monkeypatch.setattr(main_module, "_handle_mention", fake_handle)

    await main_module._handle_contextual_slack_message(
        {
            "type": "message",
            "user": "USAM",
            "channel": "C1",
            "ts": "4.0",
            "text": "20 please",
        },
        slack_team_id="T1",
        trigger_source="channel_message",
    )
    assert handled == []


@pytest.mark.asyncio
async def test_context_pipeline_failure_falls_back_only_for_direct_mentions(monkeypatch):
    handled = []

    async def failing_contextual(*args, **kwargs):
        raise RuntimeError("state database unavailable")

    async def fake_handle(event, **kwargs):
        handled.append((event, kwargs))
        return {"result": {}}

    monkeypatch.setattr(main_module, "_handle_contextual_slack_message", failing_contextual)
    monkeypatch.setattr(main_module, "_handle_mention", fake_handle)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: contextual_settings(ROO_CONTEXTUAL_SHADOW_MODE=False),
    )

    event = {"user": "USAM", "channel": "C1", "ts": "6.0", "text": "hello"}
    await main_module._handle_contextual_slack_message_safely(
        event,
        slack_team_id="T1",
        trigger_source="channel_message",
    )
    assert handled == []

    await main_module._handle_contextual_slack_message_safely(
        dict(event, type="app_mention", text="<@UROO> hello"),
        slack_team_id="T1",
        trigger_source="app_mention",
    )
    assert len(handled) == 1
