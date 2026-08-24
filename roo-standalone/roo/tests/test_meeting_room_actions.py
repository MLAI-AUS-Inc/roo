import asyncio
import hashlib
import hmac
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from roo.meeting_room_booking import (
    BOOK_ACTION_ID,
    CHOOSE_ROOM_ACTION_ID,
    MeetingRoomInputError,
    build_booking_action_value,
    parse_action_value,
    room_selection_prompt,
)
from roo.meeting_room_clarifications import (
    PUBLIC_ROOM_CHOICE_ACTION_ID,
    get_meeting_room_clarification_store,
    public_room_choice_prompt,
)
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
        room_slug="small-meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        target_slack_user_id="UTARGET",
    )


def _room_choice_values(*, now=None):
    starts_at = (
        datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    prompt = room_selection_prompt(
        [
            {"slug": "small-meeting-room", "name": "Small Meeting Room"},
            {"slug": "big-meeting-room", "name": "Big Meeting Room"},
        ],
        owner_slack_user_id="UOWNER",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        now=now,
    )
    return [button["value"] for button in prompt["blocks"][1]["elements"]]


def _public_room_choice_values(database_path):
    clarification_store = get_meeting_room_clarification_store(str(database_path))
    starts_at = datetime(2026, 8, 26, 14, tzinfo=MELBOURNE)
    clarification = clarification_store.record_prompt(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        request_message_ts="111.000",
        owner_user_id="UOWNER",
        starts_at=starts_at.isoformat(),
        ends_at=(starts_at + timedelta(hours=1)).isoformat(),
        available_room_slugs=["small-meeting-room", "big-meeting-room"],
        choice_mode="buttons",
    )
    prompt = public_room_choice_prompt(
        clarification,
        [
            {"slug": "small-meeting-room", "name": "Small Meeting Room"},
            {"slug": "big-meeting-room", "name": "Big Meeting Room"},
        ],
    )
    return clarification, [
        button["value"] for button in prompt["blocks"][1]["elements"]
    ]


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
            "room": {"name": "Small Meeting Room"},
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
    get_meeting_room_clarification_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()
    yield
    for task in list(slack_action_tasks._tasks):
        task.cancel()
    slack_action_tasks._tasks.clear()
    action_module.get_meeting_room_action_store.cache_clear()
    get_meeting_room_clarification_store.cache_clear()
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
async def test_retry_batch_leases_each_action_only_when_processing(tmp_path):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    first = _record(store)
    second = _record(store)
    second_states = []

    async def processor(record):
        if record["id"] == first["id"]:
            second_states.append(store.get(second["id"])["status"])

    count = await action_module.process_due_meeting_room_actions(
        store=store,
        processor=processor,
        limit=2,
    )

    assert count == 2
    assert second_states == ["pending"]
    assert store.get(first["id"])["status"] == "completed"
    assert store.get(second["id"])["status"] == "completed"


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
    assert store.mark_completed(
        action["id"],
        owner="worker",
        outcome="The booking is confirmed.",
    ) is True

    replay = _record(store, action_value)

    assert replay["id"] == action["id"]
    assert replay["status"] == "completed"
    assert replay["final_outcome"] == "The booking is confirmed."
    assert store.reserve(action["id"]) is None


def test_room_choice_is_first_choice_wins_and_duplicate_safe(tmp_path):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    small_value, big_value = _room_choice_values()

    small = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    duplicate = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    with pytest.raises(
        ValueError,
        match=r"already chose the \*Small Meeting Room\*",
    ):
        store.record_action(
            action_id=CHOOSE_ROOM_ACTION_ID,
            action_value=big_value,
            actor_user_id="UOWNER",
            channel_id="DOWNER",
            message_ts="123.456",
        )
    with pytest.raises(ValueError, match="Only the member"):
        store.record_action(
            action_id=CHOOSE_ROOM_ACTION_ID,
            action_value=small_value,
            actor_user_id="UATTACKER",
            channel_id="DOWNER",
            message_ts="123.456",
        )

    assert duplicate["id"] == small["id"]
    assert store.reserve(small["id"], owner="worker") is not None
    assert store.reserve(small["id"], owner="other") is None


def test_concurrent_competing_room_choices_persist_exactly_one_winner(tmp_path):
    database_path = tmp_path / "actions.db"
    stores = [
        action_module.MeetingRoomActionStore(database_path),
        action_module.MeetingRoomActionStore(database_path),
    ]
    values = _room_choice_values()
    barrier = threading.Barrier(2)

    def choose(index):
        barrier.wait()
        try:
            action = stores[index].record_action(
                action_id=CHOOSE_ROOM_ACTION_ID,
                action_value=values[index],
                actor_user_id="UOWNER",
                channel_id="DOWNER",
                message_ts="123.456",
            )
            return ("selected", action["action_value"])
        except action_module.MeetingRoomInputError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, range(2)))

    assert sorted(result[0] for result in results) == [
        "room_already_selected",
        "selected",
    ]
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT action_value FROM meeting_room_action_outbox"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] in {
        json.dumps(
            parse_action_value(value, expected_action=CHOOSE_ROOM_ACTION_ID),
            separators=(",", ":"),
            sort_keys=True,
        )
        for value in values
    }


