import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from roo.agent import RooAgent
from roo.clients.mlai_backend import MLAIBackendClient
from roo.config import Settings
from roo.skills import executor as executor_module
from roo.skills.executor import (
    SkillExecutor,
    VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE,
)
from roo.skills.loader import Skill


TEAM_ID = "TMLAI123"
CHANNEL_ID = "GVICTOR123"
USER_ID = "UANYMEMBER123"
THREAD_TS = "1700000000.123"
EVENT_ID = "EvVICTOR01"
SECRET = "victor-roo-test-secret-that-is-at-least-thirty-two-characters"


def _skill(name: str = "victor-ai-applications") -> Skill:
    return Skill(name=name, description=name, content="", path=Path("."))


def _settings():
    return SimpleNamespace(
        MLAI_BACKEND_URL="https://backend.test",
        VICTOR_AI_ROO_SIGNING_SECRET=SECRET,
        VICTOR_AI_BACKEND_TIMEOUT_SECONDS=20,
        VICTOR_AI_SLACK_CHANNEL_NAME="exp-victor-ai",
        victor_ai_slack_channel_name="exp-victor-ai",
        is_victor_ai_context_allowed=lambda *, channel_name: (
            channel_name == "exp-victor-ai"
        ),
    )


@pytest.fixture(autouse=True)
def channel_names(monkeypatch):
    monkeypatch.setattr(
        executor_module,
        "get_channel_name",
        lambda channel_id: "exp-victor-ai" if channel_id == CHANNEL_ID else "general",
    )
    monkeypatch.setattr(
        executor_module,
        "get_channel_id",
        lambda channel_name: CHANNEL_ID if channel_name == "exp-victor-ai" else None,
    )


async def _execute(
    executor: SkillExecutor,
    *,
    text: str,
    params: dict,
    channel_id=CHANNEL_ID,
):
    return await executor.execute(
        skill=_skill(),
        text=text,
        user_id=USER_ID,
        channel_id=channel_id,
        thread_ts=THREAD_TS,
        param_overrides=params,
        slack_team_id=TEAM_ID,
        event_id=EVENT_ID,
        current_message_ts=THREAD_TS,
    )


def test_skill_settings_are_disabled_by_default_and_require_shared_secret():
    base = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_SIGNING_SECRET": "test-secret",
        "OPENAI_API_KEY": "test-key",
    }
    assert Settings(**base).VICTOR_AI_SKILL_ENABLED is False

    with pytest.raises(ValidationError, match="VICTOR_AI_ROO_SIGNING_SECRET"):
        Settings(
            **base,
            VICTOR_AI_SKILL_ENABLED=True,
            MLAI_BACKEND_URL="https://backend.test",
        )

    configured = Settings(
        **base,
        VICTOR_AI_SKILL_ENABLED=True,
        MLAI_BACKEND_URL="https://backend.test",
        VICTOR_AI_ROO_SIGNING_SECRET=SECRET,
        VICTOR_AI_SLACK_CHANNEL_NAME="#exp-victor-ai",
    )
    assert configured.is_victor_ai_context_allowed(channel_name="EXP-VICTOR-AI")
    assert not configured.is_victor_ai_context_allowed(channel_name="general")


