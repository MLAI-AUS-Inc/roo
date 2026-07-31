import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-synthetic")
os.environ.setdefault("SLACK_SIGNING_SECRET", "synthetic-signing-secret")

from roo import main as main_module
from roo.clients import mlai_backend as backend_module
from roo.config import Settings, get_settings
from roo.coworking_messages import OFFICE_MANAGER_VOLUNTEER_ACTION_ID
from roo.slack_security import get_slack_receipt_store


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + str(timestamp).encode("ascii") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
    )


def _action_body(*, value=None):
    payload = {
        "type": "block_actions",
        "user": {"id": "UVERIFIED"},
        "channel": {"id": "CCOWORK"},
        "actions": [
            {
                "action_id": OFFICE_MANAGER_VOLUNTEER_ACTION_ID,
                "value": json.dumps(
                    value
                    if value is not None
                    else {
                        "date": "2026-08-03",
                        "slack_user_id": "UUNTRUSTED",
                    }
                ),
            }
        ],
    }
    return urlencode({"payload": json.dumps(payload)}).encode("utf-8")


def _signed_headers(settings, body):
    timestamp = int(time.time())
    return {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": _signature(
            settings.SLACK_SIGNING_SECRET,
            timestamp,
            body,
        ),
        "Content-Type": "application/x-www-form-urlencoded",
    }


@pytest.fixture(autouse=True)
def clear_app_state():
    get_slack_receipt_store.cache_clear()
    yield
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


def test_signed_button_uses_payload_actor_and_deduplicates_delivery(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    captured = {}

    async def fake_claim(**kwargs):
        captured.update(kwargs)

    def capture_task(coro):
        scheduled.append(coro)

    monkeypatch.setattr(
        main_module,
        "_claim_office_manager_from_action",
        fake_claim,
    )
    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)
    body = _action_body()
    headers = _signed_headers(configured, body)

    first = client.post("/slack/actions", content=body, headers=headers)
    second = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json() == {}
    assert second.status_code == 200
    assert second.json() == {}
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert captured == {
        "user_id": "UVERIFIED",
        "channel_id": "CCOWORK",
        "booking_date": "2026-08-03",
    }


def test_malformed_button_is_acknowledged_with_private_feedback(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    feedback = []
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        lambda **kwargs: feedback.append(kwargs),
    )

    body = _action_body(value={})
    response = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(configured, body),
    )

    assert response.status_code == 200
    assert feedback == [
        {
            "channel_id": "CCOWORK",
            "user_id": "UVERIFIED",
            "text": (
                "This volunteer button is no longer valid. "
                "Please use Roo's latest Office Manager announcement."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_claim_success_reports_zero_charge_and_refund_privately(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            assert slack_user_id == "UVERIFIED"
            assert booking_date == "2026-08-03"
            return {"status": "claimed", "points_refunded": 8}

    feedback = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        lambda **kwargs: feedback.append(kwargs),
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert len(feedback) == 1
    assert "without deducting Roo points" in feedback[0]["text"]
    assert "returned the 8 Roo points" in feedback[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "payload", "expected"),
    (
        (
            "already_claimed",
            {"assignee_slack_user_id": "UWINNER"},
            "Someone has already volunteered for today. <@UWINNER> has the role.",
        ),
        (
            "claim_closed",
            {},
            "The Office Manager volunteer window is closed for today.",
        ),
        (
            "member_not_eligible",
            {},
            "Roo could not confirm you as an active member",
        ),
    ),
)
async def test_claim_rejections_are_private_and_specific(
    monkeypatch,
    code,
    payload,
    expected,
):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(
                409,
                request=request,
                json={"code": code, **payload},
            )
            raise httpx.HTTPStatusError(
                "claim rejected",
                request=request,
                response=response,
            )

    feedback = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        lambda **kwargs: feedback.append(kwargs),
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert expected in feedback[0]["text"]


def test_private_feedback_falls_back_to_dm(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        main_module,
        "post_ephemeral",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ephemeral failed")),
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text: (
            delivered.append((user_id, text))
            or {"ok": True}
        ),
    )

    main_module._send_office_manager_private_feedback(
        channel_id="CCOWORK",
        user_id="UVERIFIED",
        text="Private result",
    )

    assert delivered == [("UVERIFIED", "Private result")]


def test_private_feedback_falls_back_when_ephemeral_returns_failure(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        main_module,
        "post_ephemeral",
        lambda **kwargs: {"ok": False, "error": "not_in_channel"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text: (
            delivered.append((user_id, text))
            or {"ok": True}
        ),
    )

    main_module._send_office_manager_private_feedback(
        channel_id="CCOWORK",
        user_id="UVERIFIED",
        text="Private result",
    )

    assert delivered == [("UVERIFIED", "Private result")]
