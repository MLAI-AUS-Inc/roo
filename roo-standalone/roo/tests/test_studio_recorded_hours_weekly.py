"""Canonical weekly hours preserve quantities and require supported timing."""
from types import SimpleNamespace
import json

import pytest

from roo.studio_client_balance_source import validate_snapshot_age
from roo.studio_recorded_hours import build_recorded_report, overlay_recorded_reports
from roo.studio_report_schedule import enqueue_weekly, weekly_configuration
from roo.tests.test_studio_client_balance_source import setup as finance_setup
from roo.timesheets import TimesheetError, timestamp

NOW = timestamp('2026-10-16T17:00:00+11:00')
WEEK = {'period': 'weekly', 'start': '2026-10-12T00:00:00+11:00',
        'end': NOW.isoformat(), 'action': 'summary'}
CONFIG = SimpleNamespace(projects={'p1': 'Project A', 'p2': 'Project B'},
                         builders={'b1': {'name': 'Builder A'}})
CLIENT = {'name': 'Example Client', 'project_ids': ['p1', 'p2']}


def work(key='w1', first='2026-10-13', last='2026-10-14', units=1_000_001,
         date_basis='work_dates', invoice=None, project='p1'):
    return {'id': key, 'project_id': project, 'builder_id': 'b1',
            'work_start': first, 'work_end': last, 'hour_units': units,
            'date_basis': date_basis, 'description': 'Reviewed work',
            'allocation_note': 'Supported source quantity.', 'invoice_id': invoice,
            'evidence_ids': ['PRIVATE_EVIDENCE_SENTINEL']}


def data(*hours):
    return {'as_of': NOW.isoformat(), 'coverage': {'start': '2026-09-01', 'through': '2026-10-16'},
            'hours': list(hours), 'contractor_invoices': [], 'pending_work': [],
            'qualifications': [], 'limitations': ['Reviewed quantities are not clock-time verification.'],
            'currency': 'AUD', 'basis': 'gross', 'allocation_rate_cents_per_hour': 12500,
            'payments': [], 'invoices': [], 'hours_reporting': True}


def report(dataset, selector=None, now=NOW):
    return build_recorded_report(CONFIG, CLIENT, selector or WEEK, dataset, now)


def test_contained_source_quantity_counts_once_at_exact_microhour_precision():
    result = report(data(work()))
    assert len(result['rows']) == 1
    assert result['rows'][0]['units'] == 1_000_001
    assert result['monthly'][0]['units'] == 1_000_001
    assert result['unit_scale'] == 1_000_000
    assert result['estimated_month_units'] == 0
    assert set(result['projects']) == {'p1', 'p2'}
    assert result['weekly_has_coverage']


@pytest.mark.parametrize('entry', [
    work(first='2026-10-11', last='2026-10-12', units=8_000_000),
    work(first='2026-10-16', last='2026-10-16', units=8_000_000),
    work(first='2026-10-15', last='2026-10-17', units=8_000_000),
    work(date_basis='estimated_work_period'),
    work(first='2026-10-13', last='2026-10-13', date_basis='invoice_date'),
])
def test_boundary_or_estimated_work_is_unresolved_without_proration(entry):
    result = report(data(entry))
    assert not result['rows']
    assert result['monthly'][0]['units'] == 0
    assert result['monthly'][0]['unresolved'] == 1
    assert len(result['exceptions']) == 1
    assert 'not prorated' in result['exceptions'][0]['reason']
    assert result['partial']


def test_source_timestamp_never_establishes_later_full_day_work():
    dataset = data(work(first='2026-10-14', last='2026-10-14'))
    dataset['as_of'] = '2026-10-14T12:00:00+11:00'
    result = report(dataset)
    assert not result['rows']
    assert 'source timestamp' in result['exceptions'][0]['reason']
    assert result['partial'] and result['weekly_has_coverage']


def test_no_coverage_is_explicit_and_carries_source_review_without_finance():
    dataset = data(work(first='2026-10-08', last='2026-10-09'))
    dataset['coverage']['through'] = '2026-10-11'
    dataset['as_of'] = '2026-10-12T10:33:00+11:00'
    dataset['payments'] = [{'secret': 'PRIVATE_PAYMENT_SENTINEL'}]
    dataset['pending_work'] = [{'secret': 'PRIVATE_PENDING_SENTINEL'}]
    result = report(dataset)
    assert not result['weekly_has_coverage'] and not result['period_has_coverage']
    assert result['partial'] and result['pending_work_count'] == 1
    assert result['coverage_through'] == '2026-10-11'
    assert 'not counted as zero' in ' '.join(result['limitations'])
    assert 'PRIVATE_' not in json.dumps(result)


