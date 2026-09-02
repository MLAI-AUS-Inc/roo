import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-synthetic")
os.environ.setdefault("SLACK_SIGNING_SECRET", "synthetic-signing-secret")

from roo import main as main_module
from roo import office_manager_actions as action_module
from roo import slack_client as slack_client_module
from roo import slack_action_tasks
from roo.clients import mlai_backend as backend_module
from roo.backend_identity import BackendIdentityError
from roo.config import Settings, get_settings
from roo.coworking_messages import (
    NO_FOOD_REMINDER,
    OFFICE_MANAGER_VOLUNTEER_ACTION_ID,
)
from roo.skills.executor import SkillExecutor
from roo.slack_security import (
    get_slack_receipt_store,
    verify_and_claim_slack_request,
)


_UNSET = object()


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + str(timestamp).encode("ascii") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _settings(tmp_path, **overrides):
    values = dict(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-receipts.db"),
        OPENAI_API_KEY="synthetic-openai-key",
        MLAI_BACKEND_URL="https://backend.test",
        ROO_API_KEY="synthetic-roo-api-key",
        OFFICE_MANAGER_ACTIONS_ENABLED=True,
    )
    values.update(overrides)
    return Settings(**values)


def _action_body(*, value=_UNSET, channel_id="CCOWORK", action_ts=None):
    payload = {
        "type": "block_actions",
        "user": {"id": "UVERIFIED"},
        "channel": {"id": channel_id},
        "actions": [
            {
                "action_id": OFFICE_MANAGER_VOLUNTEER_ACTION_ID,
                **({"action_ts": action_ts} if action_ts else {}),
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


def _successful_claim_payload(
    *,
    status="claimed",
    points_refunded=0,
    **overrides,
):
    payload = {
        "status": status,
        "date": "2026-08-03",
        "office_manager_slack_user_id": "UVERIFIED",
        "assignment_id": 42,
        "booking": {
            "id": "00000000-0000-0000-0000-000000000042",
            "date": "2026-08-03",
            "status": "booked",
            "points_cost": 0,
            "booking_source": "office_manager",
        },
        "points_charged": 0,
        "points_refunded": points_refunded,
        "office_manager_free_day": True,
    }
    payload.update(overrides)
    return payload


def _signed_headers(settings, body, *, timestamp=None):
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
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
def clear_app_state(monkeypatch):
    slack_action_tasks._tasks.clear()
    action_module.get_office_manager_action_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    monkeypatch.setattr(
        main_module,
        "get_current_date",
        lambda: date(2026, 8, 3),
    )
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
        "action": captured["action"],
        "store": action_store,
    }
    assert captured["action"]["locked_by"]
    assert action_store.get(1)["status"] == "completed"

    completed_replay = client.post(
        "/slack/actions",
        content=body,
        headers=headers,
    )
    assert completed_replay.status_code == 200
    assert len(scheduled) == 1
    assert action_store.get(1)["status"] == "completed"


def test_resigned_retry_with_same_action_ts_reuses_durable_attempt(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    scheduled = []
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    body = _action_body(action_ts="1.000001")
    first_timestamp = int(time.time())
    first_headers = _signed_headers(
        configured,
        body,
        timestamp=first_timestamp,
    )
    retry_headers = _signed_headers(
        configured,
        body,
        timestamp=first_timestamp + 1,
    )
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=first_headers)
    resigned_retry = client.post(
        "/slack/actions",
        content=body,
        headers=retry_headers,
    )

    assert first.status_code == resigned_retry.status_code == 200
    assert first_headers["X-Slack-Signature"] != retry_headers["X-Slack-Signature"]
    assert len(scheduled) == 1
    store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    first_record = store.get(1)
    assert first_record["action_occurrence_key"]
    assert store.get(2) is None
    scheduled[0].close()


def test_persistence_failure_is_retried_after_generic_receipt_is_committed(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    captured = []
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    original_record_action = action_store.record_action
    persistence_attempts = 0

    def fail_first_persistence(**kwargs):
        nonlocal persistence_attempts
        persistence_attempts += 1
        if persistence_attempts == 1:
            raise OSError("synthetic outbox failure")
        return original_record_action(**kwargs)

    async def capture_claim(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(action_store, "record_action", fail_first_persistence)
    monkeypatch.setattr(
        main_module,
        "_claim_office_manager_from_action",
        capture_claim,
    )
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    body = _action_body(action_ts="1.000001")
    headers = _signed_headers(configured, body)

    first = client.post("/slack/actions", content=body, headers=headers)
    fresh_body = _action_body(action_ts="2.000002")
    retry = client.post(
        "/slack/actions",
        content=fresh_body,
        headers=_signed_headers(configured, fresh_body),
    )

    assert first.status_code == 503
    assert retry.status_code == 200
    assert persistence_attempts == 2
    assert len(scheduled) == 1
    persisted = action_store.get(1)
    assert persisted is not None
    assert persisted["status"] == "pending"

    asyncio.run(scheduled[0])
    assert captured == [
        {
            "user_id": "UVERIFIED",
            "channel_id": "CCOWORK",
            "booking_date": "2026-08-03",
            "action": captured[0]["action"],
            "store": action_store,
        }
    ]
    assert action_store.get(int(persisted["id"]))["status"] == "completed"


def test_commit_uncertain_retry_is_recovered_by_durable_worker(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []
    processed = []
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    original_record_action = action_store.record_action
    persistence_attempts = 0

    def lose_first_commit_result(**kwargs):
        nonlocal persistence_attempts
        persistence_attempts += 1
        result = original_record_action(**kwargs)
        if persistence_attempts == 1:
            raise TimeoutError("synthetic response loss after commit")
        return result

    async def capture_processed(action, _store):
        processed.append(action["attempt_id"])

    monkeypatch.setattr(
        action_store,
        "record_action",
        lose_first_commit_result,
    )
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    body = _action_body(action_ts="1.000001")
    first_timestamp = int(time.time())
    headers = _signed_headers(configured, body, timestamp=first_timestamp)

    first = client.post("/slack/actions", content=body, headers=headers)
    retry = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(
            configured,
            body,
            timestamp=first_timestamp + 1,
        ),
    )

    assert first.status_code == 503
    assert retry.status_code == 200
    assert persistence_attempts == 2
    assert scheduled == []
    assert action_store.get(1)["status"] == "pending"

    recovered = asyncio.run(
        action_module.process_due_office_manager_actions(
            store=action_store,
            processor=capture_processed,
        )
    )

    assert recovered == 1
    assert processed == [action_store.get(1)["attempt_id"]]
    assert action_store.get(1)["status"] == "completed"


def test_existing_generic_receipt_without_outbox_still_records_action(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    scheduled = []
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    body = _action_body()
    headers = _signed_headers(configured, body)

    assert verify_and_claim_slack_request(
        signing_secret=configured.SLACK_SIGNING_SECRET,
        raw_body=body,
        headers=headers,
        receipt_db_path=configured.SLACK_RECEIPTS_DB_PATH,
        max_age_seconds=configured.SLACK_REQUEST_MAX_AGE_SECONDS,
        receipt_ttl_seconds=configured.SLACK_RECEIPT_TTL_SECONDS,
    ) is False

    response = TestClient(main_module.app).post(
        "/slack/actions",
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert len(scheduled) == 1
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    persisted = action_store.get(1)
    assert persisted is not None
    assert persisted["status"] == "pending"
    scheduled[0].close()


def test_concurrent_exact_retries_schedule_one_durable_action(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    scheduled = []
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    body = _action_body()
    headers = _signed_headers(configured, body)

    assert verify_and_claim_slack_request(
        signing_secret=configured.SLACK_SIGNING_SECRET,
        raw_body=body,
        headers=headers,
        receipt_db_path=configured.SLACK_RECEIPTS_DB_PATH,
        max_age_seconds=configured.SLACK_REQUEST_MAX_AGE_SECONDS,
        receipt_ttl_seconds=configured.SLACK_RECEIPT_TTL_SECONDS,
    ) is False

    def deliver_retry(_):
        return TestClient(main_module.app).post(
            "/slack/actions",
            content=body,
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(deliver_retry, range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert len(scheduled) == 1
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    assert action_store.get(1)["status"] == "pending"
    assert action_store.get(2) is None
    scheduled[0].close()


def test_distinct_signed_clicks_for_same_day_create_distinct_attempts(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    scheduled = []
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    first_body = _action_body(action_ts="1.000001")
    second_body = _action_body(action_ts="2.000002")
    client = TestClient(main_module.app)

    first = client.post(
        "/slack/actions",
        content=first_body,
        headers=_signed_headers(configured, first_body),
    )
    second = client.post(
        "/slack/actions",
        content=second_body,
        headers=_signed_headers(configured, second_body),
    )

    assert first.status_code == second.status_code == 200
    assert len(scheduled) == 2
    store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    first_record = store.get(1)
    second_record = store.get(2)
    assert first_record["attempt_id"] != second_record["attempt_id"]
    assert first_record["feedback_client_msg_id"] is None
    assert second_record["feedback_client_msg_id"] is None
    assert action_module.build_office_manager_feedback_client_msg_id(
        first_record["attempt_id"]
    ) != action_module.build_office_manager_feedback_client_msg_id(
        second_record["attempt_id"]
    )
    for coro in scheduled:
        coro.close()


def test_admin_surface_ignores_office_manager_action(tmp_path, monkeypatch):
    configured = _settings(
        tmp_path,
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN",
        OFFICE_MANAGER_ACTIONS_ENABLED=False,
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


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        ({"MLAI_BACKEND_URL": ""}, "MLAI_BACKEND_URL"),
        ({"ROO_API_KEY": ""}, "ROO_API_KEY"),
        (
            {"ROO_SURFACE": "admin", "ROO_ALLOWED_CHANNEL_IDS": "GADMIN"},
            "only on Public Roo",
        ),
        ({"TIMEZONE": "UTC"}, "Australia/Melbourne"),
        (
            {"MLAI_BACKEND_URL": "https://secret@backend.test?token=value"},
            "non-secret HTTP",
        ),
        (
            {"MLAI_BACKEND_URL": "https://backend.test/api/v1"},
            "root origin",
        ),
    ),
)
def test_office_manager_actions_fail_closed_without_dedicated_configuration(
    tmp_path,
    overrides,
    expected_error,
):
    with pytest.raises(ValueError, match=expected_error):
        _settings(tmp_path, **overrides)


def test_office_manager_kill_switch_acknowledges_without_persisting(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path, OFFICE_MANAGER_ACTIONS_ENABLED=False)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    scheduled = []
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 3))
    body = _action_body()
    headers = _signed_headers(configured, body)

    client = TestClient(main_module.app)
    response = client.post(
        "/slack/actions",
        content=body,
        headers=headers,
    )
    retry = client.post("/slack/actions", content=body, headers=headers)

    assert response.status_code == retry.status_code == 200
    assert len(scheduled) == 1
    action_store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    assert action_store.get(1) is None
    scheduled[0].close()


def test_public_readiness_exposes_non_secret_office_manager_contract(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    main_module.app.state.startup_complete = True

    payload = asyncio.run(main_module.readiness_check())

    assert payload["office_manager"] == {
        "actions_enabled": True,
        "backend_base_url": "https://backend.test",
        "claim_path": "/api/v1/points/coworking/office-manager/claim/",
        "timezone": "Australia/Melbourne",
    }
    assert "roo_api_key" not in json.dumps(payload).lower()
    assert configured.ROO_API_KEY not in json.dumps(payload)


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
    headers = _signed_headers(configured, body)
    response = client.post(
        "/slack/actions",
        content=body,
        headers=headers,
    )
    retry = client.post("/slack/actions", content=body, headers=headers)

    assert response.status_code == retry.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert "no longer valid" in feedback[0]["text"]


def test_exact_accepted_delivery_crossing_midnight_resumes_durable_attempt(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    current_date = [date(2026, 8, 3)]
    scheduled = []
    processed = []

    async def capture_action(action, store):
        processed.append((action["attempt_id"], action["booking_date"]))

    monkeypatch.setattr(main_module, "get_current_date", lambda: current_date[0])
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    monkeypatch.setattr(
        main_module,
        "_process_office_manager_action_record",
        capture_action,
    )
    body = _action_body(action_ts="1.000001")
    first_timestamp = int(time.time())
    headers = _signed_headers(configured, body, timestamp=first_timestamp)
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=headers)
    current_date[0] = date(2026, 8, 4)
    retry = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(
            configured,
            body,
            timestamp=first_timestamp + 1,
        ),
    )

    assert first.status_code == retry.status_code == 200
    assert len(scheduled) == 2
    scheduled[0].close()
    asyncio.run(scheduled[1])
    store = action_module.get_office_manager_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    record = store.get(1)
    assert processed == [(record["attempt_id"], "2026-08-03")]
    assert record["status"] == "completed"
    assert store.get(2) is None


@pytest.mark.asyncio
async def test_claim_success_reports_zero_charge_and_refund_privately(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            assert slack_user_id == "UVERIFIED"
            assert booking_date == "2026-08-03"
            return _successful_claim_payload(points_refunded=8)

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
            return _successful_claim_payload(status="already_claimed_by_you")

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
async def test_unexpected_success_response_is_retried_without_winner_feedback(
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

    with pytest.raises(
        main_module.OfficeManagerClaimUncertainError,
        match="response_invalid",
    ):
        await main_module._claim_office_manager_from_action(
            user_id="UVERIFIED",
            channel_id="CCOWORK",
            booking_date="2026-08-03",
        )

    assert feedback == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    (
        _successful_claim_payload(date="2026-08-04"),
        _successful_claim_payload(office_manager_slack_user_id="UOTHER"),
        _successful_claim_payload(assignment_id=True),
        _successful_claim_payload(
            booking={
                "date": "2026-08-03",
                "status": "booked",
                "points_cost": 0,
                "booking_source": "points",
            }
        ),
        _successful_claim_payload(points_charged=8),
        _successful_claim_payload(office_manager_free_day=False),
    ),
)
async def test_mismatched_success_contract_is_retried_without_feedback(
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

    with pytest.raises(
        main_module.OfficeManagerClaimUncertainError,
        match="response_invalid",
    ):
        await main_module._claim_office_manager_from_action(
            user_id="UVERIFIED",
            channel_id="CCOWORK",
            booking_date="2026-08-03",
        )

    assert feedback == []


@pytest.mark.asyncio
async def test_success_without_matching_attempt_id_stays_pending(
    tmp_path,
    monkeypatch,
):
    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            return _successful_claim_payload()

    delivered = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: (
            delivered.append(text)
            or {"ok": True}
        ),
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )

    assert store.get(action["id"])["status"] == "pending"
    assert delivered and "still confirming" in delivered[0]


@pytest.mark.asyncio
async def test_invalid_refund_value_keeps_claim_pending_for_recovery(monkeypatch):
    class FakeClient:
        async def claim_office_manager_day(self, slack_user_id, booking_date):
            return _successful_claim_payload(points_refunded="not-a-number")

    feedback = []

    async def capture_feedback(**kwargs):
        feedback.append(kwargs)

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )

    with pytest.raises(
        main_module.OfficeManagerClaimUncertainError,
        match="response_invalid",
    ):
        await main_module._claim_office_manager_from_action(
            user_id="UVERIFIED",
            channel_id="CCOWORK",
            booking_date="2026-08-03",
        )

    assert feedback == []


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
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
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
@pytest.mark.parametrize(
    ("code", "extra_payload", "expected"),
    (
        (
            "attempt_superseded",
            {"status": "superseded"},
            "did not recreate the assignment",
        ),
        (
            "attempt_payload_conflict",
            {},
            "No assignment was changed",
        ),
    ),
)
async def test_attempt_supersession_errors_are_terminal_and_private(
    tmp_path,
    monkeypatch,
    code,
    extra_payload,
    expected,
):
    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(
                409,
                request=request,
                json={
                    "code": code,
                    "attempt_id": attempt_id,
                    **extra_payload,
                },
            )
            raise httpx.HTTPStatusError(
                "terminal attempt state",
                request=request,
                response=response,
            )

    delivered = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: (
            delivered.append(text)
            or {"ok": True}
        ),
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )

    assert store.get(action["id"])["status"] == "completed"
    assert expected in delivered[0]


@pytest.mark.asyncio
async def test_mismatched_error_attempt_id_stays_pending_for_reconciliation(
    tmp_path,
    monkeypatch,
):
    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(
                409,
                request=request,
                json={
                    "code": "attempt_superseded",
                    "status": "superseded",
                    "attempt_id": "22222222-2222-4222-8222-222222222222",
                },
            )
            raise httpx.HTTPStatusError(
                "wrong attempt",
                request=request,
                response=response,
            )

    delivered = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: (
            delivered.append(text)
            or {"ok": True}
        ),
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )

    assert store.get(action["id"])["status"] == "pending"
    assert delivered and "still confirming" in delivered[0]


@pytest.mark.asyncio
async def test_unknown_backend_failure_is_reported_and_raised_for_durable_retry(
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

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    with pytest.raises(
        main_module.OfficeManagerClaimUncertainError,
        match="claim_result_uncertain",
    ):
        await main_module._claim_office_manager_from_action(
            user_id="UVERIFIED",
            channel_id="CCOWORK",
            booking_date="2026-08-03",
        )


@pytest.mark.asyncio
async def test_cancellation_while_reporting_uncertain_claim_is_not_masked(
    tmp_path,
    monkeypatch,
):
    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError(
                "claim result unknown",
                request=request,
                response=response,
            )

    async def cancelled_feedback(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        cancelled_feedback,
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    leased = store.reserve(action["id"], owner="cancelling-worker")
    assert leased is not None

    with pytest.raises(asyncio.CancelledError):
        await main_module._process_office_manager_action_record(
            leased,
            store,
        )
    assert store.get(action["id"])["uncertainty_notice_attempted_at"] is not None


@pytest.mark.asyncio
async def test_transient_claim_failure_retries_then_recovers_idempotent_result(
    tmp_path,
    monkeypatch,
):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    calls = []

    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            calls.append((slack_user_id, booking_date))
            if len(calls) == 1:
                request = httpx.Request("POST", "https://backend.test/claim")
                response = httpx.Response(502, request=request)
                raise httpx.HTTPStatusError(
                    "response lost",
                    request=request,
                    response=response,
                )
            return _successful_claim_payload(
                status="already_claimed_by_you",
                attempt_id=attempt_id,
            )

    delivered = []

    async def capture_feedback(**kwargs):
        delivered.append(kwargs["text"])

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    pending = store.get(action["id"])
    assert pending["status"] == "pending"
    assert pending["next_attempt_at"] == 1_005.0

    current_time[0] = 1_005.0
    assert await action_module.process_due_office_manager_actions(
        store=store,
        processor=main_module._process_office_manager_action_record,
    ) == 1
    assert store.get(action["id"])["status"] == "completed"
    assert len(calls) == 2
    assert "still confirming" in delivered[0]
    assert "already today's Office Manager" in delivered[1]


@pytest.mark.asyncio
async def test_commit_response_loss_restart_and_rollover_reuse_attempt(
    tmp_path,
    monkeypatch,
):
    current_time = [8_000.0]
    current_date = [date(2026, 8, 3)]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    monkeypatch.setattr(main_module, "get_current_date", lambda: current_date[0])
    backend_attempts = []
    delivered = []

    class CommitUncertainClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            backend_attempts.append(attempt_id)
            if len(backend_attempts) == 1:
                request = httpx.Request("POST", "https://backend.test/claim")
                response = httpx.Response(502, request=request)
                raise httpx.HTTPStatusError(
                    "backend committed but response was lost",
                    request=request,
                    response=response,
                )
            return _successful_claim_payload(
                status="already_claimed_by_you",
                attempt_id=attempt_id,
            )

    def capture_dm(user_id, text, **kwargs):
        delivered.append((user_id, text, kwargs["client_msg_id"]))
        return {"ok": True}

    monkeypatch.setattr(backend_module, "MLAIBackendClient", CommitUncertainClient)
    monkeypatch.setattr(main_module, "send_dm", capture_dm)
    database_path = tmp_path / "actions.db"
    initial_store = action_module.OfficeManagerActionStore(database_path)
    action, _ = initial_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
        request_fingerprint="c" * 64,
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=initial_store,
        processor=main_module._process_office_manager_action_record,
    )
    assert initial_store.get(action["id"])["status"] == "pending"

    current_time[0] = 8_005.0
    current_date[0] = date(2026, 8, 4)
    restarted_store = action_module.OfficeManagerActionStore(database_path)
    assert await action_module.process_due_office_manager_actions(
        store=restarted_store,
        processor=main_module._process_office_manager_action_record,
    ) == 1

    assert backend_attempts == [action["attempt_id"], action["attempt_id"]]
    assert restarted_store.get(action["id"])["status"] == "completed"
    assert len(delivered) == 2
    assert "still confirming" in delivered[0][1]
    assert "request for 2026-08-03" in delivered[1][1]
    assert "historical confirmation only" in delivered[1][1]
    assert "no action is needed now" in delivered[1][1]
    assert "today's Office Manager" not in delivered[1][1]


@pytest.mark.asyncio
async def test_repeated_transient_failures_send_one_uncertainty_notice(
    tmp_path,
    monkeypatch,
):
    current_time = [3_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    backend_calls = []
    delivered = []

    class FailingClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            backend_calls.append((slack_user_id, booking_date, current_time[0]))
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError(
                "backend unavailable",
                request=request,
                response=response,
            )

    async def capture_feedback(**kwargs):
        delivered.append((current_time[0], kwargs["text"]))

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FailingClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        capture_feedback,
    )
    database_path = tmp_path / "actions.db"
    store = action_module.OfficeManagerActionStore(database_path)
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    first_attempt = store.get(action["id"])
    assert first_attempt["uncertainty_notice_attempted_at"] == 3_000.0

    # A restarted process reads the durable notice state and retries silently.
    recovered_store = action_module.OfficeManagerActionStore(database_path)
    for retry_time in (3_005.0, 3_015.0, 3_035.0):
        current_time[0] = retry_time
        assert await action_module.process_due_office_manager_actions(
            store=recovered_store,
            processor=main_module._process_office_manager_action_record,
        ) == 1

    assert len(backend_calls) == 4
    assert len(delivered) == 1
    assert delivered[0][0] == 3_000.0
    assert "still confirming" in delivered[0][1]
    pending = recovered_store.get(action["id"])
    assert pending["status"] == "pending"
    assert pending["attempt_count"] == 4
    assert pending["next_attempt_at"] == 3_075.0


@pytest.mark.asyncio
async def test_failed_uncertainty_notice_is_not_repeated(
    tmp_path,
    monkeypatch,
):
    current_time = [4_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    backend_calls = []
    notice_attempts = []

    class FailingClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            backend_calls.append(current_time[0])
            request = httpx.Request("POST", "https://backend.test/claim")
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError(
                "backend unavailable",
                request=request,
                response=response,
            )

    async def fail_feedback(**kwargs):
        notice_attempts.append(current_time[0])
        raise RuntimeError("Slack unavailable")

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FailingClient)
    monkeypatch.setattr(
        main_module,
        "_send_office_manager_private_feedback",
        fail_feedback,
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    current_time[0] = 4_005.0
    await action_module.process_due_office_manager_actions(
        store=store,
        processor=main_module._process_office_manager_action_record,
    )

    assert backend_calls == [4_000.0, 4_005.0]
    assert notice_attempts == [4_000.0]
    assert store.get(action["id"])["status"] == "pending"


def test_existing_outbox_schema_adds_uncertainty_notice_column(tmp_path):
    database_path = tmp_path / "legacy-actions.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE office_manager_action_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                slack_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                locked_by TEXT,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )

    store = action_module.OfficeManagerActionStore(database_path)
    action, should_process = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    assert should_process is True
    assert action["uncertainty_notice_attempted_at"] is None
    assert action["feedback_text"] is None
    assert action["feedback_client_msg_id"] is None
    assert action["feedback_prepared_at"] is None
    assert action["action_occurrence_key"] is None


def test_concurrent_processes_serialize_legacy_schema_upgrade(tmp_path):
    database_path = tmp_path / "legacy-actions.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE office_manager_action_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                slack_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                locked_by TEXT,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )

    stores = [
        action_module.OfficeManagerActionStore(database_path),
        action_module.OfficeManagerActionStore(database_path),
    ]

    def upgrade(index):
        return stores[index].record_action(
            slack_user_id=f"UCONCURRENT{index}",
            channel_id="CCOWORK",
            booking_date="2026-08-03",
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        upgraded = list(executor.map(upgrade, range(2)))

    assert len(upgraded) == 2
    assert all(row["feedback_text"] is None for row in upgraded)
    assert all(row["feedback_client_msg_id"] is None for row in upgraded)
    assert all(row["action_occurrence_key"] is None for row in upgraded)


@pytest.mark.asyncio
async def test_terminal_feedback_is_staged_and_retried_with_same_message_id(
    tmp_path,
    monkeypatch,
):
    current_time = [5_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    backend_calls = []
    slack_attempts = []

    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            backend_calls.append((slack_user_id, booking_date))
            return _successful_claim_payload(attempt_id=attempt_id)

    def response_lost_then_duplicate_safe_success(user_id, text, **kwargs):
        slack_attempts.append((user_id, text, kwargs["client_msg_id"]))
        if len(slack_attempts) == 1:
            raise RuntimeError("accepted response lost")
        return {"ok": True}

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        response_lost_then_duplicate_safe_success,
    )
    database_path = tmp_path / "actions.db"
    store = action_module.OfficeManagerActionStore(database_path)
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    pending = store.get(action["id"])
    assert pending["status"] == "pending"
    assert pending["feedback_text"]
    assert pending["feedback_client_msg_id"] == slack_attempts[0][2]

    current_time[0] = 5_005.0
    monkeypatch.setattr(main_module, "get_current_date", lambda: date(2026, 8, 4))
    recovered_store = action_module.OfficeManagerActionStore(database_path)
    assert await action_module.process_due_office_manager_actions(
        store=recovered_store,
        processor=main_module._process_office_manager_action_record,
    ) == 1

    completed = recovered_store.get(action["id"])
    assert completed["status"] == "completed"
    assert completed["feedback_text"] is None
    assert completed["feedback_client_msg_id"] is None
    assert len(backend_calls) == 1
    assert [attempt[2] for attempt in slack_attempts] == [
        slack_attempts[0][2],
        slack_attempts[0][2],
    ]


@pytest.mark.asyncio
async def test_expired_worker_cannot_emit_terminal_private_feedback(
    tmp_path,
    monkeypatch,
):
    current_time = [6_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    stale = store.reserve(action["id"], owner="stale", lease_seconds=1)
    assert stale is not None
    current_time[0] += 2
    assert store.reserve(action["id"], owner="replacement") is not None
    slack_attempts = []
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda *args, **kwargs: slack_attempts.append((args, kwargs)),
    )

    with pytest.raises(action_module.OfficeManagerActionLeaseLostError):
        await main_module._send_office_manager_private_feedback(
            channel_id="CCOWORK",
            user_id="UVERIFIED",
            text="stale result",
            action=stale,
            store=store,
        )

    assert slack_attempts == []
    current = store.get(action["id"])
    assert current["locked_by"] == "replacement"
    assert current["feedback_text"] is None


@pytest.mark.asyncio
async def test_lease_renew_exception_completes_once_when_original_lease_is_live(
    tmp_path,
    monkeypatch,
):
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    renew_failures = []
    deliveries = []

    def fail_renew(*args, **kwargs):
        renew_failures.append((args, kwargs))
        raise sqlite3.OperationalError("synthetic heartbeat storage error")

    async def stage_one_terminal_delivery(record, current_store):
        while not renew_failures:
            await asyncio.sleep(0.001)
        staged = await asyncio.to_thread(
            current_store.stage_feedback,
            int(record["id"]),
            owner=str(record["locked_by"]),
            text="Private terminal result",
            client_msg_id=action_module.build_office_manager_feedback_client_msg_id(
                str(record["attempt_id"])
            ),
        )
        assert staged is not None
        deliveries.append(staged["feedback_client_msg_id"])

    monkeypatch.setattr(action_module, "DEFAULT_PROCESSING_LEASE_SECONDS", 0.03)
    monkeypatch.setattr(store, "renew", fail_renew)

    assert await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=stage_one_terminal_delivery,
    )

    completed = store.get(action["id"])
    assert len(renew_failures) == 1
    assert len(deliveries) == 1
    assert completed["status"] == "completed"
    assert completed["feedback_client_msg_id"] is None


@pytest.mark.asyncio
async def test_private_feedback_failure_keeps_action_pending_until_delivered(
    tmp_path,
    monkeypatch,
):
    current_time = [2_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    backend_calls = []

    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id=None,
        ):
            backend_calls.append((slack_user_id, booking_date))
            status = "claimed" if len(backend_calls) == 1 else "already_claimed_by_you"
            return _successful_claim_payload(status=status, attempt_id=attempt_id)

    dm_succeeds = [False]
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        main_module,
        "post_ephemeral",
        lambda **kwargs: {"ok": False, "error": "not_in_channel"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: {"ok": dm_succeeds[0]},
    )
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    assert store.get(action["id"])["status"] == "pending"

    current_time[0] = 2_005.0
    dm_succeeds[0] = True
    await action_module.process_due_office_manager_actions(
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    assert store.get(action["id"])["status"] == "completed"
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_backend_disable_then_reenable_keeps_attempt_recoverable(
    tmp_path,
    monkeypatch,
):
    current_time = [9_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    backend_calls = []

    class FakeClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            backend_calls.append(attempt_id)
            if len(backend_calls) == 1:
                request = httpx.Request("POST", "https://backend.test/claim")
                response = httpx.Response(
                    503,
                    request=request,
                    json={"code": "feature_disabled"},
                )
                raise httpx.HTTPStatusError(
                    "disabled",
                    request=request,
                    response=response,
                )
            return _successful_claim_payload(
                status="claimed",
                attempt_id=attempt_id,
            )

    delivered = []

    def capture_dm(user_id, text, **kwargs):
        delivered.append(text)
        return {"ok": True}

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)
    monkeypatch.setattr(main_module, "send_dm", capture_dm)
    database_path = tmp_path / "actions.db"
    enabled_store = action_module.OfficeManagerActionStore(database_path)
    action, _ = enabled_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )

    await action_module.process_office_manager_action(
        action["id"],
        store=enabled_store,
        processor=main_module._process_office_manager_action_record,
    )
    assert enabled_store.get(action["id"])["status"] == "pending"

    # A deployment can disable the Roo worker here. The durable row remains;
    # a later enabled process resumes the exact same backend attempt.
    current_time[0] = 9_005.0
    reenabled_store = action_module.OfficeManagerActionStore(database_path)
    assert await action_module.process_due_office_manager_actions(
        store=reenabled_store,
        processor=main_module._process_office_manager_action_record,
    ) == 1

    assert backend_calls == [action["attempt_id"], action["attempt_id"]]
    assert reenabled_store.get(action["id"])["status"] == "completed"
    assert "still confirming" in delivered[0]
    assert "today's Office Manager" in delivered[1]


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
        lambda user_id, text, **kwargs: (
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
        lambda user_id, text, **kwargs: (
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

    def fake_dm(user_id, text, **kwargs):
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
async def test_office_manager_delivery_redacts_identifiers_and_exception_text(
    monkeypatch,
    capsys,
):
    user_sentinel = "U-SECRET-RAW-ID"
    channel_sentinel = "C-SECRET-RAW-ID"
    exception_sentinel = "TOKEN-LIKE\nFORGED_LOG_SUFFIX=true"

    class TaintedSlackClient:
        def chat_postEphemeral(self, **kwargs):
            raise RuntimeError(
                f"{exception_sentinel} {kwargs['channel']} {kwargs['user']}"
            )

        def conversations_open(self, **kwargs):
            raise RuntimeError(f"{exception_sentinel} {kwargs['users']}")

    monkeypatch.setattr(slack_client_module, "_slack_client", TaintedSlackClient())

    with pytest.raises(RuntimeError, match="office_manager_private_feedback_failed"):
        await main_module._send_office_manager_private_feedback(
            channel_id=channel_sentinel,
            user_id=user_sentinel,
            text="private result",
        )

    output = capsys.readouterr().out
    assert user_sentinel not in output
    assert channel_sentinel not in output
    assert exception_sentinel not in output
    assert "FORGED_LOG_SUFFIX" not in output
    assert "error_type=RuntimeError" in output


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
    action, should_process = original_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True
    recovered_store = action_module.OfficeManagerActionStore(database_path)
    processed = []

    async def processor(record, _store):
        processed.append(record["attempt_id"])

    count = await action_module.process_due_office_manager_actions(
        store=recovered_store,
        processor=processor,
    )

    assert count == 1
    assert processed == [action["attempt_id"]]
    assert recovered_store.get(action["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_prior_date_action_survives_disabled_interval_and_recovers(tmp_path):
    database_path = tmp_path / "actions.db"
    disabled_store = action_module.OfficeManagerActionStore(database_path)
    action, should_process = disabled_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True
    assert disabled_store.get(action["id"])["status"] == "pending"

    # No worker runs while the feature is disabled. A later process, after the
    # local date has rolled over, must still discover the durable record.
    reenabled_store = action_module.OfficeManagerActionStore(database_path)
    processed_dates = []

    async def processor(record, _store):
        processed_dates.append(record["booking_date"])

    assert await action_module.process_due_office_manager_actions(
        store=reenabled_store,
        processor=processor,
    ) == 1
    assert processed_dates == ["2026-08-03"]
    assert reenabled_store.get(action["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_housekeeping_runs_without_action_processing_enabled():
    class FakeStore:
        def __init__(self):
            self.prune_calls = 0

        def prune_completed(self):
            self.prune_calls += 1

    store = FakeStore()
    task = asyncio.create_task(
        action_module.office_manager_action_housekeeping_loop(
            store=store,
            poll_seconds=0.01,
        )
    )
    try:
        for _ in range(50):
            if store.prune_calls:
                break
            await asyncio.sleep(0.01)
        assert store.prune_calls >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_prior_date_accepted_action_recovers_backend_result_privately(
    tmp_path,
    monkeypatch,
):
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, _ = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    monkeypatch.setattr(
        main_module,
        "get_current_date",
        lambda: date(2026, 8, 4),
    )

    backend_calls = []

    class RecoveringClient:
        async def claim_office_manager_day(
            self,
            slack_user_id,
            booking_date,
            attempt_id,
        ):
            backend_calls.append((slack_user_id, booking_date, attempt_id))
            return _successful_claim_payload(
                status="already_claimed_by_you",
                attempt_id=attempt_id,
            )

    delivered = []
    monkeypatch.setattr(backend_module, "MLAIBackendClient", RecoveringClient)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: (
            delivered.append((user_id, text, kwargs["client_msg_id"]))
            or {"ok": True}
        ),
    )

    assert await action_module.process_office_manager_action(
        action["id"],
        store=store,
        processor=main_module._process_office_manager_action_record,
    )
    assert store.get(action["id"])["status"] == "completed"
    assert backend_calls == [
        ("UVERIFIED", "2026-08-03", action["attempt_id"]),
    ]
    assert delivered[0][0] == "UVERIFIED"
    assert "request for 2026-08-03" in delivered[0][1]
    assert "historical confirmation only" in delivered[0][1]
    assert "no action is needed now" in delivered[0][1]
    assert "today's Office Manager" not in delivered[0][1]


def test_exact_delivery_reuses_attempt_but_new_click_gets_new_lifecycle(tmp_path):
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    first, first_created = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
        request_fingerprint="a" * 64,
    )
    replay, replay_created = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
        request_fingerprint="a" * 64,
    )
    later_click, later_created = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
        request_fingerprint="b" * 64,
    )

    assert first_created is True
    assert replay_created is False
    assert later_created is True
    assert replay["id"] == first["id"]
    assert replay["attempt_id"] == first["attempt_id"]
    assert later_click["id"] != first["id"]
    assert later_click["attempt_id"] != first["attempt_id"]
    assert str(UUID(first["attempt_id"])) == first["attempt_id"]
    assert str(UUID(later_click["attempt_id"])) == later_click["attempt_id"]
    assert action_module.build_office_manager_feedback_client_msg_id(
        first["attempt_id"]
    ) != action_module.build_office_manager_feedback_client_msg_id(
        later_click["attempt_id"]
    )


def test_retry_delay_is_exponential_and_capped():
    assert [action_module._retry_delay(attempt) for attempt in range(1, 9)] == [
        5.0,
        10.0,
        20.0,
        40.0,
        80.0,
        160.0,
        300.0,
        300.0,
    ]


@pytest.mark.asyncio
async def test_due_actions_are_leased_only_immediately_before_processing(tmp_path):
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    first, _ = store.record_action(
        slack_user_id="UFIRST",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    second, _ = store.record_action(
        slack_user_id="USECOND",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    observed_second_status = []

    async def processor(record, _store):
        if record["id"] == first["id"]:
            observed_second_status.append(store.get(second["id"])["status"])

    count = await action_module.process_due_office_manager_actions(
        store=store,
        processor=processor,
        limit=2,
    )

    assert count == 2
    assert observed_second_status == ["pending"]
    assert store.get(first["id"])["status"] == "completed"
    assert store.get(second["id"])["status"] == "completed"


def test_old_completed_actions_are_pruned_by_worker_without_new_writes(
    tmp_path,
    monkeypatch,
):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    old, _ = store.record_action(
        slack_user_id="UOLD",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    leased = store.reserve(old["id"], owner="test")
    assert leased is not None
    assert store.mark_completed(old["id"], owner="test")

    current_time[0] += action_module.COMPLETED_RETENTION_SECONDS + 1
    assert store.claim_due() == []

    assert store.get(old["id"]) is None


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered(tmp_path, monkeypatch):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.OfficeManagerActionStore(tmp_path / "actions.db")
    action, should_process = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True
    assert store.reserve(action["id"], lease_seconds=60) is not None

    current_time[0] += 61
    processed = []

    async def processor(record, _store):
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
    action, should_process = first_store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True

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
    action, should_process = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True
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
    action, should_process = store.record_action(
        slack_user_id="UVERIFIED",
        channel_id="CCOWORK",
        booking_date="2026-08-03",
    )
    assert should_process is True
    started = asyncio.Event()

    async def processor(record, _store):
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
