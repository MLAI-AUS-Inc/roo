from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx

from .utils import get_current_datetime


MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
BOOK_ACTION_ID = "meeting_room_confirm_booking"
CANCEL_ACTION_ID = "meeting_room_cancel_booking"
CONFIRMATION_TTL = timedelta(minutes=10)
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class MeetingRoomInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
    if raw_param_date == "tomorrow":
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
    if re.search(r"\btomorrow\b", normalized):
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

    raise MeetingRoomInputError(
        "missing_date",
        "What date should I check? Try `tomorrow` or `2026-08-14`.",
    )


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
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm")
    if minute != 0:
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting-room bookings must start and end on the hour.",
        )
    if ampm:
        if not 1 <= hour <= 12:
            raise MeetingRoomInputError("invalid_time", f"The {field_label} is not valid.")
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    elif hour <= 12 and match.group("minute") is None and not raw.startswith("0"):
        raise MeetingRoomInputError(
            "ambiguous_time",
            f"Is the {field_label} AM or PM? Try `2pm` or `14:00`.",
        )
    if not 0 <= hour <= 23:
        raise MeetingRoomInputError("invalid_time", f"The {field_label} is not valid.")
    return time(hour=hour)


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


def _add_actual_hours(value: datetime, hours: int) -> datetime:
    return (_as_utc(value) + timedelta(hours=hours)).astimezone(MELBOURNE_TZ)


def _natural_time_tokens(text: str) -> tuple[Optional[str], Optional[str]]:
    normalized = str(text or "").lower()
    token = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    range_match = re.search(
        rf"\bfrom\s+(?P<start>{token})\s+(?:to|until|-)\s*(?P<end>{token})\b",
        normalized,
    )
    if range_match:
        return range_match.group("start"), range_match.group("end")
    explicit_token = r"(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))"
    range_match = re.search(
        rf"\b(?P<start>{explicit_token})\s*(?:to|until|-)\s*(?P<end>{explicit_token})\b",
        normalized,
    )
    if range_match:
        return range_match.group("start"), range_match.group("end")
    start_match = re.search(rf"\bat\s+(?P<start>{token})\b", normalized)
    return (start_match.group("start"), None) if start_match else (None, None)


def _validate_resolved_interval(
    starts_at: datetime,
    ends_at: datetime,
    *,
    now: Optional[datetime],
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
    if duration_seconds <= 0 or duration_seconds % 3600:
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting Room bookings must last a whole number of hours.",
        )
    duration_hours = int(duration_seconds // 3600)
    if duration_hours > 4:
        raise MeetingRoomInputError(
            "invalid_time",
            "Meeting Room bookings can be at most four hours. Check the end time and try again.",
        )
    return starts_at, ends_at


def resolve_interval(
    text: str,
    params: Optional[dict] = None,
    *,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    params = params or {}
    exact_start = _parse_iso_timestamp(params.get("starts_at"))
    exact_end = _parse_iso_timestamp(params.get("ends_at"))
    if exact_start or exact_end:
        if not exact_start:
            raise MeetingRoomInputError("missing_start_time", "What time should the booking start?")
        if exact_end:
            return _validate_resolved_interval(exact_start, exact_end, now=now)
        duration = params.get("duration_hours")
        if duration is None:
            duration = 1
        try:
            ends_at = _add_actual_hours(exact_start, int(duration))
        except (TypeError, ValueError):
            raise MeetingRoomInputError("invalid_time", "The duration must be a whole number of hours.")
        return _validate_resolved_interval(exact_start, ends_at, now=now)

    local_date = resolve_local_date(text, params, now=now)
    natural_start, natural_end = _natural_time_tokens(text)
    start_value = params.get("start_time") or natural_start
    end_value = params.get("end_time") or natural_end
    start_time = _parse_time_value(start_value, "start time")
    if start_time is None:
        raise MeetingRoomInputError(
            "missing_start_time",
            "What time should the booking start? Try `tomorrow at 2pm`.",
        )

    starts_at = _local_datetime(local_date, start_time)
    end_time = _parse_time_value(end_value, "end time")
    if end_time is not None:
        ends_at = _local_datetime(local_date, end_time)
        if _as_utc(ends_at) <= _as_utc(starts_at):
            ends_at = _local_datetime(local_date + timedelta(days=1), end_time)
    else:
        raw_duration = params.get("duration_hours")
        duration_match = re.search(
            r"\bfor\s+(\d+)\s*(?:whole\s+)?hours?\b",
            str(text or "").lower(),
        )
        if raw_duration is None:
            raw_duration = duration_match.group(1) if duration_match else 1
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError):
            raise MeetingRoomInputError("invalid_time", "The duration must be a whole number of hours.")
        ends_at = _add_actual_hours(starts_at, duration)
    return _validate_resolved_interval(starts_at, ends_at, now=now)


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
    now: Optional[datetime] = None,
) -> str:
    issued_at = (now or get_current_datetime()).astimezone(timezone.utc)
    payload = {
        "owner_slack_user_id": owner_slack_user_id,
        "room_slug": room_slug,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "client_request_id": str(uuid4()),
        "confirmation_expires_at": (issued_at + CONFIRMATION_TTL).isoformat(),
    }
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
        )
        if any(not payload.get(field) for field in required):
            raise MeetingRoomInputError("invalid_action", "This booking action is incomplete. Ask Roo to start again.")
        try:
            UUID(str(payload["client_request_id"]))
        except ValueError:
            raise MeetingRoomInputError("invalid_action", "This booking action is not valid. Ask Roo to start again.")
        parse_backend_timestamp(payload["starts_at"])
        parse_backend_timestamp(payload["ends_at"])
        parse_backend_timestamp(payload["confirmation_expires_at"])
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


