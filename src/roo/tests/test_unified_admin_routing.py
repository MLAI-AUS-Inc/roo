import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.agent import RooAgent
from roo.backend_identity import BackendActorContext, use_backend_actor_context
from roo.router import RouteDecision
from roo.skills.loader import Skill


def _skill(name):
    return Skill(name=name, description=name, content="", path=Path("."))


def _agent():
    agent = object.__new__(RooAgent)
    agent._surface = "public"
    agent.skills = [_skill("mlai-points")]
    agent._admin_routing_skill = _skill("admin-brain")
    agent._thread_skill_context = {}
    agent.skill_executor = SimpleNamespace()
    return agent


def _context(channel_id="GADMIN123"):
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id="UADMIN123",
        slack_channel_id=channel_id,
        slack_thread_ts="1700000000.123",
        event_id="Ev01ADMINROUTE",
    )


def _settings():
    return SimpleNamespace(
        ROO_UNIFIED_ADMIN_ROUTING_ENABLED=True,
        MLAI_BACKEND_URL="https://backend.test",
        ORG_BRAIN_ROUTER_API_KEY=f"mlai_sp_{'a' * 32}.{'r' * 48}",
        ROO_ADMIN_INTERNAL_URL="http://roo-admin:8000",
        ROO_ADMIN_DISPATCH_SECRET="dispatch-secret-" + ("s" * 32),
        ORG_BRAIN_BACKEND_TIMEOUT_SECONDS=20,
    )


def test_router_catalogue_contains_admin_metadata_but_public_executor_does_not():
    agent = _agent()

    assert [skill.name for skill in agent.skills] == ["mlai-points"]
    assert [
        skill.name for skill in agent._routing_skills_for_slack_context(channel_id="G1")
    ] == ["mlai-points", "admin-brain"]


def test_points_task_stays_on_public_execution_path(monkeypatch):
    agent = _agent()
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="You have 42 points.",
                data={},
                blocks=None,
                suppress_post=False,
            )

    async def route(*args, **kwargs):
        return RouteDecision(skill="mlai-points", action="balance", params={})

    async def no_fast_path(*args, **kwargs):
        return None

    agent.skill_executor = FakeExecutor()
    monkeypatch.setattr(agent, "_route_v2", route)
    monkeypatch.setattr(agent, "_try_fast_path", no_fast_path)
    monkeypatch.setattr(agent, "_match_fast_path", lambda text: None)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda **kwargs: [])

    result = asyncio.run(
        agent.handle_mention(
            text="How many points do I have?",
            user_id="UADMIN123",
            channel_id="GADMIN123",
            thread_ts="1700000000.123",
        )
    )

    assert result["skill_used"] == "mlai-points"
    assert captured["skill"].name == "mlai-points"


def test_internal_memory_task_selected_by_router_reaches_admin_relay(monkeypatch):
    agent = _agent()
    captured = {}

    async def route(*args, **kwargs):
        return RouteDecision(skill="admin-brain", action="answer", params={})

    async def no_fast_path(*args, **kwargs):
        return None

    async def relay(**kwargs):
        captured.update(kwargs)
        return {
            "message": "Grounded internal answer",
            "skill_used": "admin-brain",
            "data": {"routed_surface": "admin"},
        }

    monkeypatch.setattr(agent, "_route_v2", route)
    monkeypatch.setattr(agent, "_try_fast_path", no_fast_path)
    monkeypatch.setattr(agent, "_match_fast_path", lambda text: None)
    monkeypatch.setattr(agent, "_execute_unified_admin_brain", relay)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda **kwargs: [])

    result = asyncio.run(
        agent.handle_mention(
            text="What did the committee decide about the venue?",
            user_id="UADMIN123",
            channel_id="GADMIN123",
            thread_ts="1700000000.123",
        )
    )

    assert result["skill_used"] == "admin-brain"
    assert result["data"]["routed_surface"] == "admin"
    assert captured["text"] == "What did the committee decide about the venue?"


