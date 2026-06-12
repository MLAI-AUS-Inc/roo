"""ROUTER_V2=on wiring through RooAgent.handle_mention (hermetic)."""
import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
fake_executor_module = types.ModuleType("roo.skills.executor")
fake_executor_module.SkillExecutor = type("SkillExecutor", (), {})
fake_executor_module.SkillResult = type("SkillResult", (), {})
sys.modules.setdefault("roo.skills.executor", fake_executor_module)

from roo.agent import RooAgent
from roo.router import RouteDecision
from roo.skills.loader import Skill


def _make_agent() -> RooAgent:
    agent = object.__new__(RooAgent)
    agent.skills = [
        Skill(name="mlai-points", description="points", content="", path=Path(".")),
        Skill(name="content-factory", description="content", content="", path=Path(".")),
    ]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    return agent


def _on_settings():
    return SimpleNamespace(ROUTER_V2="on", ROUTER_MODEL="test-model")


def test_v2_on_routes_via_router_and_passes_action_params(monkeypatch):
    agent = _make_agent()
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(message="ok", data=None, blocks=None, suppress_post=False)

    agent.skill_executor = FakeExecutor()

    async def fake_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        captured["routed_text"] = text
        return RouteDecision(
            skill="mlai-points", action="book_coworking", params={"date": "2026-06-13"}
        )

    monkeypatch.setattr(RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr("roo.agent.get_settings", _on_settings)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> book me a coworking spot for friday",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["skill_used"] == "mlai-points"
    assert captured["routed_text"] == "book me a coworking spot for friday"
    assert captured["param_overrides"] == {"action": "book_coworking", "date": "2026-06-13"}


def test_v2_on_clarification_returns_question_directly(monkeypatch):
    agent = _make_agent()

    async def fake_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        return RouteDecision(skill=None, clarification="Linear ticket or points task?")

    monkeypatch.setattr(RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr("roo.agent.get_settings", _on_settings)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> make a task for the docs",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["message"] == "Linear ticket or points task?"
    assert result["skill_used"] is None
    assert result["data"]["clarification"] is True


def test_v2_on_chat_decision_falls_to_general_response(monkeypatch):
    agent = _make_agent()

    async def fake_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        return RouteDecision(skill=None, reason="chitchat")

    async def fake_general(self, text, history=None):
        return "g'day!"

    monkeypatch.setattr(RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr(RooAgent, "_general_response", fake_general)
    monkeypatch.setattr("roo.agent.get_settings", _on_settings)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> thanks legend",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["skill_used"] is None
    assert result["message"] == "g'day!"


def test_v2_error_degrades_to_general_response(monkeypatch):
    """Provider outage: routing yields no skill; Roo answers conversationally.

    (The legacy-funnel fallback died with the funnel in Phase 3; the fast path
    still serves exact commands during an outage.)
    """
    agent = _make_agent()

    async def failing_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        return RouteDecision(skill=None, source="error", reason="connection reset")

    async def fake_general(self, text, history=None):
        return "sorry mate, having a moment — try again in a tic"

    monkeypatch.setattr(RooAgent, "_route_v2", failing_route)
    monkeypatch.setattr(RooAgent, "_general_response", fake_general)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> whats my balance",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["skill_used"] is None
    assert "sorry mate" in result["message"]


def test_v2_on_content_factory_thread_post_fill(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "1.2",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-42",
    )
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(message="ok", data=None, blocks=None, suppress_post=False)

    agent.skill_executor = FakeExecutor()

    async def fake_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        return RouteDecision(skill="content-factory", action="publish_pr", params={})

    monkeypatch.setattr(RooAgent, "_route_v2", fake_route)
    monkeypatch.setattr("roo.agent.get_settings", _on_settings)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> publish it as a PR",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "birdpsychology.com.au",
        "job_id": "job-42",
    }
