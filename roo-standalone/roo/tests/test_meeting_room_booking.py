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
from roo import meeting_room_actions as action_module
from roo import meeting_room_clarifications as clarification_module
from roo import slack_action_tasks
from roo.agent import RooAgent
from roo.clients.mlai_backend import MLAIBackendUnavailableError
from roo.config import Settings, get_settings
from roo.meeting_room_booking import (
    BOOK_ACTION_ID,
    CANCEL_ACTION_ID,
    CHOOSE_ROOM_ACTION_ID,
    MeetingRoomInputError,
    build_booking_action_value,
    parse_action_value,
    room_slug_from_text,
    resolve_interval,
)
from roo.meeting_room_clarifications import (
    PUBLIC_ROOM_CHOICE_ACTION_IDS_BY_ROOM,
    parse_public_room_choice_action_value,
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

    async def list_meeting_rooms(self):
        self.calls.append(("rooms", None, {}))
        return [
            {
                "id": "small-room-id",
                "slug": "small-meeting-room",
                "name": "Small Meeting Room",
            },
            {
                "id": "big-room-id",
                "slug": "big-meeting-room",
                "name": "Big Meeting Room",
            },
        ]

    async def check_meeting_room_availability(self, slack_user_id, **kwargs):
        self.calls.append(("availability", slack_user_id, kwargs))
        if kwargs.get("starts_at"):
            starts_at = datetime.fromisoformat(kwargs["starts_at"])
            ends_at = datetime.fromisoformat(kwargs["ends_at"])
            duration_seconds = int((ends_at - starts_at).total_seconds())
            bookable = 3600 <= duration_seconds <= 7200
            cost = (duration_seconds + 3599) // 3600 if bookable else None
            requested = {
                "starts_at": kwargs["starts_at"],
                "ends_at": kwargs["ends_at"],
            }
        else:
            cost = None
            requested = None
            bookable = None
        room_slug = kwargs["room_slug"]
        room_name = (
            "Small Meeting Room"
            if room_slug == "small-meeting-room"
            else "Big Meeting Room"
        )
        return {
            "room": {"id": f"{room_slug}-id", "slug": room_slug, "name": room_name},
            "available": True if requested else None,
            "bookable": bookable,
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
            "room": {
                "slug": "small-meeting-room",
                "name": "Small Meeting Room",
            },
        },
    }


@pytest.fixture(autouse=True)
def reset_test_state():
    FakeMeetingRoomClient.instances.clear()
    slack_action_tasks._tasks.clear()
    action_module.get_meeting_room_action_store.cache_clear()
    clarification_module.get_meeting_room_clarification_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()
    yield
    slack_action_tasks._tasks.clear()
    action_module.get_meeting_room_action_store.cache_clear()
    clarification_module.get_meeting_room_clarification_store.cache_clear()
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


def test_interval_parser_uses_melbourne_time_and_one_hour_default():
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval("book tomorrow at 2pm", now=now)

    assert starts_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 15, tzinfo=MELBOURNE)


@pytest.mark.parametrize("alias", ("tomorow", "tommorow", "tommorrow"))
def test_interval_parser_accepts_common_tomorrow_misspellings(alias):
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval(f"{alias} at 1pm", now=now)

    assert starts_at == datetime(2026, 8, 12, 13, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)


