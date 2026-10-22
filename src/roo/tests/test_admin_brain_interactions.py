import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo.admin_brain import (
    ADMIN_BRAIN_INCORRECT_CALLBACK,
    build_incorrect_feedback_modal,
)
from roo.backend_identity import get_backend_actor_context
from roo.config import Settings, get_settings
from roo.slack_security import get_slack_receipt_store


SERVICE_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
DISPATCH_SECRET = "dispatch-secret-" + ("s" * 32)


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
        MLAI_BACKEND_URL="https://backend.test",
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
        ROO_ENABLED_SKILLS="admin-brain",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_API_KEY=SERVICE_TOKEN,
    )


def _unified_settings(tmp_path):
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
        MLAI_BACKEND_URL="https://backend.test",
        ROO_SURFACE="public",
        ROO_UNIFIED_ADMIN_ROUTING_ENABLED=True,
        ORG_BRAIN_ROUTER_API_KEY=SERVICE_TOKEN,
        ROO_ADMIN_INTERNAL_URL="http://roo-admin:8000",
        ROO_ADMIN_DISPATCH_SECRET=DISPATCH_SECRET,
    )


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


def _button_payload(action_id="admin_brain_feedback_helpful"):
    return {
        "type": "block_actions",
        "trigger_id": "123.456.abc",
        "team": {"id": "TMLAI123"},
        "user": {"id": "UADMIN123"},
        "channel": {"id": "GADMIN123"},
        "message": {"ts": "1700000000.123", "thread_ts": "1700000000.123"},
        "actions": [
            {
                "action_id": action_id,
                "value": json.dumps(
                    {
                        "query_id": "query-1",
                        "requester_user_id": "UADMIN123",
                        "claim_id": "claim-1",
                    }
                ),
            }
        ],
    }


def setup_function():
    get_slack_receipt_store.cache_clear()


def teardown_function():
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


def test_signed_helpful_button_schedules_only_admin_feedback(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    captured = []

    def fake_feedback(**kwargs):
        captured.append(kwargs)

        async def complete():
            return None

        return complete()

    def fake_create_task(coro):
        coro.close()

    monkeypatch.setattr(main_module, "_record_admin_brain_feedback", fake_feedback)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    response = _post_action(TestClient(main_module.app), configured, _button_payload())

    assert response.status_code == 200
    assert captured[0]["feedback_type"] == "relevant"
    assert captured[0]["feedback"]["query_id"] == "query-1"


def test_incorrect_button_opens_correction_modal(tmp_path, monkeypatch):
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
        _button_payload("admin_brain_feedback_incorrect"),
    )

    assert response.status_code == 200
    assert opened["trigger_id"] == "123.456.abc"
    assert opened["view"]["callback_id"] == ADMIN_BRAIN_INCORRECT_CALLBACK


def test_single_public_app_relays_admin_feedback_instead_of_using_memory_key(
    tmp_path,
    monkeypatch,
):
    configured = _unified_settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    captured = []

    def fake_feedback(**kwargs):
        captured.append(kwargs)

        async def complete():
            return None

        return complete()

    def fake_create_task(coro):
        coro.close()

    monkeypatch.setattr(
        main_module,
        "_record_unified_admin_brain_feedback",
        fake_feedback,
    )
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    response = _post_action(TestClient(main_module.app), configured, _button_payload())

    assert response.status_code == 200
    assert captured[0]["feedback_type"] == "relevant"
    assert captured[0]["feedback"]["query_id"] == "query-1"
    assert configured.ORG_BRAIN_API_KEY is None


def test_verified_modal_submission_schedules_correction_feedback(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    captured = []
    modal = build_incorrect_feedback_modal(
        {
            "query_id": "query-1",
            "requester_user_id": "UADMIN123",
            "claim_id": "claim-1",
        },
        team_id="TMLAI123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
    )
    payload = {
        "type": "view_submission",
        "team": {"id": "TMLAI123"},
        "user": {"id": "UADMIN123"},
        "view": {
            "callback_id": modal["callback_id"],
            "private_metadata": modal["private_metadata"],
            "state": {
                "values": {
                    "admin_brain_correction": {
                        "correction_text": {"value": "The pilot is amber."}
                    }
                }
            },
        },
    }

    def fake_feedback(**kwargs):
        captured.append(kwargs)

        async def complete():
            return None

        return complete()

    def fake_create_task(coro):
        coro.close()

    monkeypatch.setattr(main_module, "_record_admin_brain_feedback", fake_feedback)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)
    response = _post_action(TestClient(main_module.app), configured, payload)

    assert response.status_code == 200
    assert response.json() == {"response_action": "clear"}
    assert captured[0]["feedback_type"] == "incorrect"
    assert captured[0]["correction_text"] == "The pilot is amber."


@pytest.mark.asyncio
async def test_feedback_backend_call_keeps_actor_context_bounded(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["constructor_context"] = get_backend_actor_context()

        async def submit_org_memory_feedback(self, **kwargs):
            captured["request_context"] = get_backend_actor_context()
            captured["payload"] = kwargs
            return {"feedback_id": "feedback-1"}

    messages = []
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: messages.append(kwargs))

    assert get_backend_actor_context() is None
    await main_module._record_admin_brain_feedback(
        settings=configured,
        user_id="UADMIN123",
        team_id="TMLAI123",
        channel_id="GADMIN123",
        thread_ts="1700000000.123",
        feedback={
            "query_id": "query-1",
            "requester_user_id": "UADMIN123",
            "claim_id": "claim-1",
        },
        feedback_type="incorrect",
        correction_text="The pilot is amber.",
    )

    assert captured["request_context"].acting_slack_user_id == "UADMIN123"
    assert captured["payload"]["feedback_type"] == "incorrect"
    assert get_backend_actor_context() is None
    assert "review queue" in messages[0]["text"]
