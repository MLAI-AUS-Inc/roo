import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo.admin_brain import (
    ADMIN_ACTION_APPROVE,
    ADMIN_ACTION_REJECT,
    ADMIN_ACTION_REJECT_CALLBACK,
    build_admin_action_reject_modal,
    build_admin_action_response,
)
from roo.backend_identity import BackendActorContext, get_backend_actor_context
from roo.clients.mlai_backend import MLAIBackendClient
from roo.config import Settings, get_settings
from roo.skills.executor import SkillExecutor
from roo.skills.loader import Skill
from roo.slack_security import get_slack_receipt_store


SERVICE_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
PROPOSAL_ID = "3ec0b82f-b643-4d2d-8ec6-41f6fd701513"


def _settings(tmp_path=None, **overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "SLACK_RECEIPTS_DB_PATH": str(
            (tmp_path / "slack-receipts.db") if tmp_path else "data/test-receipts.db"
        ),
        "OPENAI_API_KEY": "synthetic-openai-key",
        "MLAI_BACKEND_URL": "https://backend.test",
        "ROO_SURFACE": "admin",
        "ROO_ALLOWED_CHANNEL_IDS": "GADMIN123",
        "ROO_ENABLED_SKILLS": "admin-brain admin-actions",
        "ORG_BRAIN_ENABLED": True,
        "ORG_BRAIN_ACTIONS_ENABLED": True,
        "ORG_BRAIN_API_KEY": SERVICE_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


def _actor(user_id="UAPPROVER1"):
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id=user_id,
        slack_channel_id="GADMIN123",
        slack_thread_ts="1700000000.123",
        event_id="EvADMINACTION1",
    )


def _proposal(**overrides):
    value = {
        "id": PROPOSAL_ID,
        "action_type": "create_linear_issue",
        "target_system": "linear",
        "risk_level": "medium",
        "requires_approval": True,
        "status": "awaiting_approval",
        "requested_by_slack_id": "UPROPOSER1",
        "input_payload": {
            "team_id": "TEAM-1",
            "project_id": "PROJECT-1",
            "title": "Confirm the venue <!channel>",
            "description": "Confirm access with <@USECRET>.",
        },
        "approval": {"pending": True},
        "error_text": "",
    }
    value.update(overrides)
    return value


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(),
        b"v0:" + str(timestamp).encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _post_action(client, configured, payload):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = int(time.time())
    return client.post(
        "/slack/actions",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": str(timestamp),
            "X-Slack-Signature": _signature(
                configured.SLACK_SIGNING_SECRET,
                timestamp,
                body,
            ),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _button_payload(action_id=ADMIN_ACTION_APPROVE):
    return {
        "type": "block_actions",
        "trigger_id": "123.456.action",
        "team": {"id": "TMLAI123"},
        "user": {"id": "UAPPROVER1"},
        "channel": {"id": "GADMIN123"},
        "message": {"ts": "1700000000.123", "thread_ts": "1700000000.123"},
        "actions": [
            {
                "action_id": action_id,
                "value": json.dumps({"proposal_id": PROPOSAL_ID}),
            }
        ],
    }


def setup_function():
    get_slack_receipt_store.cache_clear()


def teardown_function():
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_backend_client_uses_scoped_action_endpoints_and_idempotency(monkeypatch):
    calls = []

    async def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json=_proposal())

    client = MLAIBackendClient(
        base_url="https://backend.test/api/v1",
        service_principal_key=SERVICE_TOKEN,
        surface="admin",
        actor_context=_actor(),
    )
    monkeypatch.setattr(client, "_request", fake_request)

    await client.create_org_memory_action(
        action_type="create_linear_issue",
        configuration_id="5ed1e325-d10d-41c5-bdce-c0c11f270032",
        input_payload={"team_id": "TEAM-1", "project_id": "PROJECT-1", "title": "Venue"},
        idempotency_key="proposal-idempotency",
    )
    await client.approve_org_memory_action(
        PROPOSAL_ID,
        idempotency_key="approval-idempotency",
    )
    await client.execute_org_memory_action(
        PROPOSAL_ID,
        idempotency_key="execution-idempotency",
    )
    await client.reject_org_memory_action(
        PROPOSAL_ID,
        reason="No longer required.",
        idempotency_key="rejection-idempotency",
    )

    assert [call[1] for call in calls] == [
        "/org-memory/actions",
        f"/org-memory/actions/{PROPOSAL_ID}/approve",
        f"/org-memory/actions/{PROPOSAL_ID}/execute",
        f"/org-memory/actions/{PROPOSAL_ID}/reject",
    ]
    assert all(call[2]["use_org_memory_identity"] is True for call in calls)
    assert [call[2]["headers"]["Idempotency-Key"] for call in calls] == [
        "proposal-idempotency",
        "approval-idempotency",
        "execution-idempotency",
        "rejection-idempotency",
    ]