def test_client_builds_backend_compatible_signature_without_generic_api_key():
    client = MLAIBackendClient(
        base_url="https://backend.test/api/v1",
        api_key="must-not-be-used",
        victor_ai_signing_secret=SECRET,
        victor_ai_actor_context={
            "slack_team_id": TEAM_ID,
            "acting_slack_user_id": USER_ID,
            "slack_channel_id": CHANNEL_ID,
            "slack_thread_ts": THREAD_TS,
            "event_id": EVENT_ID,
        },
    )

    headers = client.victor_ai_headers("roo-request-1")
    assert "X-API-Key" not in headers
    payload = {
        "acting_slack_user_id": USER_ID,
        "event_id": EVENT_ID,
        "nonce": headers["X-Victor-Roo-Nonce"],
        "request_id": "roo-request-1",
        "slack_channel_id": CHANNEL_ID,
        "slack_team_id": TEAM_ID,
        "slack_thread_ts": THREAD_TS,
        "surface": "public_roo",
        "timestamp": int(headers["X-Victor-Roo-Timestamp"]),
        "v": 1,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    expected = "v1=" + hmac.new(
        SECRET.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(headers["X-Victor-Roo-Signature"], expected)


@pytest.mark.asyncio
async def test_wrong_channel_fails_before_backend_client_is_constructed(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("wrong-channel request must not construct a backend client")

    monkeypatch.setattr(executor_module, "MLAIBackendClient", ForbiddenClient)
    result = await _execute(
        SkillExecutor(),
        text="show Victor applications",
        params={"action": "list"},
        channel_id="GOTHER123",
    )

    assert result.message == VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE
    assert result.data["allowed"] is False


@pytest.mark.asyncio
async def test_help_explains_every_supported_action_without_backend_call(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class ForbiddenClient:
        def __init__(self, **kwargs):
            raise AssertionError("help must not call the backend")

    monkeypatch.setattr(executor_module, "MLAIBackendClient", ForbiddenClient)
    result = await _execute(
        SkillExecutor(),
        text="what can I ask about Victor applications?",
        params={"action": "help"},
    )

    assert "Summary:" in result.message
    assert "List:" in result.message
    assert "Full record:" in result.message
    assert "CSV:" in result.message


@pytest.mark.asyncio
async def test_summary_is_high_level_and_escapes_breakdown_values(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["victor_ai_actor_context"] == {
                "slack_team_id": TEAM_ID,
                "acting_slack_user_id": USER_ID,
                "slack_channel_id": CHANNEL_ID,
                "slack_thread_ts": THREAD_TS,
                "event_id": EVENT_ID,
            }

        async def get_victor_application_summary(self, **kwargs):
            return {
                "complete_count": 16,
                "lead_count": 3,
                "complete_created_today": 2,
                "complete_created_last_7_days": 9,
                "breakdowns": {
                    "startup_stage": [{"value": "Prototype <MVP>", "count": 8}],
                    "industry_sector": [{"value": "Software & Enterprise", "count": 6}],
                },
                "filters": {},
            }

    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeClient)
    result = await _execute(
        SkillExecutor(),
        text="how many applications are there?",
        params={"action": "summary"},
    )

    assert "*Victor AI applications: 16 complete*" in result.message
    assert "3 partial leads" in result.message
    assert "Prototype &lt;MVP&gt;" in result.message
    assert "Software &amp; Enterprise" in result.message


@pytest.mark.asyncio
async def test_list_presents_screenshot_fields_without_slack_markup_injection(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_victor_applications(self, **kwargs):
            assert kwargs["limit"] == 10
            return {
                "applications": [
                    {
                        "id": 42,
                        "first_name": "Katie <@UATTACK>",
                        "last_name": "Forse",
                        "email": "katie@example.com",
                        "stage": "complete",
                        "role": "Founder",
                        "startup_stage": "Prototype / MVP",
                        "industry_sector": "Software & Enterprise",
                        "team_name": "Tribu",
                        "team_size": 1,
                        "created_at": "2026-07-23T00:12:00Z",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "offset": 0,
                "has_more": False,
            }

    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeClient)
    result = await _execute(
        SkillExecutor(),
        text="list the latest applications",
        params={"action": "list"},
    )

    assert "#42" in result.message
    assert "Katie &lt;@UATTACK&gt;" in result.message
    assert "Software &amp; Enterprise" in result.message
    assert "katie@example.com" in result.message
    assert "Prototype / MVP" in result.message


@pytest.mark.asyncio
async def test_detail_uses_numeric_id_and_never_renders_client_ref(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get_victor_application(self, application_id, **kwargs):
            assert application_id == 42
            return {
                "id": 42,
                "client_ref": "must-never-appear",
                "stage": "complete",
                "first_name": "Katie",
                "last_name": "Forse",
                "email": "katie@example.com",
                "linkedin": "https://linkedin.example/katie",
                "team_name": "Tribu",
                "role": "Founder",
                "startup_stage": "Prototype / MVP",
                "industry_sector": "Software & Enterprise",
                "location": "Adelaide",
                "team_size": 1,
                "team_members": [],
                "revenue_last_3_months": {"2026-06": 100},
                "idea": "A useful product",
                "support": "Mentoring",
                "consent": True,
                "created_at": "2026-07-23T00:12:00Z",
                "updated_at": "2026-07-23T00:13:00Z",
            }

    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeClient)
    result = await _execute(
        SkillExecutor(),
        text="show application 42",
        params={"action": "detail", "application_id": 42},
    )

    assert "application #42" in result.message
    assert "A useful product" in result.message
    assert "must-never-appear" not in result.message


@pytest.mark.asyncio
async def test_csv_is_uploaded_only_to_the_requesting_thread(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)
    uploaded = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def export_victor_applications_csv(self, **kwargs):
            return "id,email\n42,katie@example.com\n", "victor-ai-applications.csv"

    def fake_upload_file(**kwargs):
        uploaded.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(executor_module, "upload_file", fake_upload_file)
    result = await _execute(
        SkillExecutor(),
        text="download all applications as csv",
        params={"action": "export_csv"},
    )

    assert uploaded["channel"] == CHANNEL_ID
    assert uploaded["thread_ts"] == THREAD_TS
    assert uploaded["content"].startswith("id,email")
    assert result.data["action"] == "export_csv"


@pytest.mark.asyncio
async def test_backend_denial_returns_generic_message(monkeypatch):
    monkeypatch.setattr(executor_module, "get_settings", _settings)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get_victor_application_summary(self, **kwargs):
            request = httpx.Request("GET", "https://backend.test/victor")
            response = httpx.Response(403, request=request, json={"detail": "wrong channel"})
            raise httpx.HTTPStatusError("denied", request=request, response=response)

    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeClient)
    result = await _execute(
        SkillExecutor(),
        text="application summary",
        params={"action": "summary"},
    )

    assert result.message == VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE
    assert "wrong channel" not in result.message


def test_agent_catalog_exposes_victor_skill_only_in_named_channel(monkeypatch):
    agent = object.__new__(RooAgent)
    agent.skills = [_skill(), _skill("mlai-points")]
    monkeypatch.setattr("roo.agent.get_settings", _settings)

    allowed = agent._skills_for_slack_context(
        channel_id=CHANNEL_ID,
        channel_name="exp-victor-ai",
    )
    wrong_channel = agent._skills_for_slack_context(
        channel_id="GOTHER123",
        channel_name="general",
    )

    assert {skill.name for skill in allowed} == {
        "victor-ai-applications",
        "mlai-points",
    }
    assert {skill.name for skill in wrong_channel} == {"mlai-points"}
