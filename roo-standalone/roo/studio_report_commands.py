"""Queue client-owned reports using authenticated Slack identity, never model identity."""
from datetime import datetime, timezone
from pathlib import Path
import re

from .payment_reminders import ReceiptStore
from .studio_reports import resolve_period
from .timesheets import TimesheetError, fingerprint


def enqueue(settings, context, params, *, user_id, channel_id, thread_ts, now=None):
    if settings.ROO_SURFACE != 'public' or not settings.STUDIO_REPORTS_ENABLED:
        return 'Studio hours reports are not enabled on this Roo deployment.'
    if (context is None or not context.event_id
            or context.slack_team_id != settings.STUDIO_REPORTS_SLACK_TEAM_ID
            or context.acting_slack_user_id != user_id
            or context.slack_channel_id != channel_id
            or str(context.slack_thread_ts or '') != str(thread_ts or '')
            or not re.fullmatch(r'T[A-Z0-9]+', context.slack_team_id)
            or not re.fullmatch(r'[UW][A-Z0-9]+', user_id)
            or not re.fullmatch(r'[CDG][A-Z0-9]+', channel_id or '')):
        return 'I could not verify this request. Please ask Roo directly in Slack.'
    now = now or datetime.now(timezone.utc)
    store = ReceiptStore(Path(settings.STUDIO_REPORTS_QUEUE_DIR))
    key = fingerprint({'team': context.slack_team_id, 'actor': user_id,
                       'channel': channel_id, 'source': context.event_id})
    with store.locked(key) as acquired:
        if not acquired:
            raise TimesheetError('queue_busy')
        if store.read(key):
            return 'This Studio hours request has already been queued for private delivery.'
        selected = {k: params[k] for k in ('month', 'months', 'action') if k in params}
        if selected.get('month') == 'previous' or (
                selected.get('action') == 'detailed' and not selected.get('month')):
            previous = []
            for path in store.directory.glob('*.json'):
                candidate = store.read(path.stem)
                if (candidate and candidate.get('actor') == user_id
                        and candidate.get('team') == context.slack_team_id
                        and candidate.get('status') not in {'denied', 'rejected'}):
                    previous.append(candidate)
            prior = max(previous, key=lambda request: request['requested_at']) if previous else None
            selected['month'] = prior['selector']['month'] if prior else 'current'
            selected.setdefault('months', prior['selector']['months'] if prior else 1)
        try:
            selector, _, _ = resolve_period(selected, now)
        except TimesheetError:
            return ('Please choose a calendar month, such as “Studio hours for September 2026”, '
                    'or a range of up to 12 months ending no later than this month.')
        store.write(key, {'team': context.slack_team_id, 'actor': user_id,
                         'selector': selector, 'channel': channel_id,
                         'thread_ts': thread_ts, 'requested_at': now.isoformat(), 'status': 'pending'})
    return 'I’m preparing your Studio hours report. I’ll DM it to you after checking your project access.'
