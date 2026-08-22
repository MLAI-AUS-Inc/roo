import asyncio
import hashlib
import hmac
import importlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo import meeting_room_booking as room_module
from roo.agent import RooAgent
from roo.clients.mlai_backend import MLAIBackendUnavailableError
from roo.config import Settings, get_settings
from roo.meeting_room_booking import (
    BOOK_ACTION_ID,
    CANCEL_ACTION_ID,
    MeetingRoomInputError,
    build_booking_action_value,
    parse_action_value,
    resolve_interval,
)
from roo.skills.executor import SkillExecutor
from roo.skills.loader import load_skill_from_directory
from roo.slack_security import get_slack_receipt_store


MELBOURNE = ZoneInfo("Australia/Melbourne")


def _patch_executor(monkeypatch, configured, client_class=None):
    globals_dict = SkillExecutor._execute_meeting_room_booking.__globals__
    monkeypatch.setitem(globals_dict, "get_settings", lambda: configured)
    monkeypatch.setitem(
        globals_dict,
        "MLAIBackendClient",
        client_class or FakeMeetingRoomClient,
    )


class FakeMeetingRoomClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.bookings = []
        type(self).instances.append(self)

    async def check_meeting_room_availability(self, slack_user_id, **kwargs):
        self.calls.append(("availability", slack_user_id, kwargs))
        if kwargs.get("starts_at"):
            starts_at = datetime.fromisoformat(kwargs["starts_at"])
            ends_at = datetime.fromisoformat(kwargs["ends_at"])
            duration_seconds = int((ends_at - starts_at).total_seconds())
            cost = (duration_seconds + 3599) // 3600
            requested = {
                "starts_at": kwargs["starts_at"],
                "ends_at": kwargs["ends_at"],
            }
        else:
            cost = None
            requested = None
        return {
            "room": {"id": "room-id", "slug": "meeting-room", "name": "Meeting Room"},
            "available": True if requested else None,
            "requested_interval": requested,
            "points_cost": cost,
            "remaining_daily_hours": {},
            "busy_intervals": [],
        }

    async def get_my_meeting_room_bookings(self, slack_user_id):
        self.calls.append(("list", slack_user_id, {}))
        return list(self.bookings)


def _settings(**overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "SLACK_RECEIPTS_DB_PATH": "/tmp/roo-meeting-room-test-receipts.db",
        "OPENAI_API_KEY": "synthetic-openai-key",
        "MLAI_BACKEND_URL": "https://backend.test",
        "ROO_API_KEY": "roo-test-key",
        "MEETING_ROOM_BOOKING_ENABLED": True,
    }
    values.update(overrides)
    return Settings(**values)


def _admin_booking_response(starts_at, *, created=True):
    return {
        "created": created,
        "points_cost": 2,
        "remaining_balance": 8,
        "admin_booking": True,
        "booked_for_slack_user_id": "UTARGET",
        "booking": {
            "id": "99e4d8b2-d48c-4f51-a230-b8656c9a3127",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=2)).isoformat(),
            "points_cost": 2,
            "room": {"name": "Meeting Room"},
        },
    }


@pytest.fixture(autouse=True)
def reset_test_state():
    FakeMeetingRoomClient.instances.clear()
    main_module._meeting_room_action_tasks.clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()
    yield
    main_module._meeting_room_action_tasks.clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


def test_interval_parser_uses_melbourne_time_and_one_hour_default():
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval("book tomorrow at 2pm", now=now)

    assert starts_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 15, tzinfo=MELBOURNE)


def test_agent_cleaning_preserves_labeled_target_mention(monkeypatch):
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    agent = object.__new__(RooAgent)

    cleaned = agent._clean_mention(
        "<@UROO> book <@UTARGET|Other Member> tomorrow at 2pm"
    )

    assert cleaned == "book <@UTARGET> tomorrow at 2pm"


def test_interval_parser_handles_ranges_and_cross_midnight():
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval(
        "book tomorrow from 11pm to 1am",
        now=now,
    )

    assert starts_at == datetime(2026, 8, 12, 23, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 13, 1, tzinfo=MELBOURNE)