def test_admin_task_uses_content_free_eligibility_then_internal_dispatch(monkeypatch):
    agent = _agent()
    captured = {}

    class FakeEligibilityClient:
        def __init__(self, **kwargs):
            captured["eligibility_constructor"] = kwargs

        async def get_admin_routing_eligibility(self):
            return {"admin_brain_eligible": True, "private_context_allowed": True}

    class FakeDispatchClient:
        def __init__(self, **kwargs):
            captured["dispatch_constructor"] = kwargs

        async def dispatch(self, **kwargs):
            captured["dispatch"] = kwargs
            return {
                "result": {"message": "Grounded internal answer", "data": {"query_id": "q1"}},
                "destination": {
                    "channel_id": "GADMIN123",
                    "thread_ts": "1700000000.123",
                    "requester_user_id": "UADMIN123",
                },
            }

    monkeypatch.setattr("roo.agent.get_settings", _settings)
    monkeypatch.setattr("roo.agent.MLAIBackendClient", FakeEligibilityClient)
    monkeypatch.setattr("roo.agent.AdminDispatchClient", FakeDispatchClient)

    with use_backend_actor_context(_context()):
        result = asyncio.run(
            agent._execute_unified_admin_brain(
                text="What did the committee decide yesterday?",
                params={},
                user_id="UADMIN123",
                channel_id="GADMIN123",
                thread_ts="1700000000.123",
            )
        )

    assert result["message"] == "Grounded internal answer"
    assert result["data"]["routed_surface"] == "admin"
    assert captured["eligibility_constructor"]["surface"] == "gateway"
    assert captured["dispatch"]["kind"] == "query"
    assert captured["dispatch"]["context"] == _context()


def test_admin_route_checks_public_context_and_fails_closed_for_denied_actor(monkeypatch):
    agent = _agent()
    calls = []

    class DeniedEligibilityClient:
        def __init__(self, **kwargs):
            pass

        async def get_admin_routing_eligibility(self):
            calls.append("eligibility")
            return {"admin_brain_eligible": False}

    monkeypatch.setattr("roo.agent.get_settings", _settings)
    monkeypatch.setattr("roo.agent.MLAIBackendClient", DeniedEligibilityClient)

    with use_backend_actor_context(_context("CPUBLIC123")):
        public_result = asyncio.run(
            agent._execute_unified_admin_brain(
                text="Internal question",
                params={},
                user_id="UADMIN123",
                channel_id="CPUBLIC123",
                thread_ts="1700000000.123",
            )
        )
    assert public_result["data"]["reason"] == "committee_policy_denied"
    assert calls == ["eligibility"]

    with use_backend_actor_context(_context()):
        denied_result = asyncio.run(
            agent._execute_unified_admin_brain(
                text="Internal question",
                params={},
                user_id="UADMIN123",
                channel_id="GADMIN123",
                thread_ts="1700000000.123",
            )
        )
    assert denied_result["data"]["reason"] == "committee_policy_denied"
    assert calls == ["eligibility", "eligibility"]


def test_eligible_admin_task_dispatches_in_public_channel_with_warning(monkeypatch):
    agent = _agent()
    captured = {}

    class EligibleClient:
        def __init__(self, **kwargs):
            pass

        async def get_admin_routing_eligibility(self):
            return {"admin_brain_eligible": True}

    class FakeDispatchClient:
        def __init__(self, **kwargs):
            pass

        async def dispatch(self, **kwargs):
            captured["dispatch"] = kwargs
            return {
                "result": {
                    "message": "*🔒 Internal organisational memory*\nAnswer",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "Answer"},
                        }
                    ],
                    "data": {"query_id": "q-public"},
                },
                "destination": {
                    "channel_id": "CPUBLIC123",
                    "thread_ts": "1700000000.123",
                    "requester_user_id": "UADMIN123",
                },
            }

    monkeypatch.setattr("roo.agent.get_settings", _settings)
    monkeypatch.setattr("roo.agent.MLAIBackendClient", EligibleClient)
    monkeypatch.setattr("roo.agent.AdminDispatchClient", FakeDispatchClient)

    with use_backend_actor_context(_context("CPUBLIC123")):
        result = asyncio.run(
            agent._execute_unified_admin_brain(
                text="Summarise committee decisions",
                params={},
                user_id="UADMIN123",
                channel_id="CPUBLIC123",
                thread_ts="1700000000.123",
            )
        )

    assert result["data"]["routed_surface"] == "admin"
    assert result["data"]["public_channel_delivery"] is True
    assert "everyone in this channel can read it" in result["message"]
    assert "everyone in this channel can read it" in str(result["blocks"][0])
    assert captured["dispatch"]["context"] == _context("CPUBLIC123")
