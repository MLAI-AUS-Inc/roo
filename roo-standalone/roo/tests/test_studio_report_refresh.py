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
from roo.tests.test_studio_client_balance_source import setup, P1
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
