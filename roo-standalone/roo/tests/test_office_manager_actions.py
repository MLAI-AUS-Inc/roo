import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import date
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


_UNSET = object()


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + str(timestamp).encode("ascii") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
        **overrides,
    )


def _action_body(*, value=_UNSET, channel_id="CCOWORK"):
    payload = {
        "type": "block_actions",
        "user": {"id": "UVERIFIED"},
        "channel": {"id": channel_id},
        "actions": [
            {
                "action_id": OFFICE_MANAGER_VOLUNTEER_ACTION_ID,
                "value": json.dumps(
                    value
                    if value is not _UNSET
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
    main_module._office_manager_action_tasks.clear()
    get_slack_receipt_store.cache_clear()
    yield
    main_module._office_manager_action_tasks.clear()
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
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "_start_office_manager_action", capture_task)
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


def test_admin_surface_ignores_office_manager_action(tmp_path, monkeypatch):
    configured = _settings(
        tmp_path,
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN",
    )
    main_module.app.dependency_overrides[get_settings] = lambda: configured

    def unexpected_action(coro):
        coro.close()
        pytest.fail("Admin Roo must not process Office Manager actions")

    monkeypatch.setattr(
        main_module,
        "_start_office_manager_action",
        unexpected_action,
    )
    body = _action_body(channel_id="GADMIN")
    response = TestClient(main_module.app).post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(configured, body),
    )

    assert response.status_code == 200
    assert response.json() == {}


def test_malformed_button_is_acknowledged_with_private_feedback(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )
    monkeypatch.setattr(main_module, "_start_office_manager_action", scheduled.append)

    body = _action_body(value={})
    response = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(configured, body),
    )

    assert response.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
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


@pytest.mark.parametrize("value", (None, 123, "not-an-object", ["2026-08-03"]))
def test_non_object_button_value_is_acknowledged_without_crashing(
    tmp_path,
    monkeypatch,
    value,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )
    monkeypatch.setattr(main_module, "_start_office_manager_action", scheduled.append)

    body = _action_body(value=value)
    response = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(configured, body),
    )

    assert response.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert "no longer valid" in feedback[0]["text"]


def test_stale_button_is_rejected_before_backend_claim(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    feedback = []

    async def unexpected_claim(**kwargs):
        pytest.fail(f"stale button reached backend claim: {kwargs}")

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(
        main_module,
        "_claim_office_manager_from_action",
        unexpected_claim,
    )
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "_start_office_manager_action", scheduled.append)

    body = _action_body(value={"date": "2026-08-02"})
    response = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(configured, body),
    )

    assert response.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert "no longer valid" in feedback[0]["text"]


@pytest.mark.asyncio
async def test_claim_success_reports_zero_charge_and_refund_privately(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            assert slack_user_id == "UVERIFIED"
            assert booking_date == "2026-08-03"
            return {"status": "claimed", "points_refunded": 8}

    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
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
async def test_already_claimed_by_member_is_the_only_idempotent_success(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            return {"status": "already_claimed_by_you", "points_refunded": 0}

    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert "already today's Office Manager" in feedback[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    (
        [],
        {"status": "already_claimed", "assignee_slack_user_id": "UOTHER"},
        {"status": "unexpected_success"},
        {},
    ),
)
async def test_unexpected_success_response_never_claims_member_is_winner(
    monkeypatch,
    result,
):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            return result

    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert "unexpected response" in feedback[0]["text"]
    assert "You are today's Office Manager" not in feedback[0]["text"]


@pytest.mark.asyncio
async def test_invalid_refund_value_does_not_hide_successful_claim(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            return {"status": "claimed", "points_refunded": "not-a-number"}

    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert "You are today's Office Manager" in feedback[0]["text"]
    assert "could not confirm" not in feedback[0]["text"]
    assert "returned the" not in feedback[0]["text"]


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

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )

    await main_module._claim_office_manager_from_action(
        user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert expected in feedback[0]["text"]


@pytest.mark.asyncio
async def test_private_feedback_falls_back_to_dm(monkeypatch):
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

    await main_module._send_office_manager_private_feedback(
        channel_id="CCOWORK",
        user_id="UVERIFIED",
        text="Private result",
    )

    assert delivered == [("UVERIFIED", "Private result")]


@pytest.mark.asyncio
async def test_private_feedback_falls_back_when_ephemeral_returns_failure(monkeypatch):
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

    await main_module._send_office_manager_private_feedback(
        channel_id="CCOWORK",
        user_id="UVERIFIED",
        text="Private result",
    )

    assert delivered == [("UVERIFIED", "Private result")]


@pytest.mark.asyncio
async def test_private_feedback_offloads_slack_calls_from_event_loop(monkeypatch):
    offloaded = []

    def fake_ephemeral(**kwargs):
        return {"ok": False, "error": "not_in_channel"}

    def fake_dm(user_id, text):
        return {"ok": True}

    async def capture_to_thread(function, *args, **kwargs):
        offloaded.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(main_module, "post_ephemeral", fake_ephemeral)
    monkeypatch.setattr(main_module, "send_dm", fake_dm)
    monkeypatch.setattr(main_module.asyncio, "to_thread", capture_to_thread)

    await main_module._send_office_manager_private_feedback(
        channel_id="CCOWORK",
        user_id="UVERIFIED",
        text="Private result",
    )

    assert offloaded == [fake_ephemeral, fake_dm]


@pytest.mark.asyncio
async def test_office_manager_action_task_is_retained_until_completion():
    started = asyncio.Event()
    release = asyncio.Event()

    async def action():
        started.set()
        await release.wait()

    task = main_module._start_office_manager_action(action())
    await started.wait()

    assert task in main_module._office_manager_action_tasks

    release.set()
    await task
    await asyncio.sleep(0)

    assert task not in main_module._office_manager_action_tasks
