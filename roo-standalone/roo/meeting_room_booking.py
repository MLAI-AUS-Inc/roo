from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx

from .utils import get_current_datetime


MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
BOOK_ACTION_ID = "meeting_room_confirm_booking"
CANCEL_ACTION_ID = "meeting_room_cancel_booking"
CHOOSE_ROOM_ACTION_ID = "meeting_room_choose_room"
CHOOSE_ROOM_ACTION_IDS_BY_ROOM = {
    "big-meeting-room": f"{CHOOSE_ROOM_ACTION_ID}_big",
    "small-meeting-room": f"{CHOOSE_ROOM_ACTION_ID}_small",
}
CHOOSE_ROOM_ACTION_IDS = frozenset(
    {CHOOSE_ROOM_ACTION_ID, *CHOOSE_ROOM_ACTION_IDS_BY_ROOM.values()}
)
CONFIRMATION_TTL = timedelta(minutes=10)
MIN_BOOKING_HALF_HOURS = 2
MAX_BOOKING_HALF_HOURS = 4
MIN_AVAILABILITY_HALF_HOURS = 1
MAX_AVAILABILITY_HALF_HOURS = 48
ROOM_CHOICES = (
    ("small-meeting-room", "Small Meeting Room"),
    ("big-meeting-room", "Big Meeting Room"),
)
ROOM_NAMES = dict(ROOM_CHOICES)
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
TOMORROW_ALIASES = frozenset(
    {
        "tomorrow",
        "tomorow",
        "tommorow",
        "tommorrow",
    }
)
TOMORROW_REFERENCE_RE = re.compile(
    r"\b(?:tomorrow|tomorow|tommorow|tommorrow)\b",
    re.IGNORECASE,
)
UNRESOLVED_DATE_REFERENCE_RE = re.compile(
    r"\b(?:yesterday|tonight|next\s+(?:week|month|year)|this\s+(?:week|month|year)|"
    r"someday|tomorrow\w+|tomorow\w+|tommorow\w+|tommorrow\w+)\b",
    re.IGNORECASE,
)


class MeetingRoomInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def room_slug_from_text(text: str) -> Optional[str]:
    normalized = str(text or "").lower()
    small = bool(re.search(r"\bsmall(?:\s+meeting)?\s+room\b", normalized))
    big = bool(
        re.search(r"\b(?:big|large)(?:\s+meeting)?\s+room\b", normalized)
    )
    if small == big:
        return None
    return "small-meeting-room" if small else "big-meeting-room"


def room_choice_action_id(room_slug: str) -> str:
    """Return the unique Slack action ID for a generated private room button."""

    try:
        return CHOOSE_ROOM_ACTION_IDS_BY_ROOM[str(room_slug)]
    except KeyError as exc:
        raise ValueError("room_slug is not supported for private buttons") from exc


def supported_active_rooms(rooms: list[dict]) -> list[dict]:
    by_slug = {
        str(room.get("slug") or "").strip(): room
        for room in rooms
        if isinstance(room, dict)
    }
    return [
        {
            "id": by_slug[slug].get("id"),
            "slug": slug,
            "name": ROOM_NAMES[slug],
        }
        for slug, _ in ROOM_CHOICES
        if slug in by_slug
    ]


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MeetingRoomInputError(
            "invalid_time",
            "Please include a timezone in the booking time.",
        )
    return parsed.astimezone(MELBOURNE_TZ)


def _parse_date_value(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def has_date_reference(text: str, params: Optional[dict] = None) -> bool:
    params = params or {}
    if params.get("date") or params.get("starts_at") or params.get("start"):
        return True
    normalized = str(text or "").lower()
    return bool(
        re.search(
            r"\b(?:today|next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            normalized,
        )
        or re.search(r"\b\d{4}-\d{2}-\d{2}\b", normalized)
        or re.search(r"\btoday\b", normalized)
        or TOMORROW_REFERENCE_RE.search(normalized)
    )


def resolve_local_date(
    text: str,
    params: Optional[dict] = None,
    *,
    now: Optional[datetime] = None,
) -> date:
    params = params or {}
    current = (now or get_current_datetime()).astimezone(MELBOURNE_TZ)

    raw_param_date = str(params.get("date") or "").strip().lower()
    direct = _parse_date_value(raw_param_date)
    if direct:
        return direct
    if raw_param_date == "today":
        return current.date()
    if raw_param_date in TOMORROW_ALIASES:
        return current.date() + timedelta(days=1)
    normalized_param_date = raw_param_date.removeprefix("next ")
    if normalized_param_date in WEEKDAYS:
        target_weekday = WEEKDAYS[normalized_param_date]
        offset = (target_weekday - current.weekday()) % 7
        if raw_param_date.startswith("next "):
            offset = offset + 7 if offset else 7
        return current.date() + timedelta(days=offset)

    for key in ("starts_at", "start"):
        parsed_timestamp = _parse_iso_timestamp(params.get(key))
        if parsed_timestamp:
            return parsed_timestamp.date()

    normalized = str(text or "").lower()
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", normalized)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            raise MeetingRoomInputError(
                "invalid_date",
                "That date is not valid. Use a date like 2026-08-14.",
            )
    if TOMORROW_REFERENCE_RE.search(normalized):
        return current.date() + timedelta(days=1)
    if re.search(r"\btoday\b", normalized):
        return current.date()

    weekday_match = re.search(
        r"\b(?P<next>next\s+)?(?P<weekday>" + "|".join(WEEKDAYS) + r")\b",
        normalized,
    )
    if weekday_match:
        target_weekday = WEEKDAYS[weekday_match.group("weekday")]
        offset = (target_weekday - current.weekday()) % 7
        if weekday_match.group("next"):
            offset = offset + 7 if offset else 7
        return current.date() + timedelta(days=offset)

    if raw_param_date or UNRESOLVED_DATE_REFERENCE_RE.search(normalized):
        raise MeetingRoomInputError(
            "invalid_date",
            "I could not understand that date. Try `tomorrow` or `2026-08-14`.",
        )

    return current.date() + timedelta(days=1)


