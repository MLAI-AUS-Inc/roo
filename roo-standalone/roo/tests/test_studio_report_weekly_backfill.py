"""Weekly source quantities are never prorated or assigned guessed work dates."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from roo.studio_report_backfill import validate_backfill
from roo.studio_report_weekly_backfill import apply_weekly_backfill
from roo.timesheets import TimesheetError, timestamp

P1 = '00000000-0000-0000-0000-000000000001'
P2 = '00000000-0000-0000-0000-000000000002'
B1 = '00000000-0000-0000-0000-000000000011'
NOW = timestamp('2026-10-02T06:00:00Z')  # Friday 4 pm AEST.


@pytest.fixture
def config():
    return SimpleNamespace(team='T123', organization='00000000-0000-0000-0000-000000000020',
                           projects={P1: 'Client project', P2: 'Other private project'},
                           builders={B1: {'name': 'Alice'}})


def report(*, start='2026-09-28T00:00:00+10:00', end='2026-10-02T16:00:00+10:00', projects=None):
    return {'selector': {'period': 'weekly', 'start': start, 'end': end, 'month': '2026-09',
                         'months': 2, 'action': 'summary'},
            'projects': projects or {P1: 'Client project'}, 'rows': [], 'exceptions': [],
            'monthly': [{'month': '2026-09', 'units': 0, 'unresolved': 0, 'allowance_units': None},
                        {'month': '2026-10', 'units': 0, 'unresolved': 0, 'allowance_units': None}],
            'complete': True, 'basis': 'completed_ticket_size_hours', 'unit_scale': 100}


def manifest(config, *, start='2026-09-28', end='2026-10-01', hours='12.83', kind='invoice', **updates):
    source = {'kind': kind, 'message_id': 'private-mail-id', 'sha256': 'a' * 64,
              'builder_id': B1, 'hours': hours,
              **({'invoice': 'INV-001'} if kind == 'invoice' else {'reference': 'TIME-001'})}
    entry = {'id': 'source:line', 'line_id': 'line', 'source_id': 'source', 'project_id': P1,
             'builder_id': B1, 'start': start, 'end': end, 'hours': hours,
             'description': 'Reviewed work', 'review_note': 'Private provenance', 'replaces': [],
             **({'date_status': 'unallocated', 'date_note': 'No confirmed source work dates.'} if start is None else {})}
    entry.update(updates)
    return validate_backfill({'version': 2, 'team': config.team, 'organization': config.organization,
                              'coverage': {'start': '2026-09-01', 'through': '2026-10-02'},
                              'invoice_first_projects': [P1], 'sources': {'source': source},
                              'entries': [entry], 'pending': []}, config)


@pytest.mark.parametrize('kind,source', [('invoice', 'reviewed_invoice'), ('recorded_time', 'reviewed_recorded_time')])
def test_full_dated_window_preserves_exact_decimal_and_actual_month(config, kind, source):
    result = apply_weekly_backfill(report(), manifest(config, kind=kind), config, NOW)
    assert result['complete'] and result['basis'] == 'invoice_first_recorded_hours'
    assert [month['units'] for month in result['monthly']] == [0, 1283]
    row, = result['rows']
    assert row['units'] == 1283 and row['month'] == '2026-10' and row['source'] == source
    assert (row['work_start'], row['work_end']) == ('2026-09-28', '2026-10-01')
    assert result['unallocated_units'] == result['estimated_month_units'] == 0
    assert 'private-mail-id' not in json.dumps(result) and 'Private provenance' not in json.dumps(result)


@pytest.mark.parametrize('start,end', [
    ('2026-09-27', '2026-09-29'),  # Extends before the weekly start.
    ('2026-09-28', '2026-10-02'),  # Friday has no clock time, so it may extend past 4 pm.
    ('2026-10-02', '2026-10-02'),
])
def test_overlapping_work_with_no_supported_weekly_split_is_excluded(config, start, end):
    result = apply_weekly_backfill(report(), manifest(config, start=start, end=end), config, NOW)
    assert not result['complete'] and not result['rows']
    assert not sum(month['units'] for month in result['monthly'])
    assert result['exceptions'] and all('not prorated' in item['reason'] for item in result['exceptions'])


@pytest.mark.parametrize('start,end', [('2026-09-20', '2026-09-27'), ('2026-10-02', '2026-10-02')])
def test_outside_and_exact_boundary_windows_are_ignored_without_false_partial(config, start, end):
    current = report(end='2026-10-02T00:00:00+10:00')
    result = apply_weekly_backfill(current, manifest(config, start=start, end=end), config, NOW)
    assert result['complete'] and not result['rows'] and not result['exceptions']


def test_contained_work_dates_override_estimated_month_allocation(config):
    backfill = manifest(config, allocation_month='2026-09', allocation_note='Estimated monthly split.')
    result = apply_weekly_backfill(report(), backfill, config, NOW)
    row, = result['rows']
    assert row['month'] == '2026-10' and row['month_allocation'] == 'work_dates'
    assert not row['allocation_note'] and result['complete'] and not result['estimated_month_units']
    assert backfill['entries'][0]['allocation_month'] == '2026-09'


@pytest.mark.parametrize('allocation', [None, '2026-09'])
def test_undated_work_is_partial_even_with_estimated_month_allocation(config, allocation):
    changes = {'allocation_month': allocation, 'allocation_note': 'Estimated month.'} if allocation else {}
    result = apply_weekly_backfill(report(), manifest(config, start=None, end=None, **changes), config, NOW)
    assert not result['complete'] and not result['rows']
    assert [month['unresolved'] for month in result['monthly']] == [1, 1]
    assert all('monthly allocation cannot establish weekly hours' in item['reason'] for item in result['exceptions'])


def test_scoped_rows_pending_and_qualifications_never_expose_other_client(config):
    backfill = manifest(config)
    backfill['entries'][0]['project_id'] = P2
    backfill['entries'][0]['description'] = 'Other private work'
    backfill['pending'] = [{'project_id': P2, 'months': ['2026-09'],
                            'reference': 'Secret reference', 'reason': 'Secret reason'}]
    backfill['invoice_first_projects'] = [P2]
    backfill['qualifications'] = [{'project_id': P2, 'reference': 'Secret reference', 'reason': 'Secret condition'}]
    result = apply_weekly_backfill(report(), validate_backfill(backfill, config), config, NOW)
    assert result['complete'] and not result['rows'] and not result['exceptions'] and not result['qualifications']
    assert 'Secret' not in json.dumps(result) and 'Other private' not in json.dumps(result)


def test_pending_months_conservatively_mark_only_period_months_partial(config):
    backfill = manifest(config, start='2026-09-20', end='2026-09-21')
    backfill['pending'] = [{'project_id': P1, 'months': ['2026-08', '2026-09'],
                            'reference': 'INV-002', 'reason': 'Supported work dates needed.'}]
    result = apply_weekly_backfill(report(), backfill, config, NOW)
    assert not result['complete'] and not result['rows']
    assert [month['unresolved'] for month in result['monthly']] == [1, 0]
    assert result['exceptions'] == [{'month': '2026-09', 'project': 'Client project',
                                     'identifier': 'INV-002', 'reason': 'Supported work dates needed.'}]


def test_invoice_first_policy_retains_qualifications_and_mixed_source_basis(config):
    backfill = manifest(config)
    backfill['qualifications'] = [{'project_id': P1, 'reference': 'INV-001',
                                   'reason': 'Includes approved billing adjustments.'}]
    current = report()
    current['rows'] = [{'month': '2026-10', 'project_id': P1, 'project': 'Client project',
                        'completed_at': '2026-10-02T11:00:00+10:00', 'identifier': 'STU-1', 'units': 100}]
    current['monthly'][1]['units'] = 100
    result = apply_weekly_backfill(current, backfill, config, NOW)
    assert result['invoice_first_projects'] == [P1]
    assert result['basis'] == 'reviewed_invoices_and_completed_ticket_sizes'
    assert not result['complete'] and result['qualifications']
    assert result['monthly'][1]['units'] == 1383


@pytest.mark.parametrize('kind', ['dated', 'undated', 'allocated'])
def test_future_work_evidence_fails_closed(config, kind):
    if kind == 'dated':
        backfill = manifest(config, start='2026-10-03', end='2026-10-03')
    else:
        changes = {'allocation_month': '2026-10', 'allocation_note': 'Estimated month.'} if kind == 'allocated' else {}
        backfill = manifest(config, start=None, end=None, **changes)
        backfill['coverage']['through'] = '2026-10-03'
    with pytest.raises(TimesheetError, match='future_invoice_work'):
        apply_weekly_backfill(report(), backfill, config, NOW)


def test_date_window_uses_melbourne_dst_midnights(config):
    backfill = manifest(config, start='2026-10-05', end='2026-10-05')
    current = report(start='2026-10-05T00:00:00+11:00', end='2026-10-06T00:00:00+11:00')
    result = apply_weekly_backfill(current, backfill, config, timestamp('2026-10-06T00:00:00+11:00'))
    assert result['complete'] and result['monthly'][1]['units'] == 1283


def test_version_one_uses_exact_quarter_hour_units(config):
    backfill = manifest(config, start='2026-09-28', end='2026-09-29', hours='2.5')
    backfill['version'] = 1
    backfill.pop('coverage')
    backfill.pop('invoice_first_projects')
    backfill['sources']['source'].pop('kind')
    backfill = validate_backfill(backfill, config)
    current = report()
    current['unit_scale'] = 4
    result = apply_weekly_backfill(current, backfill, config, NOW)
    assert result['complete'] and result['rows'][0]['units'] == 10
    assert result['monthly'][0]['units'] == 10


def test_no_backfill_does_not_change_weekly_ticket_report():
    current = report()
    before = deepcopy(current)
    assert apply_weekly_backfill(current, None, None, NOW) is current
    assert current == before
