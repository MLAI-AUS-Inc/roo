"""Deterministic coworking booking list (no AI or booking writes)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from .clients.mlai_backend import MLAIBackendClient

logger = logging.getLogger(__name__)
MELBOURNE = ZoneInfo('Australia/Melbourne')
LOOKUP_BUDGET = 2.0
USAGE = 'Usage: @Roo coworking-today [YYYY-MM-DD]'
LOAD_ERROR = "Couldn't load coworking bookings. Please try again."
TOO_LARGE = 'The booking list is too large to display safely in Slack. Please contact the Roo maintainer.'


def private(text: str) -> dict:
    return {'response_type': 'ephemeral', 'text': text}


def render_snapshot(data: dict, day: date) -> dict:
    if not isinstance(data, dict) or data.get('date') != day.isoformat():
        raise ValueError('Unexpected snapshot date')
    people = data.get('people')
    count = data.get('count')
    if not isinstance(people, list) or type(count) is not int or count != len(people):
        raise ValueError('Invalid snapshot count')
    seen = set()
    names = []
    for person in people:
        if not isinstance(person, dict):
            raise ValueError('Invalid person')
        ident, name = person.get('user_id'), person.get('name')
        if not isinstance(ident, str) or not ident or ident in seen or not isinstance(name, str) or not name.strip():
            raise ValueError('Invalid person')
        seen.add(ident)
        names.append(' '.join(name.split()))
    heading = f"Coworking bookings · {day.day} {day.strftime('%B %Y')}"
    summary = f"{count} {'person' if count == 1 else 'people'} booked"
    lines = [heading, summary] + ([f'• {name}' for name in names] if names else ['No active bookings for this date.'])
    chunks = []
    for line in lines:
        if len(line) > 2900:
            return private(TOO_LARGE)
        if chunks and len(chunks[-1]) + len(line) + 1 <= 2900:
            chunks[-1] += '\n' + line
        else:
            chunks.append(line)
    if len(chunks) > 45:
        return private(TOO_LARGE)
    return {
        'response_type': 'in_channel',
        # Names stay in plain-text blocks, never in the mrkdwn fallback.
        'text': heading + ' — ' + summary + (' — No active bookings for this date.' if not count else ''),
        'blocks': [{'type': 'section', 'text': {'type': 'plain_text', 'text': chunk}} for chunk in chunks],
    }


async def handle_command(text: str, user_id: str, client: MLAIBackendClient,
                         *, now: datetime | None = None) -> dict:
    raw = text.strip()
    try:
        if raw:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', raw):
                raise ValueError()
            day = date.fromisoformat(raw)
        else:
            day = (now or datetime.now(MELBOURNE)).astimezone(MELBOURNE).date()
    except ValueError:
        return private(USAGE)
    if not user_id:
        return private('Unable to identify the requester. Please run the command again.')
    try:
        data = await asyncio.wait_for(client.get_coworking_snapshot(user_id, day.isoformat()), LOOKUP_BUDGET)
        return render_snapshot(data, day)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return private('Only active Roo Points Admins can view coworking bookings.')
        logger.warning('Coworking snapshot HTTP failure: %s', exc.response.status_code)
        return private(LOAD_ERROR)
    except Exception as exc:
        logger.warning('Coworking snapshot failed: %s', type(exc).__name__)
        return private(LOAD_ERROR)