def _parse_time_value(value: Any, field_label: str) -> Optional[time]:
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    match = re.fullmatch(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)?", raw)
    if not match:
        raise MeetingRoomInputError(
            "invalid_time",
            f"I could not understand the {field_label}. Use a time like `2pm` or `14:00`.",
        )
    hour_text = match.group("hour")
    hour = int(hour_text)
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm")
    if minute not in (0, 30):
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting Room bookings must start and end on the hour or half-hour.",
        )
    if ampm:
        if not 1 <= hour <= 12:
            raise MeetingRoomInputError("invalid_time", f"The {field_label} is not valid.")
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    elif hour <= 12 and not hour_text.startswith("0"):
        raise MeetingRoomInputError(
            "ambiguous_time",
            f"Is the {field_label} AM or PM? Try `2pm` or `14:00`.",
        )
    if not 0 <= hour <= 23:
        raise MeetingRoomInputError("invalid_time", f"The {field_label} is not valid.")
    return time(hour=hour, minute=minute)


def _local_datetime(local_date: date, local_time: time) -> datetime:
    naive = datetime.combine(local_date, local_time)
    aware = naive.replace(tzinfo=MELBOURNE_TZ)
    round_trip = aware.astimezone(timezone.utc).astimezone(MELBOURNE_TZ)
    if round_trip.replace(tzinfo=None) != naive:
        raise MeetingRoomInputError(
            "invalid_time",
            "That local time does not exist because of daylight saving. Choose another hour.",
        )
    alternate = naive.replace(tzinfo=MELBOURNE_TZ, fold=1)
    if alternate.utcoffset() != aware.utcoffset():
        raise MeetingRoomInputError(
            "ambiguous_time",
            "That local hour occurs twice because of daylight saving. Choose another hour.",
        )
    return aware


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _add_actual_half_hours(value: datetime, half_hours: int) -> datetime:
    return (_as_utc(value) + timedelta(minutes=30 * half_hours)).astimezone(
        MELBOURNE_TZ
    )


def _duration_half_hours(
    value: Any,
    *,
    minimum: int = MIN_BOOKING_HALF_HOURS,
    maximum: int = MAX_BOOKING_HALF_HOURS,
) -> int:
    if isinstance(value, bool):
        raise MeetingRoomInputError(
            "invalid_time",
            "The duration must be between 1 and 2 hours in 30-minute increments.",
        )
    try:
        duration = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise MeetingRoomInputError(
            "invalid_time",
            "The duration must be between 1 and 2 hours in 30-minute increments.",
        )
    half_hours = duration * 2
    if half_hours != half_hours.to_integral_value():
        raise MeetingRoomInputError(
            "invalid_time",
            "The duration must use 30-minute increments, such as 1 or 1.5 hours.",
        )
    result = int(half_hours)
    if result < minimum or result > maximum:
        if minimum == MIN_AVAILABILITY_HALF_HOURS and maximum == MAX_AVAILABILITY_HALF_HOURS:
            message = "Availability checks must be between 30 minutes and 24 hours."
        else:
            message = "Meeting Room bookings must be between 1 and 2 hours."
        raise MeetingRoomInputError(
            "invalid_time",
            message,
        )
    return result


def _natural_duration(text: str) -> Optional[str]:
    normalized = str(text or "").lower()
    if re.search(
        r"\bfor\s+(?:half\s+(?:an?\s+)?hour|an?\s+half[- ]hour)\b",
        normalized,
    ):
        return "0.5"
    if re.search(
        r"\bfor\s+(?:(?:an?|one)\s+hour\s+and\s+a\s+half|one\s+and\s+a\s+half\s+hours?)\b",
        normalized,
    ):
        return "1.5"
    if re.search(r"\bfor\s+(?:an?|one)\s+hours?\b", normalized):
        return "1"
    if re.search(r"\bfor\s+two\s+(?:hours?|hrs?)\b", normalized):
        return "2"
    hours_match = re.search(
        r"\bfor\s+(?P<hours>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\b",
        normalized,
    )
    if hours_match:
        return hours_match.group("hours")
    minutes_match = re.search(
        r"\bfor\s+(?P<minutes>\d+)\s*(?:minutes?|mins?)\b",
        normalized,
    )
    if minutes_match:
        return str(Decimal(minutes_match.group("minutes")) / Decimal(60))
    if re.search(
        r"\bfor\b[^.?!,\n]{0,40}\b(?:hours?|hrs?|minutes?|mins?)\b",
        normalized,
    ):
        raise MeetingRoomInputError(
            "invalid_time",
            "I could not understand that duration. Try `1 hour`, `1.5 hours`, or `90 minutes`.",
        )
    return None