def booking_preview(
    availability: dict,
    *,
    owner_slack_user_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> dict:
    room = availability.get("room") or {}
    room_name = str(room.get("name") or "Meeting Room")
    room_slug = str(room.get("slug") or "meeting-room")
    cost = int(availability.get("points_cost") or 0)
    duration = int((_as_utc(ends_at) - _as_utc(starts_at)).total_seconds() // 3600)
    message = (
        f"Confirm your *{room_name}* booking.\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n"
        f"*Duration:* {duration} hour{'s' if duration != 1 else ''}\n"
        f"*Cost:* {cost} Roo Point{'s' if cost != 1 else ''}\n\n"
        "The room is not reserved until you confirm. This button expires in 10 minutes."
    )
    value = build_booking_action_value(
        owner_slack_user_id=owner_slack_user_id,
        room_slug=room_slug,
        starts_at=starts_at,
        ends_at=ends_at,
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
    heading = "This booking was already confirmed." if replay else "Your booking is confirmed."
    balance_line = f"\n*Remaining balance:* {balance} Roo Points" if balance is not None else ""
    return (
        f"{heading}\n*Room:* {room_name}\n"
        f"*When:* {format_interval(starts_at, ends_at)} (Melbourne time)\n"
        f"*Charged:* {points} Roo Point{'s' if points != 1 else ''}{balance_line}"
    )


def format_cancellation_result(result: dict) -> str:
    booking = result.get("booking") or {}
    starts_at = parse_backend_timestamp(booking.get("starts_at"))
    ends_at = parse_backend_timestamp(booking.get("ends_at"))
    points = int(booking.get("points_cost") or 0)
    if result.get("already_cancelled"):
        return "That Meeting Room booking was already cancelled. No duplicate refund was issued."
    return (
        f"Your Meeting Room booking for {format_interval(starts_at, ends_at)} has been cancelled.\n"
        f"*Refunded:* {points} Roo Point{'s' if points != 1 else ''}\n"
        f"*Balance:* {result.get('remaining_balance', 0)} Roo Points"
    )


def backend_error_message(
    exc: Exception,
    *,
    mutation_result_uncertain: bool = False,
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
    messages = {
        "booking_conflict": "That time was booked by someone else before you confirmed. No points were deducted.",
        "room_blocked": "The Meeting Room is unavailable during that time. No points were deducted.",
        "daily_limit": "That booking would take you over the four-hour daily limit. No points were deducted.",
        "insufficient_balance": "You do not have enough Roo Points for that booking. No booking was created.",
        "unlinked_user": "I could not find your linked MLAI account. DM Roo `link`, then try again.",
        "inactive_user": "Your MLAI member account is inactive, so I cannot book the room.",
        "expired_confirmation": "That confirmation expired. Ask Roo to check the time again for a fresh button.",
        "inactive_room": "The Meeting Room is not accepting bookings right now.",
        "feature_disabled": "Meeting-room booking is not enabled right now.",
        "booking_started": "That booking has already started and can no longer be cancelled.",
        "not_booking_owner": "You can only cancel your own Meeting Room bookings.",
    }
    if code in messages:
        return messages[code]
    if mutation_result_uncertain and (
        not isinstance(exc, httpx.HTTPStatusError)
        or exc.response.status_code >= 500
    ):
        return (
            "I could not confirm whether that Meeting Room change completed. "
            "Ask Roo `show my meeting room bookings` before trying again."
        )
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return "The meeting-room service is unavailable right now. Nothing was changed."
    return detail or "I could not complete that meeting-room request. Nothing was changed."
