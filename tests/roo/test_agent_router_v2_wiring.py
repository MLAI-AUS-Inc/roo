"""ROUTER_V2=on wiring through RooAgent.handle_mention (hermetic)."""
import asyncio
import sys
import types
import pytest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "roo-standalone")))
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


def test_generic_booking_bypasses_model_and_books_coworking_today(monkeypatch):
    agent = _make_agent()
    calls = []

    async def fake_execute_fast_points(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {
            "message": "Booked coworking",
            "skill_used": "mlai-points (fast)",
            "data": {"action": action},
        }

    async def router_must_not_run(*args, **kwargs):
        raise AssertionError("generic bookings must not reach the model router")

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute_fast_points)
    monkeypatch.setattr(agent, "_route_v2", router_must_not_run)
    monkeypatch.setattr(agent, "_get_today", lambda: date(2026, 8, 25))
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> book me in for 1pm today",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert result["skill_used"] == "mlai-points (fast)"
    assert calls == [
        (
            "U123",
            "book_coworking",
            {
                "date": "2026-08-25",
                "channel_id": "C123",
                "thread_ts": "1.2",
            },
        )
    ]


def test_generic_booking_date_and_resource_boundaries(monkeypatch):
    agent = _make_agent()
    monkeypatch.setattr(agent, "_get_today", lambda: date(2026, 8, 25))

    assert agent._match_fast_path("book") == "book_coworking"
    assert agent._match_fast_path("book me in") == "book_coworking"
    assert agent._match_fast_path("book me in tomorrow") == "book_coworking"
    assert agent._match_fast_path("please book me in at 1:30 pm tomorrow") == "book_coworking"
    assert agent._match_fast_path("book me in on 2026-09-01") == "book_coworking"

    assert agent._match_fast_path("book the meeting room at 1pm today") is None
    assert agent._match_fast_path("book the big room") is None
    assert agent._match_fast_path("book a photographer") is None


def test_generic_booking_fast_path_preserves_tomorrow_and_iso_dates(monkeypatch):
    agent = _make_agent()
    calls = []

    async def fake_execute_fast_points(user_id, action, **kwargs):
        calls.append(kwargs["date"])
        return {"message": "ok", "data": {"action": action}}

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute_fast_points)
    monkeypatch.setattr(agent, "_get_today", lambda: date(2026, 8, 25))

    asyncio.run(agent._try_fast_path("book me in tomorrow", "U123"))
    asyncio.run(agent._try_fast_path("book me in on 2026-09-01", "U123"))

    assert calls == ["2026-08-26", "2026-09-01"]


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

    async def fake_general(self, text, history=None, *, fallback_reason="general_chat"):
        assert fallback_reason == "general_chat"
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


def test_v2_error_returns_unavailable_without_general_chat(monkeypatch):
    """Router failure must produce an unavailable outcome without general chat.

    (The legacy-funnel fallback died with the funnel in Phase 3; the fast path
    still serves exact commands during an outage.)
    """
    agent = _make_agent()

    async def failing_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        return RouteDecision(skill=None, source="error", reason="provider_transport_error")

    async def fake_general(self, text, history=None):
        pytest.fail("Router failure must not invoke general chat")

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
    assert result["data"]["outcome"] == "unavailable"
    assert "hasn't been completed" in result["message"]

@pytest.mark.parametrize(
    "failure_reason",
    [
        "provider_timeout",
        "provider_transport_error",
        "invalid_tool_calls",
    ],
)
def test_router_failures_never_execute_skills_or_general_chat(
    monkeypatch, failure_reason
):
    agent = _make_agent()

    async def failing_route(
        self, text, thread_history, channel_id, thread_ts,
        event_files=None,
    ):
        return RouteDecision(
            skill=None,
            source="error",
            reason=failure_reason,
        )

    async def unexpected_general(self, text, history=None):
        pytest.fail("General chat must not run after router failure")

    async def unexpected_execute(**kwargs):
        pytest.fail("Skill execution must not run after router failure")

    agent.skill_executor.execute = unexpected_execute

    monkeypatch.setattr(RooAgent, "_route_v2", failing_route)
    monkeypatch.setattr(RooAgent, "_general_response", unexpected_general)
    monkeypatch.setattr(
        "roo.agent.get_thread_messages",
        lambda channel, thread_ts: [],
    )
    monkeypatch.setattr(
        "roo.slack_client.get_bot_user_id",
        lambda: "U090FV0GTT4",
    )

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> whats my balance",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
            current_message_ts="1234567890.123456",
            slack_team_id="T123",
            event_id="Ev123",
        )
    )

    assert result["skill_used"] is None
    assert result["data"]["outcome"] == "unavailable"
    assert "hasn't been completed" in result["message"]
    assert result["data"]["reason_code"] == failure_reason
    assert failure_reason not in result["message"]

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


