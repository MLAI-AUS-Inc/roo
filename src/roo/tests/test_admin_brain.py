import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import skills as skills_package
from roo.admin_brain import (
    ADMIN_BRAIN_UNAVAILABLE_MESSAGE,
    build_admin_brain_response,
)
from roo.agent import RooAgent
from roo.backend_identity import BackendActorContext
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from roo.config import Settings
from roo.router import RouteDecision
from roo.routing_eval import runner as routing_eval_runner
from roo.skills.executor import SkillExecutor
from roo.skills.loader import Skill, load_skills


SERVICE_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
SKILLS_DIR = Path(skills_package.__file__).resolve().parents[2] / "skills"


def _actor():
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id="UADMIN123",
        slack_channel_id="GADMIN123",
        slack_thread_ts="1700000000.123",
        event_id="EvADMIN1",
    )


def _settings(**overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "OPENAI_API_KEY": "synthetic-openai-key",
        "MLAI_BACKEND_URL": "https://backend.test/api/v1",
        "ROO_SURFACE": "admin",
        "ROO_ALLOWED_CHANNEL_IDS": "GADMIN123",
        "ROO_ENABLED_SKILLS": "admin-brain",
        "ORG_BRAIN_ENABLED": True,
        "ORG_BRAIN_API_KEY": SERVICE_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_scoped_client_uses_answer_trace_feedback_contract_and_api_v1_base(monkeypatch):
    calls = []

    async def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        if endpoint.endswith("/answer"):
            return httpx.Response(200, request=request, json={"query_id": "query-1"})
        if endpoint.endswith("/trace"):
            return httpx.Response(200, request=request, json={"selected_claim_ids": ["claim-1"]})
        return httpx.Response(201, request=request, json={"feedback_id": "feedback-1"})

    client = MLAIBackendClient(
        base_url="https://backend.test/api/v1",
        service_principal_key=SERVICE_TOKEN,
        surface="admin",
        actor_context=_actor(),
    )
    monkeypatch.setattr(client, "_request", fake_request)

    answer = await client.answer_org_memory(
        "What is the latest on Pilot?",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
        max_context_tokens=6000,
    )
    trace = await client.get_org_memory_query_trace(answer["query_id"])
    feedback = await client.submit_org_memory_feedback(
        query_id=answer["query_id"],
        claim_id=trace["selected_claim_ids"][0],
        feedback_type="relevant",
    )

    assert answer == {"query_id": "query-1"}
    assert feedback == {"feedback_id": "feedback-1"}
    assert [call[1] for call in calls] == [
        "/org-memory/answer",
        "/org-memory/queries/query-1/trace",
        "/org-memory/feedback",
    ]
    assert calls[0][2]["use_org_memory_identity"] is True
    assert calls[0][2]["json"] == {
        "query": "What is the latest on Pilot?",
        "answer_mode": "auto",
        "max_context_tokens": 6000,
        "channel_id": "GADMIN123",
        "thread_ts": "1700000000.123",
    }


def test_root_backend_base_keeps_full_org_memory_prefix():
    client = MLAIBackendClient(
        base_url="https://backend.test",
        service_principal_key=SERVICE_TOKEN,
        surface="admin",
        actor_context=_actor(),
    )
    assert client._org_memory_endpoint("answer") == "/api/v1/org-memory/answer"


def test_admin_brain_blocks_escape_mentions_and_render_citations_and_feedback():
    payload = {
        "query_id": "query-1",
        "answer": "The launch is blocked. <!channel> <@USECRET>",
        "confidence": 0.81,
        "evidence_sufficiency": "sufficient",
        "freshness": {
            "latest_evidence_at": "2026-07-20T12:00:00+00:00",
            "contains_stale_memory": True,
        },
        "warnings": ["stale_memory", "unresolved_conflict"],
        "presentation": {"source_display": "links", "show_evidence_status": True},
        "citations": [
            {
                "provider": "google_drive",
                "label": "Meeting <notes>",
                "source_url": "https://drive.example/document/1",
                "occurred_at": "2026-07-20T12:00:00+00:00",
            }
        ],
    }

    result = build_admin_brain_response(
        payload,
        requester_user_id="UADMIN123",
        primary_claim_id="claim-1",
    )

    rendered = str(result["blocks"])
    assert "<!channel>" not in rendered
    assert "<@USECRET>" not in rendered
    assert "&lt;!channel&gt;" in rendered
    assert "https://drive.example/document/1" in rendered
    assert "Sources" in rendered
    action_ids = {
        element["action_id"]
        for block in result["blocks"]
        for element in block.get("elements", [])
        if isinstance(element, dict) and element.get("type") == "button"
    }
    assert action_ids == {
        "admin_brain_feedback_helpful",
        "admin_brain_feedback_incorrect",
        "admin_brain_feedback_stale",
        "admin_brain_feedback_missing",
    }


def test_admin_brain_does_not_present_request_time_as_latest_evidence():
    payload = {
        "query_id": "query-abstained",
        "answer": "I do not have enough authorised evidence to answer that reliably.",
        "confidence": 0,
        "evidence_sufficiency": "insufficient",
        "freshness": {
            "as_of": "2026-08-02T05:04:00+00:00",
            "latest_evidence_at": None,
            "contains_stale_memory": False,
        },
        "warnings": [],
        "presentation": {"source_display": "none", "show_evidence_status": True},
        "citations": [{}],
    }

    result = build_admin_brain_response(
        payload,
        requester_user_id="UADMIN123",
    )

    rendered = str(result["blocks"])
    assert "couldn't find enough reliable internal evidence" in rendered
    assert "Current authorised evidence" not in rendered
    assert "Latest evidence" not in rendered
    assert "02 Aug 2026" not in rendered


def test_admin_brain_surfaces_partial_evidence_with_its_timestamp():
    payload = {
        "query_id": "query-answered",
        "answer": "The committee approved the launch.",
        "confidence": 0.88,
        "evidence_sufficiency": "partial",
        "freshness": {
            "as_of": "2026-08-02T05:04:00+00:00",
            "latest_evidence_at": "2026-07-20T08:30:00+00:00",
            "contains_stale_memory": False,
        },
        "warnings": [],
        "presentation": {"source_display": "none", "show_evidence_status": True},
        "citations": [
            {
                "provider": "google_drive",
                "label": "Committee meeting notes",
                "source_url": "https://drive.example/document/committee",
                "occurred_at": "2026-07-20T08:30:00+00:00",
            }
        ],
    }

    result = build_admin_brain_response(
        payload,
        requester_user_id="UADMIN123",
    )

    rendered = str(result["blocks"])
    assert "available internal evidence is partial" in rendered
    assert "Latest evidence" in rendered
    assert "20 Jul 2026" in rendered
    assert "02 Aug 2026" not in rendered


def test_admin_brain_keeps_grounding_internal_for_normal_answers():
    payload = {
        "query_id": "query-conversational",
        "answer": (
            "We agreed to focus on fewer, higher-quality events. "
            "[claim:957f1ddb-099f-4937-b862-38fb5d37863b]"
        ),
        "confidence": 0.88,
        "evidence_sufficiency": "sufficient",
        "freshness": {
            "latest_evidence_at": "2026-07-20T08:30:00+00:00",
            "contains_stale_memory": False,
        },
        "warnings": [],
        "presentation": {"source_display": "none", "show_evidence_status": False},
        "citations": [
            {
                "provider": "google_drive",
                "label": "Committee meeting notes",
                "source_url": "https://drive.example/document/committee",
                "occurred_at": "2026-07-20T08:30:00+00:00",
            }
        ],
    }

    result = build_admin_brain_response(
        payload,
        requester_user_id="UADMIN123",
        primary_claim_id="claim-1",
    )

    rendered = str(result["blocks"])
    assert "We agreed to focus on fewer, higher-quality events." in rendered
    assert "claim:" not in rendered
    assert "https://drive.example" not in rendered
    assert "Sources" not in rendered
    assert "confidence" not in rendered
    assert "Current authorised evidence" not in rendered
    assert "From MLAI's internal memory" in rendered
    assert "claim:" not in result["message"]
    assert result["data"]["source_display"] == "none"


def test_admin_brain_title_mode_does_not_add_a_sources_block():
    payload = {
        "query_id": "query-titles",
        "answer": "That came from the 20 July committee notes.",
        "confidence": 0.88,
        "evidence_sufficiency": "sufficient",
        "freshness": {},
        "warnings": [],
        "presentation": {"source_display": "titles", "show_evidence_status": False},
        "citations": [
            {
                "provider": "google_drive",
                "label": "Committee meeting notes",
                "source_url": "https://drive.example/document/committee",
            }
        ],
    }

    result = build_admin_brain_response(payload, requester_user_id="UADMIN123")

    rendered = str(result["blocks"])
    assert "20 July committee notes" in rendered
    assert "https://drive.example" not in rendered
    assert "Sources" not in rendered


@pytest.mark.asyncio
async def test_executor_returns_grounded_blocks_and_never_calls_generic_fallback(monkeypatch):
    configured = _settings()

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def answer_org_memory(self, query, **kwargs):
            assert query == "What is the latest on Pilot?"
            return {
                "query_id": "query-1",
                "answer": "Pilot is green.",
                "confidence": 0.9,
                "evidence_sufficiency": "sufficient",
                "freshness": {},
                "warnings": [],
                "citations": [],
            }

        async def get_org_memory_query_trace(self, query_id, **kwargs):
            return {"selected_claim_ids": ["claim-1"]}

    executor_globals = SkillExecutor._execute_admin_brain.__globals__
    monkeypatch.setitem(executor_globals, "get_settings", lambda: configured)
    monkeypatch.setitem(executor_globals, "MLAIBackendClient", FakeClient)
    executor = SkillExecutor()

    async def forbidden_fallback(*args, **kwargs):
        raise AssertionError("Admin Brain must never fall back to the generic LLM executor")

    monkeypatch.setattr(executor, "_execute_with_llm", forbidden_fallback)
    result = await executor.execute(
        Skill(
            name="admin-brain",
            description="test",
            content="",
            path=Path("."),
        ),
        text="What is the latest on Pilot?",
        user_id="UADMIN123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
    )

    assert result.success
    assert result.message == "Pilot is green."
    assert result.blocks
    assert result.data["query_id"] == "query-1"


@pytest.mark.asyncio
async def test_executor_fails_closed_when_memory_backend_is_unavailable(monkeypatch):
    configured = _settings()

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def answer_org_memory(self, *args, **kwargs):
            raise MLAIBackendUnavailableError("timeout")

    executor_globals = SkillExecutor._execute_admin_brain.__globals__
    monkeypatch.setitem(executor_globals, "get_settings", lambda: configured)
    monkeypatch.setitem(executor_globals, "MLAIBackendClient", FailingClient)
    result = await SkillExecutor().execute(
        Skill("admin-brain", "test", "", Path(".")),
        text="What is the latest on Pilot?",
        user_id="UADMIN123",
        channel_id="GADMIN123",
    )

    assert result.success
    assert result.message == ADMIN_BRAIN_UNAVAILABLE_MESSAGE
    assert result.blocks is None


@pytest.mark.asyncio
async def test_admin_surface_never_falls_back_to_general_chat(monkeypatch):
    configured = _settings()
    agent = object.__new__(RooAgent)
    agent.skills = [Skill("admin-brain", "test", "", Path("."))]
    agent.skill_executor = SkillExecutor()
    agent._thread_skill_context = {}
    agent._surface = "admin"

    async def no_route(*args, **kwargs):
        return RouteDecision(skill=None, source="error", reason="router unavailable")

    async def forbidden_general_response(*args, **kwargs):
        raise AssertionError("Admin Roo must never invoke general chat")

    monkeypatch.setattr(agent, "_route_v2", no_route)
    monkeypatch.setattr(agent, "_general_response", forbidden_general_response)
    monkeypatch.setattr("roo.agent.get_settings", lambda: configured)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda **kwargs: [])

    result = await agent.handle_mention(
        text="What is the latest on Pilot?",
        user_id="UADMIN123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
    )

    assert result["message"] == ADMIN_BRAIN_UNAVAILABLE_MESSAGE
    assert result["data"]["reason"] == "no_authorised_memory_route"


def test_admin_skill_catalog_has_explicit_positive_and_public_skill_boundaries():
    routing_eval_runner._ensure_real_frontmatter()
    skill = load_skills(SKILLS_DIR, allowed_names={"admin-brain"})[0]
    instead = {
        row.get("instead") for row in skill.routing.get("negative_examples", [])
    }

    assert skill.name == "admin-brain"
    assert len(skill.routing["examples"]) >= 8
    assert {
        "mlai-points",
        "luma-events",
        "content-factory",
        "admin-actions",
        "respond_in_chat",
    }.issubset(instead)