def test_interval_parser_accepts_misspelled_tomorrow_model_parameter():
    now = datetime(2026, 8, 11, 9, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval(
        "at 1pm",
        {"date": "tommorrow"},
        now=now,
    )

    assert starts_at == datetime(2026, 8, 12, 13, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)


def test_interval_parser_defaults_missing_date_to_next_occurrence_today():
    now = datetime(2026, 8, 11, 7, 20, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval("at 1pm", now=now)

    assert starts_at == datetime(2026, 8, 11, 13, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 11, 14, tzinfo=MELBOURNE)


def test_missing_date_next_occurrence_uses_melbourne_date_for_utc_clock():
    now = datetime(2026, 8, 24, 21, 20, tzinfo=timezone.utc)

    starts_at, _ = resolve_interval("at 1pm", now=now)

    assert starts_at == datetime(2026, 8, 25, 13, tzinfo=MELBOURNE)


def test_interval_parser_defaults_missing_date_to_tomorrow_after_time_passes():
    now = datetime(2026, 8, 11, 19, 20, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval("at 1pm", now=now)

    assert starts_at == datetime(2026, 8, 12, 13, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)


def test_interval_parser_defaults_missing_date_to_tomorrow_at_exact_start_time():
    now = datetime(2026, 8, 11, 13, tzinfo=MELBOURNE)

    starts_at, _ = resolve_interval("at 1pm", now=now)

    assert starts_at == datetime(2026, 8, 12, 13, tzinfo=MELBOURNE)


def test_interval_parser_uses_next_occurrence_for_routed_start_time():
    now = datetime(2026, 8, 11, 7, 20, tzinfo=MELBOURNE)

    starts_at, _ = resolve_interval(
        "book the big meeting room",
        {"start_time": "1pm"},
        now=now,
    )

    assert starts_at == datetime(2026, 8, 11, 13, tzinfo=MELBOURNE)


def test_missing_date_next_occurrence_rejects_nonexistent_daylight_saving_hour():
    with pytest.raises(MeetingRoomInputError, match="does not exist"):
        resolve_interval(
            "at 2am",
            now=datetime(2026, 10, 4, 0, 30, tzinfo=MELBOURNE),
        )


def test_missing_date_default_uses_melbourne_calendar_at_day_boundary():
    now = datetime(2026, 8, 11, 23, 55, tzinfo=MELBOURNE)

    starts_at, ends_at = resolve_interval("at 12:30am", now=now)

    assert starts_at == datetime(2026, 8, 12, 0, 30, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 1, 30, tzinfo=MELBOURNE)


def test_interval_parser_does_not_fuzz_unrelated_date_words():
    with pytest.raises(MeetingRoomInputError) as raised:
        resolve_interval(
            "tomorrowish at 1pm",
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )

    assert raised.value.code == "invalid_date"


@pytest.mark.parametrize("text", ("next month at 1pm", "someday at 1pm"))
def test_interval_parser_rejects_vague_dates_instead_of_defaulting(text):
    with pytest.raises(MeetingRoomInputError) as raised:
        resolve_interval(
            text,
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )

    assert raised.value.code == "invalid_date"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("book the small room tomorrow", "small-meeting-room"),
        ("book the small meeting room tomorrow", "small-meeting-room"),
        ("book the big room tomorrow", "big-meeting-room"),
        ("book the big meeting room tomorrow", "big-meeting-room"),
        ("book the large room tomorrow", "big-meeting-room"),
        ("book either meeting room tomorrow", None),
        ("compare the small and big rooms tomorrow", None),
    ),
)
def test_room_slug_is_derived_only_from_explicit_message_words(text, expected):
    assert room_slug_from_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "<@UROO> book <@UTARGET|Other Member> tomorrow at 2pm",
            "book <@UTARGET> tomorrow at 2pm",
        ),
        (
            "<@UROO> connect me with someone like <@UTARGET|Other Member>",
            "connect me with someone like <@UTARGET>",
        ),
        (
            "<@UROO|Roo> book <@UTARGET|Other Member> tomorrow at 2pm",
            "book <@UTARGET> tomorrow at 2pm",
        ),
    ),
)
def test_agent_cleaning_preserves_labeled_mentions(monkeypatch, text, expected):
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    agent = object.__new__(RooAgent)

    cleaned = agent._clean_mention(text)

    assert cleaned == expected


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
    assert "Google/Outlook sync" in skill.routing["avoid_when"]
    assert "events, calendars" not in skill.routing["avoid_when"]
    assert any(
        example["text"] == "room calendar tomorrow?"
        and example["action"] == "check_room_availability"
        for example in skill.routing["examples"]
    )
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