def test_action_card_escapes_content_and_keeps_button_values_content_free():
    rendered = build_admin_action_response(_proposal())
    text = str(rendered["blocks"])

    assert "<!channel>" not in text
    assert "<@USECRET>" not in text
    assert "&lt;!channel&gt;" in text
    buttons = rendered["blocks"][-1]["elements"]
    assert {button["action_id"] for button in buttons} == {
        ADMIN_ACTION_APPROVE,
        ADMIN_ACTION_REJECT,
    }
    assert all(json.loads(button["value"]) == {"proposal_id": PROPOSAL_ID} for button in buttons)
    assert "Confirm the venue" not in buttons[0]["value"]


@pytest.mark.asyncio
async def test_admin_actions_executor_creates_and_executes_local_draft(monkeypatch):
    configured = _settings()
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def create_org_memory_action(self, **kwargs):
            calls.append(("create", kwargs))
            return {
                **_proposal(
                    action_type="draft_gmail",
                    target_system="gmail",
                    risk_level="low",
                    requires_approval=False,
                    status="proposed",
                    approval={"pending": False},
                    input_payload=kwargs["input_payload"],
                ),
            }

        async def execute_org_memory_action(self, proposal_id, **kwargs):
            calls.append(("execute", {"proposal_id": proposal_id, **kwargs}))
            return {
                **_proposal(
                    action_type="draft_gmail",
                    target_system="gmail",
                    risk_level="low",
                    requires_approval=False,
                    status="completed",
                    approval={"pending": False},
                    input_payload={
                        "to": ["sam@example.com"],
                        "subject": "Pilot",
                        "body": "The pilot is green.",
                    },
                ),
            }

    executor_globals = SkillExecutor._execute_admin_actions.__globals__
    monkeypatch.setitem(executor_globals, "get_settings", lambda: configured)
    monkeypatch.setitem(executor_globals, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor().execute(
        Skill("admin-actions", "test", "", Path(".")),
        text="draft the email",
        user_id="UADMIN123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
        param_overrides={
            "action": "draft_gmail",
            "to": ["sam@example.com"],
            "subject": "Pilot",
            "body": "The pilot is green.",
        },
    )

    assert result.success
    assert result.data["status"] == "completed"
    assert [call[0] for call in calls] == ["init", "create", "execute"]
    assert calls[1][1]["action_type"] == "draft_gmail"
    assert calls[2][1]["idempotency_key"] == f"roo-draft-execute-{PROPOSAL_ID}"


@pytest.mark.asyncio
async def test_admin_actions_executor_refuses_incomplete_linear_proposal(monkeypatch):
    configured = _settings()

    class ForbiddenClient:
        def __init__(self, **kwargs):
            pass

        async def create_org_memory_action(self, **kwargs):
            raise AssertionError("An incomplete action must not reach the backend")

    executor_globals = SkillExecutor._execute_admin_actions.__globals__
    monkeypatch.setitem(executor_globals, "get_settings", lambda: configured)
    monkeypatch.setitem(executor_globals, "MLAIBackendClient", ForbiddenClient)
    result = await SkillExecutor().execute(
        Skill("admin-actions", "test", "", Path(".")),
        text="create a Linear issue",
        user_id="UADMIN123",
        channel_id="GADMIN123",
        param_overrides={"action": "create_linear_issue", "title": "Venue"},
    )

    assert result.success
    assert "haven't created a proposal" in result.message
    assert "`configuration_id`" in result.message
    assert "`project_id`" in result.message
    assert "`team_id`" in result.message


def test_signed_approve_button_schedules_backend_owned_review(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    captured = []

    def fake_review(**kwargs):
        captured.append(kwargs)

        async def complete():
            return None

        return complete()

    def fake_create_task(coro):
        coro.close()

    monkeypatch.setattr(main_module, "_review_admin_action", fake_review)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    response = _post_action(
        TestClient(main_module.app),
        configured,
        _button_payload(),
    )

    assert response.status_code == 200
    assert captured[0]["decision"] == "approve"
    assert captured[0]["proposal_id"] == PROPOSAL_ID
    assert captured[0]["user_id"] == "UAPPROVER1"


def test_public_roo_ignores_admin_action_button_without_backend_call(tmp_path, monkeypatch):
    configured = Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "public-slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
        ROO_SURFACE="public",
    )
    main_module.app.dependency_overrides[get_settings] = lambda: configured

    def forbidden_review(**kwargs):
        raise AssertionError("Public Roo must not dispatch Admin action review")

    monkeypatch.setattr(main_module, "_review_admin_action", forbidden_review)
    response = _post_action(
        TestClient(main_module.app),
        configured,
        _button_payload(),
    )

    assert response.status_code == 200


def test_reject_button_opens_reason_modal_without_calling_backend(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    opened = {}

    class FakeSlackClient:
        def views_open(self, **kwargs):
            opened.update(kwargs)

    monkeypatch.setattr("roo.slack_client.get_slack_client", lambda: FakeSlackClient())
    response = _post_action(
        TestClient(main_module.app),
        configured,
        _button_payload(ADMIN_ACTION_REJECT),
    )

    assert response.status_code == 200
    assert opened["trigger_id"] == "123.456.action"
    assert opened["view"]["callback_id"] == ADMIN_ACTION_REJECT_CALLBACK
    assert json.loads(opened["view"]["private_metadata"])["proposal_id"] == PROPOSAL_ID


def test_verified_rejection_submission_schedules_reasoned_review(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    captured = []
    modal = build_admin_action_reject_modal(
        {"proposal_id": PROPOSAL_ID},
        team_id="TMLAI123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
    )
    payload = {
        "type": "view_submission",
        "team": {"id": "TMLAI123"},
        "user": {"id": "UAPPROVER1"},
        "view": {
            "callback_id": modal["callback_id"],
            "private_metadata": modal["private_metadata"],
            "state": {
                "values": {
                    "admin_action_rejection": {
                        "reason": {"value": "The project scope is wrong."}
                    }
                }
            },
        },
    }

    def fake_review(**kwargs):
        captured.append(kwargs)

        async def complete():
            return None

        return complete()

    def fake_create_task(coro):
        coro.close()

    monkeypatch.setattr(main_module, "_review_admin_action", fake_review)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    response = _post_action(TestClient(main_module.app), configured, payload)

    assert response.status_code == 200
    assert response.json() == {"response_action": "clear"}
    assert captured[0]["decision"] == "reject"
    assert captured[0]["rejection_reason"] == "The project scope is wrong."


@pytest.mark.asyncio
async def test_review_worker_binds_clicking_actor_then_approves_and_executes(monkeypatch):
    configured = _settings()
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            captured.append(("init", get_backend_actor_context(), kwargs))

        async def get_org_memory_action(self, proposal_id, **kwargs):
            captured.append(("get", get_backend_actor_context(), proposal_id, kwargs))
            return _proposal()

        async def approve_org_memory_action(self, proposal_id, **kwargs):
            captured.append(("approve", get_backend_actor_context(), proposal_id, kwargs))
            return _proposal(status="approved", approval={"pending": False})

        async def execute_org_memory_action(self, proposal_id, **kwargs):
            captured.append(("execute", get_backend_actor_context(), proposal_id, kwargs))
            return _proposal(status="completed", approval={"pending": False})

    messages = []
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: messages.append(kwargs))

    assert get_backend_actor_context() is None
    await main_module._review_admin_action(
        settings=configured,
        decision="approve",
        proposal_id=PROPOSAL_ID,
        user_id="UAPPROVER1",
        team_id="TMLAI123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
    )

    assert [row[0] for row in captured] == ["init", "get", "approve", "execute"]
    assert all(
        row[1].acting_slack_user_id == "UAPPROVER1"
        for row in captured
    )
    assert captured[2][3]["idempotency_key"] == (
        f"slack-approve-{PROPOSAL_ID}-UAPPROVER1"
    )
    assert captured[3][3]["idempotency_key"] == f"slack-execute-{PROPOSAL_ID}"
    assert get_backend_actor_context() is None
    assert "Completed" in str(messages[0]["blocks"])


def test_public_surface_cannot_be_configured_with_controlled_actions():
    with pytest.raises(ValueError, match="Public Roo"):
        Settings(
            _env_file=None,
            SLACK_BOT_TOKEN="xoxb-synthetic",
            SLACK_SIGNING_SECRET="synthetic-signing-secret",
            OPENAI_API_KEY="synthetic-openai-key",
            ORG_BRAIN_ACTIONS_ENABLED=True,
        )


def test_proposal_id_must_be_canonical_uuid():
    with pytest.raises(ValueError, match="valid controlled-action proposal ID"):
        MLAIBackendClient._org_memory_action_id("../../other-action")
    assert MLAIBackendClient._org_memory_action_id(PROPOSAL_ID) == str(
        UUID(PROPOSAL_ID)
    )
