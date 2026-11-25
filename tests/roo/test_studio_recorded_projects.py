"""Project-specific views of a validated multi-project owner's recorded hours."""
import json
from copy import deepcopy

import pytest

from roo.studio_client_balance_source import validate_balance_snapshot
from roo.studio_recorded_hours import build_recorded_report, overlay_recorded_reports
from roo.studio_reports import build_client_report
from roo.timesheets import TimesheetError
from tests.roo.test_studio_client_balance_source import NOW, P1, P2, setup as finance_setup


@pytest.fixture
def portfolio(finance_setup):
    env = finance_setup
    source = env.dataset
    ids = [P1, P2]
    env.clients['UMARK']['project_ids'] = ids
    source['project_ids'] = ids
    source['hours_reporting'] = True
    first = source['hours'][0]
    first['builder_id'] = next(iter(env.config.builders))
    source['hours'].append({**first, 'id': 'OTHER-SENTINEL', 'invoice_id': None,
                            'project_id': P2, 'work_start': '2026-10-01', 'work_end': '2026-10-01'})
    source['qualifications'] = [{'reference': 'Review', 'reason': 'Included note', 'project_id': P1},
                                {'reference': 'Review', 'reason': 'SENTINEL note', 'project_id': P2}]
    source['pending_work'] = [{'id': 'pending', 'supplier': 'Supplier', 'invoice_number': 'SENTINEL',
                               'note': 'Unallocated work', 'evidence_date': '2026-06-01',
                               'evidence_ids': ['evidence']}]
    source['limitations'] = ['SENTINEL portfolio comment']
    source['source_freshness'] = {'hours_complete_through': NOW.isoformat(), 'hours_reviewed_at': NOW.isoformat(),
                                  'finance_checked_at': NOW.isoformat(), 'missing': [],
                                  'unreviewed_invoice_count': 0, 'latest_work_date': '2026-10-01'}
    validate_balance_snapshot(env.value, env.config, env.clients)
    return env


def test_filtered_rows_keep_precise_recorded_units_and_only_scoped_qualifications(portfolio):
    source = deepcopy(portfolio.dataset)
    report = build_recorded_report(portfolio.config, portfolio.clients['UMARK'], {'month': 'all'},
                                   portfolio.dataset, NOW, project_ids=[P1])
    row, = report['rows']
    assert row['units'] == 123_010_000 and row['source'] == 'reviewed_invoice'
    assert set(report['projects']) == {P1} and report['coverage_through'] == '2026-06-03'
    assert report['qualifications'][0]['reason'] == 'Included note'
    assert 'awaiting allocation' in report['qualifications'][1]['reason']
    assert report['pending_work_count'] == 0
    assert 'SENTINEL' not in json.dumps(report)
    assert portfolio.dataset == source


def test_later_sibling_work_cannot_establish_this_projects_coverage(portfolio):
    report = build_recorded_report(portfolio.config, portfolio.clients['UMARK'], {'month': '2026-10'},
                                   portfolio.dataset, NOW, project_ids={P1})
    assert not report['rows']
    assert not report['period_has_coverage'] and not report['monthly'][0]['coverage_available']
    assert report['partial']


def test_filtered_project_with_no_submissions_has_no_covered_months(portfolio):
    source = portfolio.dataset
    source['hours'] = [row for row in source['hours'] if row['project_id'] != P2]
    source['source_freshness']['latest_work_date'] = '2026-06-03'
    validate_balance_snapshot(portfolio.value, portfolio.config, portfolio.clients)
    report = build_recorded_report(portfolio.config, portfolio.clients['UMARK'], {'month': 'all'},
                                   source, NOW, project_ids={P2})
    assert not report['rows'] and not report['period_has_coverage']
    assert all(not row['coverage_available'] for row in report['monthly'])


def test_distinct_review_windows_keep_later_rows_and_flag_variable_coverage(portfolio):
    client = portfolio.clients['UMARK']
    selector = {'month': '2026-06', 'months': 5}
    reviewed = [build_recorded_report(portfolio.config, client, selector, portfolio.dataset,
                                      NOW, project_ids=[key]) for key in (P1, P2)]
    base = build_client_report(portfolio.config, client, selector, [], NOW)
    report = overlay_recorded_reports(base, reviewed)
    assert report['coverage_start'] == '2026-06-01' and report['reviewed_coverage_varies']
    assert [row['month'] for row in report['rows']] == ['2026-06', '2026-10']
    assert report['monthly'][-1]['units'] == portfolio.dataset['hours'][1]['hour_units']


@pytest.mark.parametrize('ids', [[], ['unknown'], [P1, 'unknown']])
def test_invalid_projection_fails_closed(portfolio, ids):
    with pytest.raises(TimesheetError, match='client_balance_scope_mismatch'):
        build_recorded_report(portfolio.config, portfolio.clients['UMARK'], {'month': 'all'},
                              portfolio.dataset, NOW, project_ids=ids)
