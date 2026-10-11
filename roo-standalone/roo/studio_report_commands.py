"""Queue client-owned reports using authenticated Slack identity, never model identity."""
from datetime import datetime, timezone
from pathlib import Path
import re

from .payment_reminders import ReceiptStore
from .studio_reports import resolve_period
from .timesheets import TimesheetError, fingerprint


def enqueue(settings, context, params, *, user_id, channel_id, thread_ts, now=None,
            report_kind='hours'):
    """The executor selects report_kind; it is never read from model params."""
    if report_kind not in {'hours', 'client_balance'}:
        return 'I could not verify this report request. Please ask Roo directly in Slack.'
    balance = report_kind == 'client_balance'
    report_name = 'Studio hours and purchased-hours' if balance else 'Studio hours'
    flag = 'STUDIO_CLIENT_BALANCE_ENABLED' if balance else 'STUDIO_REPORTS_ENABLED'
    if settings.ROO_SURFACE != 'public' or not getattr(settings, flag, False):
        return f'{report_name.capitalize()} reports are not enabled on this Roo deployment.'
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
    identity = {'team': context.slack_team_id, 'actor': user_id,
                'channel': channel_id, 'source': context.event_id}
    # Preserve existing hours receipt keys and stored shape for retries. A
    # balance request has a separate key even if the Slack event is the same.
    if balance:
        identity['report_kind'] = report_kind
    key = fingerprint(identity)
    with store.locked(key) as acquired:
        if not acquired:
            raise TimesheetError('queue_busy')
        if store.read(key):
            return f'This {report_name} request has already been queued for private delivery.'
        selected = {k: params[k] for k in ('month', 'months', 'action', 'client') if k in params}
        if selected.get('month') == 'previous' or selected.get('action') == 'detailed':
            previous = []
            for path in store.directory.glob('*.json'):
                candidate = store.read(path.stem)
                if (candidate and candidate.get('actor') == user_id
                        and candidate.get('team') == context.slack_team_id
                        and candidate.get('report_kind', 'hours') == report_kind
                        and candidate.get('status') not in {'denied', 'rejected', 'error'}):
                    previous.append(candidate)
            prior = max(previous, key=lambda request: request['requested_at']) if previous else None
            if not selected.get('month') or selected.get('month') == 'previous':
                selected['month'] = prior['selector']['month'] if prior else ('all' if balance else 'current')
                selected.setdefault('months', prior['selector']['months'] if prior else 1)
            if prior and 'client' in prior['selector']:
                selected.setdefault('client', prior['selector']['client'])
        if balance and not selected.get('month') and 'months' not in selected:
            selected['month'] = 'all'
        try:
            selector, _, _ = resolve_period(selected, now)
        except (TimesheetError, TypeError, ValueError) as exc:
            if str(exc) == 'invalid_report_client':
                if balance:
                    return 'Please provide a client name for the Studio hours and purchased-hours report, or ask for your own report.'
                return 'Please provide a client name, or ask for all clients.'
            if balance:
                return ('Please choose a calendar month for the Studio hours and purchased-hours report, '
                        'a range of up to 12 months ending no later than this month, or project-to-date.')
            return ('Please choose a calendar month, such as “Studio hours for September 2026”, '
                    'or a range of up to 12 months ending no later than this month.')
        store.write(key, {'team': context.slack_team_id, 'actor': user_id,
                         'selector': selector, 'channel': channel_id,
                         'thread_ts': thread_ts, 'requested_at': now.isoformat(), 'status': 'pending',
                         **({'report_kind': report_kind} if balance else {})})
    if balance:
        return ('I’m preparing your Studio hours and purchased-hours report. '
                'I’ll DM it to you after checking your hours and payment report access.')
    return 'I’m preparing your Studio hours report. I’ll DM it to you after checking your project access.'
