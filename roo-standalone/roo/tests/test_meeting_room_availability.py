"""Available starts must correspond to complete, future, non-overlapping meetings."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.meeting_room_availability import (
    available_start_ranges, format_day_availability, resolve_search_duration,
)
from roo.meeting_room_booking import MeetingRoomInputError, resolve_interval

TZ = ZoneInfo('Australia/Melbourne')
DAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 14, 12, tzinfo=TZ)


def busy(start, end):
    return {'starts_at': start, 'ends_at': end}


def local_clock_ranges(rows, day=DAY, duration=2, now=NOW):
    return [(a.astimezone(TZ).strftime('%H:%M'), b.astimezone(TZ).strftime('%H:%M'))
            for a, b in available_start_ranges(rows, day, duration, now=now)]


@pytest.mark.parametrize('text,params,expected', [
    ('what times are the meeting rooms available tomorrow?', {}, 2),
    ('what hours are the rooms free tomorrow?', {}, 2),
    ('find a one-hour meeting room slot tomorrow', {}, 2),
    ('find a two-hour meeting room slot tomorrow', {}, 4),
    ('room free tomorrow for 2 hours?', {'duration_hours': 1}, 4),
])
def test_search_duration(text, params, expected):
    assert resolve_search_duration(text, params) == expected


@pytest.mark.parametrize('text,params', [
    ('find a 30-minute room slot', {}),
    ('find a 90-minute room slot tomorrow', {}),
    ('find a 1.5 hour meeting room slot tomorrow', {}),
    ('room free tomorrow for an hour and a half?', {}),
    ('room free tomorrow for one and a half hours?', {}),
    ('room free tomorrow?', {'duration_hours': 1.5}),
    ('find a 45-minute room slot', {}),
    ('find a three-hour room slot', {}),
    ('room free for three quarters of an hour?', {}),
    ('room free?', {'duration_hours': True}),
    ('room free?', {'duration_hours': 'NaN'}),
    ('room free?', {'duration_hours': 'Infinity'}),
])
def test_invalid_duration_does_not_silently_default(text, params):
    with pytest.raises(MeetingRoomInputError):
        resolve_search_duration(text, params)


@pytest.mark.parametrize('duration,last', [(2, '23:00'), (4, '22:00')])
def test_empty_day_last_start_finishes_at_midnight(duration, last):
    assert local_clock_ranges([], duration=duration) == [('00:00', last)]


def test_unsorted_overlapping_blocks_and_adjacent_bookings():
    rows = [busy('2026-09-15T11:00:00+10:00', '2026-09-15T12:00:00+10:00'),
            busy('2026-09-15T09:30:00+10:00', '2026-09-15T11:30:00+10:00'),
            busy('2026-09-15T09:00:00+10:00', '2026-09-15T10:00:00+10:00')]
    assert local_clock_ranges(rows) == [('00:00', '08:00'), ('12:00', '23:00')]


def test_off_grid_blocks_round_starts_and_require_whole_duration():
    rows = [busy('2026-09-15T00:00:00+10:00', '2026-09-15T09:10:00+10:00'),
            busy('2026-09-15T10:45:00+10:00', '2026-09-16T00:00:00+10:00')]
    assert local_clock_ranges(rows) == [('09:30', '09:30')]
    assert local_clock_ranges(rows, duration=4) == []


def test_blocks_spanning_day_boundaries_and_different_offsets():
    rows = [busy('2026-09-14T10:00:00Z', '2026-09-15T02:00:00Z'),
            busy('2026-09-15T13:30:00Z', '2026-09-16T10:00:00Z')]
    assert local_clock_ranges(rows) == [('12:00', '22:30')]


@pytest.mark.parametrize('clock,expected', [
    ((9, 0, 0), [('09:30', '23:00')]),
    ((9, 0, 1), [('09:30', '23:00')]),
    ((9, 29, 59), [('09:30', '23:00')]),
    ((23, 0, 0), []),
])
def test_today_strictly_excludes_started_slots(clock, expected):
    assert local_clock_ranges([], now=datetime(2026, 9, 15, *clock, tzinfo=TZ)) == expected


def test_fully_blocked_day_and_past_day_have_no_slots():
    assert local_clock_ranges([busy('2026-09-14T00:00:00Z', '2026-09-17T00:00:00Z')]) == []
    assert local_clock_ranges([], now=NOW + timedelta(days=2)) == []


@pytest.mark.parametrize('day', [date(2026, 10, 4), date(2027, 4, 4)])
@pytest.mark.parametrize('duration', [2, 4])
def test_dst_suggestions_round_trip_through_booking_parser(day, duration):
    now = datetime.combine(day - timedelta(days=1), datetime.min.time(), TZ)
    for first, last in available_start_ranges([], day, duration, now=now):
        start = first
        while start <= last:
            local = start.astimezone(TZ)
            actual_start, end = resolve_interval(
                f'book small room on {day} at {local:%I:%M%p}',
                {'duration_hours': duration / 2}, now=now,
            )
            assert actual_start.astimezone(timezone.utc) == start
            assert end.astimezone(timezone.utc) - start == timedelta(minutes=duration * 30)
            assert end.astimezone(TZ).date() <= day + timedelta(days=1)
            start += timedelta(minutes=30)


@pytest.mark.parametrize('rows', [None, {}, [None], [{}], [busy('bad', 'bad')],
    [busy('2026-09-15T12:00:00+10:00', '2026-09-15T11:00:00+10:00')]])
def test_invalid_snapshot_never_claims_free_day(rows):
    with pytest.raises(MeetingRoomInputError):
        format_day_availability([{'busy_intervals': rows}], DAY, 2, now=NOW)


def test_readable_room_output_and_booking_instructions():
    message = format_day_availability([
        {'room': {'name': 'Small Meeting Room'}, 'busy_intervals': []},
        {'room': {'name': 'Big Meeting Room'}, 'busy_intervals': [
            busy('2026-09-15T00:00:00+10:00', '2026-09-16T00:00:00+10:00')]},
    ], DAY, 4, now=NOW)
    assert 'Tuesday 15 September 2026' in message
    assert 'Available start times for a 2-hour meeting' in message
    assert '12:00 AM to 10:00 PM' in message
    assert 'No 2-hour slots available' in message
    assert 'for 2 hours' in message
    assert 'Confirm booking' in message
    assert 'Nothing is reserved' in message
    assert 'Conference' not in message
