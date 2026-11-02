"""Friday source barriers, private scope, retries and frozen shared charts."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest

from roo.studio_report_refresh import SourcePending, SourceRefresh
from roo.studio_report_schedule import enqueue_weekly, weekly_configuration
from tests.roo.test_studio_client_balance_source import setup, P1
from roo.timesheets import TimesheetError, timestamp

FRIDAY = timestamp('2026-10-16T17:00:00+11:00')
KEY = 'synthetic-source-key-' * 3


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    class Clock(datetime):
        value = FRIDAY
        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz)
    monkeypatch.setattr('roo.studio_report_worker.datetime', Clock)
    monkeypatch.setattr('roo.studio_report_refresh.datetime', Clock)
    return Clock


def configure(setup, *, missing=False):
    value = deepcopy(setup.value)
    data = value['clients']['UMARK']
    data.update(hours_reporting=True, as_of=FRIDAY.isoformat())
    data['coverage']['through'] = '2026-10-16'
    builder = next(iter(setup.config.builders))
    for work in data['hours']:
        work['builder_id'] = builder
    data['hours'].append({'id': 'oct-work', 'project_id': P1, 'hour_units': 5000000,
        'builder_id': builder, 'work_start': '2026-10-12', 'work_end': '2026-10-12',
        'date_basis': 'work_dates', 'allocation_note': 'Dated work submission.',
        'description': 'Reviewed work', 'evidence_ids': ['evidence']})
    checks = {key: {'checked_at': FRIDAY.isoformat()}
              for key in ('hours', 'contractor_invoices', 'client_invoices', 'payments')}
    checks['hours'].update(complete_through=FRIDAY.isoformat(), reviewed_at=FRIDAY.isoformat())
    checks['contractor_invoices']['unreviewed_count'] = 0
    template = {'version': 1, 'owner': 'UMARK', 'team': setup.config.team,
        'organization': setup.config.organization, 'project_ids': [P1],
        'cutoff': FRIDAY.isoformat(), 'checked_at': FRIDAY.isoformat(), 'checks': checks,
        'status': 'waiting_data' if missing else 'ready',
        'missing': ['hours_coverage_missing'] if missing else [], 'snapshot': value}
    http = Mock()

    def response(*args, **kwargs):
        result = {**deepcopy(template), 'nonce': kwargs['json']['nonce']}
        body = json.dumps(result, sort_keys=True, separators=(',', ':'))
        envelope = {'result': result, 'signature': hmac.new(KEY.encode(), body.encode(), hashlib.sha256).hexdigest()}
        return Mock(json=Mock(return_value=envelope))

    http.post.side_effect = response
    refresh = SourceRefresh(setup.config, setup.clients, http, 'https://source.example/prepare', KEY)
    setup.service.refresh = refresh
    setup.service.schedules = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'mark', 'actor': 'UMARK', 'client': 'UMARK', 'window': 'workweek', 'starts_on': '2026-10-16'},
            {'id': 'sam', 'actor': 'USAM', 'client': 'UMARK', 'window': 'workweek', 'starts_on': '2026-10-16'}])}, setup.clients)
    return refresh, http, template


def test_shared_snapshot_three_pngs_and_durable_delivery_for_both_recipients(setup):
    _, http, _ = configure(setup)
    events = enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    results = setup.service.commands(FRIDAY)
    assert [row['status'] for row in results] == ['sent', 'sent']
    assert http.post.call_count == 1
    reports = [setup.service.requests.read(event['request']) for event in events]
    assert reports[0]['finance_digest'] == reports[1]['finance_digest']
    assert reports[0]['preparation_key'] == reports[1]['preparation_key']
    assert sum(row['units'] for row in reports[0]['rows']) == 5000000
    for report in reports:
        assert [part['filename'] for part in report['parts'] if part.get('filename', '').endswith('.png')] == [
            'studio-hours-week.png', 'studio-hours-month-to-date.png', 'studio-client-payment-burndown.png']
        assert 'Sources checked:' in report['parts'][0]['content']
    assert setup.service.commands(FRIDAY + timedelta(minutes=6)) == []
    assert setup.api.upload_csv.call_count == 6


def test_missing_data_flags_retries_and_sends_one_notice_without_zero_charts(setup, clock):
    _, http, template = configure(setup, missing=True)
    events = enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    results = setup.service.commands(FRIDAY)
    assert {row['status'] for row in results} == {'waiting_data'}
    assert http.post.call_count == 1
    assert setup.api.upload_csv.call_count == 0
    assert setup.api.post_message.call_count == 2
    assert all('waiting for: hours coverage' in call.args[1] for call in setup.api.post_message.call_args_list)
    for event in events:
        queued = setup.service.weekly_queue.read(event['request'])
        assert queued['status'] == 'waiting_data' and queued['retry_at'] >= FRIDAY.timestamp() + 300
        assert setup.service.requests.read(event['request']) is None
    setup.service.commands(FRIDAY + timedelta(minutes=1))
    assert http.post.call_count == 1
    template.update(status='ready', missing=[])
    clock.value = FRIDAY + timedelta(minutes=6)
    results = setup.service.commands(FRIDAY + timedelta(minutes=6))
    assert {row['status'] for row in results} == {'sent'}
    assert http.post.call_count == 2
    assert setup.api.upload_csv.call_count == 6


def test_warmup_never_sends_and_cannot_suppress_closing_refresh(setup):
    refresh, http, template = configure(setup, missing=True)
    warm = FRIDAY - timedelta(minutes=1)
    template['checked_at'] = warm.isoformat()
    refresh.prewarm(setup.service.schedules, warm)
    assert http.post.call_count == 1
    assert setup.api.post_message.call_count == 0
    enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    setup.service.commands(FRIDAY)
    assert http.post.call_count == 2


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(owner='UOTHER'),
    lambda r: r.update(project_ids=[]),
    lambda r: r.update(cutoff=(FRIDAY + timedelta(hours=1)).isoformat()),
    lambda r: r['checks']['hours'].update(complete_through=(FRIDAY - timedelta(days=7)).isoformat()),
    lambda r: r['snapshot']['clients']['UMARK']['payments'][0].update(amount_cents=1),
])
def test_signed_but_wrong_scope_or_incomplete_response_stays_pending(setup, mutation):
    refresh, _, template = configure(setup)
    mutation(template)
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY)


def test_invalid_hmac_or_transport_failure_does_not_fallback_to_old_snapshot(setup):
    refresh, http, _ = configure(setup)
    http.post.return_value = Mock(json=Mock(return_value={'result': {}, 'signature': 'wrong'}))
    http.post.side_effect = None
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY)
    http.post.side_effect = RuntimeError('private provider error')
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY + timedelta(minutes=6))
    assert 'private provider error' not in json.dumps(refresh.store.read(refresh.identity('UMARK', FRIDAY)))


def test_weekly_ambiguous_work_dates_stay_pending_even_when_all_syncs_succeeded(setup):
    _, _, template = configure(setup)
    work = template['snapshot']['clients']['UMARK']['hours'][-1]
    work.update(work_start='2026-10-16', work_end='2026-10-16')
    enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    results = setup.service.commands(FRIDAY)
    assert {row['status'] for row in results} == {'waiting_data'}
    assert setup.api.upload_csv.call_count == 0


def test_corrected_dates_are_fetched_after_unusable_projection(setup, clock):
    _, http, template = configure(setup)
    work = template['snapshot']['clients']['UMARK']['hours'][-1]
    work.update(work_start='2026-10-16', work_end='2026-10-16')
    enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    setup.service.commands(FRIDAY)
    work.update(work_start_at='2026-10-16T10:00:00+11:00', work_end_at='2026-10-16T16:00:00+11:00')
    clock.value = FRIDAY + timedelta(minutes=6)
    results = setup.service.commands(clock.value)
    assert {row['status'] for row in results} == {'sent'}
    assert http.post.call_count == 2


def test_payment_chart_failure_cannot_send_or_complete_partial_report(setup, monkeypatch):
    configure(setup)
    events = enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    monkeypatch.setattr('roo.studio_client_balance.render_chart', Mock(side_effect=RuntimeError('private failure')))
    results = setup.service.commands(FRIDAY)
    assert {row['status'] for row in results} == {'error'}
    assert setup.api.upload_csv.call_count == 0
    for event in events:
        assert setup.service.requests.read(event['request']) is None
    assert all('Sources checked:' not in call.args[1] for call in setup.api.post_message.call_args_list)


def test_prepared_snapshot_rechecked_for_scope_before_delivery(setup):
    refresh, http, _ = configure(setup)
    events = enqueue_weekly(setup.service, setup.service.schedules, FRIDAY)
    request = setup.service.weekly_queue.read(events[0]['request'])
    report = setup.service.report_for_request(request)
    cached = refresh.store.read(report['preparation_key'])
    cached['result']['owner'] = 'UOTHER'
    refresh.store.write(report['preparation_key'], cached)
    with pytest.raises(TimesheetError, match='studio_source_scope_mismatch'):
        setup.service.deliver_request(report, request, 'synthetic-delivery', FRIDAY)
    setup.api.post_message.assert_not_called()
    setup.api.upload_csv.assert_not_called()
    assert http.post.call_count == 1


def on_demand(kind='hours', actor='USAM', at=FRIDAY):
    return {'team': 'T123', 'actor': actor, 'requested_at': at.isoformat(),
            'report_kind': kind, 'selector': {'month': 'all', 'action': 'summary', 'client': 'Mark'}}


@pytest.mark.parametrize('kind', ['hours', 'client_balance'])
def test_on_demand_reads_current_sources_and_freezes_retry_without_reusing_old_file(setup, kind):
    _, http, _ = configure(setup)
    # Discovery file deliberately remains at October 11. Only the signed
    # fresh response contains October 12 work and coverage through the 16th.
    setup.service.allow_historical_snapshot = True
    setup.value['clients']['UMARK']['hours_reporting'] = True
    for row in setup.value['clients']['UMARK']['hours']:
        row['builder_id'] = next(iter(setup.config.builders))
    setup.path.write_text(json.dumps(setup.value))
    request = on_demand(kind)
    report = setup.service.report_for_request(request)
    assert http.post.call_count == 1
    assert report['source_stale'] is False
    assert report['source_preparations'].keys() == {'UMARK'}
    assert 'Sources checked:' in report['parts'][0]['content']
    assert not any('Later activity is unavailable' in p['content'] for p in report['parts'] if p['kind'] == 'message')
    assert setup.service.deliver_request(deepcopy(report), request, 'fresh-test', FRIDAY) == 'sent'
    assert setup.service.deliver_request(deepcopy(report), request, 'fresh-test', FRIDAY) == 'sent'
    assert http.post.call_count == 1
    assert setup.api.upload_csv.call_count == len([p for p in report['parts'] if p['kind'] == 'file'])


@pytest.mark.parametrize('kind', ['hours', 'client_balance'])
def test_on_demand_missing_sources_never_fall_back_to_october_snapshot(setup, kind):
    _, http, _ = configure(setup, missing=True)
    setup.service.allow_historical_snapshot = True
    setup.value['clients']['UMARK']['hours_reporting'] = True
    for row in setup.value['clients']['UMARK']['hours']:
        row['builder_id'] = next(iter(setup.config.builders))
    setup.path.write_text(json.dumps(setup.value))
    request = on_demand(kind)
    with setup.service.queue.locked('a' * 64):
        setup.service.queue.write('a' * 64, request)
    outcome, = setup.service.commands(FRIDAY)
    assert outcome['status'] == 'waiting_data'
    assert http.post.call_count == 1
    setup.api.upload_csv.assert_not_called()
    text = setup.api.post_message.call_args.args[1]
    assert 'Your current Studio report is waiting for: hours coverage' in text
    assert 'Friday' not in text
    assert 'need Studio review' in text
    assert setup.service.requests.read('a' * 64) is None


def test_new_slack_request_for_same_period_performs_new_source_read(setup, clock):
    _, http, template = configure(setup)
    setup.service.report_for_request(on_demand('client_balance'))
    clock.value = FRIDAY + timedelta(minutes=1)
    template.update(cutoff=clock.value.isoformat(), checked_at=clock.value.isoformat())
    template['snapshot']['clients']['UMARK']['as_of'] = clock.value.isoformat()
    for check in template['checks'].values():
        check['checked_at'] = clock.value.isoformat()
    template['checks']['hours'].update(complete_through=clock.value.isoformat(), reviewed_at=clock.value.isoformat())
    setup.service.report_for_request(on_demand('client_balance', at=clock.value))
    assert http.post.call_count == 2


def test_on_demand_finance_access_is_checked_before_source_read(setup):
    _, http, _ = configure(setup)
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        setup.service.report_for_request(on_demand('client_balance', actor='UOTHER'))
    http.post.assert_not_called()


def test_rate_limited_source_retry_is_preserved_and_explained(setup):
    refresh, _, template = configure(setup, missing=True)
    retry = FRIDAY + timedelta(hours=6)
    template.update(missing=['finance_rate_limited', 'hours_coverage_missing'], retry_at=retry.isoformat())
    with pytest.raises(SourcePending) as failure:
        refresh.prepare('UMARK', FRIDAY, FRIDAY)
    assert failure.value.retry_at == retry.timestamp()
    request = {**on_demand('client_balance'), 'error': 'studio_source_waiting_data'}
    _, notice = setup.service.failure_notice(request)
    assert 'Xero rate limit' in notice and 'retry time' in notice


@pytest.mark.parametrize('field,value', [('as_of', '2026-10-11T00:00:00Z'),
                                        ('coverage', {'start': '2026-06-01', 'through': '2026-10-11'})])
def test_signed_fresh_checks_cannot_launder_stale_snapshot(setup, field, value):
    refresh, _, template = configure(setup)
    template['snapshot']['clients']['UMARK'][field] = value
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY)


def test_staff_overview_refreshes_every_finance_owner_with_separate_scope(setup):
    from tests.roo.test_studio_client_balance_source import P2
    _, http, template = configure(setup)
    setup.clients['USAM']['report_client_ids'].append('UOTHER')
    other = deepcopy(template['snapshot']['clients']['UMARK'])
    other['project_ids'] = [P2]
    for row in other['hours']:
        row['project_id'] = P2
    discovery = deepcopy(template['snapshot'])
    discovery['clients']['UOTHER'] = other
    setup.path.write_text(json.dumps(discovery))

    def response(*args, **kwargs):
        owner = kwargs['json']['owner']
        result = {**deepcopy(template), 'owner': owner, 'nonce': kwargs['json']['nonce']}
        if owner == 'UOTHER':
            result['project_ids'] = [P2]
            result['snapshot']['clients'] = {'UOTHER': deepcopy(other)}
        body = json.dumps(result, sort_keys=True, separators=(',', ':'))
        return Mock(json=Mock(return_value={'result': result,
            'signature': hmac.new(KEY.encode(), body.encode(), hashlib.sha256).hexdigest()}))

    http.post.side_effect = response
    request = on_demand()
    request['selector']['client'] = 'all'
    report = setup.service.report_for_request(request)
    assert report['source_preparations'].keys() == {'UMARK', 'UOTHER'}
    assert {call.kwargs['json']['owner'] for call in http.post.call_args_list} == {'UMARK', 'UOTHER'}
    assert setup.service.deliver_request(report, request, 'staff-fresh', FRIDAY) == 'sent'
    assert http.post.call_count == 2


def test_prepared_owner_projections_reject_conflicting_shared_evidence(setup):
    refresh, _, template = configure(setup)
    first = template['snapshot']
    second = deepcopy(first)
    second['evidence']['evidence']['sha256'] = 'b' * 64
    with pytest.raises(TimesheetError, match='studio_source_scope_mismatch'):
        refresh.combine([first, second])
