import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo import meeting_room_actions as action_module
from roo import slack_action_tasks
from roo.clients.mlai_backend import MLAIBackendUnavailableError
from roo.config import Settings, get_settings
from roo.meeting_room_booking import BOOK_ACTION_ID, build_booking_action_value
from roo.slack_security import get_slack_receipt_store


MELBOURNE = ZoneInfo("Australia/Melbourne")


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "SLACK_RECEIPTS_DB_PATH": str(tmp_path / "roo-state.db"),
        "OPENAI_API_KEY": "synthetic-openai-key",
        "MLAI_BACKEND_URL": "https://backend.test",
        "ROO_API_KEY": "roo-test-key",
        "MEETING_ROOM_BOOKING_ENABLED": True,
    }
    values.update(overrides)
    return Settings(**values)


def _booking_action_value():
    starts_at = (
        datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    return build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        target_slack_user_id="UTARGET",
    )


def _record(store, action_value=None):
    return store.record_action(
        action_id=BOOK_ACTION_ID,
        action_value=action_value or _booking_action_value(),
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )


def _admin_booking_response(action_value, *, created):
    payload = json.loads(action_value)
    return {
        "created": created,
        "already_booked": not created,
        "points_cost": 2,
        "remaining_balance": 8,
        "admin_booking": True,
        "booked_for_slack_user_id": "UTARGET",
        "booking": {
            "id": "99e4d8b2-d48c-4f51-a230-b8656c9a3127",
            "starts_at": payload["starts_at"],
            "ends_at": payload["ends_at"],
            "points_cost": 2,
            "room": {"name": "Meeting Room"},
        },
    }


