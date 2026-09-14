"""Day-wide room discovery from the backend's bookings and blocks snapshot."""

import re
from datetime import date, datetime, time, timedelta, timezone

from .meeting_room_booking import (
    MELBOURNE_TZ,
    MeetingRoomInputError,
    _duration_half_hours,
    _format_clock,
    _local_datetime,
    _natural_duration,
    parse_backend_timestamp,
)
from .utils import get_current_datetime


def resolve_search_duration(text: str, params: dict) -> int:
    """Return the requested duration in half-hours; default to a one-hour slot."""
    normalized = re.sub(r"[-\u2010-\u2013]", " ", str(text or "").lower())
    # Also accept noun phrases such as 'a two-hour slot' without a 'for'.
    phrase = re.search(
        r"\b((?:(?:an?|one) hour and a half|one and a half hours?|"
        r"half an? hour|an? half hour|(?:\d+(?:\.\d+)?|an?|one|two)\s*"
        r"(?:hours?|hrs?|minutes?|mins?)))\b",
        normalized,
    )
    natural = _natural_duration(normalized)
    if natural is None and phrase:
        natural = _natural_duration("for " + phrase.group(1))
    value = natural if natural is not None else params.get("duration_hours")
    if value is None:
        if re.search(
            r"\b(?:\d+|three|four|five|half|quarter|quarters)\b.{0,24}"
            r"\b(?:hours?|hrs?|minutes?|mins?)\b",
            normalized,
        ):
            raise MeetingRoomInputError(
                "invalid_time",
                "What meeting length should I look for? Try `1 hour`, `1.5 hours`, or `2 hours`.",
            )
        value = 1
    return _duration_half_hours(value)


def available_start_ranges(
    busy_intervals: list[dict],
    local_date: date,
    duration_half_hours: int,
    *,
    now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Inclusive ranges of half-hour starts whose full meeting fits in this day.

    Iterate in UTC so DST days and offset-bearing blocks use elapsed time.
    Skip repeated local hours that the natural-language booking parser rejects.
    """
    day_start = datetime.combine(local_date, time.min, MELBOURNE_TZ).astimezone(timezone.utc)
    day_end = datetime.combine(
        local_date + timedelta(days=1), time.min, MELBOURNE_TZ,
    ).astimezone(timezone.utc)
    current = (now or get_current_datetime()).astimezone(timezone.utc)
    duration = timedelta(minutes=duration_half_hours * 30)
    step = timedelta(minutes=30)
    busy = []
    for interval in busy_intervals:
        start = parse_backend_timestamp(interval.get("starts_at")).astimezone(timezone.utc)
        end = parse_backend_timestamp(interval.get("ends_at")).astimezone(timezone.utc)
        if end <= start:
            raise MeetingRoomInputError(
                "invalid_response", "I could not read the room availability. Please try again.",
            )
        busy.append((start, end))

    ranges = []
    start = day_start
    while start + duration <= day_end:
        end = start + duration
        local_start = start.astimezone(MELBOURNE_TZ)
        local_end = end.astimezone(MELBOURNE_TZ)
        unambiguous = True
        for value in (local_start, local_end):
            try:
                _local_datetime(value.date(), value.time().replace(tzinfo=None, fold=0))
            except MeetingRoomInputError:
                unambiguous = False
        if (
            start > current and unambiguous
            and not any(start < b and end > a for a, b in busy)
        ):
            # Split across clock changes: 'every 30 minutes' must be literal.
            if (
                ranges and start == ranges[-1][1] + step
                and local_start.utcoffset()
                == ranges[-1][1].astimezone(MELBOURNE_TZ).utcoffset()
            ):
                ranges[-1] = (ranges[-1][0], start)
            else:
                ranges.append((start, start))
        start += step
    return ranges


def format_day_availability(
    results: list[dict],
    local_date: date,
    duration_half_hours: int,
    *,
    now: datetime | None = None,
) -> str:
    reference_now = now or get_current_datetime()
    duration_label = f"{duration_half_hours / 2:g}-hour"
    booking_duration = f"{duration_half_hours / 2:g} hour" + ("s" if duration_half_hours != 2 else "")
    lines = [
        f"*{local_date.strftime('%A %-d %B %Y')}* (Melbourne time)",
        f"Available start times for a {duration_label} meeting (every 30 minutes within each range):",
    ]
    for result in results:
        busy = result.get("busy_intervals")
        if not isinstance(busy, list) or any(not isinstance(row, dict) for row in busy):
            raise MeetingRoomInputError(
                "invalid_response", "I could not read the room availability. Please try again.",
            )
        ranges = available_start_ranges(busy, local_date, duration_half_hours, now=reference_now)
        name = (result.get("room") or {}).get("name") or "Meeting Room"
        lines.extend(["", f"*{name}*"])
        if not ranges:
            lines.append(f"No {duration_label} slots available on this date.")
        for first, last in ranges:
            label = _format_clock(first)
            if first != last:
                label += f" to {_format_clock(last)}"
            lines.append(f"- {label}")
    lines.extend([
        "",
        "These are room openings, subject to your booking limits and Roo Points. "
        f"To book, tell me the room, date and start time and say `for {booking_duration}`; "
        "I’ll recheck before showing Confirm booking. "
        "Nothing is reserved yet. You can also ask for a 1.5-hour or 2-hour meeting.",
    ])
    return "\n".join(lines)
