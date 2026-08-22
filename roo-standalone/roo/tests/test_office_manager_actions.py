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
from roo import office_manager_actions as action_module
from roo import slack_action_tasks
from roo.clients import mlai_backend as backend_module
from roo.config import Settings, get_settings
from roo.coworking_messages import (
    NO_FOOD_REMINDER,
    OFFICE_MANAGER_VOLUNTEER_ACTION_ID,
)
from roo.skills.executor import SkillExecutor
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
    slack_action_tasks._tasks.clear()
    action_module.get_office_manager_action_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    yield
    slack_action_tasks._tasks.clear()
    action_module.get_office_manager_action_store.cache_clear()
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
    monkeypatch.setattr(main_module, "start_slack_action", capture_task)
    body = _action_body()
    headers = _signed_headers(configured, body)

    first = client.post("/slack/actions", content=body, headers=headers)
    second = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json() == {}
    assert second.status_code == 200
    assert second.json() == {}
    assert len(scheduled) == 1
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    assert action_store.get(1)["status"] == "pending"

    asyncio.run(scheduled[0])
    assert captured == {
        "user_id": "UVERIFIED",
        "channel_id": "CCOWORK",
        "booking_date": "2026-08-03",
    }
    assert action_store.get(1)["status"] == "completed"


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
        "start_slack_action",
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
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)

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
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)

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
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)

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
async def test_unknown_backend_failure_does_not_claim_no_booking_was_created(
    monkeypatch,
):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(
                502,
                request=request,
                json={"code": "upstream_failure"},
            )
            raise httpx.HTTPStatusError(
                "claim result unknown",
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

    message = feedback[0]["text"]
    assert "could not confirm the result" in message
    assert "latest Office Manager announcement" in message
    assert "No Office Manager booking was created" not in message


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

    task = slack_action_tasks.start(action())
    await started.wait()

    assert task in slack_action_tasks._tasks

    release.set()
    await task
    await asyncio.sleep(0)

    assert task not in slack_action_tasks._tasks


@pytest.mark.asyncio
async def test_pending_action_is_recovered_after_restart(tmp_path):
    database_path = tmp_path / "actions.db"
    original_store = action_module.OfficeManagerActionStore(database_path)
    action = original_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    recovered_store = action_module.OfficeManagerActionStore(database_path)
    processed = []

    async def processor(record):
        processed.append(record["idempotency_key"])

    count = await action_module.process_due_office_manager_actions(
        store=recovered_store,
        processor=processor,
    )

    assert count == 1
    assert processed == ["office_manager:UVERIFIED:2026-08-03"]
    assert recovered_store.get(action["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered(tmp_path, monkeypatch):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert store.reserve(action["id"], lease_seconds=60) is not None

    current_time[0] += 61
    processed = []

    async def processor(record):
        processed.append(record["attempt_count"])

    count = await action_module.process_due_office_manager_actions(
        store=store,
        processor=processor,
    )

    assert count == 1
    assert processed == [2]
    assert store.get(action["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_only_one_worker_can_reserve_an_action(tmp_path):
    database_path = tmp_path / "actions.db"
    first_store = action_module.OfficeManagerActionStore(database_path)
    second_store = action_module.OfficeManagerActionStore(database_path)
    action = first_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    reservations = await asyncio.gather(
        asyncio.to_thread(first_store.reserve, action["id"], owner="first"),
        asyncio.to_thread(second_store.reserve, action["id"], owner="second"),
    )

    assert sum(reservation is not None for reservation in reservations) == 1


def test_expired_worker_cannot_overwrite_replacement_worker_state(
    tmp_path,
    monkeypatch,
):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    stale = store.reserve(action["id"], owner="stale", lease_seconds=1)
    current_time[0] += 2
    replacement = store.reserve(action["id"], owner="replacement")

    assert stale is not None
    assert replacement is not None
    assert store.mark_completed(action["id"], owner="stale") is False
    assert store.release(
        action["id"],
        owner="stale",
        error="late_failure",
    ) is False
    current = store.get(action["id"])
    assert current["status"] == "processing"
    assert current["locked_by"] == "replacement"


@pytest.mark.asyncio
async def test_cancelled_action_returns_to_pending_for_recovery(tmp_path):
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    started = asyncio.Event()

    async def processor(record):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        action_module.process_office_manager_action(
            action["id"],
            store=store,
            processor=processor,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get(action["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_shutdown_drain_waits_for_retained_action():
    started = asyncio.Event()
    release = asyncio.Event()

    async def action():
        started.set()
        await release.wait()

    task = slack_action_tasks.start(action())
    await started.wait()
    drain_task = asyncio.create_task(
        slack_action_tasks.drain(timeout_seconds=1)
    )
    release.set()
    await drain_task

    assert task.done()
    assert task not in slack_action_tasks._tasks


@pytest.mark.parametrize("admin_checkin", (False, True))
def test_primary_coworking_confirmations_include_no_food_reminder(
    admin_checkin,
):
    message = SkillExecutor._format_coworking_booking_success(
        object(),
        booking_date="2026-08-03",
        target_user_id="UMEMBER",
        cost=8,
        new_balance=42,
        admin_checkin=admin_checkin,
    )

    assert f"\n\n{NO_FOOD_REMINDER}" in message


def test_admin_batch_coworking_confirmation_includes_no_food_reminder():
    message = SkillExecutor._format_admin_coworking_batch_success(
        object(),
        booking_date="2026-08-03",
        batch_result={
            "created_count": 1,
            "already_booked_count": 0,
            "results": [
                {
                    "slack_user_id": "UMEMBER",
                    "points_cost": 4,
                    "already_booked": False,
                }
            ],
        },
    )

    assert f"\n\n{NO_FOOD_REMINDER}" in message
