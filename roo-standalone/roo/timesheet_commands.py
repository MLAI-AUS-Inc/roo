"""Exact Slack command ingress. Holds no Linear credentials and calls no LLM."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from .payment_reminders import ReceiptStore
from .timesheets import TimesheetConfig, TimesheetError, fingerprint

HELP = 'Use `timesheet`, `timesheet YYYY-MM-DD` (payment Friday), or `timesheet current`.'


def parse_command(text, bot_user_id='', *, dm=False):
    text = str(text or '').strip()
    if not dm:
        mention = f'<@{bot_user_id}>'
        if not bot_user_id or not text.startswith(mention):
            return None
        text = text[len(mention):].strip()
    elif bot_user_id and text.startswith(f'<@{bot_user_id}>'):
        text = text[len(f'<@{bot_user_id}>'):].strip()
    if not re.match(r'^timesheet(?:\s|$)', text, re.I):
        return None
    match = re.fullmatch(r'timesheet(?:\s+(latest|current|\d{4}-\d{2}-\d{2}))?', text, re.I)
    if not match:
        raise TimesheetError('invalid_command')
    return (match.group(1) or 'latest').lower()


def enqueue(settings, *, team, actor, channel, source_id, text, dm, now=None, thread_ts=None):
    """Called only after Slack HMAC verification; safe even on a receipt replay."""
    now = now or datetime.now(timezone.utc)
    try:
        selector = parse_command(text, getattr(settings, 'TIMESHEET_SLACK_BOT_USER_ID', ''), dm=dm)
    except TimesheetError:
        return {'handled': True, 'text': HELP, 'new': True}
    if selector is None:
        return {'handled': False}
    if getattr(settings, 'ROO_SURFACE', '') != 'public' or not getattr(settings, 'TIMESHEET_COMMANDS_ENABLED', False):
        return {'handled': True, 'text': 'Timesheet commands are not enabled on this Roo deployment.', 'new': True}
    if (team != getattr(settings, 'TIMESHEET_SLACK_TEAM_ID', '')
            or not re.fullmatch(r'T[A-Z0-9]+', str(team))
            or not re.fullmatch(r'[UW][A-Z0-9]+', str(actor))
            or not re.fullmatch(r'[CDG][A-Z0-9]+', str(channel))):
        return {'handled': True, 'text': 'This timesheet request could not be verified.', 'new': True}
    # One production identity: Dr Sam is both requester and recipient.
    if actor != getattr(settings, 'TIMESHEET_RECIPIENT_SLACK_ID', ''):
        return {'handled': True, 'text': 'You are not authorized to request Studio timesheets.', 'new': True}
    if selector not in {'latest', 'current'}:
        try:
            config = TimesheetConfig.from_env({'TIMESHEET_FIRST_CUTOFF': getattr(settings, 'TIMESHEET_FIRST_CUTOFF', '2026-09-11')})
            if config.cutoff(selector) > now:
                return {'handled': True, 'text': 'That period is still open. Use `timesheet current` for a draft.', 'new': True}
        except (ValueError, TypeError):
            return {'handled': True, 'text': HELP, 'new': True}
    if not source_id:
        raise TimesheetError('request_identity_missing')
    key = fingerprint({'team': team, 'actor': actor, 'channel': channel, 'source': source_id})
    store = ReceiptStore(Path(settings.TIMESHEET_QUEUE_DIR))
    with store.locked(key) as acquired:
        if not acquired:
            if store.read(key) is None:
                raise TimesheetError('queue_busy')
            return {'handled': True, 'new': False}
        existing = store.read(key)
        if existing:
            if existing['selector'] != selector:
                raise TimesheetError('request_replay_mismatch')
            return {'handled': True, 'new': False}
        store.write(key, {'team': team, 'actor': actor, 'channel': channel, 'selector': selector,
                          'thread_ts': thread_ts, 'requested_at': now.isoformat(), 'status': 'pending'})
    return {'handled': True, 'new': True, 'text': 'Your timesheet request is queued. Roo will DM the report directly to Dr Sam.'}


def enqueue_event(settings, payload, now=None):
    event = payload.get('event') or {}
    if event.get('type') not in {'message', 'app_mention'} or event.get('bot_id') or event.get('subtype'):
        return {'handled': False}
    dm = event.get('channel_type') == 'im' and str(event.get('channel', '')).startswith('D')
    return enqueue(settings, team=payload.get('team_id'), actor=event.get('user'),
                   channel=event.get('channel'), source_id=event.get('ts'),
                   text=event.get('text'), dm=dm, now=now,
                   thread_ts=event.get('thread_ts') or event.get('ts'))