@pytest.mark.parametrize("invalid_action", ["fly", None], ids=["unknown", "missing"])
def test_two_invalid_router_actions_never_reach_skill_execution(
    monkeypatch, invalid_action
):
    from roo import router
    from roo.llm import ToolCall

    agent = _make_agent()
    agent.skills[0].actions = [{"name": "balance"}]
    provider_calls = []
    execution_calls = []

    async def invalid_tool_call(messages, tools, **kwargs):
        provider_calls.append(messages)
        return ToolCall(name="mlai-points", arguments={"action": invalid_action})

    async def capture_execute(**kwargs):
        execution_calls.append(kwargs)
        return SimpleNamespace(
            message="unexpected execution", data=None, blocks=None, suppress_post=False
        )

    async def unexpected_general(*args, **kwargs):
        pytest.fail("Invalid router actions must not invoke general chat")

    agent.skill_executor.execute = capture_execute
    monkeypatch.setattr(router, "chat_tools", invalid_tool_call)
    monkeypatch.setattr(agent, "_general_response", unexpected_general)
    monkeypatch.setattr(agent, "_safe_channel_name", lambda channel: "general")
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> whats my balance",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
        )
    )

    assert execution_calls == [], "Invalid router actions reached skill execution"
    assert len(provider_calls) == 2
    assert result["skill_used"] is None
    assert result["data"]["outcome"] == "unavailable"
    assert "hasn't been completed" in result["message"]


@pytest.mark.parametrize("surface", ["public", "admin"])
@pytest.mark.parametrize(
    ("implicit_addressing", "chat_allowed", "should_suppress"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
    ids=["implicit-blocked", "implicit-allowed", "explicit-chat-disabled", "explicit-chat-enabled"],
)
def test_router_unavailable_preserves_response_suppression(
    monkeypatch, surface, implicit_addressing, chat_allowed, should_suppress
):
    agent = _make_agent()
    agent._surface = surface

    async def failed_route(*args, **kwargs):
        return RouteDecision(skill=None, source="error", reason="provider timeout")

    async def unexpected_execution(*args, **kwargs):
        pytest.fail("Router failure must not execute skills or generate chat")

    agent.skill_executor.execute = unexpected_execution
    monkeypatch.setattr(agent, "_route_v2", failed_route)
    monkeypatch.setattr(agent, "_general_response", unexpected_execution)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")
    monkeypatch.setattr(
        "roo.agent.get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset(
                {"respond_in_chat"} if chat_allowed else {"mlai-points:balance"}
            )
        ),
    )

    result = asyncio.run(
        agent.handle_mention(
            text="whats my balance",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.2",
            implicit_addressing=implicit_addressing,
        )
    )

    assert bool(result.get("suppress_post")) is should_suppress
    if should_suppress:
        assert result["message"] is None
        assert result["data"]["implicit_action_blocked"] is True
    else:
        assert result["skill_used"] is None
        assert result["data"]["outcome"] == "unavailable"
        assert "hasn't been completed" in result["message"]



PHASE2_SECRET = "synthetic-provider-secret request-body-sentinel"


def _phase2_agent(monkeypatch, surface="public"):
    agent = _make_agent()
    agent._surface = surface
    agent.skills[0].actions = [{"name": "balance"}]
    monkeypatch.setattr(agent, "_safe_channel_name", lambda channel: "general")
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")
    return agent