@pytest.mark.asyncio
async def test_competing_room_click_does_not_overwrite_first_choice(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    small_value, big_value = _room_choice_values()
    first = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    scheduled = []
    posted = []
    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: store,
    )
    monkeypatch.setattr(
        main_module,
        "start_slack_action",
        lambda action: scheduled.append(action),
    )
    monkeypatch.setattr(
        main_module,
        "_deliver_meeting_room_action_outcome",
        lambda **kwargs: pytest.fail(
            "competing choice must not overwrite the first choice card"
        ),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    await main_module._persist_and_start_meeting_room_action(
        settings=configured,
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=big_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    await scheduled[0]

    persisted = store.get(first["id"])
    assert persisted["action_value"] == json.dumps(
        parse_action_value(
            small_value,
            expected_action=CHOOSE_ROOM_ACTION_ID,
        ),
        separators=(",", ":"),
        sort_keys=True,
    )
    assert persisted["status"] == "pending"
    assert posted == [
        {
            "channel": "DOWNER",
            "text": (
                "You already chose the *Small Meeting Room* for this request. "
                "Continue with that confirmation card, or start a new booking "
                "request to choose a different room."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_completed_room_choice_reclick_keeps_card_and_posts_feedback(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    small_value, _ = _room_choice_values()
    action = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    reserved = store.reserve(action["id"], owner="worker")
    assert reserved is not None
    assert store.mark_completed(
        action["id"],
        owner="worker",
        outcome="Confirm your Small Meeting Room booking.",
    )
    scheduled = []
    posted = []
    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: store,
    )
    monkeypatch.setattr(
        main_module,
        "start_slack_action",
        lambda action: scheduled.append(action),
    )
    monkeypatch.setattr(
        main_module,
        "_deliver_meeting_room_action_outcome",
        lambda **kwargs: pytest.fail(
            "completed room choice re-click must not replace the confirmation card"
        ),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    await main_module._persist_and_start_meeting_room_action(
        settings=configured,
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    await scheduled[0]

    assert store.get(action["id"])["status"] == "completed"
    assert posted[0]["channel"] == "DOWNER"
    assert "already chose the *Small Meeting Room*" in posted[0]["text"]


@pytest.mark.asyncio
async def test_duplicate_room_choice_delivery_does_not_repeat_feedback(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    small_value, big_value = _room_choice_values()
    store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: store,
    )
    monkeypatch.setattr(
        main_module,
        "start_slack_action",
        lambda action: pytest.fail("duplicate delivery must not repeat feedback"),
    )

    await main_module._persist_and_start_meeting_room_action(
        settings=configured,
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=big_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
        duplicate_delivery=True,
    )


@pytest.mark.asyncio
async def test_room_choice_rechecks_availability_and_uses_stable_booking_id(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    small_value, _ = _room_choice_values()
    choice = parse_action_value(
        small_value,
        expected_action=CHOOSE_ROOM_ACTION_ID,
    )
    action = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    availability_calls = []
    updates = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def check_meeting_room_availability(self, slack_user_id, **kwargs):
            availability_calls.append((slack_user_id, kwargs))
            return {
                "room": {
                    "slug": "small-meeting-room",
                    "name": "Small Meeting Room",
                },
                "available": True,
                "bookable": True,
                "requested_interval": {
                    "starts_at": kwargs["starts_at"],
                    "ends_at": kwargs["ends_at"],
                },
                "points_cost": 1,
                "remaining_daily_hours": {},
                "busy_intervals": [],
            }

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
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

    terminal = store.get(action["id"])
    confirm_button = updates[0]["blocks"][1]["elements"][0]
    confirmation = parse_action_value(
        confirm_button["value"],
        expected_action=BOOK_ACTION_ID,
    )
    assert terminal["status"] == "completed"
    assert availability_calls[0][0] == "UOWNER"
    assert availability_calls[0][1]["room_slug"] == "small-meeting-room"
    assert confirm_button["action_id"] == BOOK_ACTION_ID
    assert confirmation["room_slug"] == "small-meeting-room"
    assert (
        confirmation["client_request_id"]
        == choice["booking_client_request_id"]
    )


@pytest.mark.asyncio
async def test_room_choice_rejects_mismatched_backend_room(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    small_value, _ = _room_choice_values()
    action = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    updates = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def check_meeting_room_availability(self, slack_user_id, **kwargs):
            return {
                "room": {
                    "slug": "big-meeting-room",
                    "name": "Big Meeting Room",
                },
                "available": True,
                "points_cost": 1,
            }

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
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

    assert store.get(action["id"])["status"] == "completed"
    assert "verify the selected meeting room" in updates[0]["text"]
    assert [block["type"] for block in updates[0]["blocks"]] == ["section"]


@pytest.mark.asyncio
async def test_expired_room_choice_does_not_call_backend(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    expired_at = datetime.now(MELBOURNE) - timedelta(minutes=11)
    small_value, _ = _room_choice_values(now=expired_at)
    action = store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )
    updates = []

    class Client:
        def __init__(self, *args, **kwargs):
            raise AssertionError("expired choice must not call the backend")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
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

    assert store.get(action["id"])["status"] == "completed"
    assert "expired" in updates[0]["text"].lower()


def test_existing_outbox_schema_is_upgraded_without_losing_notification_state(
    tmp_path,
):
    database_path = tmp_path / "legacy-actions.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE meeting_room_action_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_key TEXT NOT NULL UNIQUE,
                action_id TEXT NOT NULL,
                action_value TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                locked_by TEXT,
                last_error TEXT,
                target_notified_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO meeting_room_action_outbox (
                action_key, action_id, action_value, actor_user_id,
                channel_id, message_ts, status, next_attempt_at,
                target_notified_at, created_at, updated_at
            ) VALUES ('legacy', ?, '{}', 'UOWNER', 'DOWNER', '123.456',
                      'pending', 1, 2, 1, 1)
            """,
            (BOOK_ACTION_ID,),
        )

    upgraded = action_module.MeetingRoomActionStore(database_path).get(1)

    assert upgraded["target_notification_state"] == "sent"
    assert upgraded["target_notification_attempted_at"] is None
    assert upgraded["final_outcome"] is None


def test_concurrent_workers_serialize_existing_outbox_schema_upgrade(tmp_path):
    database_path = tmp_path / "legacy-actions.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE meeting_room_action_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_key TEXT NOT NULL UNIQUE,
                action_id TEXT NOT NULL,
                action_value TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                locked_by TEXT,
                last_error TEXT,
                target_notified_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO meeting_room_action_outbox (
                action_key, action_id, action_value, actor_user_id,
                channel_id, message_ts, status, next_attempt_at,
                created_at, updated_at
            ) VALUES ('legacy', ?, '{}', 'UOWNER', 'DOWNER', '123.456',
                      'pending', 1, 1, 1)
            """,
            (BOOK_ACTION_ID,),
        )

    stores = [
        action_module.MeetingRoomActionStore(database_path),
        action_module.MeetingRoomActionStore(database_path),
    ]
    barrier = threading.Barrier(len(stores))

    def upgrade(store):
        barrier.wait()
        return store.get(1)

    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        rows = list(executor.map(upgrade, stores))

    assert [row["target_notification_state"] for row in rows] == [
        "pending",
        "pending",
    ]
    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(meeting_room_action_outbox)"
            ).fetchall()
        }
    assert {
        "target_notification_state",
        "target_notification_attempted_at",
        "final_outcome",
    } <= columns


@pytest.mark.asyncio
async def test_retry_exhaustion_parks_action_in_terminal_state(tmp_path, monkeypatch):
    store = action_module.MeetingRoomActionStore(tmp_path / "actions.db")
    action = _record(store)
    monkeypatch.setattr(action_module, "MAX_ACTION_ATTEMPTS", 1)

    async def failing_processor(record):
        raise RuntimeError("deterministic failure")

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=failing_processor,
    )

    terminal = store.get(action["id"])
    assert terminal["status"] == "failed"
    assert "repeated attempts" in terminal["final_outcome"]
    assert store.claim_due() == []


@pytest.mark.asyncio
async def test_completed_button_reclick_replays_stored_outcome(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action_value = _booking_action_value()
    action = _record(store, action_value)
    reserved = store.reserve(action["id"], owner="worker")
    assert reserved is not None
    assert store.mark_completed(
        action["id"],
        owner="worker",
        outcome="The booking is confirmed.",
    )
    scheduled = []
    deliveries = []

    async def deliver(**kwargs):
        deliveries.append(kwargs)
        return True

    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: store,
    )
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    monkeypatch.setattr(
        main_module,
        "_deliver_meeting_room_action_outcome",
        deliver,
    )

    await main_module._persist_and_start_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="original-card.123",
    )

    assert len(scheduled) == 1
    await scheduled[0]
    assert "already processed" in deliveries[0]["outcome"]
    assert "No additional Roo Points" in deliveries[0]["outcome"]
    assert "The booking is confirmed" in deliveries[0]["outcome"]
    assert store.get(action["id"])["status"] == "completed"


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
    assert target_dms[0][1] == {"raise_on_error": True}
    assert "was already confirmed" in updates[-1]["text"]


@pytest.mark.asyncio
async def test_expired_queued_replay_recovers_committed_admin_booking(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    starts_at = (
        datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="small-meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        target_slack_user_id="UTARGET",
        now=datetime.now(ZoneInfo("UTC")) - timedelta(minutes=11),
    )
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
            return _admin_booking_response(action_value, created=False)

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
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

    assert backend_calls == [request_id]
    assert store.get(action["id"])["status"] == "completed"
    assert target_dms[0][0][0] == "UTARGET"
    assert target_dms[0][1] == {"raise_on_error": True}
    assert "was already confirmed" in updates[-1]["text"]


@pytest.mark.asyncio
async def test_definite_target_dm_failure_is_retried_without_unsupported_slack_id(
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
    assert [attempt[1] for attempt in dm_attempts] == [
        {"raise_on_error": True},
        {"raise_on_error": True},
    ]


@pytest.mark.asyncio
async def test_abandoned_sending_notification_is_not_sent_twice(
    tmp_path,
    monkeypatch,
):
    current_time = [1_000.0]
    monkeypatch.setattr(action_module.time, "time", lambda: current_time[0])
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action = _record(store, action_value)
    first_attempt = store.reserve(action["id"], owner="crashed", lease_seconds=1)
    assert first_attempt is not None
    assert store.begin_target_notification(action["id"], owner="crashed") is True
    current_time[0] += 2
    target_dms = []
    updates = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            return _admin_booking_response(action_value, created=False)

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
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

    terminal = store.get(action["id"])
    assert terminal["status"] == "failed"
    assert terminal["target_notification_state"] == "uncertain"
    assert target_dms == []
    assert "will not send another automatically" in updates[-1]["text"]


@pytest.mark.asyncio
async def test_permanent_target_dm_failure_stops_without_retry(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
    store = action_module.MeetingRoomActionStore(configured.SLACK_RECEIPTS_DB_PATH)
    action = _record(store, action_value)
    backend_calls = []
    dm_attempts = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            backend_calls.append(kwargs["client_request_id"])
            return _admin_booking_response(action_value, created=True)

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda *args, **kwargs: (
            dm_attempts.append((args, kwargs))
            or {"ok": False, "error": "users_not_found"}
        ),
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: {"ok": True}),
    )

    await action_module.process_meeting_room_action(
        action["id"],
        store=store,
        processor=main_module._process_meeting_room_action_record,
    )

    terminal = store.get(action["id"])
    assert terminal["status"] == "failed"
    assert terminal["target_notification_state"] == "failed"
    assert len(backend_calls) == 1
    assert len(dm_attempts) == 1
    assert store.claim_due() == []


@pytest.mark.parametrize(
    "credential_error",
    ("invalid_auth", "token_revoked", "not_authed", "missing_scope"),
)
@pytest.mark.asyncio
async def test_roo_credential_target_dm_failure_retries_within_bounds(
    tmp_path,
    monkeypatch,
    credential_error,
):
    configured = _settings(tmp_path)
    action_value = _booking_action_value()
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
        if len(dm_attempts) == 1:
            return {"ok": False, "error": credential_error}
        return {"ok": True}

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

    terminal = store.get(action["id"])
    assert terminal["status"] == "completed"
    assert terminal["target_notification_state"] == "sent"
    assert len(backend_calls) == 2
    assert len(dm_attempts) == 2


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


def test_public_room_buttons_share_one_durable_first_choice_key(tmp_path):
    database_path = tmp_path / "state.db"
    _, (big_value, small_value) = _public_room_choice_values(database_path)
    store = action_module.MeetingRoomActionStore(database_path)

    first = store.record_action(
        action_id=PUBLIC_ROOM_CHOICE_ACTION_ID,
        action_value=big_value,
        actor_user_id="UOWNER",
        channel_id="CROOMS",
        message_ts="112.000",
    )

    assert first["action_key"].startswith("public_choose:")
    with pytest.raises(MeetingRoomInputError) as raised:
        store.record_action(
            action_id=PUBLIC_ROOM_CHOICE_ACTION_ID,
            action_value=small_value,
            actor_user_id="UOWNER",
            channel_id="CROOMS",
            message_ts="112.000",
        )
    assert raised.value.code == "room_already_selected"
    assert "Big Meeting Room" in raised.value.message


def test_public_button_retry_recovers_failed_outbox_handoff(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    _, (big_value, _) = _public_room_choice_values(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    payload = {
        "type": "block_actions",
        "team": {"id": "TMLAI"},
        "user": {"id": "UOWNER"},
        "channel": {"id": "CROOMS"},
        "container": {"message_ts": "112.000"},
        "message": {"ts": "112.000", "thread_ts": "111.000"},
        "actions": [
            {"action_id": PUBLIC_ROOM_CHOICE_ACTION_ID, "value": big_value}
        ],
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
    retry = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 503
    assert retry.status_code == 200
    assert len(calls) == 2
    assert len(scheduled) == 1
    scheduled[0].close()


def test_duplicate_public_button_delivery_processes_one_durable_choice(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    _, (big_value, _) = _public_room_choice_values(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    payload = {
        "type": "block_actions",
        "team": {"id": "TMLAI"},
        "user": {"id": "UOWNER"},
        "channel": {"id": "CROOMS"},
        "container": {"message_ts": "112.000"},
        "message": {"ts": "112.000", "thread_ts": "111.000"},
        "actions": [
            {"action_id": PUBLIC_ROOM_CHOICE_ACTION_ID, "value": big_value}
        ],
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
    scheduled = []
    processed = []

    async def processor(action):
        processed.append(action["action_key"])

    monkeypatch.setattr(main_module, "_process_meeting_room_action_record", processor)
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=headers)
    duplicate = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert len(scheduled) == 2

    async def process_scheduled():
        await asyncio.gather(*scheduled)

    asyncio.run(process_scheduled())
    assert len(processed) == 1

    completed_retry = client.post("/slack/actions", content=body, headers=headers)
    assert completed_retry.status_code == 200
    assert len(scheduled) == 2


def test_public_button_rejects_another_member_without_persisting(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    _, (big_value, _) = _public_room_choice_values(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    payload = {
        "type": "block_actions",
        "team": {"id": "TMLAI"},
        "user": {"id": "UOTHER"},
        "channel": {"id": "CROOMS"},
        "container": {"message_ts": "112.000"},
        "message": {"ts": "112.000", "thread_ts": "111.000"},
        "actions": [
            {"action_id": PUBLIC_ROOM_CHOICE_ACTION_ID, "value": big_value}
        ],
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
    scheduled = []
    ephemeral = []
    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: (_ for _ in ()).throw(AssertionError("must not persist")),
    )
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    monkeypatch.setattr(
        main_module,
        "post_ephemeral",
        lambda **kwargs: ephemeral.append(kwargs) or {"ok": True},
    )
    client = TestClient(main_module.app)

    response = client.post("/slack/actions", content=body, headers=headers)

    assert response.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert ephemeral[0]["user"] == "UOTHER"
    assert "Only the person" in ephemeral[0]["text"]


@pytest.mark.asyncio
async def test_public_button_retry_reuses_private_preview_and_updates_prompt(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    clarification, (big_value, _) = _public_room_choice_values(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    action_store = action_module.MeetingRoomActionStore(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    action = action_store.record_action(
        action_id=PUBLIC_ROOM_CHOICE_ACTION_ID,
        action_value=big_value,
        actor_user_id="UOWNER",
        channel_id="CROOMS",
        message_ts="112.000",
    )
    calls = []
    updates = []

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            calls.append(kwargs)
            return {
                "message": "I've sent you a private reply about the Meeting Room.",
                "data": {"delivery": "direct_message"},
            }

    class SlackClient:
        def chat_update(self, **kwargs):
            updates.append(kwargs)
            return {"ok": len(updates) > 1}

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SlackClient(),
    )
    monkeypatch.setattr(action_module, "_retry_delay", lambda attempts: 0)

    await action_module.process_meeting_room_action(
        action["id"],
        store=action_store,
        processor=main_module._process_meeting_room_action_record,
    )
    assert action_store.get(action["id"])["status"] == "pending"

    await action_module.process_meeting_room_action(
        action["id"],
        store=action_store,
        processor=main_module._process_meeting_room_action_record,
    )

    assert action_store.get(action["id"])["status"] == "completed"
    assert len(calls) == 2
    request_ids = {call["booking_client_request_id"] for call in calls}
    assert len(request_ids) == 1
    assert all("private reply" in update["text"] for update in updates)
    assert all("2:00" not in update["text"] for update in updates)
    stored = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    ).find(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
    )
    assert stored["status"] == "completed"
    assert request_ids == {stored["booking_client_request_id"]}


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


@pytest.mark.asyncio
async def test_disabled_feature_does_not_persist_or_consume_queued_action(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path, MEETING_ROOM_BOOKING_ENABLED=False)
    queued_store = action_module.MeetingRoomActionStore(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    queued_action = _record(queued_store)
    scheduled = []
    handled = []

    async def handle(**kwargs):
        handled.append(kwargs)

    monkeypatch.setattr(
        main_module,
        "get_meeting_room_action_store",
        lambda path: pytest.fail("disabled actions must not touch the durable queue"),
    )
    monkeypatch.setattr(main_module, "_handle_meeting_room_action", handle)
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)

    await main_module._persist_and_start_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=_booking_action_value(),
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert len(scheduled) == 1
    await scheduled[0]
    assert handled[0]["settings"] is configured
    assert queued_store.get(queued_action["id"])["status"] == "pending"