def _natural_time_tokens(text: str) -> tuple[Optional[str], Optional[str]]:
    normalized = str(text or "").lower()
    token = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    range_match = re.search(
        rf"\bfrom\s+(?P<start>{token})\s+(?:to|until|-)\s*(?P<end>{token})\b",
        normalized,
    )
    if range_match:
        return range_match.group("start"), range_match.group("end")
    explicit_token = r"(?:\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))"
    range_match = re.search(
        rf"\b(?P<start>{explicit_token})\s*(?:to|until|-)\s*(?P<end>{explicit_token})\b",
        normalized,
    )
    if range_match:
        return range_match.group("start"), range_match.group("end")
    start_match = re.search(rf"\bat\s+(?P<start>{token})\b", normalized)
    if start_match:
        return start_match.group("start"), None
    bare_times = re.findall(rf"\b({explicit_token})\b", normalized)
    return (bare_times[0], None) if len(bare_times) == 1 else (None, None)


def _validate_resolved_interval(
    starts_at: datetime,
    ends_at: datetime,
    *,
    now: Optional[datetime],
    minimum_half_hours: int = MIN_BOOKING_HALF_HOURS,
    maximum_half_hours: int = MAX_BOOKING_HALF_HOURS,
) -> tuple[datetime, datetime]:
    current = (now or get_current_datetime()).astimezone(timezone.utc)
    utc_start = _as_utc(starts_at)
    utc_end = _as_utc(ends_at)
    if utc_start <= current:
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting Room bookings must start in the future.",
        )
    duration_seconds = (utc_end - utc_start).total_seconds()
    for value in (starts_at.astimezone(MELBOURNE_TZ), ends_at.astimezone(MELBOURNE_TZ)):
        if value.minute not in (0, 30) or value.second or value.microsecond:
            raise MeetingRoomInputError(
                "invalid_time",
                "Meeting Room bookings must start and end on the hour or half-hour.",
            )
    if duration_seconds <= 0 or duration_seconds % 1800:
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting Room bookings must use 30-minute increments.",
        )
    duration_half_hours = int(duration_seconds // 1800)
    if duration_half_hours < minimum_half_hours:
        minimum_message = (
            "Availability checks must be at least 30 minutes."
            if minimum_half_hours == MIN_AVAILABILITY_HALF_HOURS
            else "Meeting Room bookings must be at least one hour."
        )
        raise MeetingRoomInputError(
            "invalid_time",
            minimum_message,
        )
    if duration_half_hours > maximum_half_hours:
        maximum_message = (
            "Availability checks can span at most 24 hours."
            if maximum_half_hours == MAX_AVAILABILITY_HALF_HOURS
            else "Meeting Room bookings can be at most two hours. Check the end time and try again."
        )
        raise MeetingRoomInputError(
            "invalid_time",
            maximum_message,
        )
    return starts_at, ends_at


def _implicit_same_day_weekday_has_passed(
    text: str,
    params: dict,
    *,
    local_date: date,
    starts_at: datetime,
    now: Optional[datetime],
) -> bool:
    current = (now or get_current_datetime()).astimezone(MELBOURNE_TZ)
    if local_date != current.date() or _as_utc(starts_at) > _as_utc(current):
        return False

    normalized = str(text or "").lower()
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b|\btoday\b|\btomorrow\b", normalized):
        return False
    if re.search(
        r"\b(?<!next\s)(?:" + "|".join(WEEKDAYS) + r")\b",
        normalized,
    ):
        return True

    raw_param_date = str(params.get("date") or "").strip().lower()
    return bool(
        raw_param_date in WEEKDAYS
        and not re.search(
            r"\bnext\s+(?:" + "|".join(WEEKDAYS) + r")\b",
            normalized,
        )
    )


def resolve_interval(
    text: str,
    params: Optional[dict] = None,
    *,
    now: Optional[datetime] = None,
    minimum_half_hours: int = MIN_BOOKING_HALF_HOURS,
    maximum_half_hours: int = MAX_BOOKING_HALF_HOURS,
) -> tuple[datetime, datetime]:
    params = params or {}
    reference_now = now or get_current_datetime()
    exact_start = _parse_iso_timestamp(params.get("starts_at"))
    exact_end = _parse_iso_timestamp(params.get("ends_at"))
    if exact_start or exact_end:
        if not exact_start:
            raise MeetingRoomInputError("missing_start_time", "What time should the booking start?")
        if exact_end:
            return _validate_resolved_interval(
                exact_start,
                exact_end,
                now=reference_now,
                minimum_half_hours=minimum_half_hours,
                maximum_half_hours=maximum_half_hours,
            )
        duration = params.get("duration_hours", 1)
        ends_at = _add_actual_half_hours(
            exact_start,
            _duration_half_hours(
                duration,
                minimum=minimum_half_hours,
                maximum=maximum_half_hours,
            ),
        )
        return _validate_resolved_interval(
            exact_start,
            ends_at,
            now=reference_now,
            minimum_half_hours=minimum_half_hours,
            maximum_half_hours=maximum_half_hours,
        )

    local_date = resolve_local_date(text, params, now=reference_now)
    natural_start, natural_end = _natural_time_tokens(text)
    start_value = params.get("start_time") or natural_start
    end_value = params.get("end_time") or natural_end
    start_time = _parse_time_value(start_value, "start time")
    if start_time is None:
        raise MeetingRoomInputError(
            "missing_start_time",
            "What time should the booking start? Try `2pm`.",
        )

    if not has_date_reference(text, params):
        current = reference_now.astimezone(MELBOURNE_TZ)
        same_day_start = _local_datetime(current.date(), start_time)
        local_date = (
            current.date()
            if _as_utc(same_day_start) > _as_utc(current)
            else current.date() + timedelta(days=1)
        )

    starts_at = _local_datetime(local_date, start_time)
    end_time = _parse_time_value(end_value, "end time")
    if end_time is not None:
        ends_at = _local_datetime(local_date, end_time)
        if _as_utc(ends_at) <= _as_utc(starts_at):
            ends_at = _local_datetime(local_date + timedelta(days=1), end_time)
    else:
        raw_duration = params.get("duration_hours")
        if raw_duration is None:
            raw_duration = _natural_duration(text) or 1
        ends_at = _add_actual_half_hours(
            starts_at,
            _duration_half_hours(
                raw_duration,
                minimum=minimum_half_hours,
                maximum=maximum_half_hours,
            ),
        )
    if _implicit_same_day_weekday_has_passed(
        text,
        params,
        local_date=local_date,
        starts_at=starts_at,
        now=reference_now,
    ):
        starts_at = _local_datetime(
            starts_at.date() + timedelta(days=7),
            starts_at.time().replace(tzinfo=None),
        )
        ends_at = _local_datetime(
            ends_at.date() + timedelta(days=7),
            ends_at.time().replace(tzinfo=None),
        )
    return _validate_resolved_interval(
        starts_at,
        ends_at,
        now=reference_now,
        minimum_half_hours=minimum_half_hours,
        maximum_half_hours=maximum_half_hours,
    )


def resolve_availability_interval(
    text: str,
    params: Optional[dict] = None,
    *,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    return resolve_interval(
        text,
        params,
        now=now,
        minimum_half_hours=MIN_AVAILABILITY_HALF_HOURS,
        maximum_half_hours=MAX_AVAILABILITY_HALF_HOURS,
    )


def _format_clock(value: datetime) -> str:
    rendered = value.astimezone(MELBOURNE_TZ).strftime("%I:%M %p")
    return rendered.lstrip("0")


def format_interval(starts_at: datetime, ends_at: datetime) -> str:
    local_start = starts_at.astimezone(MELBOURNE_TZ)
    local_end = ends_at.astimezone(MELBOURNE_TZ)
    start_date = local_start.strftime("%A %-d %B %Y")
    if local_start.date() == local_end.date():
        return f"{start_date}, {_format_clock(local_start)} to {_format_clock(local_end)}"
    end_date = local_end.strftime("%A %-d %B %Y")
    return (
        f"{start_date}, {_format_clock(local_start)} to "
        f"{end_date}, {_format_clock(local_end)}"
    )


def parse_backend_timestamp(value: Any) -> datetime:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        raise MeetingRoomInputError("invalid_response", "The backend returned an invalid booking time.")
    return parsed


def build_booking_action_value(
    *,
    owner_slack_user_id: str,
    room_slug: str,
    starts_at: datetime,
    ends_at: datetime,
    expected_points_cost: int,
    target_slack_user_id: Optional[str] = None,
    client_request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    if room_slug not in ROOM_NAMES:
        raise MeetingRoomInputError(
            "invalid_response",
            "That meeting room is not supported. Ask Roo to start again.",
        )
    issued_at = (now or get_current_datetime()).astimezone(timezone.utc)
    payload = {
        "owner_slack_user_id": owner_slack_user_id,
        "room_slug": room_slug,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "expected_points_cost": expected_points_cost,
        "client_request_id": str(client_request_id or uuid4()),
        "confirmation_expires_at": (issued_at + CONFIRMATION_TTL).isoformat(),
    }
    if target_slack_user_id and target_slack_user_id != owner_slack_user_id:
        payload["target_slack_user_id"] = target_slack_user_id
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def build_cancel_action_value(*, owner_slack_user_id: str, booking_id: str) -> str:
    try:
        normalized_booking_id = str(UUID(str(booking_id)))
    except (TypeError, ValueError, AttributeError):
        raise MeetingRoomInputError(
            "invalid_response",
            "I could not read one of those bookings. Ask Roo to list your bookings again.",
        )
    return json.dumps(
        {
            "owner_slack_user_id": owner_slack_user_id,
            "booking_id": normalized_booking_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def build_room_choice_action_value(
    *,
    owner_slack_user_id: str,
    selection_id: str,
    room_slug: str,
    starts_at: datetime,
    ends_at: datetime,
    booking_client_request_id: str,
    selection_expires_at: datetime,
    target_slack_user_id: Optional[str] = None,
) -> str:
    payload = {
        "owner_slack_user_id": owner_slack_user_id,
        "selection_id": selection_id,
        "room_slug": room_slug,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "booking_client_request_id": booking_client_request_id,
        "selection_expires_at": selection_expires_at.isoformat(),
    }
    if target_slack_user_id and target_slack_user_id != owner_slack_user_id:
        payload["target_slack_user_id"] = target_slack_user_id
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_action_value(raw_value: Any, *, expected_action: str) -> dict:
    try:
        payload = json.loads(str(raw_value or ""))
    except (TypeError, json.JSONDecodeError):
        raise MeetingRoomInputError("invalid_action", "This action is not valid. Ask Roo to start again.")
    if not isinstance(payload, dict) or not str(payload.get("owner_slack_user_id") or "").strip():
        raise MeetingRoomInputError("invalid_action", "This action is not valid. Ask Roo to start again.")

    if expected_action == BOOK_ACTION_ID:
        required = (
            "room_slug",
            "starts_at",
            "ends_at",
            "client_request_id",
            "confirmation_expires_at",
            "expected_points_cost",
        )
        if any(not payload.get(field) for field in required):
            raise MeetingRoomInputError("invalid_action", "This booking action is incomplete. Ask Roo to start again.")
        if payload["room_slug"] not in ROOM_NAMES:
            raise MeetingRoomInputError(
                "invalid_action",
                "This booking action is not valid. Ask Roo to start again.",
            )
        try:
            UUID(str(payload["client_request_id"]))
        except ValueError:
            raise MeetingRoomInputError("invalid_action", "This booking action is not valid. Ask Roo to start again.")
        parse_backend_timestamp(payload["starts_at"])
        parse_backend_timestamp(payload["ends_at"])
        parse_backend_timestamp(payload["confirmation_expires_at"])
        try:
            expected_cost = Decimal(str(payload["expected_points_cost"]))
            if (
                not expected_cost.is_finite()
                or expected_cost != expected_cost.to_integral_value()
                or expected_cost < 0
            ):
                raise ValueError
        except (InvalidOperation, TypeError, ValueError):
            raise MeetingRoomInputError("invalid_action", "This booking action is not valid. Ask Roo to start again.")
        payload["expected_points_cost"] = int(expected_cost)
        target_slack_user_id = str(payload.get("target_slack_user_id") or "").strip()
        if target_slack_user_id and not re.fullmatch(r"[A-Z0-9]+", target_slack_user_id):
            raise MeetingRoomInputError("invalid_action", "This booking action is not valid. Ask Roo to start again.")
    elif expected_action == CHOOSE_ROOM_ACTION_ID:
        required = (
            "selection_id",
            "room_slug",
            "starts_at",
            "ends_at",
            "booking_client_request_id",
            "selection_expires_at",
        )
        if any(not payload.get(field) for field in required):
            raise MeetingRoomInputError(
                "invalid_action",
                "This room choice is incomplete. Ask Roo to start again.",
            )
        try:
            UUID(str(payload["selection_id"]))
            UUID(str(payload["booking_client_request_id"]))
        except ValueError:
            raise MeetingRoomInputError(
                "invalid_action",
                "This room choice is not valid. Ask Roo to start again.",
            )
        if payload["room_slug"] not in ROOM_NAMES:
            raise MeetingRoomInputError(
                "invalid_action",
                "This room choice is not supported. Ask Roo to start again.",
            )
        parse_backend_timestamp(payload["starts_at"])
        parse_backend_timestamp(payload["ends_at"])
        parse_backend_timestamp(payload["selection_expires_at"])
        target_slack_user_id = str(payload.get("target_slack_user_id") or "").strip()
        if target_slack_user_id and not re.fullmatch(r"[A-Z0-9]+", target_slack_user_id):
            raise MeetingRoomInputError(
                "invalid_action",
                "This room choice is not valid. Ask Roo to start again.",
            )
    elif expected_action == CANCEL_ACTION_ID:
        try:
            UUID(str(payload.get("booking_id") or ""))
        except ValueError:
            raise MeetingRoomInputError("invalid_action", "This cancellation action is not valid. Ask Roo to start again.")
    else:
        raise MeetingRoomInputError("invalid_action", "This action is not supported.")
    return payload


def confirmation_expired(payload: dict, *, now: Optional[datetime] = None) -> bool:
    expires_at = parse_backend_timestamp(payload.get("confirmation_expires_at"))
    current = (now or get_current_datetime()).astimezone(timezone.utc)
    return expires_at.astimezone(timezone.utc) <= current


def room_choice_expired(payload: dict, *, now: Optional[datetime] = None) -> bool:
    expires_at = parse_backend_timestamp(payload.get("selection_expires_at"))
    current = (now or get_current_datetime()).astimezone(timezone.utc)
    return expires_at.astimezone(timezone.utc) <= current


def validate_room_selection_prompt(message: str, blocks: list[dict]) -> None:
    """Fail closed before Slack sees a malformed private room-choice card."""

    def invalid_prompt() -> MeetingRoomInputError:
        return MeetingRoomInputError(
            "invalid_response",
            "I couldn't build a safe room-choice card. Ask Roo to start again.",
        )

    fallback_text = str(message or "").strip()
    if (
        not fallback_text
        or len(fallback_text) > 40_000
        or not isinstance(blocks, list)
        or len(blocks) > 50
        or any(not isinstance(block, dict) for block in blocks)
    ):
        raise invalid_prompt()

    actions_blocks = [block for block in blocks if block.get("type") == "actions"]
    if len(actions_blocks) != 1:
        raise invalid_prompt()
    section_blocks = [block for block in blocks if block.get("type") == "section"]
    if len(section_blocks) != 1:
        raise invalid_prompt()
    section_text_payload = section_blocks[0].get("text")
    if not isinstance(section_text_payload, dict):
        raise invalid_prompt()
    section_text = section_text_payload.get("text")
    if (
        section_text_payload.get("type") not in {"mrkdwn", "plain_text"}
        or not isinstance(section_text, str)
        or not section_text.strip()
        or len(section_text) > 3_000
    ):
        raise invalid_prompt()
    block_id = str(actions_blocks[0].get("block_id") or "")
    if not block_id or len(block_id) > 255:
        raise invalid_prompt()
    elements = actions_blocks[0].get("elements")
    if not isinstance(elements, list) or not elements or len(elements) > 25:
        raise invalid_prompt()

    seen_action_ids: set[str] = set()
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "button":
            raise invalid_prompt()
        action_id = str(element.get("action_id") or "")
        label_payload = element.get("text")
        if not isinstance(label_payload, dict):
            raise invalid_prompt()
        label = label_payload.get("text")
        raw_value = str(element.get("value") or "")
        if (
            not action_id
            or len(action_id) > 255
            or action_id in seen_action_ids
            or action_id not in CHOOSE_ROOM_ACTION_IDS_BY_ROOM.values()
            or label_payload.get("type") != "plain_text"
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 75
            or not raw_value
            or len(raw_value) > 2_000
        ):
            raise invalid_prompt()
        seen_action_ids.add(action_id)
        try:
            value = parse_action_value(
                raw_value,
                expected_action=CHOOSE_ROOM_ACTION_ID,
            )
            expected_action_id = room_choice_action_id(value["room_slug"])
        except (KeyError, MeetingRoomInputError, ValueError) as exc:
            raise invalid_prompt() from exc
        if action_id != expected_action_id:
            raise invalid_prompt()


def room_selection_prompt(
    rooms: list[dict],
    *,
    owner_slack_user_id: str,
    starts_at: datetime,
    ends_at: datetime,
    target_slack_user_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    choices = supported_active_rooms(rooms)
    if not choices:
        raise MeetingRoomInputError(
            "inactive_room",
            "No meeting rooms are accepting bookings right now.",
        )
    issued_at = (now or get_current_datetime()).astimezone(timezone.utc)
    selection_id = str(uuid4())
    selection_expires_at = issued_at + CONFIRMATION_TTL
    target_text = (
        f" for <@{target_slack_user_id}>"
        if target_slack_user_id and target_slack_user_id != owner_slack_user_id
        else ""
    )
    message = (
        f"Choose a meeting room{target_text}.\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n\n"
        "No room is reserved until you choose a room and confirm the booking. "
        "These choices expire in 10 minutes."
    )
    buttons = []
    for room in choices:
        # Each option owns a booking id; the shared durable selection id ensures
        # only the first room clicked can advance to its confirmation card.
        buttons.append(
            {
                "type": "button",
                "action_id": room_choice_action_id(room["slug"]),
                "text": {"type": "plain_text", "text": room["name"]},
                "value": build_room_choice_action_value(
                    owner_slack_user_id=owner_slack_user_id,
                    selection_id=selection_id,
                    room_slug=room["slug"],
                    starts_at=starts_at,
                    ends_at=ends_at,
                    booking_client_request_id=str(uuid4()),
                    selection_expires_at=selection_expires_at,
                    target_slack_user_id=target_slack_user_id,
                ),
            }
        )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": message}},
        {
            "type": "actions",
            "block_id": f"meeting_room_choice_{selection_id}",
            "elements": buttons,
        },
    ]
    validate_room_selection_prompt(message, blocks)
    return {"message": message, "blocks": blocks}


def room_choice_already_selected_message(payload: dict) -> str:
    room_name = ROOM_NAMES.get(
        str(payload.get("room_slug") or ""),
        "meeting room",
    )
    return (
        f"You already chose the *{room_name}* for this request. "
        "Continue with that confirmation card, or start a new booking request "
        "to choose a different room."
    )


def booking_preview(
    availability: dict,
    *,
    owner_slack_user_id: str,
    starts_at: datetime,
    ends_at: datetime,
    expected_room_slug: Optional[str] = None,
    target_slack_user_id: Optional[str] = None,
    client_request_id: Optional[str] = None,
) -> dict:
    room = availability.get("room") or {}
    room_name = str(room.get("name") or "Meeting Room")
    room_slug = str(room.get("slug") or "")
    if room_slug not in ROOM_NAMES:
        raise MeetingRoomInputError(
            "invalid_response",
            "I could not verify that meeting room. Ask Roo to start again.",
        )
    if expected_room_slug and room_slug != expected_room_slug:
        raise MeetingRoomInputError(
            "invalid_response",
            "I could not verify the selected meeting room. Ask Roo to start again.",
        )
    cost = int(availability.get("points_cost") or 0)
    duration_half_hours = int(
        (_as_utc(ends_at) - _as_utc(starts_at)).total_seconds() // 1800
    )
    duration = duration_half_hours / 2
    duration_text = str(int(duration)) if duration.is_integer() else str(duration)
    target_line = ""
    heading = f"Confirm your *{room_name}* booking."
    if target_slack_user_id and target_slack_user_id != owner_slack_user_id:
        heading = f"Confirm a *{room_name}* booking for <@{target_slack_user_id}>."
        target_line = " Their Roo Points account will be charged."
    message = (
        f"{heading}\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n"
        f"*Duration:* {duration_text} hour{'s' if duration != 1 else ''}\n"
        f"*Cost:* {cost} Roo Point{'s' if cost != 1 else ''}\n\n"
        f"The room is not reserved until you confirm.{target_line} "
        "This button expires in 10 minutes."
    )
    value = build_booking_action_value(
        owner_slack_user_id=owner_slack_user_id,
        room_slug=room_slug,
        starts_at=starts_at,
        ends_at=ends_at,
        expected_points_cost=cost,
        target_slack_user_id=target_slack_user_id,
        client_request_id=client_request_id,
    )
    return {
        "message": message,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": message}},
            {
                "type": "actions",
                "block_id": "meeting_room_booking_confirmation",
                "elements": [
                    {
                        "type": "button",
                        "action_id": BOOK_ACTION_ID,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Confirm booking"},
                        "value": value,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Book Meeting Room?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": f"This will deduct {cost} Roo Point{'s' if cost != 1 else ''}.",
                            },
                            "confirm": {"type": "plain_text", "text": "Book"},
                            "deny": {"type": "plain_text", "text": "Go back"},
                        },
                    }
                ],
            },
        ],
    }


def cancellation_selection(bookings: list[dict], *, owner_slack_user_id: str) -> dict:
    message = "Choose the Meeting Room booking to cancel. You will receive a full refund if it has not started."
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]
    valid_booking_count = 0
    for booking in bookings:
        starts_at = parse_backend_timestamp(booking.get("starts_at"))
        ends_at = parse_backend_timestamp(booking.get("ends_at"))
        room_name = str((booking.get("room") or {}).get("name") or "Meeting Room")
        label = f"{starts_at.strftime('%a %-d %b')} {_format_clock(starts_at)}"
        try:
            action_value = build_cancel_action_value(
                owner_slack_user_id=owner_slack_user_id,
                booking_id=booking.get("id"),
            )
        except MeetingRoomInputError:
            continue
        valid_booking_count += 1
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{room_name}*\n{format_interval(starts_at, ends_at)}",
                },
                "accessory": {
                    "type": "button",
                    "action_id": CANCEL_ACTION_ID,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": f"Cancel {label}"[:75]},
                    "value": action_value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Cancel booking?"},
                        "text": {"type": "mrkdwn", "text": "The room will become available to other members."},
                        "confirm": {"type": "plain_text", "text": "Cancel booking"},
                        "deny": {"type": "plain_text", "text": "Keep booking"},
                    },
                },
            }
        )
    if not valid_booking_count:
        raise MeetingRoomInputError(
            "invalid_response",
            "I could not read those bookings. Ask Roo to list your bookings again.",
        )
    return {"message": message, "blocks": blocks}