@pytest.mark.parametrize(
    ("text", "params"),
    (
        ("book tomorrow 2pm-4pm", {}),
        ("book tomorrow 2pm until 4pm", {}),
        ("book tomorrow", {"start_time": "2pm", "end_time": "4pm"}),
    ),
)
def test_interval_parser_preserves_explicit_end_times_without_from(text, params):
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval(text, params, now=now)

    assert starts_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 16, tzinfo=MELBOURNE)


def test_skill_schema_exposes_end_time_for_availability_and_booking():
    skill = load_skill_from_directory(
        Path(__file__).resolve().parents[2] / "skills" / "meeting_room_booking"
    )
    actions = {action["name"]: action for action in skill.actions}

    assert "end_time" in actions["check_room_availability"]["params"]
    assert "end_time" in actions["book_meeting_room"]["params"]
    assert actions["book_meeting_room"]["params"]["duration_hours"]["type"] == "number"
    assert {
        example["action"]
        for example in skill.routing["examples"]
    } == {
        "check_room_availability",
        "book_meeting_room",
        "list_my_room_bookings",
        "cancel_meeting_room",
    }


def test_interval_parser_accepts_unambiguous_24_hour_time():
    starts_at, ends_at = resolve_interval(
        "book tomorrow",
        {"start_time": "09:00"},
        now=datetime(2026, 8, 11, 8, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 9, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 10, tzinfo=MELBOURNE)


@pytest.mark.parametrize(
    "text",
    (
        "book tomorrow at 2pm for an hour and a half",
        "book tomorrow at 2pm for one and a half hours",
        "book tomorrow at 2pm for 1.5 hours",
        "book tomorrow at 2pm for 90 minutes",
    ),
)
def test_interval_parser_accepts_ninety_minute_phrasings(text):
    starts_at, ends_at = resolve_interval(
        text,
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 15, 30, tzinfo=MELBOURNE)


def test_interval_parser_accepts_half_hour_boundaries_and_bare_start_time():
    starts_at, ends_at = resolve_interval(
        "book tomorrow 2:30pm",
        {"duration_hours": 1.5},
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 14, 30, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 16, tzinfo=MELBOURNE)


def test_interval_parser_rejects_past_start_before_backend_call():
    with pytest.raises(MeetingRoomInputError, match="start in the future"):
        resolve_interval(
            "book today at 2pm",
            now=datetime(2026, 8, 11, 16, tzinfo=MELBOURNE),
        )


def test_interval_parser_rejects_probable_reversed_range_instead_of_rolling_23_hours():
    with pytest.raises(MeetingRoomInputError, match="at most two hours"):
        resolve_interval(
            "book tomorrow from 2pm to 1pm",
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )


def test_interval_parser_counts_actual_hours_across_daylight_saving():
    starts_at, ends_at = resolve_interval(
        "book the meeting room 2026-10-04 at 1am",
        {"duration_hours": 2},
        now=datetime(2026, 9, 20, 9, tzinfo=MELBOURNE),
    )

    assert ends_at.hour == 4
    assert (
        ends_at.astimezone(timezone.utc)
        - starts_at.astimezone(timezone.utc)
    ) == timedelta(hours=2)


def test_interval_parser_rejects_ambiguous_daylight_saving_hour():
    with pytest.raises(MeetingRoomInputError, match="occurs twice"):
        resolve_interval(
            "book the meeting room 2027-04-04 at 2am",
            now=datetime(2027, 3, 20, 9, tzinfo=MELBOURNE),
        )


@pytest.mark.parametrize(
    ("text", "code"),
    (
        ("book at 2pm", "missing_date"),
        ("book tomorrow", "missing_start_time"),
        ("book tomorrow at 2", "ambiguous_time"),
        ("book tomorrow at 2:15pm", "invalid_time"),
        ("book tomorrow at 2pm for 45 minutes", "invalid_time"),
        ("book tomorrow at 2pm for 2.5 hours", "invalid_time"),
    ),
)
def test_interval_parser_asks_for_missing_or_ambiguous_details(text, code):
    with pytest.raises(MeetingRoomInputError) as raised:
        resolve_interval(
            text,
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )
    assert raised.value.code == code


def test_booking_action_payload_is_bound_to_owner_and_expires_in_ten_minutes():
    now = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        now=now,
    )

    parsed = parse_action_value(value, expected_action=BOOK_ACTION_ID)

    assert parsed["owner_slack_user_id"] == "UOWNER"
    assert parsed["expected_points_cost"] == 2
    assert datetime.fromisoformat(parsed["confirmation_expires_at"]) == now + timedelta(minutes=10)