def test_cross_month_contained_work_has_one_unsplit_quantity():
    now = timestamp('2026-10-02T17:00:00+10:00')
    selector = {**WEEK, 'start': '2026-09-28T00:00:00+10:00', 'end': now.isoformat()}
    dataset = data(work(first='2026-09-29', last='2026-10-01'))
    dataset['as_of'] = now.isoformat()
    dataset['coverage']['through'] = '2026-10-02'
    result = report(dataset, selector, now)
    assert [month['units'] for month in result['monthly']] == [0, 1_000_001]
    assert len(result['rows']) == 1
    assert result['rows'][0]['month_allocation'] == 'work_dates'


def test_rolling_week_across_dst_preserves_source_quantity():
    now = timestamp('2026-10-09T17:00:00+11:00')
    selector = {**WEEK, 'start': '2026-10-02T17:00:00+10:00', 'end': now.isoformat()}
    dataset = data(work(first='2026-10-03', last='2026-10-04'))
    dataset['as_of'] = now.isoformat()
    dataset['coverage']['through'] = '2026-10-09'
    result = report(dataset, selector, now)
    assert result['rows'][0]['units'] == 1_000_001


def test_overlay_keeps_weekly_exceptions_and_replaces_legacy_rows():
    reviewed = report(data(work(first='2026-10-16', last='2026-10-16')))
    base = {'selector': WEEK, 'projects': CONFIG.projects, 'unit_scale': 4,
            'rows': [{'project_id': 'p1', 'units': 4}], 'exceptions': [],
            'monthly': [{'month': '2026-10', 'units': 4, 'unresolved': 0, 'allowance_units': 160}]}
    result = overlay_recorded_reports(base, [reviewed])
    assert not result['rows']
    assert result['exceptions'] == reviewed['exceptions']
    assert result['monthly'][0]['unresolved'] == 1
    assert result['monthly'][0]['allowance_units'] is None
    assert result['weekly_has_coverage']


def test_overlay_propagates_no_weekly_coverage():
    dataset = data()
    dataset['coverage']['through'] = '2026-10-11'
    reviewed = report(dataset)
    base = {'selector': WEEK, 'projects': CONFIG.projects, 'unit_scale': 4,
            'rows': [], 'exceptions': [],
            'monthly': [{'month': '2026-10', 'units': 0, 'unresolved': 0, 'allowance_units': 160}]}
    assert not overlay_recorded_reports(base, [reviewed])['weekly_has_coverage']


@pytest.mark.parametrize('hours,code', [
    ([work(), work()], 'duplicate_client_balance_hours'),
    ([work(project='other')], 'client_balance_scope_mismatch'),
])
def test_scope_and_duplicate_guards(hours, code):
    with pytest.raises(TimesheetError, match=code):
        report(data(*hours))


def test_monthly_canonical_projection_retains_existing_uniform_allocation():
    dataset = data(work(first='2026-09-30', last='2026-10-01', units=1_000_001))
    result = report(dataset, {'month': '2026-09', 'months': 2, 'action': 'summary'})
    assert [month['units'] for month in result['monthly']] == [500_001, 500_000]
    assert sum(row['units'] for row in result['rows']) == 1_000_001
    assert result['estimated_month_units'] == 1_000_001


def test_default_freshness_gate_is_unchanged():
    dataset = data()
    validate_snapshot_age(dataset, NOW)
    with pytest.raises(TimesheetError, match='client_balance_source_stale'):
        validate_snapshot_age(dataset, timestamp('2026-10-24T17:00:00+11:00'))


def test_month_to_date_contains_earlier_month_work_once_and_excludes_estimated_or_cutoff_dates():
    dataset = data(work(key='earlier', first='2026-10-03', last='2026-10-07', units=8_000_001),
                   work(key='current', units=1_000_001),
                   work(key='friday', first='2026-10-16', last='2026-10-16', units=4_000_000),
                   work(key='month-boundary', first='2026-09-30', last='2026-10-01', units=5_000_000),
                   work(key='estimated', first='2026-10-04', last='2026-10-05', date_basis='estimated_work_period'))
    month = report(dataset, {'period': 'month_to_date', 'end': NOW.isoformat()})
    assert month['selector']['start'] == '2026-10-01T00:00:00+10:00'
    assert {row['issue_id'] for row in month['rows']} == {'earlier', 'current'}
    assert month['monthly'][0]['units'] == 9_000_002
    assert month['unit_scale'] == 1_000_000 and month['estimated_month_units'] == 0
    assert month['monthly'][0]['unresolved'] == 3
    assert all('month-to-date' in item['reason'] for item in month['exceptions'])


