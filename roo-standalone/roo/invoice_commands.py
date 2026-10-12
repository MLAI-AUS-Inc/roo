"""Exact signed invoice ingress; no builder mappings, source keys or inference."""
from datetime import datetime, timezone
from pathlib import Path
import re

from .payment_reminders import ReceiptStore
from .studio_invoices import current_period, validate_period
from .timesheets import TimesheetConfig, TimesheetError, fingerprint

HELP = ('Use `invoice` for your current-fortnight draft, `invoice latest` for your most recent final, '
        'or `invoice YYYY-MM-DD` with a payment Friday from 2026-10-23. Roo delivers privately. '
        'Fill in business, ABN and bank details locally in the PDF.')


def parse_command(text, bot_user_id='', *, dm=False):
    text = str(text or '').strip()
    mention = f'<@{bot_user_id}>'
    if not dm:
        if not bot_user_id or not text.startswith(mention):
            return None
        text = text[len(mention):].strip()
    elif bot_user_id and text.startswith(mention):
        text = text[len(mention):].strip()
    if not re.match(r'^invoice(?:\s|$)', text, re.I):
        return None
    match = re.fullmatch(r'invoice(?:\s+(latest|\d{4}-\d{2}-\d{2}))?', text, re.I)
    if not match:
        raise TimesheetError('invalid_invoice_command')
    return (match.group(1) or 'current').lower()


def enqueue(settings, *, team, actor, channel, source_id, text, dm, now=None, thread_ts=None):
    """Invoke only after existing HMAC, replay and channel gates."""
    now = now or datetime.now(timezone.utc)
    try:
        selector = parse_command(text, getattr(settings, 'TIMESHEET_SLACK_BOT_USER_ID', ''), dm=dm)
    except TimesheetError:
        return {'handled': True, 'new': True, 'text': HELP}
    if selector is None:
        return {'handled': False}
    if getattr(settings, 'ROO_SURFACE', '') != 'public' or not getattr(settings, 'STUDIO_INVOICE_COMMANDS_ENABLED', False):
        return {'handled': True, 'new': True, 'text': 'Invoice commands are not enabled on this Roo deployment.'}
    if (team != getattr(settings, 'TIMESHEET_SLACK_TEAM_ID', '')
            or not re.fullmatch(r'T[A-Z0-9]+', str(team))
            or not re.fullmatch(r'[UW][A-Z0-9]+', str(actor))
            or not re.fullmatch(r'[CDG][A-Z0-9]+', str(channel))):
        return {'handled': True, 'new': True, 'text': 'This invoice request could not be verified.'}
    if selector not in {'current', 'latest'}:
        try:
            config = TimesheetConfig.from_env({'TIMESHEET_FIRST_CUTOFF': getattr(settings, 'TIMESHEET_FIRST_CUTOFF', '2026-09-11')})
            end = validate_period(config, selector)
            if end > current_period(config, now):
                raise TimesheetError('invoice_period_in_future')
        except (ValueError, TypeError):
            return {'handled': True, 'new': True, 'text': HELP}
    if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
        raise TimesheetError('request_identity_missing')
    identity = {'team': team, 'actor': actor, 'channel': channel, 'source': source_id}
    key = fingerprint(identity)
    store = ReceiptStore(Path(settings.STUDIO_INVOICE_QUEUE_DIR))
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
        store.write(key, {**identity, 'selector': selector, 'thread_ts': thread_ts,
                          'requested_at': now.isoformat(), 'status': 'pending'})
    return {'handled': True, 'new': True, 'text': 'Your invoice request is queued. Roo will verify your builder mapping and DM the result to you.'}


def enqueue_event(settings, payload, now=None):
    event = payload.get('event') or {}
    if event.get('type') not in {'message', 'app_mention'} or event.get('bot_id') or event.get('subtype'):
        return {'handled': False}
    dm = event.get('channel_type') == 'im' and str(event.get('channel', '')).startswith('D')
    return enqueue(settings, team=payload.get('team_id'), actor=event.get('user'),
        channel=event.get('channel'), source_id=event.get('ts'), text=event.get('text'), dm=dm,
        now=now, thread_ts=event.get('thread_ts') or event.get('ts'))
