"""Private, opt-in Friday client reports using the durable command delivery path.

Weekly requests remain in the private data volume so Public Roo cannot mistake
them for monthly command context. Each also requires a private issuance receipt.
"""
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .payment_reminders import ReceiptStore
from .studio_report_clients import client_key, select_client, validate_client_reporting
from .timesheets import TimesheetError, fingerprint, flag, timestamp

TZ = ZoneInfo('Australia/Melbourne')
_INPUT_KEYS = {'id', 'actor', 'client', 'time', 'window', 'action', 'starts_on'}
_REQUEST_KEYS = {'team', 'actor', 'selector', 'channel', 'thread_ts', 'requested_at', 'scheduled'}
_LIFECYCLE_KEYS = {'status', 'error', 'retry_at'}


def _target(clients, actor, query):
    if client_key(query) in {'all', 'self'}:
        raise TimesheetError('weekly_client_required')
    _, _, targets = select_client(clients, actor, {'client': query})
    if len(targets) != 1:
        raise TimesheetError('weekly_client_required')
    owner = next(iter(targets))
    return owner, fingerprint({'actor': actor, 'requester': clients[actor],
                               'owner': owner, 'client': clients[owner]})


def weekly_configuration(env, clients):
    """Validate private schedule configuration; the feature defaults to off.

    An explicit name/alias/owner ID selects exactly one existing client grant.
    The actor is also the recipient; recipient/channel overrides are rejected.
    ``starts_on`` optionally prevents catch-up before an activation date.
    """
    if not flag(env, 'STUDIO_REPORTS_WEEKLY_ENABLED'):
        return []
    try:
        raw = json.loads(env.get('STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON', '[]'))
    except (TypeError, ValueError) as exc:
        raise TimesheetError('invalid_weekly_schedules') from exc
    if not isinstance(raw, list):
        raise TimesheetError('invalid_weekly_schedules')
    validate_client_reporting(clients)
    schedules, ids = [], set()
    for entry in raw:
        if (not isinstance(entry, dict) or set(entry) - _INPUT_KEYS
                or not {'id', 'actor', 'client'} <= set(entry)
                or not isinstance(entry['id'], str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', entry['id']) is None
                or entry['id'] in ids
                or not isinstance(entry['actor'], str)
                or re.fullmatch(r'[UW][A-Z0-9]+', entry['actor']) is None
                or entry['actor'] not in clients):
            raise TimesheetError('invalid_weekly_schedule')
        clock = entry.get('time', '17:00')
        window = entry.get('window', 'workweek')
        action = entry.get('action', 'summary')
        if (not isinstance(clock, str)
                or re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock) is None
                or not isinstance(window, str) or window not in {'workweek', 'last_7_days'}
                or not isinstance(action, str) or action not in {'summary', 'detailed'}):
            raise TimesheetError('invalid_weekly_schedule')
        starts_on = entry.get('starts_on')
        if starts_on is not None:
            try:
                if (not isinstance(starts_on, str)
                        or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', starts_on) is None):
                    raise ValueError
                date.fromisoformat(starts_on)
            except ValueError as exc:
                raise TimesheetError('invalid_weekly_start_date') from exc
        owner, grant = _target(clients, entry['actor'], entry['client'])
        schedule = {'id': entry['id'], 'actor': entry['actor'], 'client': owner,
                    'time': clock, 'window': window, 'action': action,
                    'starts_on': starts_on, 'grant_fingerprint': grant}
        schedule['fingerprint'] = fingerprint({**schedule, 'timezone': TZ.key})
        schedules.append(schedule)
        ids.add(entry['id'])
    return schedules


def latest_due(schedule, now):
    """Return only the latest elapsed Friday cutoff, in Melbourne calendar time.

    A delayed worker catches up until the next Friday's cutoff. It never builds
    a backlog of earlier Fridays or moves the requested reporting window.
    """
    local = timestamp(now).astimezone(TZ)
    friday = local.date() - timedelta(days=(local.weekday() - 4) % 7)
    hour, minute = map(int, schedule['time'].split(':'))
    due = datetime.combine(friday, time(hour, minute), TZ)
    if timestamp(due) > timestamp(local):
        due -= timedelta(days=7)
    if schedule['starts_on'] and due.date() < date.fromisoformat(schedule['starts_on']):
        return None
    return due


def weekly_selector(schedule, due):
    due = timestamp(due).astimezone(TZ)
    if schedule['window'] == 'workweek':
        start = datetime.combine(due.date() - timedelta(days=4), time(), TZ)
    else:
        # Calendar arithmetic keeps the Melbourne cutoff hour across DST.
        start = due - timedelta(days=7)
    return {'period': 'weekly', 'start': start.isoformat(), 'end': due.isoformat(),
            'action': schedule['action'], 'client': schedule['client']}


def _request_key(team, schedule, due):
    return fingerprint({'kind': 'studio-weekly-v1', 'team': team,
                        'schedule': schedule['id'], 'date': due.date().isoformat(),
                        'recipient': schedule['actor'], 'client': schedule['client']})


def _current_scope(service, schedule, selector):
    owner, grant = _target(service.clients, schedule['actor'], schedule['client'])
    if owner != schedule['client'] or grant != schedule['grant_fingerprint']:
        raise TimesheetError('weekly_schedule_changed')
    return service.scope_fingerprint(schedule['actor'], selector)


def _canonical_request(service, schedule, due):
    selector = weekly_selector(schedule, due)
    key = _request_key(service.config.team, schedule, due)
    request = {'team': service.config.team, 'actor': schedule['actor'],
               'selector': selector, 'channel': '', 'thread_ts': '',
               'requested_at': due.isoformat(),
               'scheduled': {'id': schedule['id'], 'due_at': due.isoformat(),
                             'fingerprint': schedule['fingerprint'],
                             'request_key': key,
                             'scope': _current_scope(service, schedule, selector)}}
    return key, request


def _issuances(service):
    return ReceiptStore(Path(service.config.directory) / 'weekly-schedules')


def _issuance(request):
    return {'version': 1, 'request_fingerprint': fingerprint(request)}


def _enqueue_one(service, schedule, schedules, now):
    due = latest_due(schedule, now)
    if due is None:
        return None
    key, request = _canonical_request(service, schedule, due)
    event = {'schedule': schedule['id'], 'request': key}
    with service.weekly_queue.locked(key) as acquired:
        if not acquired:
            return {**event, 'status': 'busy'}
        existing = service.weekly_queue.read(key)
        if existing is not None:
            if not scheduled_request_authorized(existing, schedules, service=service):
                return {**event, 'status': 'error', 'reason': 'weekly_request_changed'}
            return None
        expected = _issuance(request)
        store = _issuances(service)
        with store.locked(key) as issued:
            if not issued:
                return {**event, 'status': 'busy'}
            prior = store.read(key)
            if prior is not None and prior != expected:
                return {**event, 'status': 'error', 'reason': 'weekly_request_changed'}
            if prior is None:
                # Private provenance must exist before the queued entry.
                store.write(key, expected)
            service.weekly_queue.write(key, {**request, 'status': 'pending'})
        return {**event, 'status': 'queued', 'due_at': due.isoformat()}


def enqueue_weekly(service, schedules, now):
    """Issue due requests privately, then queue them once using existing locks.

    This performs no source reads or Slack actions. Queue entries and issuance
    receipts are durable; changed schedules cannot replace a prior Friday's
    entry or issue a duplicate report for the same recipient and client.
    A damaged schedule/receipt is isolated so other schedules and on-demand
    commands can still run. Provider details and file contents are never logged.
    """
    results = []
    for schedule in schedules:
        try:
            event = _enqueue_one(service, schedule, schedules, now)
            if event is not None:
                results.append(event)
        except Exception as exc:
            results.append({'schedule': schedule['id'], 'status': 'error',
                            'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__})
    return results


def scheduled_request_authorized(request, schedules, *, service=None):
    """Authenticate canonical schedule, current grants and private issuance.

    Pass the private worker service on every source read and delivery. Removed,
    disabled, changed, forged or recipient-overridden schedules fail closed.
    Mutable command lifecycle fields never grant additional report authority.
    """
    if service is None or not isinstance(request, dict):
        return False
    if set(request) - (_REQUEST_KEYS | _LIFECYCLE_KEYS) or not _REQUEST_KEYS <= set(request):
        return False
    metadata = request.get('scheduled')
    if not isinstance(metadata, dict):
        return False
    matches = [item for item in schedules if item['id'] == metadata.get('id')]
    if len(matches) != 1:
        return False
    schedule = matches[0]
    try:
        due = timestamp(metadata['due_at']).astimezone(TZ)
        # Verify an actual schedule cutoff, not just a caller-provided Friday.
        if latest_due(schedule, due) != due:
            return False
        key, canonical = _canonical_request(service, schedule, due)
        envelope = {field: request[field] for field in _REQUEST_KEYS}
        if envelope != canonical:
            return False
        return _issuances(service).read(key) == _issuance(canonical)
    except (TimesheetError, ValueError, TypeError, KeyError, AttributeError, OSError):
        return False