def format_booking_result(result: dict) -> str:
    booking = result.get("booking") or {}
    starts_at = parse_backend_timestamp(booking.get("starts_at"))
    ends_at = parse_backend_timestamp(booking.get("ends_at"))
    room_name = str((booking.get("room") or {}).get("name") or "Meeting Room")
    points = int(result.get("points_cost", booking.get("points_cost", 0)) or 0)
    balance = result.get("remaining_balance")
    replay = (
        not bool(result.get("created"))
        if "created" in result
        else bool(result.get("already_booked"))
    )
    target_slack_user_id = str(result.get("booked_for_slack_user_id") or "").strip()
    admin_booking = bool(result.get("admin_booking"))
    if admin_booking and target_slack_user_id:
        heading = (
            f"The booking for <@{target_slack_user_id}> was already confirmed."
            if replay
            else f"The booking for <@{target_slack_user_id}> is confirmed."
        )
    else:
        heading = "This booking was already confirmed." if replay else "Your booking is confirmed."
    balance_line = (
        f"\n*Remaining balance:* {balance} Roo Points"
        if balance is not None and not admin_booking
        else ""
    )
    return (
        f"{heading}\n*Room:* {room_name}\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n"
        f"*Charged:* {points} Roo Point{'s' if points != 1 else ''}{balance_line}"
    )