@pytest.mark.parametrize("surface", ["public", "admin"])
@pytest.mark.parametrize(
    ("kind", "expected_code", "expected_calls"),
    [("timeout", "provider_timeout", 1), ("connection", "provider_transport_error", 1),
     ("malformed", "invalid_tool_calls", 2), ("unknown", "unexpected_failure", 1)],
)
def test_phase2_real_routing_failure_has_safe_agent_disposition(
    monkeypatch, capsys, surface, kind, expected_code, expected_calls
):
    from roo import router
    from roo.llm import ToolCallParseError
    from roo.backend_identity import (
        BackendActorContext, get_backend_actor_context, use_backend_actor_context,
    )

    agent = _phase2_agent(monkeypatch, surface)
    calls = []

    async def provider(*args, **kwargs):
        calls.append(True)
        errors = {"timeout": TimeoutError, "connection": ConnectionResetError,
                  "malformed": ToolCallParseError, "unknown": RuntimeError}
        raise errors[kind](PHASE2_SECRET)

    async def unexpected_execution(*args, **kwargs):
        pytest.fail("Routing failure must not dispatch skills, mutate backends or generate chat")

    agent.skill_executor.execute = unexpected_execution
    monkeypatch.setattr(agent, "_general_response", unexpected_execution)
    monkeypatch.setattr(router, "chat_tools", provider)
    context = BackendActorContext("T123", "U123", "C123", "1.2", "Ev123")
    with use_backend_actor_context(context):
        result = asyncio.run(agent.handle_mention(
            text="whats my balance", user_id="U123", channel_id="C123", thread_ts="1.2",
            slack_team_id="T123", current_message_ts="3.4", event_id="Ev123",
        ))
        assert get_backend_actor_context() == context

    assert result["data"]["reason_code"] == expected_code
    assert result["data"]["outcome"] == "unavailable"
    assert result["skill_used"] is None
    assert "hasn't been completed" in result["message"]
    assert len(calls) == expected_calls
    assert agent._thread_skill_context == {}
    assert PHASE2_SECRET not in repr(result) + capsys.readouterr().out


@pytest.mark.parametrize("kind", ["exception", "none", "invalid_source", "untrusted_reason"])
def test_phase2_agent_routing_boundary_fails_closed(monkeypatch, capsys, kind):
    agent = _phase2_agent(monkeypatch)

    async def bad_route(*args, **kwargs):
        if kind == "exception":
            raise RuntimeError(PHASE2_SECRET)
        if kind == "none":
            return None
        if kind == "invalid_source":
            return RouteDecision(skill="mlai-points", source=PHASE2_SECRET)
        return RouteDecision(skill="mlai-points", source="error", reason=PHASE2_SECRET)

    async def no_execution(*args, **kwargs):
        pytest.fail("Untrusted routing failure must not dispatch or generate chat")

    monkeypatch.setattr(agent, "_route_v2", bad_route)
    monkeypatch.setattr(agent, "_general_response", no_execution)
    agent.skill_executor.execute = no_execution
    result = asyncio.run(agent.handle_mention(text="hello", user_id="U123"))
    assert result["data"]["outcome"] == "unavailable"
    assert result["data"]["reason_code"] == "unexpected_failure"
    assert PHASE2_SECRET not in repr(result) + capsys.readouterr().out


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("unsupported_capability", "unsupported_capability"),
     (PHASE2_SECRET, "general_chat"), (["unsupported_capability"], "general_chat")],
)
def test_phase2_bounded_fallback_context_reaches_renderer(monkeypatch, capsys, reason, expected):
    agent = _phase2_agent(monkeypatch)
    captured = {}

    async def route(*args, **kwargs):
        return RouteDecision(skill=None, reason=reason)

    async def render(text, history=None, **kwargs):
        captured.update(kwargs)
        return "existing conversational reply"

    monkeypatch.setattr(agent, "_route_v2", route)
    monkeypatch.setattr(agent, "_general_response", render)
    result = asyncio.run(agent.handle_mention(text="hello", user_id="U123"))
    assert captured["fallback_reason"] == expected
    assert result["message"] == "existing conversational reply"
    if expected == "unsupported_capability":
        assert result["data"] == {"router": "v2", "fallback_reason": "unsupported_capability"}
        assert result["data"].get("outcome") != "unavailable"
    else:
        assert result["data"] is None
    assert PHASE2_SECRET not in repr(result) + repr(captured) + capsys.readouterr().out


def test_phase2_unsupported_hint_cannot_bypass_admin_boundary(monkeypatch):
    from roo.admin_brain import ADMIN_BRAIN_UNAVAILABLE_MESSAGE
    agent = _phase2_agent(monkeypatch, "admin")

    async def route(*args, **kwargs):
        return RouteDecision(skill=None, reason="unsupported_capability")

    async def no_chat(*args, **kwargs):
        pytest.fail("An unsupported hint must not enable Admin generic chat")

    monkeypatch.setattr(agent, "_route_v2", route)
    monkeypatch.setattr(agent, "_general_response", no_chat)
    result = asyncio.run(agent.handle_mention(text="hello", user_id="U123"))
    assert result["message"] == ADMIN_BRAIN_UNAVAILABLE_MESSAGE
    assert result["data"]["reason"] == "no_authorised_memory_route"