@pytest.mark.parametrize("time_value", ("2:30", "9:30", "12:30"))
def test_interval_parser_asks_am_or_pm_for_colloquial_times_with_minutes(
    time_value,
):
    with pytest.raises(MeetingRoomInputError) as raised:
        resolve_interval(
            f"book tomorrow at {time_value}",
            now=datetime(2026, 8, 11, 8, tzinfo=MELBOURNE),
        )

    assert raised.value.code == "ambiguous_time"


def test_interval_parser_keeps_leading_zero_time_unambiguous():
    starts_at, ends_at = resolve_interval(
        "book tomorrow at 09:30",
        now=datetime(2026, 8, 11, 8, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 9, 30, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 10, 30, tzinfo=MELBOURNE)


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


def test_interval_parser_accepts_word_number_two_hours():
    starts_at, ends_at = resolve_interval(
        "book tomorrow at 2pm for two hours",
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 16, tzinfo=MELBOURNE)


def test_interval_parser_accepts_half_hour_boundaries_and_bare_start_time():
    starts_at, ends_at = resolve_interval(
        "book tomorrow 2:30pm",
        {"duration_hours": 1.5},
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    assert starts_at == datetime(2026, 8, 12, 14, 30, tzinfo=MELBOURNE)
    assert ends_at == datetime(2026, 8, 12, 16, tzinfo=MELBOURNE)


@pytest.mark.parametrize(
    "text",
    (
        "book tomorrow at 2pm for half an hour",
        "book tomorrow at 2pm for a half-hour",
    ),
)
def test_booking_parser_rejects_half_hour_wording_without_defaulting(text):
    with pytest.raises(MeetingRoomInputError, match="between 1 and 2 hours"):
        resolve_interval(
            text,
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )


def test_booking_parser_rejects_unrecognized_duration_without_defaulting():
    with pytest.raises(MeetingRoomInputError, match="could not understand that duration"):
        resolve_interval(
            "book tomorrow at 2pm for three quarters of an hour",
            now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
        )


def test_availability_parser_allows_long_and_half_hour_checks():
    long_start, long_end = room_module.resolve_availability_interval(
        "is the room free tomorrow from 2pm to 6pm?",
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )
    short_start, short_end = room_module.resolve_availability_interval(
        "is the room free tomorrow at 2pm for half an hour?",
        now=datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    assert long_end - long_start == timedelta(hours=4)
    assert short_end - short_start == timedelta(minutes=30)


def test_past_implicit_weekday_rolls_to_next_week_but_explicit_today_does_not():
    now = datetime(2026, 8, 14, 16, tzinfo=MELBOURNE)

    starts_at, _ = resolve_interval("book friday at 3pm", now=now)

    assert starts_at == datetime(2026, 8, 21, 15, tzinfo=MELBOURNE)
    routed_start, _ = resolve_interval(
        "book friday at 3pm",
        {"date": "2026-08-14", "start_time": "3pm"},
        now=now,
    )
    assert routed_start == datetime(2026, 8, 21, 15, tzinfo=MELBOURNE)
    with pytest.raises(MeetingRoomInputError, match="start in the future"):
        resolve_interval("book today friday at 3pm", now=now)


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
        room_slug="small-meeting-room",
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
            room_slug="small-meeting-room",
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


def test_booking_action_rejects_retired_generic_room():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)

    with pytest.raises(MeetingRoomInputError, match="not supported"):
        build_booking_action_value(
            owner_slack_user_id="UOWNER",
            room_slug="meeting-room",
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            expected_points_cost=1,
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


def test_cancellation_result_names_selected_room():
    starts_at = datetime(2026, 8, 12, 14, tzinfo=MELBOURNE)
    result = {
        "cancelled": True,
        "remaining_balance": 10,
        "booking": {
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "points_cost": 1,
            "room": {
                "slug": "big-meeting-room",
                "name": "Big Meeting Room",
            },
        },
    }

    message = room_module.format_cancellation_result(result)

    assert "*Big Meeting Room* booking" in message


def test_cross_room_conflict_error_is_clear_and_names_selected_room():
    request = httpx.Request("POST", "https://backend.test/meeting-rooms/book/")
    response = httpx.Response(
        409,
        request=request,
        json={"code": "user_booking_conflict"},
    )
    error = httpx.HTTPStatusError(
        "conflict",
        request=request,
        response=response,
    )

    message = room_module.backend_error_message(
        error,
        room_slug="big-meeting-room",
    )

    assert "another meeting-room booking" in message
    assert "Big Meeting Room" in message
    assert "No points were deducted" in message


@pytest.mark.asyncio
async def test_public_unspecified_booking_asks_for_room_in_same_thread(
    tmp_path,
    monkeypatch,
):
    configured = _settings(
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "clarifications.db")
    )
    sent = []
    _patch_executor(monkeypatch, configured)
    monkeypatch.setattr(
        room_module,
        "get_current_datetime",
        lambda: datetime(2026, 8, 25, 7, 20, tzinfo=MELBOURNE),
    )
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda user_id, message, **kwargs: sent.append((user_id, message, kwargs)) or {"ok": True},
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room at 1pm",
        params={
            "action": "book_meeting_room",
            "room_slug": "big-meeting-room",
        },
        user_id="UOWNER",
        channel_id="CPUBLIC",
        thread_ts="111.000",
        slack_team_id="TMLAI",
        request_message_ts="111.000",
    )

    assert "Which room should I use" in result["message"]
    assert "buttons expire in 10 minutes" in result["message"]
    assert "1:00" not in result["message"]
    buttons = result["blocks"][1]["elements"]
    assert [button["text"]["text"] for button in buttons] == [
        "Big Meeting Room",
        "Small Meeting Room",
    ]
    assert [button["action_id"] for button in buttons] == [
        PUBLIC_ROOM_CHOICE_ACTION_IDS_BY_ROOM["big-meeting-room"],
        PUBLIC_ROOM_CHOICE_ACTION_IDS_BY_ROOM["small-meeting-room"],
    ]
    assert len({button["action_id"] for button in buttons}) == len(buttons)
    button_values = [
        parse_public_room_choice_action_value(button["value"])
        for button in buttons
    ]
    assert [value["room_slug"] for value in button_values] == [
        "big-meeting-room",
        "small-meeting-room",
    ]
    assert all("starts_at" not in button["value"] for button in buttons)
    assert all("ends_at" not in button["value"] for button in buttons)
    assert sent == []
    stored = clarification_module.get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    ).find(
        team_id="TMLAI",
        channel_id="CPUBLIC",
        thread_ts="111.000",
    )
    assert stored["owner_user_id"] == "UOWNER"
    assert stored["status"] == "awaiting_choice"
    assert stored["choice_mode"] == "buttons"
    assert stored["available_room_slugs"] == [
        "big-meeting-room",
        "small-meeting-room",
    ]
    assert stored["starts_at"] == "2026-08-25T13:00:00+10:00"
    assert stored["ends_at"] == "2026-08-25T14:00:00+10:00"
    assert [call[0] for call in FakeMeetingRoomClient.instances[0].calls] == ["rooms"]