def _finance_weekly_request(setup, monkeypatch, read_at, *, as_of='2026-10-17T12:00:00+11:00'):
    """Real validated snapshot and private issuance; only external I/O is mocked."""
    import roo.studio_report_worker as worker

    class Clock:
        @classmethod
        def now(cls, tz):
            return read_at.astimezone(tz)

    monkeypatch.setattr(worker, 'datetime', Clock)
    project = setup.clients['UMARK']['project_ids'][0]
    builder = next(iter(setup.config.builders))
    setup.dataset.update(hours_reporting=True, as_of=as_of)
    setup.dataset['coverage']['through'] = timestamp(as_of).astimezone(setup.config.tz).date().isoformat()
    first = work(first='2026-10-13', last='2026-10-14', invoice='BILL1', project=project)
    later = work(key='later-saturday', first='2026-10-17', last='2026-10-17',
                 units=9_000_000, project=project)
    for entry in (first, later):
        entry.update(builder_id=builder, evidence_ids=['evidence'])
    later['description'] = 'Saturday work outside frozen window'
    # A stale snapshot can retain only its older, valid historical work. This
    # keeps provenance validation real instead of stubbing the freshness gate.
    if timestamp(as_of) < NOW:
        first.update(work_start='2026-10-05', work_end='2026-10-06')
        setup.dataset['hours'] = [first]
    else:
        setup.dataset['hours'] = [first, later]
    setup.dataset['contractor_invoices'][0]['hour_units'] = first['hour_units']
    setup.path.write_text(json.dumps(setup.value))
    schedules = weekly_configuration({
        'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'mark-friday', 'actor': 'UMARK', 'client': 'UMARK',
             'time': '17:00', 'window': 'workweek', 'starts_on': '2026-10-16'}])}, setup.clients)
    setup.service.schedules = schedules
    assert enqueue_weekly(setup.service, schedules, NOW)[0]['status'] == 'queued'
    path = next(setup.service.weekly_queue.directory.glob('*.json'))
    return setup.service.weekly_queue.read(path.stem)


def test_weekly_catchup_accepts_later_review_and_keeps_friday_cutoff(finance_setup, monkeypatch):
    read_at = timestamp('2026-10-19T12:00:00+11:00')
    request = _finance_weekly_request(finance_setup, monkeypatch, read_at)
    result = finance_setup.service.report_for_request(request)
    assert result['selector']['start'] == WEEK['start']
    assert timestamp(result['selector']['end']) == NOW
    assert result['source_as_of'] == '2026-10-17T12:00:00+11:00'
    assert sum(row['units'] for row in result['rows']) == 1_000_001
    assert 'Saturday work outside frozen window' not in json.dumps(result)
    assert result['finance_owners'] == ['UMARK']
    finance_setup.api.collect.assert_not_called()
    finance_setup.api.post_message.assert_not_called()
    finance_setup.api.upload_csv.assert_not_called()


@pytest.mark.parametrize('as_of', [
    '2026-10-20T12:00:00+11:00',  # Future relative to actual Monday read.
    '2026-10-11T12:00:00+11:00',  # More than seven days old at Monday read.
])
def test_weekly_catchup_rejects_future_and_stale_review_at_actual_read_time(
        finance_setup, monkeypatch, as_of):
    request = _finance_weekly_request(finance_setup, monkeypatch,
                                    timestamp('2026-10-19T12:00:00+11:00'), as_of=as_of)
    with pytest.raises(TimesheetError, match='client_balance_source_stale'):
        finance_setup.service.report_for_request(request)
    finance_setup.api.verify_recipient.assert_not_called()
    finance_setup.api.collect.assert_not_called()
    finance_setup.api.post_message.assert_not_called()


def test_on_demand_hours_freshness_keeps_request_time(finance_setup, monkeypatch):
    request = _finance_weekly_request(finance_setup, monkeypatch,
                                    timestamp('2026-10-19T12:00:00+11:00'))
    ordinary = {'actor': request['actor'], 'team': request['team'],
                'requested_at': NOW.isoformat(),
                'selector': {'month': '2026-10', 'action': 'summary', 'client': 'UMARK'}}
    # Saturday's source is future relative to this Friday on-demand request;
    # only scheduled weekly catch-up uses the worker's actual read clock.
    with pytest.raises(TimesheetError, match='client_balance_source_stale'):
        finance_setup.service.report_for_request(ordinary)
