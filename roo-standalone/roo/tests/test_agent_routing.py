"""Agent plumbing tests: delegation, normalization, and param flow.

Routing itself is the v2 tool-calling router (roo/router.py) — covered by
test_router_v2.py (unit), test_router_catalog.py (SKILL.md lint), the
hermetic eval gate (test_routing_eval_gate.py), and the live eval
(`scripts/run_routing_eval.py --mode v2`). The phrase-by-phrase funnel tests
that used to live here were ported to roo/routing_eval/cases/ when the
regex/keyword funnel was deleted (Phase 3 of the routing redesign).
"""
import asyncio
import json
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
        Skill(name="content-factory", description="content", content="", path=Path(".")),
        Skill(name="mlai-points", description="points", content="", path=Path(".")),
    ]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    return agent


def _add_linear_skill(agent: RooAgent) -> None:
    agent.skills.append(
        Skill(name="linear-meeting-actions", description="linear", content="", path=Path("."))
    )


def _add_mlai_data_query_skill(agent: RooAgent) -> None:
    agent.skills.append(
        Skill(name="mlai-data-query", description="data", content="", path=Path("."))
    )


class _CaptureExecutor:
    def __init__(self, captured: dict):
        self._captured = captured

    async def execute(self, **kwargs):
        self._captured.update(kwargs)
        return SimpleNamespace(message="ok", data=kwargs.get("param_overrides"), blocks=None, suppress_post=False)


def _patch_route(monkeypatch, decision: RouteDecision, captured: dict):
    async def fake_route(self, text, thread_history, channel_id, thread_ts, event_files=None):
        captured["routed_text"] = text
        return decision

    monkeypatch.setattr(RooAgent, "_route_v2", fake_route)


def test_founder_account_link_fast_path_is_exact_and_avoids_collisions():
    agent = _make_agent()

    assert agent._match_fast_path("link") == "link_founder_account"
    assert agent._match_fast_path(" LINK ") == "link_founder_account"
    assert agent._match_fast_path("link my github account") is None
    assert agent._match_fast_path("connect me with a founder") is None


def test_founder_account_link_fast_path_executes_with_event_context(monkeypatch):
    agent = _make_agent()
    captured = {}

    async def fake_execute(user_id, action, **kwargs):
        captured.update({"user_id": user_id, "action": action, **kwargs})
        return {
            "message": "sent privately",
            "data": {"action": action},
        }

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute)

    result = asyncio.run(
        agent._try_fast_path(
            "link",
            "U123",
            channel_id="C123",
            thread_ts="111.222",
        )
    )

    assert result["message"] == "sent privately"
    assert captured == {
        "user_id": "U123",
        "action": "link_founder_account",
        "text": "link",
        "channel_id": "C123",
        "thread_ts": "111.222",
    }


def test_account_link_routing_logs_redact_ingress_identity_and_token_sentinels(
    monkeypatch,
    capsys,
):
    agent = _make_agent()
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)
    token = "AUniqueAccountLinkToken_12345678901234567890"
    email = "private-link@example.com"
    slack_user_id = "UACCOUNT123"
    channel_id = "CSECRET123"
    thread_ts = "1758000000.123456"
    text = (
        "link https://mlai.au/founder-tools/link-roo?token="
        f"{token} for {email} <@{slack_user_id}>"
    )

    _patch_route(
        monkeypatch,
        RouteDecision(
            skill="mlai-points",
            action="link_founder_account",
            params={
                "token": token,
                "email": email,
                "slack_user_id": slack_user_id,
            },
        ),
        captured,
    )
    def fail_thread_history(**kwargs):
        del kwargs
        raise RuntimeError(
            f"thread failed {token} {email} {slack_user_id} {channel_id} {thread_ts}"
        )

    monkeypatch.setattr("roo.agent.get_thread_messages", fail_thread_history)

    result = asyncio.run(
        agent.handle_mention(
            text=text,
            user_id=slack_user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
    )

    assert result["skill_used"] == "mlai-points"
    output = capsys.readouterr().out
    for sentinel in (token, email, slack_user_id, channel_id, thread_ts):
        assert sentinel not in output
    routing_line = next(
        line.removeprefix("ROUTING_DECISION ")
        for line in output.splitlines()
        if line.startswith("ROUTING_DECISION ")
    )
    routing_payload = json.loads(routing_line)
    assert routing_payload["text"] == "[account-link request]"
    assert routing_payload["params"] == {}
    assert routing_payload["destination_type"] == "channel"
    assert routing_payload["in_thread"] is True
    assert "channel_id" not in routing_payload
    assert "thread_ts" not in routing_payload


def test_bare_link_in_roo_dm_uses_secure_implicit_action(monkeypatch):
    agent = _make_agent()
    captured = {}

    async def fake_execute(user_id, action, **kwargs):
        captured.update({"user_id": user_id, "action": action, **kwargs})
        return {
            "message": "Founder Tools link sent",
            "data": {"action": action},
        }

    async def router_must_not_run(*args, **kwargs):
        raise AssertionError("The exact DM link command must not reach the model router")

    monkeypatch.setattr(agent, "_execute_fast_points", fake_execute)
    monkeypatch.setattr(agent, "_route_v2", router_must_not_run)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda **kwargs: [])
    monkeypatch.setattr(
        "roo.agent.get_settings",
        lambda: SimpleNamespace(
            implicit_action_allowlist=frozenset(
                {"mlai-points:link_founder_account"}
            )
        ),
    )

    result = asyncio.run(
        agent.handle_mention(
            text="link",
            user_id="U123",
            channel_id="D123",
            thread_ts="111.222",
            implicit_addressing=True,
        )
    )

    assert result["message"] == "Founder Tools link sent"
    assert captured == {
        "user_id": "U123",
        "action": "link_founder_account",
        "text": "link",
        "channel_id": "D123",
        "thread_ts": "111.222",
    }