@pytest.mark.asyncio
async def test_public_room_reply_sends_only_private_deterministic_preview(monkeypatch):
    configured = _settings()
    sent = []
    _patch_executor(monkeypatch, configured)
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda user_id, message, **kwargs: (
            sent.append((user_id, message, kwargs)) or {"ok": True}
        ),
    )

    result = await SkillExecutor().complete_meeting_room_room_choice(
        user_id="UOWNER",
        channel_id="CPUBLIC",
        room_slug="big-meeting-room",
        starts_at="2026-08-25T14:00:00+10:00",
        ends_at="2026-08-25T15:00:00+10:00",
        booking_client_request_id="1409fd17-c84d-4774-af8a-7b847c16bd30",
    )

    assert result["message"] == "I've sent you a private reply about the Meeting Room."
    assert len(sent) == 1
    assert sent[0][0] == "UOWNER"
    assert "Big Meeting Room" in sent[0][1]
    assert "2:00 PM to 3:00 PM" in sent[0][1]
    assert sent[0][2]["client_msg_id"] == "1409fd17-c84d-4774-af8a-7b847c16bd30"
    confirm = sent[0][2]["blocks"][1]["elements"][0]
    parsed = parse_action_value(confirm["value"], expected_action=BOOK_ACTION_ID)
    assert parsed["client_request_id"] == "1409fd17-c84d-4774-af8a-7b847c16bd30"
    assert [call[0] for call in FakeMeetingRoomClient.instances[0].calls] == [
        "rooms",
        "availability",
    ]