def test_booking_action_rejects_fractional_preview_cost():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    payload = json.loads(
        build_booking_action_value(
            owner_slack_user_id="UOWNER",
            room_slug="meeting-room",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1.5),
            expected_points_cost=2,
        )
    )
    payload["expected_points_cost"] = 1.5

    with pytest.raises(MeetingRoomInputError, match="not valid"):
        parse_action_value(
            json.dumps(payload),
            expected_action=BOOK_ACTION_ID,
        )


def test_cancellation_selection_skips_malformed_booking_ids():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    bookings = [
        {
            "id": "not-a-uuid",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "room": {"name": "Meeting Room"},
        },
        {
            "id": "1409fd17-c84d-4774-af8a-7b847c16bd30",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "room": {"name": "Meeting Room"},
        },
    ]

    selection = room_module.cancellation_selection(
        bookings,
        owner_slack_user_id="UOWNER",
    )

    assert len(selection["blocks"]) == 2
    assert "1409fd17-c84d-4774-af8a-7b847c16bd30" in selection["blocks"][1]["accessory"]["value"]


def test_cancellation_selection_reports_invalid_backend_rows():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    with pytest.raises(MeetingRoomInputError, match="could not read"):
        room_module.cancellation_selection(
            [
                {
                    "id": None,
                    "starts_at": starts_at.isoformat(),
                    "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
                }
            ],
            owner_slack_user_id="UOWNER",
        )


def test_booking_result_uses_created_flag_for_idempotent_replays():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)

    message = room_module.format_booking_result(
        {
            "created": False,
            "points_cost": 2,
            "booking": {
                "starts_at": starts_at.isoformat(),
                "ends_at": (starts_at + timedelta(hours=2)).isoformat(),
                "room": {"name": "Meeting Room"},
            },
        }
    )

    assert message.startswith("This booking was already confirmed.")


@pytest.mark.asyncio
async def test_public_booking_request_sends_private_confirmation_without_booking(monkeypatch):
    configured = _settings()
    sent = []
    _patch_executor(monkeypatch, configured)
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda user_id, message, **kwargs: sent.append((user_id, message, kwargs)) or {"ok": True},
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room tomorrow from 2pm to 4pm",
        params={"action": "book_meeting_room"},
        user_id="UOWNER",
        channel_id="CPUBLIC",
    )

    assert result["message"] == "I've sent you a private reply about the Meeting Room."
    assert len(sent) == 1
    blocks = sent[0][2]["blocks"]
    button = blocks[1]["elements"][0]
    assert button["action_id"] == BOOK_ACTION_ID
    assert parse_action_value(button["value"], expected_action=BOOK_ACTION_ID)["owner_slack_user_id"] == "UOWNER"
    assert [call[0] for call in FakeMeetingRoomClient.instances[0].calls] == ["availability"]


