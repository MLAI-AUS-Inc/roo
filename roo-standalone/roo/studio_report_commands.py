"""Queue client-owned reports using authenticated Slack identity, never model identity."""
from datetime import datetime, timezone
from pathlib import Path
import re

from .payment_reminders import ReceiptStore
from .studio_report_clients import client_key
from .studio_reports import resolve_period
from .timesheets import TimesheetError, fingerprint


_MONTH_COUNTS = "one two three four five six seven eight nine ten eleven twelve".split()
_RELATIVE_MONTHS = re.compile(
    r"\b(?:last|past|previous)\s+(?:a\s+)?(\d+|couple(?: of)?|" + "|".join(_MONTH_COUNTS)
    + r")\s+(?:(full|complete|completed|calendar)\s+)?months?\b", re.I)
_CONTRIBUTORS = re.compile(
    r"\b(?:worker|builder|contributor|person|team member)[ -](?:hours? )?breakdown\b|"
    r"\b(?:by|per|each) (?:person|worker|builder|contributor|team member)\b|"
    r"\bwho (?:did|worked|logged|spent|burnt|has been doing|has worked)\b|"
    r"\bwhich (?:person|people|workers?) (?:did|worked|logged)\b", re.I)
_EXPLICIT_PERIOD = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{4}-\d{2})\b", re.I)


def hours_params(text, params):
    """Recover unambiguous display and trailing-period choices from this Slack turn."""
    selected = dict(params)
    if _CONTRIBUTORS.search(text):
        if re.search(r"\b(?:no|without) (?:a |any )?(?:charts?|graphs?)\b|\btext only\b", text, re.I):
            selected.pop('chart', None)
        elif re.search(r"\bboth\b", text, re.I) and re.search(r"\b(?:charts?|views?|lines?)\b", text, re.I):
            selected['chart'] = 'both'
        elif re.search(r"\bcumulative\b|\brunning totals?\b", text, re.I):
            selected['chart'] = 'cumulative'
        elif re.search(r"\b(?:per|by|each) project\b|\bacross (?:all )?(?:my |our )?projects\b", text, re.I):
            selected.setdefault('chart', 'stacked')
        elif selected.get('project') and re.search(r"\bbreakdown\b|\bwho (?:did|has been doing)\b", text, re.I):
            selected['chart'] = 'monthly'
        else:
            selected.setdefault('chart', 'monthly' if selected.get('project') else 'stacked')
    period = _RELATIVE_MONTHS.search(text)
    if period and not _EXPLICIT_PERIOD.search(text):
        count = period[1].lower()
        if count.startswith('couple'):
            count = 2
        elif count.isdigit():
            count = int(count) if len(count) <= 2 else 13
        else:
            count = _MONTH_COUNTS.index(count) + 1
        selected.update(month='last_complete' if period[2] and period[2].lower() != 'calendar'
                        else 'recent', months=count)
    return selected


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
        existing = store.read(key)
        if existing:
            if not balance and channel_id.startswith('D') and existing.get('selector', {}).get('chart'):
                return {'message': 'Your chart request is already queued.', 'suppress_post': True}
            return f'This {report_name} request has already been queued for private delivery.'
        fields = ('month', 'months', 'action', 'client')
        if not balance:
            fields += ('project', 'chart')
        selected = {key: params[key] for key in fields if key in params}
        prior_selector = {}
        if selected.get('month') == 'previous' or selected.get('action') == 'detailed':
            previous = []
            for path in store.directory.glob('*.json'):
                candidate = store.read(path.stem)
                if (candidate and candidate.get('actor') == user_id
                        and candidate.get('team') == context.slack_team_id
                        and candidate.get('selector', {}).get('period') != 'weekly'
                        and candidate.get('report_kind', 'hours') == report_kind
                        and candidate.get('status') not in {'denied', 'rejected', 'error'}):
                    previous.append(candidate)
            prior = max(previous, key=lambda request: request['requested_at']) if previous else None
            prior_selector = prior['selector'] if prior else {}
            if not selected.get('month') or selected.get('month') == 'previous':
                selected['month'] = prior_selector.get('month', 'all' if balance else 'current')
                selected.setdefault('months', prior_selector.get('months', 1))
            if 'client' in prior_selector:
                selected.setdefault('client', prior_selector['client'])
            if not balance and 'chart' in prior_selector:
                selected.setdefault('chart', prior_selector['chart'])
        if balance and not selected.get('month') and 'months' not in selected:
            selected['month'] = 'all'
        try:
            if (not balance and 'project' in prior_selector and 'project' not in selected
                    and client_key(selected.get('client', 'self')) == client_key(prior_selector.get('client', 'self'))):
                selected['project'] = prior_selector['project']
            if selected.get('project') == 'all':
                del selected['project']
            selector, _, _ = resolve_period(selected, now)
        except (TimesheetError, TypeError, ValueError) as exc:
            if str(exc) == 'invalid_report_project':
                return 'Please give one project name or ask for all your projects.'
            if str(exc) == 'invalid_report_chart':
                return 'Please choose a stacked hours chart by project, cumulative lines by person, or both.'
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
    if not balance and selector.get('chart'):
        if channel_id.startswith('D'):
            return {'message': 'Your private chart is queued.', 'suppress_post': True}
        return 'I’ll send the chart to your DM.'
    if balance:
        return ('I’m preparing your Studio hours and purchased-hours report. '
                'I’ll DM it to you after checking your hours and payment report access.')
    return 'I’m preparing your Studio hours report. I’ll DM it to you after checking your project access.'