@pytest.mark.asyncio
async def test_public_room_preview_recovers_duplicate_deterministic_dm(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    class DuplicateMessageError(RuntimeError):
        response = {"error": "duplicate_message"}

    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda *args, **kwargs: (_ for _ in ()).throw(DuplicateMessageError()),
    )

    result = await SkillExecutor().complete_meeting_room_room_choice(
        user_id="UOWNER",
        channel_id="CPUBLIC",
        room_slug="big-meeting-room",
        starts_at="2026-08-25T14:00:00+10:00",
        ends_at="2026-08-25T15:00:00+10:00",
        booking_client_request_id="1409fd17-c84d-4774-af8a-7b847c16bd30",
    )

    assert result["message"] == "I've sent you a private reply about the Meeting Room."


@pytest.mark.asyncio
async def test_public_room_preview_surfaces_uncertain_dm_for_durable_retry(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("response lost")),
    )

    with pytest.raises(RuntimeError, match="response lost"):
        await SkillExecutor().complete_meeting_room_room_choice(
            user_id="UOWNER",
            channel_id="CPUBLIC",
            room_slug="big-meeting-room",
            starts_at="2026-08-25T14:00:00+10:00",
            ends_at="2026-08-25T15:00:00+10:00",
            booking_client_request_id="1409fd17-c84d-4774-af8a-7b847c16bd30",
        )


@pytest.mark.asyncio
async def test_public_room_prompt_fails_closed_when_state_cannot_persist(monkeypatch):
    configured = _settings()
    sent = []
    _patch_executor(monkeypatch, configured)
    monkeypatch.setitem(
        SkillExecutor._execute_meeting_room_booking.__globals__,
        "get_meeting_room_clarification_store",
        lambda path: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )
    monkeypatch.setitem(
        SkillExecutor._deliver_meeting_room_response.__globals__,
        "send_dm",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True},
    )

    result = await SkillExecutor().execute(
        skill=SimpleNamespace(name="meeting-room-booking"),
        text="book the meeting room tomorrow at 2pm",
        user_id="UOWNER",
        channel_id="CPUBLIC",
        thread_ts="111.000",
        param_overrides={"action": "book_meeting_room"},
        slack_team_id="TMLAI",
        current_message_ts="111.000",
    )

    assert result.success is False
    assert "problem executing" in result.message
    assert "Which room" not in result.message
    assert sent == []


def test_missing_channel_never_returns_private_meeting_room_details_inline():
    result = SkillExecutor()._deliver_meeting_room_response(
        user_id="UOWNER",
        channel_id=None,
        message="Private booking details: 2pm in the Big Meeting Room",
        action="check_room_availability",
    )

    assert result["data"]["delivery_failed"] is True
    assert "Private booking details" not in result["message"]
    assert "DM Roo" in result["message"]


