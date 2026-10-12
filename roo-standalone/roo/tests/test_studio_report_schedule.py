"""Offline Friday scheduling, private issuance and client-grant boundaries."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from roo.payment_reminders import ReceiptStore
from roo.backend_identity import BackendActorContext
from roo.studio_report_clients import select_client
from roo.studio_report_commands import enqueue as enqueue_monthly
from roo.studio_report_schedule import (TZ, _request_key, enqueue_weekly, latest_due,
                                        scheduled_request_authorized, weekly_configuration,
                                        weekly_selector)
from roo.timesheets import TimesheetError, fingerprint, timestamp
from roo.tests.test_studio_reports import (P1, P2, P3, setup as report_setup,
                                          ticket)


@pytest.fixture
def setup(tmp_path):
    clients = {
        'UMARK': {'name': 'Mark Ghiasy', 'aliases': ['Mark'], 'project_ids': ['p1', 'p2']},
        'UOTHER': {'name': 'Other client', 'project_ids': ['p3']},
        'USTAFF': {'name': 'Studio staff', 'project_ids': ['p1', 'p2', 'p3'],
                   'report_client_ids': ['UMARK', 'UOTHER']},
    }
    entry = {'id': 'mark-weekly', 'actor': 'USTAFF', 'client': 'Mark Ghiasy'}
    env = {'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
           'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([entry])}
    config = SimpleNamespace(team='T123', directory=tmp_path / 'private',
                             projects={'p1': 'App', 'p2': 'Website', 'p3': 'Other'})
    service = SimpleNamespace(config=config, clients=clients,
                              queue=ReceiptStore(tmp_path / 'queue'),
                              weekly_queue=ReceiptStore(config.directory / 'weekly-queue'), api=Mock())

    def scope(actor, selector):
        client = clients[actor]
        _, groups, targets = select_client(clients, actor, selector)
        return fingerprint({'actor': actor, 'client': client, 'targets': targets,
                            'groups': groups, 'projects': config.projects})

    service.scope_fingerprint = scope
    return SimpleNamespace(env=env, entry=entry, clients=clients, service=service)


def configured(setup, **changes):
    env = {**setup.env, 'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON':
           json.dumps([{**setup.entry, **changes}])}
    return weekly_configuration(env, setup.clients)


def queued(setup, now='2026-10-09T06:00:00Z', **changes):
    schedules = configured(setup, **changes)
    events = enqueue_weekly(setup.service, schedules, timestamp(now))
    key = events[0]['request']
    return schedules, key, setup.service.weekly_queue.read(key)


def test_disabled_ignores_unconfigured_or_invalid_schedules():
    assert weekly_configuration({}, {}) == []
    assert weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'false',
                                 'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': 'malformed'}, {}) == []


def test_configuration_defaults_and_explicit_owner_identity(setup):
    schedule = configured(setup)[0]
    assert schedule['client'] == 'UMARK'
    assert schedule['actor'] == 'USTAFF'
    assert schedule['time'] == '17:00'
    assert schedule['window'] == 'workweek'
    assert schedule['action'] == 'summary'
    assert schedule['starts_on'] is None
    # An owner's named request is valid and still delivers to that same owner.
    assert configured(setup, actor='UMARK', client='Mark')[0]['client'] == 'UMARK'


@pytest.mark.parametrize('changes', [
    {'id': '../escape'}, {'id': ''}, {'id': []}, {'actor': 'UUNKNOWN'},
    {'actor': 4}, {'client': 'all'}, {'client': 'self'}, {'client': 'Unknown'},
    {'client': 4}, {'time': '24:00'}, {'time': '9:00'}, {'time': 1700},
    {'window': 'monthly'}, {'window': []}, {'action': 'send'}, {'action': {}},
    {'recipient': 'UOTHER'}, {'channel': 'C123'}, {'starts_on': '2026-02-30'},
    {'starts_on': '2026-2-01'}, {'starts_on': 2026},
])
def test_invalid_schedule_rejected(setup, changes):
    with pytest.raises(TimesheetError):
        configured(setup, **changes)


@pytest.mark.parametrize('raw', ['{}', 'null', 'bad json', '[4]'])
def test_invalid_schedule_list_rejected(setup, raw):
    with pytest.raises(TimesheetError):
        weekly_configuration({**setup.env, 'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': raw}, setup.clients)


def test_duplicate_schedule_id_rejected(setup):
    with pytest.raises(TimesheetError, match='invalid_weekly_schedule'):
        weekly_configuration({**setup.env, 'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON':
                              json.dumps([setup.entry, setup.entry])}, setup.clients)


def test_inaccessible_and_ambiguous_clients_rejected(setup):
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        configured(setup, actor='UMARK', client='Other client')
    setup.clients['UOTHER']['aliases'] = ['Mark']
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        configured(setup, client='Mark')


def test_no_report_before_first_friday_cutoff(setup):
    schedules = configured(setup, starts_on='2026-10-09')
    assert latest_due(schedules[0], timestamp('2026-10-09T05:59:59Z')) is None
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-09T05:59:59Z')) == []
    assert not setup.service.config.directory.exists()
    assert latest_due(schedules[0], timestamp('2026-10-09T06:00:00Z')).isoformat() == '2026-10-09T17:00:00+11:00'


def test_activation_date_prevents_prior_week_catchup(setup):
    schedules = configured(setup, starts_on='2026-10-16')
    assert latest_due(schedules[0], timestamp('2026-10-12T02:00:00Z')) is None
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-12T02:00:00Z')) == []


@pytest.mark.parametrize(('now', 'expected'), [
    ('2026-10-09T05:59:59Z', '2026-10-02T17:00:00+10:00'),
    ('2026-10-09T06:00:00Z', '2026-10-09T17:00:00+11:00'),
    ('2026-10-12T00:00:00Z', '2026-10-09T17:00:00+11:00'),
    ('2026-10-16T05:59:59Z', '2026-10-09T17:00:00+11:00'),
    ('2026-10-16T06:00:00Z', '2026-10-16T17:00:00+11:00'),
])
def test_latest_friday_cutoff_and_catchup_window(setup, now, expected):
    assert latest_due(configured(setup)[0], timestamp(now)).isoformat() == expected


@pytest.mark.parametrize(('due', 'start', 'elapsed_hours'), [
    ('2026-10-09T17:00:00+11:00', '2026-10-02T17:00:00+10:00', 167),
    ('2026-04-10T17:00:00+10:00', '2026-04-03T17:00:00+11:00', 169),
])
def test_seven_calendar_days_preserve_hour_across_dst(setup, due, start, elapsed_hours):
    selector = weekly_selector(configured(setup, window='last_7_days')[0], timestamp(due))
    assert selector['start'] == start and selector['end'] == due
    assert (timestamp(due) - timestamp(start)).total_seconds() / 3600 == elapsed_hours


@pytest.mark.parametrize(('due', 'start'), [
    ('2026-10-02T17:00:00+10:00', '2026-09-28T00:00:00+10:00'),
    ('2027-01-01T17:00:00+11:00', '2026-12-28T00:00:00+11:00'),
])
def test_workweek_starts_monday_across_month_and_year(setup, due, start):
    selector = weekly_selector(configured(setup)[0], timestamp(due))
    assert selector == {'period': 'weekly', 'start': start, 'end': due,
                        'client': 'UMARK', 'action': 'summary'}


def test_late_tick_freezes_cutoff_and_repeated_ticks_issue_once(setup):
    schedules, key, request = queued(setup, now='2026-10-12T02:00:00Z')
    assert request['requested_at'] == '2026-10-09T17:00:00+11:00'
    assert request['selector']['end'] == request['requested_at']
    assert request['selector']['start'] == '2026-10-05T00:00:00+11:00'
    assert request['channel'] == request['thread_ts'] == ''
    assert request['actor'] == 'USTAFF' and request['status'] == 'pending'
    assert scheduled_request_authorized(request, schedules, service=setup.service)
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-15T02:00:00Z')) == []
    assert len(list(setup.service.weekly_queue.directory.glob('*.json'))) == 1
    setup.service.api.assert_not_called()
    assert not setup.service.api.method_calls
    request.update(status='done')
    setup.service.weekly_queue.write(key, request)
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-15T05:00:00Z')) == []
    next_events = enqueue_weekly(setup.service, schedules, timestamp('2026-10-16T06:00:00Z'))
    assert next_events[0]['request'] != key
    assert len(list(setup.service.weekly_queue.directory.glob('*.json'))) == 2


def test_no_provenance_without_private_receipt(setup):
    schedules, key, request = queued(setup)
    assert not scheduled_request_authorized(request, schedules)
    private = setup.service.config.directory / 'weekly-schedules' / (key + '.json')
    private.unlink()
    assert not scheduled_request_authorized(request, schedules, service=setup.service)
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-09T07:00:00Z'))[0]['reason'] == 'weekly_request_changed'


def test_private_issuance_precedes_private_queue_and_recovers_queue_crash(setup, monkeypatch):
    schedules = configured(setup)
    write = setup.service.weekly_queue.write

    def crash(key, request):
        assert (setup.service.config.directory / 'weekly-schedules' / (key + '.json')).exists()
        raise RuntimeError('simulated queue write crash')

    monkeypatch.setattr(setup.service.weekly_queue, 'write', crash)
    failed = enqueue_weekly(setup.service, schedules, timestamp('2026-10-09T06:00:00Z'))
    assert failed == [{'schedule': 'mark-weekly', 'status': 'error', 'reason': 'RuntimeError'}]
    monkeypatch.setattr(setup.service.weekly_queue, 'write', write)
    events = enqueue_weekly(setup.service, schedules, timestamp('2026-10-12T02:00:00Z'))
    assert len(events) == 1 and events[0]['status'] == 'queued'
    assert scheduled_request_authorized(setup.service.weekly_queue.read(events[0]['request']), schedules,
                                        service=setup.service)


@pytest.mark.parametrize('changes', [
    {'time': '18:00'}, {'window': 'last_7_days'}, {'action': 'detailed'},
    {'client': 'Other client'}, {'actor': 'UMARK'}, {'starts_on': '2026-10-16'},
])
def test_changed_and_disabled_schedules_revoke_pending_requests(setup, changes):
    schedules, _, request = queued(setup)
    assert scheduled_request_authorized(request, schedules, service=setup.service)
    assert not scheduled_request_authorized(request, [], service=setup.service)
    assert not scheduled_request_authorized(request, configured(setup, **changes), service=setup.service)


@pytest.mark.parametrize('mutation', ['grant', 'client_projects', 'client_name', 'project_name'])
def test_current_grants_and_source_scope_checked_again(setup, mutation):
    schedules, _, request = queued(setup)
    if mutation == 'grant':
        setup.clients['USTAFF']['report_client_ids'].remove('UMARK')
    elif mutation == 'client_projects':
        setup.clients['UMARK']['project_ids'].remove('p2')
    elif mutation == 'client_name':
        setup.clients['UMARK']['name'] = 'Changed owner'
    else:
        setup.service.config.projects['p1'] = 'Changed project'
    assert not scheduled_request_authorized(request, schedules, service=setup.service)
    assert not setup.service.api.method_calls


@pytest.mark.parametrize('mutation', ['team', 'actor', 'recipient', 'channel', 'thread', 'selector',
                                     'timestamp', 'metadata', 'fingerprint', 'scope', 'cutoff'])
def test_queue_schedule_claims_cannot_change_authority(setup, mutation):
    schedules, key, request = queued(setup)
    changed = deepcopy(request)
    if mutation == 'team': changed['team'] = 'TOTHER'
    elif mutation == 'actor': changed['actor'] = 'UOTHER'
    elif mutation == 'recipient': changed['recipient'] = 'UOTHER'
    elif mutation == 'channel': changed['channel'] = 'C123'
    elif mutation == 'thread': changed['thread_ts'] = '1.2'
    elif mutation == 'selector': changed['selector']['month'] = 'all'
    elif mutation == 'timestamp': changed['requested_at'] = '2026-10-09T06:00:00Z'
    elif mutation == 'metadata': changed['scheduled']['unexpected'] = True
    elif mutation == 'fingerprint': changed['scheduled']['fingerprint'] = 'forged'
    elif mutation == 'scope': changed['scheduled']['scope'] = 'forged'
    else: changed['scheduled']['due_at'] = '2026-10-09T17:00:01+11:00'
    assert not scheduled_request_authorized(changed, schedules, service=setup.service)
    setup.service.weekly_queue.write(key, changed)
    assert enqueue_weekly(setup.service, schedules, timestamp('2026-10-12T02:00:00Z'))[0]['reason'] == 'weekly_request_changed'
    assert setup.service.weekly_queue.read(key) == changed


def test_copying_known_schedule_metadata_cannot_forge_another_friday(setup):
    schedules, _, request = queued(setup)
    later = datetime(2026, 10, 16, 17, tzinfo=TZ)
    forged = deepcopy(request)
    forged['requested_at'] = later.isoformat()
    forged['selector'] = weekly_selector(schedules[0], later)
    forged['scheduled'].update(due_at=later.isoformat(),
                              request_key=_request_key('T123', schedules[0], later))
    assert not scheduled_request_authorized(forged, schedules, service=setup.service)


def test_lifecycle_fields_do_not_change_issuance(setup):
    schedules, _, request = queued(setup)
    request.update(status='error', error='source_rate_limited', retry_at=123)
    assert scheduled_request_authorized(request, schedules, service=setup.service)


def test_existing_private_receipt_never_rewritten_for_changed_schedule(setup):
    schedules, key, request = queued(setup)
    private = setup.service.config.directory / 'weekly-schedules' / (key + '.json')
    prior = private.read_bytes()
    (setup.service.weekly_queue.directory / (key + '.json')).unlink()
    changed = configured(setup, window='last_7_days')
    results = enqueue_weekly(setup.service, changed, timestamp('2026-10-09T06:00:00Z'))
    assert results[0]['reason'] == 'weekly_request_changed'
    assert private.read_bytes() == prior
    assert setup.service.weekly_queue.read(key) is None
    assert not scheduled_request_authorized(request, changed, service=setup.service)


def real_schedules(report_setup):
    report_setup.clients['USTAFF'] = {'name': 'Studio staff', 'project_ids': [P1, P2, P3],
                                     'monthly_hours': None, 'report_client_ids': ['UMARK', 'UOTHER']}
    schedules = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'mark-friday', 'actor': 'USTAFF', 'client': 'Mark', 'starts_on': '2026-10-09'}])},
        report_setup.clients)
    report_setup.service.schedules = schedules
    return schedules


def test_service_delivers_owned_projects_privately_at_frozen_weekly_cutoff(report_setup, monkeypatch):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', lambda *_: b'offline chart')
    schedules = real_schedules(report_setup)
    service, api = report_setup.service, report_setup.api
    api.collect.return_value = [
        ticket(1, project=P1, completed='2026-10-06T01:00:00Z'),
        ticket(2, project=P2, size='Medium (M)', completed='2026-10-08T01:00:00Z'),
        ticket(3, project=P3, title='Other private client work', completed='2026-10-07T01:00:00Z'),
        ticket(4, completed='2026-10-04T01:00:00Z'),  # Before Monday.
        ticket(5, completed='2026-10-09T06:00:00Z'),  # At Friday's exclusive cutoff.
        ticket(6, completed='2026-10-10T01:00:00Z'),
    ]
    delayed = timestamp('2026-10-12T02:00:00Z')
    event = enqueue_weekly(service, schedules, delayed)[0]
    assert service.commands(delayed) == [{'request': event['request'], 'status': 'sent'}]
    report = service.requests.read(event['request'])
    assert report['selector']['period'] == 'weekly'
    assert report['generated_at'] == '2026-10-09T17:00:00+11:00'
    assert {row['project_id'] for row in report['rows']} == {P1, P2}
    assert sum(row['units'] for row in report['rows']) == 12
    assert {row['identifier'] for row in report['rows']} == {'STU-1', 'STU-2'}
    source, as_of, _ = api.collect.call_args.args
    assert set(source.projects) == {P1, P2}
    assert as_of == timestamp('2026-10-09T06:00:00Z')
    api.open_dm.assert_called_once_with('USTAFF')
    assert all(call.args[0] == 'DUSTAFF' for call in api.post_message.call_args_list)
    delivered = '\n'.join(call.args[1] for call in api.post_message.call_args_list)
    assert 'Master App' in delivered and 'Cybertest' in delivered
    assert 'Other private client' not in delivered and 'Other client secret' not in delivered
    api.verify_recipient.assert_called_with('USTAFF', 'T123')
    source_calls, posts = api.collect.call_count, api.post_message.call_count
    assert enqueue_weekly(service, schedules, delayed) == []
    assert service.commands(delayed) == []
    assert api.collect.call_count == source_calls and api.post_message.call_count == posts


@pytest.mark.parametrize('snapshot', [False, True])
def test_service_revoked_schedule_blocks_queue_and_snapshot_before_delivery(report_setup, monkeypatch, snapshot):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', lambda *_: b'offline chart')
    schedules = real_schedules(report_setup)
    service, api = report_setup.service, report_setup.api
    now = timestamp('2026-10-09T06:00:00Z')
    event = enqueue_weekly(service, schedules, now)[0]
    request = service.weekly_queue.read(event['request'])
    if snapshot:
        report = service.report_for_request(request)
        with service.requests.locked(event['request']) as acquired:
            assert acquired
            service.requests.write(event['request'], report)
        api.reset_mock()
    service.schedules = []
    assert service.commands(now) == []
    assert service.weekly_queue.read(event['request'])['status'] == 'denied'
    assert not api.method_calls
    with pytest.raises(TimesheetError, match='not_authorized'):
        service.report_for_request(request)
    if snapshot:
        with pytest.raises(TimesheetError, match='not_authorized'):
            service.deliver_request(report, request, 'command-' + event['request'], now)
    assert not api.method_calls


def test_copying_issued_request_to_another_queue_filename_does_not_redeliver(report_setup, monkeypatch):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', lambda *_: b'offline chart')
    schedules = real_schedules(report_setup)
    service, api = report_setup.service, report_setup.api
    now = timestamp('2026-10-09T06:00:00Z')
    event = enqueue_weekly(service, schedules, now)[0]
    original = service.weekly_queue.read(event['request'])
    assert service.commands(now)[0]['status'] == 'sent'
    copied_key = 'b' * 64
    assert copied_key != event['request']
    with service.queue.locked(copied_key) as acquired:
        assert acquired
        service.queue.write(copied_key, original)
    assert service.request_authorized(original)
    assert not service.request_key_authorized(original, copied_key)
    api.reset_mock()
    assert service.commands(now) == []
    assert service.queue.read(copied_key)['status'] == 'denied'
    assert not api.method_calls


@pytest.mark.parametrize('damaged', ['queue', 'issuance', 'scope', 'io'])
def test_failed_schedule_does_not_block_other_schedules_or_monthly_commands(report_setup, monkeypatch, damaged):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', lambda *_: b'offline chart')
    schedules = real_schedules(report_setup)
    schedules.extend(weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'other-friday', 'actor': 'USTAFF', 'client': 'Other', 'starts_on': '2026-10-09'}])},
        report_setup.clients))
    service = report_setup.service
    now = timestamp('2026-10-09T06:00:00Z')
    first_key = _request_key(service.config.team, schedules[0], latest_due(schedules[0], now))
    monthly_key = fingerprint({'offline': 'monthly'})
    with service.queue.locked(monthly_key) as acquired:
        assert acquired
        service.queue.write(monthly_key, {'team': 'T123', 'actor': 'UMARK',
            'selector': {'month': '2026-10', 'months': 1, 'action': 'summary'},
            'channel': '', 'thread_ts': '', 'requested_at': now.isoformat(), 'status': 'pending'})
    if damaged == 'queue':
        service.weekly_queue.directory.mkdir(parents=True, exist_ok=True)
        (service.weekly_queue.directory / (first_key + '.json')).write_text('damaged sensitive content')
    elif damaged == 'issuance':
        directory = service.config.directory / 'weekly-schedules'
        directory.mkdir(parents=True)
        (directory / (first_key + '.json')).write_text('damaged sensitive content')
    elif damaged == 'scope':
        report_setup.clients['USTAFF']['report_client_ids'].remove('UMARK')
        # Update the other schedule's grant fingerprint to match the current
        # private grant configuration; the removed target remains invalid.
        schedules[1] = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
            'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
                {'id': 'other-friday', 'actor': 'USTAFF', 'client': 'Other', 'starts_on': '2026-10-09'}])},
            report_setup.clients)[0]
    else:
        original_read = service.weekly_queue.read

        def unavailable(key):
            if key == first_key:
                raise OSError('sensitive filesystem details')
            return original_read(key)

        monkeypatch.setattr(service.weekly_queue, 'read', unavailable)
    events = enqueue_weekly(service, schedules, now)
    assert events[0]['schedule'] == 'mark-friday' and events[0]['status'] == 'error'
    assert 'sensitive' not in json.dumps(events)
    assert events[1]['schedule'] == 'other-friday' and events[1]['status'] == 'queued'
    results = service.commands(now)
    assert {'request': monthly_key, 'status': 'sent'} in results
    assert {'request': events[1]['request'], 'status': 'sent'} in results
    assert report_setup.api.collect.call_count == 2
    assert service.queue.read(monthly_key)['status'] == 'done'
    assert service.weekly_queue.read(events[1]['request'])['status'] == 'done'


def test_mark_and_sam_weekly_requests_stay_private_and_preserve_monthly_followup(report_setup):
    service = report_setup.service
    report_setup.clients['USAM'] = {'name': 'Sam', 'project_ids': [P1, P2],
                                   'report_client_ids': ['UMARK']}
    schedules = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'mark-friday', 'actor': 'UMARK', 'client': 'Mark', 'starts_on': '2026-10-09'},
            {'id': 'sam-mark-friday', 'actor': 'USAM', 'client': 'Mark', 'starts_on': '2026-10-09'}])},
        report_setup.clients)
    service.schedules = schedules
    monthly_key = fingerprint({'offline': 'monthly-before-friday'})
    with service.queue.locked(monthly_key) as acquired:
        assert acquired
        service.queue.write(monthly_key, {'team': 'T123', 'actor': 'UMARK',
            'selector': {'month': '2026-09', 'months': 1, 'action': 'summary', 'client': 'Mark'},
            'channel': 'C123', 'thread_ts': '1.0', 'requested_at': '2026-09-22T04:00:00+00:00',
            'status': 'pending'})
    now = timestamp('2026-10-12T02:00:00Z')
    events = enqueue_weekly(service, schedules, now)
    assert len(events) == 2 and all(event['status'] == 'queued' for event in events)
    private_requests = [service.weekly_queue.read(event['request']) for event in events]
    assert {request['actor'] for request in private_requests} == {'UMARK', 'USAM'}
    assert all(request['selector']['period'] == 'weekly' for request in private_requests)
    assert {path.stem for path in service.queue.directory.glob('*.json')} == {monthly_key}
    assert not {event['request'] for event in events} & {
        path.stem for path in service.queue.directory.glob('*.json')}
    context = BackendActorContext('T123', 'UMARK', 'C123', '1.0', 'monthly-details-after-friday')
    response = enqueue_monthly(report_setup.settings, context,
        {'month': 'previous', 'action': 'detailed'}, user_id='UMARK', channel_id='C123',
        thread_ts='1.0', now=now)
    assert 'preparing' in response
    followups = [service.queue.read(path.stem) for path in service.queue.directory.glob('*.json')
                 if path.stem != monthly_key]
    assert len(followups) == 1
    assert followups[0]['selector']['month'] == '2026-09'
    assert followups[0]['selector']['months'] == 1
    assert followups[0]['selector']['action'] == 'detailed'
    assert 'period' not in followups[0]['selector']
    assert not report_setup.api.method_calls


def test_copied_canonical_request_in_shared_queue_reuses_private_delivery_receipt(report_setup, monkeypatch):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', lambda *_: b'offline chart')
    schedules = real_schedules(report_setup)
    service, api = report_setup.service, report_setup.api
    now = timestamp('2026-10-09T06:00:00Z')
    event = enqueue_weekly(service, schedules, now)[0]
    original = service.weekly_queue.read(event['request'])
    assert service.commands(now) == [{'request': event['request'], 'status': 'sent'}]
    source_calls, posts, uploads = api.collect.call_count, api.post_message.call_count, api.upload_csv.call_count
    with service.queue.locked(event['request']) as acquired:
        assert acquired
        service.queue.write(event['request'], original)
    assert service.commands(now) == [{'request': event['request'], 'status': 'sent'}]
    assert api.collect.call_count == source_calls
    assert api.post_message.call_count == posts and api.upload_csv.call_count == uploads
    api.open_dm.assert_called_once_with('USTAFF')
    assert service.queue.read(event['request'])['status'] == 'done'
    assert service.weekly_queue.read(event['request'])['status'] == 'done'