def test_phase2_unsupported_hint_respects_implicit_suppression(monkeypatch):
    agent = _phase2_agent(monkeypatch)

    async def route(*args, **kwargs):
        return RouteDecision(skill=None, reason="unsupported_capability")

    async def no_chat(*args, **kwargs):
        pytest.fail("Disallowed implicit fallback must not render")

    monkeypatch.setattr(agent, "_route_v2", route)
    monkeypatch.setattr(agent, "_general_response", no_chat)
    monkeypatch.setattr("roo.agent.get_settings", lambda: SimpleNamespace(
        implicit_action_allowlist=frozenset({"mlai-points:balance"}),
    ))
    result = asyncio.run(agent.handle_mention(
        text="hello", user_id="U123", implicit_addressing=True,
    ))
    assert result["message"] is None and result["suppress_post"] is True


@pytest.mark.parametrize("reason", ["unsupported_capability", PHASE2_SECRET])
def test_phase2_renderer_uses_fixed_context_not_model_reason(monkeypatch, reason):
    agent = _phase2_agent(monkeypatch)
    captured = []

    async def chat(messages, **kwargs):
        captured.extend(messages)
        return SimpleNamespace(content="existing reply")

    monkeypatch.setattr("roo.agent.chat", chat)
    result = asyncio.run(agent._general_response("hello", fallback_reason=reason))
    assert result == "existing reply"
    assert PHASE2_SECRET not in repr(captured)
    if reason == "unsupported_capability":
        assert "do not describe this as a temporary outage" in captured[0]["content"]


def test_phase2_chat_provider_failure_does_not_log_exception_text(monkeypatch, capsys):
    agent = _phase2_agent(monkeypatch)

    async def chat(*args, **kwargs):
        raise TimeoutError(PHASE2_SECRET)

    monkeypatch.setattr("roo.agent.chat", chat)
    result = asyncio.run(agent._general_response("hello"))
    assert result == "G'day! Sorry, I'm having a bit of trouble at the moment. Mind trying again? 🦘"
    output = capsys.readouterr().out
    assert PHASE2_SECRET not in result + output
    assert "provider_timeout" in output



@pytest.mark.parametrize(
    ("kind", "expected_code", "expected_calls"),
    [("timeout", "provider_timeout", 1), ("connection", "provider_transport_error", 1),
     ("invalid_json", "invalid_tool_calls", 2), ("invalid_action", "invalid_tool_calls", 2),
     ("parse_then_timeout", "provider_timeout", 2)],
)
def test_phase2_sdk_adapter_failure_reaches_agent(
    monkeypatch, capsys, kind, expected_code, expected_calls
):
    import httpx
    import json
    import openai
    from roo import llm

    agent = _phase2_agent(monkeypatch)
    calls = []
    request = httpx.Request("POST", "https://provider.invalid", headers={"Authorization": PHASE2_SECRET})

    async def create(**kwargs):
        calls.append(kwargs)
        if kind == "timeout" or (kind == "parse_then_timeout" and len(calls) == 2):
            raise openai.APITimeoutError(request=request)
        if kind == "connection":
            raise openai.APIConnectionError(message=PHASE2_SECRET, request=request)
        arguments = json.dumps({"action": PHASE2_SECRET}) if kind == "invalid_action" else "{"
        return SimpleNamespace(
            output=[SimpleNamespace(type="function_call", name="mlai-points", arguments=arguments)],
            model="gpt-6.2-sol",
        )

    async def no_execution(*args, **kwargs):
        pytest.fail("SDK/parser failures must stop before skill execution and general chat")

    client = llm.OpenAIClient.__new__(llm.OpenAIClient)
    client.model = "gpt-6.2-sol"
    client.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "_default_client", client)
    monkeypatch.setattr("roo.config.get_settings", lambda: SimpleNamespace(ROUTER_MODEL="gpt-6.2-sol"))
    monkeypatch.setattr(agent, "_general_response", no_execution)
    agent.skill_executor.execute = no_execution
    result = asyncio.run(agent.handle_mention(text="whats my balance", user_id="U123"))
    assert result["data"]["reason_code"] == expected_code
    assert result["data"]["outcome"] == "unavailable"
    assert len(calls) == expected_calls
    assert PHASE2_SECRET not in repr(result) + capsys.readouterr().out