@pytest.mark.asyncio
async def test_direct_message_unspecified_booking_choices_preserve_default_hour(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the meeting room tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    assert "2:00 PM to 3:00 PM" in result["message"]
    assert result["blocks"][1]["elements"][0]["action_id"] == CHOOSE_ROOM_ACTION_ID


@pytest.mark.asyncio
async def test_large_room_message_selects_big_room_over_model_parameter(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the large room tomorrow at 2pm",
        params={
            "action": "book_meeting_room",
            "room": "small-meeting-room",
        },
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    assert [call[0] for call in calls] == ["rooms", "availability"]
    assert calls[1][2]["room_slug"] == "big-meeting-room"
    confirm = result["blocks"][1]["elements"][0]
    parsed = parse_action_value(
        confirm["value"],
        expected_action=BOOK_ACTION_ID,
    )
    assert parsed["room_slug"] == "big-meeting-room"
    assert "Big Meeting Room" in result["message"]


@pytest.mark.asyncio
async def test_bare_24_hour_availability_request_checks_exact_interval(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="is the meeting room free tomorrow 14:00?",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    assert [call[0] for call in calls] == ["rooms", "availability", "availability"]
    call = calls[1]
    assert call[0] == "availability"
    assert datetime.fromisoformat(call[2]["starts_at"]).hour == 14
    assert datetime.fromisoformat(call[2]["ends_at"]).hour == 15
    assert "is available" in result["message"]


@pytest.mark.asyncio
async def test_misspelled_tomorrow_clarification_checks_requested_time(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="tommorrow at 1pm",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    assert [call[0] for call in calls] == ["rooms", "availability", "availability"]
    starts_at = datetime.fromisoformat(calls[1][2]["starts_at"])
    ends_at = datetime.fromisoformat(calls[1][2]["ends_at"])
    assert starts_at.hour == 13
    assert ends_at.hour == 14
    assert "What date should I check?" not in result["message"]
    assert "is available" in result["message"]


@pytest.mark.asyncio
async def test_availability_without_date_defaults_to_next_occurrence(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)
    monkeypatch.setattr(
        room_module,
        "get_current_datetime",
        lambda: datetime(2026, 8, 11, 7, 20, tzinfo=MELBOURNE),
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="is the meeting room free at 3pm",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    starts_at = datetime.fromisoformat(calls[1][2]["starts_at"])
    assert starts_at == datetime(2026, 8, 11, 15, tzinfo=MELBOURNE)
    assert "What date should I check?" not in result["message"]


@pytest.mark.asyncio
async def test_booking_without_date_defaults_to_next_occurrence(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)
    monkeypatch.setattr(
        room_module,
        "get_current_datetime",
        lambda: datetime(2026, 8, 11, 7, 20, tzinfo=MELBOURNE),
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the big meeting room for me at 3pm",
        params={"action": "book_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    starts_at = datetime.fromisoformat(calls[1][2]["starts_at"])
    assert starts_at == datetime(2026, 8, 11, 15, tzinfo=MELBOURNE)
    assert "Big Meeting Room" in result["message"]
    assert result["blocks"][1]["elements"][0]["text"]["text"] == "Confirm booking"


@pytest.mark.asyncio
async def test_booking_without_date_or_time_asks_only_for_time(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book meeting room",
        params={"action": "book_meeting_room"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    assert result["message"] == "What time should the booking start? Try `2pm`."
    assert "What date should I check?" not in result["message"]


@pytest.mark.asyncio
async def test_bare_misspelled_tomorrow_clarification_checks_the_day(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)
    monkeypatch.setattr(
        room_module,
        "get_current_datetime",
        lambda: datetime(2026, 8, 11, 9, tzinfo=MELBOURNE),
    )

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="tommorrow",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    assert [call[0] for call in calls] == ["rooms", "availability", "availability"]
    assert calls[1][2]["date"] == "2026-08-12"
    assert "starts_at" not in calls[1][2]
    assert "What date should I check?" not in result["message"]
    assert "no bookings currently shown" in result["message"]


@pytest.mark.asyncio
async def test_long_availability_request_is_answered_without_booking_price(monkeypatch):
    configured = _settings()
    _patch_executor(monkeypatch, configured)

    result = await SkillExecutor()._execute_meeting_room_booking(
        text="is the meeting room free tomorrow from 2pm to 6pm?",
        params={"action": "check_room_availability"},
        user_id="UOWNER",
        channel_id="DOWNER",
    )

    calls = FakeMeetingRoomClient.instances[0].calls
    assert [call[0] for call in calls] == ["rooms", "availability", "availability"]
    call = calls[1]
    assert datetime.fromisoformat(call[2]["ends_at"]) - datetime.fromisoformat(
        call[2]["starts_at"]
    ) == timedelta(hours=4)
    assert "is available" in result["message"]
    assert "availability check only" in result["message"]
    assert "costs" not in result["message"]


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
        text="book the small meeting room for <@UOTHER|Other Member> tomorrow at 2pm for 1.5 hours",
        params={"action": "book_meeting_room"},
        user_id="UADMIN",
        channel_id="DADMIN",
    )

    assert "for <@UOTHER>" in result["message"]
    assert "1.5 hours" in result["message"]
    assert "Their Roo Points account will be charged" in result["message"]
    availability_call = FakeMeetingRoomClient.instances[0].calls[1]
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
        text="book the small meeting room for <@UOTHER> tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UNONADMIN",
        channel_id="DNONADMIN",
    )

    assert "Only full Roo Points Admins" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("unlinked_user", "tagged member does not have a linked MLAI account"),
        ("inactive_user", "tagged member's MLAI account is inactive"),
        ("insufficient_balance", "tagged member does not have enough Roo Points"),
    ),
)
async def test_admin_target_errors_identify_the_tagged_member(
    monkeypatch,
    code,
    expected,
):
    configured = _settings()

    class RejectedClient(FakeMeetingRoomClient):
        async def check_meeting_room_availability(self, slack_user_id, **kwargs):
            request = httpx.Request(
                "POST",
                "https://backend.test/meeting-rooms/availability/",
            )
            response = httpx.Response(
                409,
                request=request,
                json={"code": code, "error": "backend detail"},
            )
            response.raise_for_status()

    _patch_executor(monkeypatch, configured, RejectedClient)
    result = await SkillExecutor()._execute_meeting_room_booking(
        text="book the small meeting room for <@UOTHER> tomorrow at 2pm",
        params={"action": "book_meeting_room"},
        user_id="UADMIN",
        channel_id="DADMIN",
    )

    assert expected in result["message"]
    assert "your linked MLAI account" not in result["message"]


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
        (None, "member notification is still pending"),
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
        room_slug="small-meeting-room",
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
        lambda user_id, message, **kwargs: (
            target_dms.append((user_id, message, kwargs)) or dm_response
        ),
    )
    monkeypatch.setattr(
        "roo.slack_client.get_slack_client",
        lambda: SimpleNamespace(
            chat_update=lambda **kwargs: updates.append(kwargs) or {"ok": True}
        ),
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
    assert "<@UOWNER> booked the *Small Meeting Room* for you" in target_dms[0][1]
    assert "Charged to your account:* 2 Roo Points" in target_dms[0][1]
    assert "show my meeting room bookings" in target_dms[0][1]
    assert "Remaining balance" not in target_dms[0][1]
    assert target_dms[0][2] == {"raise_on_error": True}


@pytest.mark.asyncio
async def test_replayed_admin_booking_recovers_target_notification_idempotently(monkeypatch):
    configured = _settings()
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
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def book_meeting_room(self, slack_user_id, **kwargs):
            return _admin_booking_response(starts_at, created=False)

    updates = []
    target_dms = []
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", Client)
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

    await main_module._handle_meeting_room_action(
        settings=configured,
        action_id=BOOK_ACTION_ID,
        action_value=action_value,
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="123.456",
    )

    assert "was already confirmed" in updates[0]["text"]
    assert "notified" in updates[0]["text"]
    assert target_dms[0][0][0] == "UTARGET"
    assert target_dms[0][1] == {"raise_on_error": True}


@pytest.mark.asyncio
async def test_action_handler_rejects_mismatched_actor_before_backend(monkeypatch):
    configured = _settings()
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="small-meeting-room",
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
        room_slug="small-meeting-room",
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
        room_slug="small-meeting-room",
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
        room_slug="small-meeting-room",
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


def test_duplicate_signed_button_delivery_processes_one_durable_action(tmp_path, monkeypatch):
    configured = _settings(SLACK_RECEIPTS_DB_PATH=str(tmp_path / "receipts.db"))
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    starts_at = datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    action_value = build_booking_action_value(
        owner_slack_user_id="UOWNER",
        room_slug="small-meeting-room",
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
    scheduled = []
    processed = []

    async def fake_processor(record):
        processed.append(record["message_ts"])

    monkeypatch.setattr(
        main_module,
        "_process_meeting_room_action_record",
        fake_processor,
    )
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=headers)
    second = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(scheduled) == 2

    async def process_scheduled():
        await asyncio.gather(*scheduled)

    asyncio.run(process_scheduled())
    assert processed == ["card.456"]
    store = action_module.get_meeting_room_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    assert store.get(1)["status"] == "completed"


def test_competing_room_click_feedback_is_not_repeated_for_slack_retry(
    tmp_path,
    monkeypatch,
):
    configured = _settings(SLACK_RECEIPTS_DB_PATH=str(tmp_path / "receipts.db"))
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    starts_at = (
        datetime.now(MELBOURNE).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    prompt = room_module.room_selection_prompt(
        [
            {"slug": "small-meeting-room", "name": "Small Meeting Room"},
            {"slug": "big-meeting-room", "name": "Big Meeting Room"},
        ],
        owner_slack_user_id="UOWNER",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    small_button, big_button = prompt["blocks"][1]["elements"]
    store = action_module.get_meeting_room_action_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    store.record_action(
        action_id=CHOOSE_ROOM_ACTION_ID,
        action_value=small_button["value"],
        actor_user_id="UOWNER",
        channel_id="DOWNER",
        message_ts="card.456",
    )
    payload = {
        "type": "block_actions",
        "user": {"id": "UOWNER"},
        "channel": {"id": "DOWNER"},
        "container": {"message_ts": "card.456"},
        "message": {"ts": "card.456"},
        "actions": [big_button],
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
    posted = []
    monkeypatch.setattr(main_module, "start_slack_action", scheduled.append)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )
    client = TestClient(main_module.app)

    first = client.post("/slack/actions", content=body, headers=headers)
    retry = client.post("/slack/actions", content=body, headers=headers)

    assert first.status_code == 200
    assert retry.status_code == 200
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert len(posted) == 1
    assert "already chose the *Small Meeting Room*" in posted[0]["text"]


@pytest.mark.asyncio
async def test_meeting_room_action_task_is_retained_until_completion():
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

    await client.list_meeting_rooms()
    await client.check_meeting_room_availability(
        "<@UOWNER>",
        room_slug="small-meeting-room",
        date="2026-08-12",
        target_slack_user_id="<@UTARGET>",
    )
    await client.book_meeting_room(
        "<@UOWNER>",
        room_slug="big-meeting-room",
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
        "/api/v1/points/meeting-rooms/rooms/",
        "/api/v1/points/meeting-rooms/availability/",
        "/api/v1/points/meeting-rooms/book/",
        "/api/v1/points/meeting-rooms/my-bookings/",
        "/api/v1/points/meeting-rooms/cancel/",
    ]
    assert captured[1][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[1][2]["json"]["room_slug"] == "small-meeting-room"
    assert captured[1][2]["json"]["target_slack_user_id"] == "UTARGET"
    assert captured[2][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[2][2]["json"]["room_slug"] == "big-meeting-room"
    assert captured[2][2]["json"]["client_request_id"] == "1409fd17-c84d-4774-af8a-7b847c16bd30"
    assert captured[2][2]["json"]["expected_points_cost"] == 2
    assert captured[2][2]["json"]["target_slack_user_id"] == "UTARGET"
    assert captured[3][2]["json"]["slack_user_id"] == "UOWNER"
    assert captured[4][2]["json"]["slack_user_id"] == "UOWNER"


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