def test_handle_mention_normalizes_slack_link_and_passes_scan_params(monkeypatch):
    agent = _make_agent()
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="scan", params={"domain": "woofya.com.au"}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> scan the repo for the domain <http://woofya.com.au|woofya.com.au>",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    # Slack link markup must be normalized before the router sees the text
    assert captured["routed_text"] == "scan the repo for the domain woofya.com.au"
    assert captured["text"] == "scan the repo for the domain woofya.com.au"
    assert captured["param_overrides"] == {
        "action": "scan",
        "domain": "woofya.com.au",
    }


def test_handle_mention_parses_delegated_scan_and_passes_identity_overrides(monkeypatch):
    agent = _make_agent()
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="scan", params={"domain": "woofya.com.au"}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text=(
                "<@U090FV0GTT4> scan the repo for the domain woofya.com.au "
                "as <@U0AQV5X9G0J|Target Founder>"
            ),
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    # the delegation clause is stripped before routing
    assert captured["routed_text"] == "scan the repo for the domain woofya.com.au"
    assert captured["param_overrides"] == {
        "action": "scan",
        "domain": "woofya.com.au",
        "requested_by_slack_user_id": "U05QPB483K9",
        "effective_slack_user_id": "U0AQV5X9G0J",
    }


def test_handle_mention_preserves_labeled_user_id_for_connect_users(monkeypatch):
    agent = _make_agent()
    agent.skills.append(
        Skill(name="connect-users", description="connect", content="", path=Path("."))
    )
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(
            skill="connect-users",
            action="search",
            params={"query": "AI research"},
        ),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text=(
                "<@U090FV0GTT4> connect me with someone like "
                "<@UTARGET|Other Member> in AI research"
            ),
            user_id="UOWNER",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    expected_text = "connect me with someone like <@UTARGET> in AI research"
    assert result["skill_used"] == "connect-users"
    assert captured["routed_text"] == expected_text
    assert captured["text"] == expected_text


def test_handle_mention_rejects_unauthorized_delegation(monkeypatch):
    agent = _make_agent()

    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> scan mlai.au as <@U0AQV5X9G0J>",
            user_id="U123OTHER",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result == {
        "message": "Only <@U05QPB483K9> can run Content Factory as another user.",
        "skill_used": "content-factory",
        "data": None,
    }