def format_admin_target_notification(
    result: dict,
    *,
    admin_slack_user_id: str,
) -> str:
    """Describe an admin-created booking to the member whose points were charged."""
    booking = result.get("booking") or {}
    starts_at = parse_backend_timestamp(booking.get("starts_at"))
    ends_at = parse_backend_timestamp(booking.get("ends_at"))
    room_name = str((booking.get("room") or {}).get("name") or "Meeting Room")
    points = int(result.get("points_cost", booking.get("points_cost", 0)) or 0)
    return (
        f"<@{admin_slack_user_id}> booked the *{room_name}* for you.\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n"
        f"*Charged to your account:* {points} Roo Point{'s' if points != 1 else ''}\n\n"
        "To view or cancel it, DM Roo `show my meeting room bookings`."
    )


def format_cancellation_result(result: dict) -> str:
    booking = result.get("booking") or {}
    starts_at = parse_backend_timestamp(booking.get("starts_at"))
    ends_at = parse_backend_timestamp(booking.get("ends_at"))
    points = int(booking.get("points_cost") or 0)
    room_name = str((booking.get("room") or {}).get("name") or "Meeting Room")
    if result.get("already_cancelled"):
        return (
            f"That *{room_name}* booking was already cancelled. "
            "No duplicate refund was issued."
        )
    return (
        f"Your *{room_name}* booking for {format_interval(starts_at, ends_at)} has been cancelled.\n"
        f"*Refunded:* {points} Roo Point{'s' if points != 1 else ''}\n"
        f"*Balance:* {result.get('remaining_balance', 0)} Roo Points"
    )