@pytest.mark.asyncio
async def test_direct_message_booking_preview_defaults_to_one_hour(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    assert "Duration:* 1 hour" in result["message"]
    assert result["blocks"][1]["elements"][0]["action_id"] == BOOK_ACTION_ID


@pytest.mark.asyncio
async def test_public_dm_failure_never_exposes_booking_details(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda *args, **kwargs: None,
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="is the meeting room free tomorrow at 2pm",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="CPUBLIC",
    )

    assert result["data"]["delivery_failed"] is True
    assert "DM Roo" in result["message"]
    assert "2:00" not in result["message"]
    assert "available" not in result["message"].lower()


def test_date_availability_makes_an_empty_day_explicitly_clear():
    message = SkillExecutor._format_meeting_room_availability(
        {
            "room": {"name": "Meeting Room"},
            "requested_interval": None,
            "busy_intervals": [],
        }
    )

    assert message == (
        "The *Meeting Room* has no bookings currently shown for that date "
        "(Melbourne time)."
    )


def test_date_availability_lists_busy_intervals_and_marks_everything_else_free():
    message = SkillExecutor._format_meeting_room_availability(
        {
            "room": {"name": "Meeting Room"},
            "requested_interval": None,
            "busy_intervals": [
                {
                    "starts_at": "2026-08-17T10:00:00+10:00",
                    "ends_at": "2026-08-17T11:00:00+10:00",
                },
                {
                    "starts_at": "2026-08-17T14:00:00+10:00",
                    "ends_at": "2026-08-17T16:00:00+10:00",
                },
            ],
        }
    )

    assert "unavailable at these times" in message
    assert "10:00 AM to 11:00 AM" in message
    assert "2:00 PM to 4:00 PM" in message
    assert message.endswith("All other times that day are currently available.")


@pytest.mark.asyncio
async def test_cancel_request_filters_members_own_bookings_and_builds_buttons(monkeypatch):
    configured = _settings()
    tomorrow = room_module.get_current_datetime().astimezone(MELBOURNE).date() + timedelta(days=1)
    starts_at = datetime.combine(tomorrow, datetime.min.time(), tzinfo=MELBOURNE).replace(hour=14)
    client_holder = {}

    class Client(FakeMeetingRoomClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.bookings = [
                {
                    "id": "1409fd17-c84d-4774-af8a-7b847c16bd30",
                    "starts_at": starts_at.isoformat(),
                    "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
                    "points_cost": 1,
                    "room": {"name": "Meeting Room"},
                },
                {
                    "id": "7267a2c2-bd8d-4ac1-ae1b-13765dcfd81e",
                    "starts_at": (starts_at + timedelta(days=1)).isoformat(),
                    "ends_at": (starts_at + timedelta(days=1, hours=1)).isoformat(),
                    "points_cost": 1,
                    "room": {"name": "Meeting Room"},
                },
            ]
            client_holder["client"] = self

    _patch_executor(monkeypatch, configured, Client)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="cancel my room booking tomorrow",
        params={"action": "cancel_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    buttons = [block["accessory"] for block in result["blocks"] if block.get("accessory")]
    assert len(buttons) == 1
    parsed = parse_action_value(buttons[0]["value"], expected_action=CANCEL_ACTION_ID)
    assert parsed["booking_id"] == "1409fd17-c84d-4774-af8a-7b847c16bd30"
    assert client_holder["client"].calls == [("list", "UOWNER", {})]


@pytest.mark.asyncio
async def test_cancel_request_with_too_many_matches_requires_a_date(monkeypatch):
    configured = _settings()

    class Client(FakeMeetingRoomClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.bookings = [{} for _ in range(41)]

    _patch_executor(monkeypatch, configured, Client)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="cancel a meeting room booking",
        params={"action": "cancel_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    assert "too many upcoming bookings" in result["message"]
    assert "cancellation date" in result["message"]
    assert result["blocks"] is None
    assert Client.instances[0].calls == [("list", "UOWNER", {})]


@pytest.mark.asyncio
async def test_points_admin_tagged_booking_previews_target_charge(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room for <@UOTHER|Other Member> tomorrow at 2pm for 1.5 hours",
        params={"action": "book_meeting_room"},
        user_id="UADMIN",
        channel_id="DADMIN",
    )

    assert "for <@UOTHER>" in result["message"]
    assert "1.5 hours" in result["message"]
    assert "Their Roo Points account will be charged" in result["message"]
    availability_call = FakeMeetingRoomClient.instances[0].calls[0]
    assert availability_call[1] == "UADMIN"
    assert availability_call[2]["target_slack_user_id"] == "UOTHER"
    button = result["blocks"][1]["elements"][0]
    payload = parse_action_value(button["value"], expected_action=BOOK_ACTION_ID)
    assert payload["owner_slack_user_id"] == "UADMIN"
    assert payload["target_slack_user_id"] == "UOTHER"
    assert payload["expected_points_cost"] == 2


@pytest.mark.asyncio
async def test_non_admin_tagged_booking_reports_backend_denial_privately(monkeypatch):
    configured = _settings()

    class DeniedClient(FakeMeetingRoomClient):
        async def check_meeting_room_availability(self, slack_user_id, **kwargs):
            request = httpx.Request("POST", "https://backend.test/meeting-rooms/availability/")
            response = httpx.Response(
                403,
                request=request,
                json={
                    "code": "admin_required",
                    "error": "Only full Roo Points Admins can book for another member",
                },
            )
            response.raise_for_status()

    _patch_executor(monkeypatch, configured, DeniedClient)
    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room for <@UOTHER> tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UNONADMIN",
        channel_id="DNONADMIN",
    )

    assert "Only full Roo Points Admins" in result["message"]


@pytest.mark.asyncio
async def test_admin_booking_rejects_multiple_tagged_members_before_backend(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book <@UONE> and <@UTWO> into the meeting room tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UADMIN",
        channel_id="DADMIN",
    )

    assert "Tag exactly one member" in result["message"]
    assert FakeMeetingRoomClient.instances == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dm_response", "expected_admin_text"),
    (
        ({"ok": True}, "I notified <@UTARGET> privately"),
        (None, "I could not notify the booked member privately"),
    ),
)
async def test_action_handler_uses_verified_admin_actor_and_notifies_target(
    monkeypatch,
    dm_response,
    expected_admin_text,
):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        target_slack_user_id="UTARGET",
    )
    expected_request_id = json.loads(action_value)["client_request_id"]
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            calls.append((slack_user_id, kwargs))
            return _admin_booking_response(starts_at)

    updates = []
    target_dms = []
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, message: target_dms.append((user_id, message)) or dm_response,
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert calls[0][0] == "UOWNER"
    assert calls[0][1]["client_request_id"] == expected_request_id
    assert calls[0][1]["expected_points_cost"] == 2
    assert calls[0][1]["target_slack_user_id"] == "UTARGET"
    assert "for <@UTARGET> is confirmed" in updates[0]["text"]
    assert expected_admin_text in updates[0]["text"]
    assert target_dms[0][0] == "UTARGET"
    assert "<@UOWNER> booked the *Meeting Room* for you" in target_dms[0][1]
    assert "Charged to your account:* 2 Roo Points" in target_dms[0][1]
    assert "show my meeting room bookings" in target_dms[0][1]
    assert "Remaining balance" not in target_dms[0][1]


@pytest.mark.asyncio
async def test_replayed_admin_booking_does_not_notify_target_again(monkeypatch):
    configured = _settings()
    starts_at = (
        datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=2),
        expected_points_cost=2,
        target_slack_user_id="UTARGET",
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            return _admin_booking_response(starts_at, created=False)

    updates = []
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("a replay must not notify the target again"),
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert "was already confirmed" in updates[0]["text"]
    assert "notified" not in updates[0]["text"]


@pytest.mark.asyncio
async def test_action_handler_rejects_mismatched_actor_before_backend(monkeypatch):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        expected_points_cost=1,
    )
    updates = []

    class NeverClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("backend must not be called")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", NeverClient)
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOTHER",
        channel_id="DOTHER",
        message_ts="123.456",
    )

    assert "Only the member" in updates[0]["text"]