def test_linear_skill_receives_bounded_slack_context_after_routing(monkeypatch):
    agent = _make_agent()
    _add_linear_skill(agent)
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)
    _patch_route(
        monkeypatch,
        RouteDecision(skill="linear-meeting-actions", action="create", params={}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])

    context = {
        "messages": [
            {"user": "UJESS", "text": "Sam can you send the run sheet?", "ts": "1.1"},
            {"user": "USAM", "text": "add this as a task for me in Linear", "ts": "1.2"},
        ],
        "request": {"user_id": "USAM", "message_ts": "1.2"},
        "selection": {"mode": "recent_channel"},
    }
    monkeypatch.setattr(
        "roo.linear_context.build_linear_slack_context",
        lambda **kwargs: context,
    )

    result = asyncio.run(
        agent.handle_mention(
            text="add this as a task for me in Linear",
            user_id="USAM",
            channel_id="C1",
            thread_ts="1.2",
            current_message_ts="1.2",
            slack_team_id="T1",
            event_id="Ev1",
        )
    )

    assert result["skill_used"] == "linear-meeting-actions"
    assert captured["thread_history"] == context["messages"]
    assert captured["slack_context"] == context


def test_handle_mention_scopes_full_history_to_mlai_data_query(monkeypatch):
    agent = _make_agent()
    _add_mlai_data_query_skill(agent)
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)
    _patch_route(
        monkeypatch,
        RouteDecision(skill="mlai-data-query", action="query", params={}),
        captured,
    )
    history = [
        {"user": "UROO" if index == 0 else "U123", "text": f"message {index}", "ts": str(index)}
        for index in range(15)
    ]
    monkeypatch.setattr(
        "roo.agent.get_thread_messages",
        lambda channel, thread_ts: history,
    )

    asyncio.run(
        agent.handle_mention(
            text="show me number 2",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.1",
        )
    )

    assert captured["thread_history"] == history[-10:]
    assert captured["linear_thread_history"] == history


def test_handle_mention_keeps_non_linear_execution_history_recent(monkeypatch):
    agent = _make_agent()
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)
    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="scan", params={"domain": "example.com"}),
        captured,
    )
    history = [
        {"user": "U123", "text": f"message {index}", "ts": str(index)}
        for index in range(15)
    ]
    monkeypatch.setattr(
        "roo.agent.get_thread_messages",
        lambda channel, thread_ts: history,
    )

    asyncio.run(
        agent.handle_mention(
            text="scan example.com",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.1",
        )
    )

    assert captured["thread_history"] == history[-10:]
    assert "linear_thread_history" not in captured


def test_handle_mention_ignores_incomplete_thread_prefix(monkeypatch):
    agent = _make_agent()
    _add_mlai_data_query_skill(agent)
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)
    _patch_route(
        monkeypatch,
        RouteDecision(skill="mlai-data-query", action="query", params={}),
        captured,
    )

    class IncompleteHistory(list):
        complete = False

    monkeypatch.setattr(
        "roo.agent.get_thread_messages",
        lambda channel, thread_ts: IncompleteHistory(
            [{"user": "UROO", "text": "stale list", "ts": "1"}]
        ),
    )

    asyncio.run(
        agent.handle_mention(
            text="show me number 2",
            user_id="U123",
            channel_id="C123",
            thread_ts="1.1",
        )
    )

    assert captured["thread_history"] == []
    assert captured["linear_thread_history"] == []


def test_handle_mention_keeps_delegated_target_sticky_within_thread(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "123.456",
        domain="studynash.co",
        workflow="scan",
        requested_by_slack_user_id="U05QPB483K9",
        effective_slack_user_id="U0AQV5X9G0J",
    )
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="publish_pr", params={}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> publish this article as a PR",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    # thread post-fill supplies the domain; identity stickiness supplies the
    # delegated user pair
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "studynash.co",
        "requested_by_slack_user_id": "U05QPB483K9",
        "effective_slack_user_id": "U0AQV5X9G0J",
    }


def test_handle_mention_passes_publish_pr_job_from_thread_context(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "123.456",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-content-123",
    )
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="publish_pr", params={}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> publish this article as a PR",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "birdpsychology.com.au",
        "job_id": "job-content-123",
    }


def test_remember_selected_skill_stores_router_action_as_workflow(monkeypatch):
    agent = _make_agent()
    captured = {}
    agent.skill_executor = _CaptureExecutor(captured)

    _patch_route(
        monkeypatch,
        RouteDecision(skill="content-factory", action="research", params={"domain": "mlai.au"}),
        captured,
    )
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> what should I write next for mlai.au?",
            user_id="U123",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    context = agent.get_thread_context("C123", "123.456")
    assert context["skill_name"] == "content-factory"
    assert context["workflow"] == "research"  # the router's action, not a regex guess
    assert context["domain"] == "mlai.au"