def backend_error_message(
    exc: Exception,
    *,
    mutation_result_uncertain: bool = False,
    target_slack_user_id: Optional[str] = None,
    room_slug: Optional[str] = None,
) -> str:
    code = ""
    detail = ""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            code = str(payload.get("code") or "")
            detail = str(payload.get("error") or payload.get("detail") or "")
        except (ValueError, AttributeError):
            pass
    targeted = bool(str(target_slack_user_id or "").strip())
    room_name = ROOM_NAMES.get(str(room_slug or ""), "meeting room")
    messages = {
        "booking_conflict": f"The {room_name} was booked by someone else before you confirmed. No points were deducted.",
        "user_booking_conflict": (
            f"The tagged member already has another meeting-room booking that overlaps the {room_name} time. No points were deducted."
            if targeted
            else f"You already have another meeting-room booking that overlaps the {room_name} time. No points were deducted."
        ),
        "room_blocked": f"The {room_name} is unavailable during that time. No points were deducted.",
        "daily_limit": (
            "That booking would take the tagged member over their four-hour daily limit. No points were deducted."
            if targeted
            else "That booking would take you over the four-hour daily limit. No points were deducted."
        ),
        "insufficient_balance": (
            "The tagged member does not have enough Roo Points for that booking. No booking was created."
            if targeted
            else "You do not have enough Roo Points for that booking. No booking was created."
        ),
        "unlinked_user": (
            "The tagged member does not have a linked MLAI account. Ask them to DM Roo `link`, then try again."
            if targeted
            else "I could not find your linked MLAI account. DM Roo `link`, then try again."
        ),
        "inactive_user": (
            "The tagged member's MLAI account is inactive, so I cannot book the room for them."
            if targeted
            else "Your MLAI member account is inactive, so I cannot book the room."
        ),
        "expired_confirmation": "That confirmation expired. Ask Roo to check the time again for a fresh button.",
        "inactive_room": f"The {room_name} is not accepting bookings right now.",
        "feature_disabled": "Meeting-room booking is not enabled right now.",
        "booking_started": "That booking has already started and can no longer be cancelled.",
        "not_booking_owner": "You can only cancel your own Meeting Room bookings.",
        "admin_required": "Only full Roo Points Admins can book the Meeting Room for another member.",
        "price_changed": "The booking price changed after the preview. Ask Roo to check the time again before confirming.",
    }
    if code in messages:
        return messages[code]
    if mutation_result_uncertain and (
        not isinstance(exc, httpx.HTTPStatusError)
        or exc.response.status_code >= 500
    ):
        if targeted:
            return (
                "I could not confirm whether that Meeting Room booking completed. "
                "Ask the tagged member to DM Roo `show my meeting room bookings` "
                "before trying again."
            )
        return (
            "I could not confirm whether that Meeting Room change completed. "
            "Ask Roo `show my meeting room bookings` before trying again."
        )
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return "The meeting-room service is unavailable right now. Nothing was changed."
    return detail or "I could not complete that meeting-room request. Nothing was changed."