@pytest.mark.asyncio
async def test_action_handler_rejects_public_channel_before_backend(monkeypatch):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        expected_points_cost=1,
    )
    updates = []

    class NeverClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("backend must not be called")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", NeverClient)
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="CPUBLIC",
        message_ts="123.456",
    )

    assert "private Roo DM" in updates[0]["text"]


@pytest.mark.asyncio
async def test_action_handler_rejects_expired_confirmation_before_backend(monkeypatch):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        expected_points_cost=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=11),
    )
    updates = []

    class NeverClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("backend must not be called")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", NeverClient)
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert "expired" in updates[0]["text"]


@pytest.mark.asyncio
async def test_action_handler_does_not_claim_failed_booking_request_changed_nothing(monkeypatch):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        expected_points_cost=1,
    )
    updates = []

    class UnavailableClient:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            raise MLAIBackendUnavailableError("connection dropped after request")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", UnavailableClient)
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updates.append(kwargs)),
    )

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert "could not confirm whether" in updates[0]["text"]
    assert "show my meeting room bookings" in updates[0]["text"]
    assert "Nothing was changed" not in updates[0]["text"]


@pytest.mark.asyncio
async def test_action_handler_logs_when_update_and_fallback_delivery_both_fail(monkeypatch, capsys):
    configured = _settings(MEETING_ROOM_BOOKING_ENABLED=False)

    def fail_delivery(**kwargs):
        raise RuntimeError("slack unavailable")

    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(chat_update=fail_delivery),
    )
    monkeypatch.setattr(main_module, "post_message", fail_delivery)

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value="",
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert "MEETING_ROOM_ACTION_DELIVERY_FAILED" in capsys.readouterr().out