def _signature(secret, timestamp, body):
    digest = hmac.new(
        secret.encode(),
        b"v0:" + str(timestamp).encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


@pytest.fixture(autouse=True)
def reset_action_state():
    slack_action_tasks._tasks.clear()
    action_module.get_meeting_room_action_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()
    yield
    for task in list(slack_action_tasks._tasks):
        task.cancel()
    slack_action_tasks._tasks.clear()
    action_module.get_meeting_room_action_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pending_action_is_recovered_after_restart(tmp_path):
    database_path = tmp_path / "actions.db"
    original_store = action_module.MeetingRoomActionStore(database_path)
    action = _record(original_store)
    recovered_store = action_module.MeetingRoomActionStore(database_path)
    processed = []

    async def processor(record):
        processed.append(record["action_key"])

    count = await action_module.process_due_meeting_room_actions(
        store=recovered_store,
        processor=processor,
    )

    assert count == 1
    assert processed == [action["action_key"]]
    assert recovered_store.get(action["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_only_one_worker_can_process_duplicate_action(tmp_path):
    database_path = tmp_path / "actions.db"
    first_store = action_module.MeetingRoomActionStore(database_path)
    second_store = action_module.MeetingRoomActionStore(database_path)
    action_value = _booking_action_value()
    first = _record(first_store, action_value)
    second = _record(second_store, action_value)
    processed = []

    async def processor(record):
        processed.append(record["locked_by"])
        await asyncio.sleep(0.01)

    results = await asyncio.gather(
        action_module.process_meeting_room_action(
            first["id"], store=first_store, processor=processor
        ),
        action_module.process_meeting_room_action(
            second["id"], store=second_store, processor=processor
        ),
    )

    assert sum(results) == 1
    assert len(processed) == 1
    assert first_store.get(first["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered(tmp_path, monkeypatch):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action = _record(store)
    assert store.reserve(action["id"], owner="stale", lease_seconds=10) is not None
    current_time[0] += 11
    processed = []

    async def processor(record):
        processed.append(record["attempt_count"])

    count = await action_module.process_due_meeting_room_actions(
        store=store,
        processor=processor,
    )

    assert count == 1
    assert processed == [2]
    assert store.get(action["id"])["status"] == "completed"


def test_expired_worker_cannot_overwrite_replacement_state(tmp_path, monkeypatch):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action = _record(store)
    assert store.reserve(action["id"], owner="stale", lease_seconds=1) is not None
    current_time[0] += 2
    assert store.reserve(action["id"], owner="replacement") is not None

    assert store.mark_completed(action["id"], owner="stale") is False
    assert store.release(
        action["id"],
        owner="stale",
        error="late_failure",
        delay_seconds=0,
    ) is False
    current = store.get(action["id"])
    assert current["status"] == "processing"
    assert current["locked_by"] == "replacement"


def test_completed_action_is_not_reopened_by_another_click(tmp_path):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action_value = _booking_action_value()
    action = _record(store, action_value)
    reserved = store.reserve(action["id"], owner="worker")
    assert reserved is not None
    assert store.mark_completed(action["id"], owner="worker") is True

    replay = _record(store, action_value)

    assert replay["id"] == action["id"]
    assert replay["status"] == "completed"
    assert store.reserve(action["id"]) is None


def test_same_request_id_cannot_be_reused_with_different_payload(tmp_path):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action_value = _booking_action_value()
    _record(store, action_value)
    changed = json.loads(action_value)
    changed["ends_at"] = (
        datetime.fromisoformat(changed["ends_at"]) + timedelta(minutes=30)
    ).isoformat()

    with pytest.raises(ValueError, match="conflicts with an earlier request"):
        _record(store, json.dumps(changed))


@pytest.mark.asyncio
async def test_cancelled_worker_releases_action_for_recovery(tmp_path):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action = _record(store)
    started = asyncio.Event()

    async def processor(record):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        action_module.process_meeting_room_action(
            action["id"], store=store, processor=processor
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.get(action["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_backend_commit_response_loss_recovers_target_notification(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
    request_id = json.loads(action_value)["client_request_id"]
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action = _record(store, action_value)
    backend_calls = []
    target_dms = []
    updates = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            backend_calls.append(kwargs["client_request_id"])
            if len(backend_calls) == 1:
                raise MLAIBackendUnavailableError("response lost after commit")
            return _admin_booking_response(action_value, created=False)

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(action_module, "_retry_delay", lambda attempts: 0)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda *args, **kwargs: target_dms.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(
            chat_update=lambda **kwargs: updates.append(kwargs) or {"ok": True}
        ),
    )

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=main_module._process_meeting_room_action_record,
    )
    assert store.get(action["id"])["status"] == "pending"
    assert target_dms == []

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=main_module._process_meeting_room_action_record,
    )

    assert backend_calls == [request_id, request_id]
    assert store.get(action["id"])["status"] == "completed"
    assert target_dms[0][0][0] == "UTARGET"
    assert target_dms[0][1]["client_msg_id"] == request_id
    assert "was already confirmed" in updates[-1]["text"]


@pytest.mark.asyncio
async def test_target_dm_failure_is_retried_with_stable_client_message_id(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
    request_id = json.loads(action_value)["client_request_id"]
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action = _record(store, action_value)
    backend_calls = []
    dm_attempts = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            backend_calls.append(kwargs["client_request_id"])
            return _admin_booking_response(
                action_value,
                created=len(backend_calls) == 1,
            )

    def send_target_dm(*args, **kwargs):
        dm_attempts.append((args, kwargs))
        return None if len(dm_attempts) == 1 else {"ok": True}

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(action_module, "_retry_delay", lambda attempts: 0)
    monkeypatch.setattr(main_module, "send_dm", send_target_dm)
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: {"ok": True}),
    )

    for _ in range(2):
        await action_module.process_meeting_room_action(
            action["id"],
            store=store,
            processor=main_module._process_meeting_room_action_record,
        )

    assert store.get(action["id"])["status"] == "completed"
    assert backend_calls == [request_id, request_id]
    assert [attempt[1]["client_msg_id"] for attempt in dm_attempts] == [
        request_id,
        request_id,
    ]


@pytest.mark.asyncio
async def test_admin_result_delivery_retry_does_not_repeat_successful_target_dm(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action = _record(store, action_value)
    backend_calls = []
    target_dms = []
    result_deliveries = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            backend_calls.append(kwargs["client_request_id"])
            return _admin_booking_response(
                action_value,
                created=len(backend_calls) == 1,
            )

    async def deliver_result(**kwargs):
        result_deliveries.append(kwargs["outcome"])
        return len(result_deliveries) > 1

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(action_module, "_retry_delay", lambda attempts: 0)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda *args, **kwargs: target_dms.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        main_module,
        "_deliver_meeting_room_action_outcome",
        deliver_result,
    )

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=main_module._process_meeting_room_action_record,
    )
    first_attempt = store.get(action["id"])
    assert first_attempt["status"] == "pending"
    assert first_attempt["target_notified_at"] is not None

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=main_module._process_meeting_room_action_record,
    )

    assert store.get(action["id"])["status"] == "completed"
    assert len(target_dms) == 1
    assert "was already notified privately" in result_deliveries[-1]


def test_duplicate_slack_retry_retries_failed_outbox_persistence(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    action_value = _booking_action_value()
    payload = {
        "type": "block_actions",
        "user": {"id": "UOWNER"},
        "channel": {"id": "DOWNER"},
        "container": {"message_ts": "123.456"},
        "message": {"ts": "123.456"},
        "actions": [{"action_id": BOOK_ACTION_ID, "value": action_value}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = int(time.time())
    headers = {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": _signature(
            configured.SLACK_SIGNING_SECRET,
            timestamp,
            body,
        ),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    calls = []
    scheduled = []

    class Store:
        def record_action(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("temporary disk failure")
            return {"id": 1}

    monkeypatch.setattr(main_module, "get_meeting_room_action_store", lambda path: Store())
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    client = TestClient(main_module.app, raise_server_exceptions=False)

    first = client.post("/slack/actions", content=body, headers=headers)
    second = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 500
    assert second.status_code == 200
    assert len(calls) == 2
    assert len(scheduled) == 1
    scheduled[0].close()


@pytest.mark.asyncio
async def test_shutdown_drain_waits_for_retained_action():
    started = asyncio.Event()
    release = asyncio.Event()

    async def action():
        started.set()
        await release.wait()

    task = slack_action_tasks.start(action())
    await started.wait()
    drain = asyncio.create_task(slack_action_tasks.drain(timeout_seconds=1))
    await asyncio.sleep(0)

    assert not drain.done()
    release.set()
    await drain
    assert task.done()