def _signature(secret, timestamp, body):
    digest = hmac.new(
        secret.encode(),
        b"v0:" + str(timestamp).encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def test_duplicate_signed_button_delivery_schedules_only_one_action(tmp_path, monkeypatch):
    configured = _settings(SLACK_RECEIPTS_DB_PATH=str(tmp_path / "receipts.db"))
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="meeting-room",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        expected_points_cost=1,
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "UOWNER"},
        "channel": {"id": "DOWNER"},
        "container": {"message_ts": "card.456"},
        "message": {"ts": "fallback-card.456", "thread_ts": "member-parent.123"},
        "actions": [{"action_id": BOOK_ACTION_ID, "value": action_value}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = int(time.time())
    headers = {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": _signature(configured.SLACK_SIGNING_SECRET, timestamp, body),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    scheduled_message_timestamps = []

    def fake_start_meeting_room_action(coro):
        scheduled_message_timestamps.append(coro.cr_frame.f_locals["message_ts"])
        coro.close()

    monkeypatch.setattr(
        main_module,
        "_start_meeting_room_action",
        fake_start_meeting_room_action,
    )
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=headers)
    second = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert scheduled_message_timestamps == ["card.456"]


@pytest.mark.asyncio
async def test_meeting_room_action_task_is_retained_until_completion():
    started = asyncio.Event()
    release = asyncio.Event()

    async def action():
        started.set()
        await release.wait()

    task = main_module._start_meeting_room_action(action())
    await started.wait()

    assert task in main_module._meeting_room_action_tasks

    release.set()
    await task
    await asyncio.sleep(0)

    assert task not in main_module._meeting_room_action_tasks


@pytest.mark.asyncio
async def test_backend_client_uses_canonical_meeting_room_endpoints(monkeypatch):
    backend_module = importlib.import_module("roo.clients.mlai_backend")
    captured = []

    async def fake_request(method, endpoint, **kwargs):
        captured.append((method, endpoint, kwargs))
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        payload = {"rooms": []} if endpoint.endswith("/rooms/") else {"bookings": []}
        return httpx.Response(200, request=request, json=payload)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-key",
        internal_api_key="roo-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    await client.check_meeting_room_availability(
        "<@UOWNER>",
        date="2026-08-12",
        target_slack_user_id="<@UTARGET>",
    )
    await client.book_meeting_room(
        "<@UOWNER>",
        room_slug="meeting-room",
        starts_at="2026-08-12T14:00:00+10:00",
        ends_at="2026-08-12T16:00:00+10:00",
        client_request_id="1409fd17-c84d-4774-af8a-7b847c16bd30",
        confirmation_expires_at="2026-08-11T23:00:00Z",
        expected_points_cost=2,
        slack_channel_id="DOWNER",
        target_slack_user_id="<@UTARGET>",
    )
    await client.get_my_meeting_room_bookings("<@UOWNER>")
    await client.cancel_meeting_room_booking(
        "<@UOWNER>",
        "1409fd17-c84d-4774-af8a-7b847c16bd30",
    )

    assert [endpoint for _, endpoint, _ in captured] == [
        "/api/v1/points/meeting-rooms/availability/",
        "/api/v1/points/meeting-rooms/book/",
        "/api/v1/points/meeting-rooms/my-bookings/",
        "/api/v1/points/meeting-rooms/cancel/",
    ]
    assert captured[0][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[0][2]["json"]["target_slack_user_id"] == "UTARGET"
    assert captured[1][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[1][2]["json"]["client_request_id"] == "1409fd17-c84d-4774-af8a-7b847c16bd30"
    assert captured[1][2]["json"]["expected_points_cost"] == 2
    assert captured[1][2]["json"]["target_slack_user_id"] == "UTARGET"
    assert captured[2][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[3][2]["json"]["slack_user_id"] == "UOWNER"


def test_feature_flag_is_disabled_by_default_and_fails_closed():
    disabled = Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic",
        SLACK_SIGNING_SECRET="synthetic-signing-secret",
        OPENAI_API_KEY="synthetic-openai-key",
    )
    assert disabled.MEETING_ROOM_BOOKING_ENABLED is False
    assert "meeting-room-booking" not in disabled.enabled_skill_names

    enabled = _settings()
    assert "meeting-room-booking" in enabled.enabled_skill_names

    with pytest.raises(Exception, match="cannot be enabled"):
        Settings(
            _env_file=None,
            ROO_ENABLED_SKILLS="meeting-room-booking",
            MEETING_ROOM_BOOKING_ENABLED=False,
        )
