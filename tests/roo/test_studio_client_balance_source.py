"""Finance source conservation, access and transport using synthetic records only."""
from copy import deepcopy
import base64
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from roo.studio_client_balance_source import (load_balance_snapshot, select_balance_dataset,
                                             validate_balance_snapshot)
from roo.studio_report_worker import StudioReportService, configuration
from roo.timesheets import TimesheetError, timestamp

P1 = '00000000-0000-0000-0000-000000000001'
P2 = '00000000-0000-0000-0000-000000000002'
NOW = timestamp('2026-10-11T00:00:00Z')


@pytest.fixture
def setup(tmp_path):
    env = {'STUDIO_REPORTS_SLACK_TEAM_ID': 'T123',
        'TIMESHEET_LINEAR_ORGANIZATION_ID': '00000000-0000-0000-0000-000000000020',
        'TIMESHEET_PROJECTS_JSON': json.dumps({P1: 'Project A', P2: 'Project B'}),
        'TIMESHEET_BUILDERS_JSON': json.dumps([{'builder_id': '00000000-0000-0000-0000-000000000011',
                                              'slack_user_id': 'UBUILDER', 'name': 'Builder'}]),
        'STUDIO_REPORTS_CLIENTS_JSON': json.dumps({
            'UMARK': {'name': 'Mark', 'project_ids': [P1]},
            'USAM': {'name': 'Sam', 'project_ids': [P1, P2], 'report_client_ids': ['UMARK']},
            'UOTHER': {'name': 'Other', 'project_ids': [P2]}}),
        'STUDIO_REPORTS_DATA_DIR': str(tmp_path / 'data'),
        'STUDIO_REPORTS_QUEUE_DIR': str(tmp_path / 'queue')}
    config, clients = configuration(env)
    dataset = {'project_ids': [P1], 'currency': 'AUD', 'basis': 'gross',
        'allocation_rate_cents_per_hour': 12500,
        'coverage': {'start': '2026-06-01', 'through': '2026-10-11'}, 'as_of': NOW.isoformat(),
        'limitations': ['Recorded contractor hours only.'],
        'invoices': [{'id': 'INV1', 'number': 'INV1', 'date': '2026-06-01', 'amount_cents': 10000,
                      'paid_cents': 10000, 'status': 'paid', 'evidence_ids': ['evidence']}],
        'payments': [{'id': 'PAY1', 'bank_transaction_id': 'BANK1', 'date': '2026-07-01',
            'amount_cents': 10000, 'invoice_ids': ['INV1'], 'description': 'Invoice payment',
            'allocations': [{'invoice_id': 'INV1', 'amount_cents': 10000}], 'evidence_ids': ['evidence']}],
        'contractor_invoices': [{'id': 'BILL1', 'number': 'BILL1', 'supplier': 'Builder', 'date': '2026-06-03',
            'hour_units': 123010000, 'excluded_hour_units': 0, 'unresolved_hour_units': 0,
            'allocation_note': 'Invoice counted once.', 'evidence_ids': ['evidence']}],
        'hours': [{'id': 'COST1', 'invoice_id': 'BILL1', 'project_id': P1, 'hour_units': 123010000,
            'work_start': '2026-06-01', 'work_end': '2026-06-03', 'date_basis': 'work_dates',
            'allocation_note': 'Reviewed invoice dates.', 'description': 'Work', 'evidence_ids': ['evidence']}]}
    value = {'version': 1, 'team': config.team, 'organization': config.organization, 'currency': 'AUD',
        'basis': 'gross', 'evidence': {'evidence': {'reference': 'Reviewed source', 'sha256': 'a' * 64}},
        'clients': {'UMARK': dataset}}
    path = tmp_path / 'finance.json'
    path.write_text(json.dumps(value))
    api = Mock()
    api.open_dm.return_value = 'DMARK'
    api.post_message.return_value = '1.2'
    api.upload_csv.return_value = 'F123'
    service = StudioReportService(config, api, clients, balance_path=path, balance_enabled=True)
    return SimpleNamespace(config=config, clients=clients, value=value, path=path, api=api, service=service,
                           dataset=dataset)


def request(actor='UMARK', **selector):
    return {'actor': actor, 'team': 'T123', 'report_kind': 'client_balance',
            'selector': {'month': 'all', 'action': 'summary', **selector}, 'requested_at': NOW.isoformat()}


def test_valid_hours_and_paid_money_conserved_and_authorised_staff_can_select_one_client(setup):
    snapshot = validate_balance_snapshot(setup.value, setup.config, setup.clients)
    client, data, owner = select_balance_dataset(snapshot, setup.config, setup.clients, 'USAM',
                                                 {'client': 'Mark'}, NOW)
    assert owner == 'UMARK' and client['project_ids'] == [P1]
    assert data['payments'][0]['amount_cents'] == 10000


@pytest.mark.parametrize('mutation', [
    lambda d: d['payments'].append({**d['payments'][0], 'id': 'SECOND'}),
    lambda d: d['payments'][0].update(amount_cents=20000),
    lambda d: d['payments'][0]['allocations'][0].update(amount_cents=10001),
    lambda d: d['payments'][0].update(invoice_ids=['UNKNOWN']),
    lambda d: d['invoices'][0].update(status='voided'),
    lambda d: d['invoices'][0].update(amount_cents=True),
    lambda d: d['invoices'].append({**d['invoices'][0], 'id': 'SECOND'}),
    lambda d: d['hours'][0].update(project_id=P2),
    lambda d: d['hours'][0].update(hour_units=123000000),
    lambda d: d['hours'].append({**d['hours'][0], 'id': 'SECOND'}),
    lambda d: d['hours'][0].update(date_basis='bank_date'),
    lambda d: d['hours'][0].update(evidence_ids=['UNKNOWN']),
    lambda d: d.update(project_ids=[P1, P2]),
    lambda d: d.update(currency='USD'),
    lambda d: d.update(basis='net'),
    lambda d: d.update(allocation_rate_cents_per_hour=True),
    lambda d: d.update(allocation_rate_cents_per_hour=3),
    lambda d: d['hours'][0].update(hour_units=True),
    lambda d: d['contractor_invoices'][0].update(unresolved_hour_units=1),
    lambda d: d['hours'][0].update(work_end='2026-10-12'),
    lambda d: d['payments'][0].update(date='20260601'),
])
def test_invalid_or_duplicate_source_rejects_whole_snapshot(setup, mutation):
    value = deepcopy(setup.value)
    mutation(value['clients']['UMARK'])
    with pytest.raises(TimesheetError, match='invalid_client_balance_snapshot'):
        validate_balance_snapshot(value, setup.config, setup.clients)


def test_stale_future_missing_and_changed_scope_fail_closed(setup):
    with pytest.raises(TimesheetError, match='source_stale'):
        select_balance_dataset(setup.value, setup.config, setup.clients, 'UMARK', {}, NOW + timedelta(days=8))
    with pytest.raises(TimesheetError, match='source_stale'):
        select_balance_dataset(setup.value, setup.config, setup.clients, 'UMARK', {}, NOW - timedelta(hours=1))
    with pytest.raises(TimesheetError, match='not_configured'):
        load_balance_snapshot(None, setup.config, setup.clients)
    with pytest.raises(TimesheetError, match='source_unavailable'):
        load_balance_snapshot(setup.path.parent / 'missing', setup.config, setup.clients)
    setup.clients['UMARK']['project_ids'].append(P2)
    with pytest.raises(TimesheetError, match='invalid_client_balance_snapshot'):
        load_balance_snapshot(setup.path, setup.config, setup.clients)


def test_reviewed_finance_scope_does_not_create_new_grants(setup):
    for actor, selector, error in [('UOTHER', {'client': 'Mark'}, 'report_client_unavailable'),
                                   ('USAM', {}, 'client_balance_access_unavailable'),
                                   ('UMARK', {'client': 'all'}, 'single_client_required')]:
        with pytest.raises(TimesheetError, match=error):
            select_balance_dataset(setup.value, setup.config, setup.clients, actor, selector, NOW)


def test_private_worker_renders_without_linear_or_accounting_calls(setup):
    report = setup.service.report_for_request(request('USAM', client='Mark', action='detailed'))
    assert report['kind'] == 'client_balance' and report['actor'] == 'USAM'
    assert report['balance_hour_units'] == 122210000
    assert report['finance_owner'] == 'UMARK'
    assert 'studio-client-balance.png' in [p.get('filename') for p in report['parts']]
    setup.api.collect.assert_not_called()
    setup.api.verify.assert_not_called()
    setup.api.post_message.assert_not_called()
    setup.api.upload_csv.assert_not_called()


def test_worker_unknown_identity_or_disabled_feature_never_reads_or_sends(setup, monkeypatch):
    import roo.studio_client_balance_source as source
    read = Mock(side_effect=AssertionError('Unauthorised read'))
    monkeypatch.setattr(source, 'load_balance_snapshot', read)
    with pytest.raises(TimesheetError, match='client_access_not_configured'):
        setup.service.report_for_request(request('UWRONGMARK'))
    setup.service.balance_enabled = False
    with pytest.raises(TimesheetError, match='not_configured'):
        setup.service.report_for_request(request())
    read.assert_not_called()
    setup.api.verify_recipient.assert_not_called()


def test_snapshot_change_revocation_and_staleness_block_delivery(setup):
    req = request()
    report = setup.service.report_for_request(req)
    setup.value['clients']['UMARK']['limitations'].append('New finance review')
    setup.path.write_text(json.dumps(setup.value))
    with pytest.raises(TimesheetError, match='source_changed'):
        setup.service.deliver_request(report, req, 'KEY', NOW)
    setup.api.open_dm.assert_not_called()
    setup.value['clients']['UMARK']['limitations'].pop()
    setup.path.write_text(json.dumps(setup.value))
    with pytest.raises(TimesheetError, match='source_stale'):
        setup.service.deliver_request(report, req, 'KEY', NOW + timedelta(days=8))
    setup.clients['UMARK']['project_ids'] = [P2]
    with pytest.raises(TimesheetError, match='client_access_changed'):
        setup.service.deliver_request(report, req, 'KEY', NOW)
    setup.api.open_dm.assert_not_called()


def test_unknown_kind_rejected_and_hours_worker_still_uses_existing_path(setup):
    with pytest.raises(TimesheetError, match='invalid_report_kind'):
        setup.service.report_for_request({**request(), 'report_kind': 'xero_execute'})
    setup.api.collect.assert_not_called()


def enable_recorded_hours(setup):
    setup.dataset['hours_reporting'] = True
    setup.dataset['hours'][0]['builder_id'] = next(iter(setup.config.builders))
    setup.path.write_text(json.dumps(setup.value))


def test_project_monthly_and_paid_credit_views_use_identical_work_source(setup):
    enable_recorded_hours(setup)
    from roo.studio_reports import total_units, summary
    balance = setup.service.report_for_request(request())
    hours = setup.service.report_for_request({**request(), 'report_kind': 'hours'})
    assert total_units(hours) == balance['used_hour_units'] == 123010000
    assert sum(month['units'] for month in hours['monthly']) == total_units(hours)
    assert hours['unit_scale'] == 1000000 and hours['finance_owners'] == ['UMARK']
    assert all(row['source'] == 'reviewed_invoice' for row in hours['rows'])
    assert all(month['allowance_units'] is None for month in hours['monthly'])
    assert 'not verified clock time' in summary(hours)
    assert 'Provisional recorded-units' in balance['parts'][0]['content']
    setup.api.collect.assert_not_called()
    setup.api.verify.assert_not_called()
    setup.api.post_message.assert_not_called()
    setup.api.upload_csv.assert_not_called()


def test_shared_source_preserves_cross_month_microhours_and_staff_all_client_totals(setup):
    enable_recorded_hours(setup)
    setup.dataset['hours'][0].update(hour_units=333333, work_start='2026-09-30', work_end='2026-10-01')
    setup.dataset['contractor_invoices'][0]['hour_units'] = 333333
    setup.path.write_text(json.dumps(setup.value))
    setup.api.collect.return_value = []
    from roo.studio_reports import total_units
    balance = setup.service.report_for_request(request('USAM', client='Mark'))
    single = setup.service.report_for_request({**request(), 'report_kind': 'hours'})
    all_clients = setup.service.report_for_request({**request('USAM', client='all'), 'report_kind': 'hours',
                                                  'selector': {'client':'all','month':'recent','months':3}})
    assert total_units(single) == total_units(all_clients) == balance['used_hour_units'] == 333333
    assert [row['units'] for row in single['rows']] == [166667, 166666]
    assert single['estimated_month_units'] == 333333
    assert 'client_groups' in all_clients
    # The legacy collector receives only the other accessible project.
    assert set(setup.api.collect.call_args.args[0].projects) == {P2}


def test_updated_or_stale_canonical_work_blocks_old_hours_delivery(setup):
    enable_recorded_hours(setup)
    req = {**request(), 'report_kind': 'hours'}
    report = setup.service.report_for_request(req)
    setup.dataset['limitations'].append('New review')
    setup.path.write_text(json.dumps(setup.value))
    with pytest.raises(TimesheetError, match='source_changed'):
        setup.service.deliver_request(report, req, 'OLD', NOW)
    setup.dataset['limitations'].pop()
    setup.path.write_text(json.dumps(setup.value))
    with pytest.raises(TimesheetError, match='source_stale'):
        setup.service.deliver_request(report, req, 'OLD', NOW + timedelta(days=8))
    setup.api.open_dm.assert_not_called()


def test_canonical_hours_require_verified_builder_and_keep_existing_access_boundaries(setup):
    setup.dataset['hours_reporting'] = True
    with pytest.raises(TimesheetError, match='invalid_client_balance_snapshot'):
        validate_balance_snapshot(setup.value, setup.config, setup.clients)
    enable_recorded_hours(setup)
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        setup.service.report_for_request({**request('UOTHER', client='Mark'), 'report_kind':'hours'})
    setup.api.verify_recipient.assert_not_called()


def test_partial_bill_allocation_must_explicitly_account_for_excluded_work(setup):
    value = deepcopy(setup.value)
    bill = value['clients']['UMARK']['contractor_invoices'][0]
    bill.update(hour_units=126260000, excluded_hour_units=3250000, allocation_note='Separate project work excluded.')
    assert validate_balance_snapshot(value, setup.config, setup.clients) is value


def test_basic_date_cannot_bypass_invoice_receipt_chronology(setup):
    value = deepcopy(setup.value)
    data = value['clients']['UMARK']
    data['invoices'][0]['date'] = '2026-07-02'
    data['payments'][0]['date'] = '20260701'
    with pytest.raises(TimesheetError, match='invalid_client_balance_snapshot'):
        validate_balance_snapshot(value, setup.config, setup.clients)


def test_separately_recorded_hours_need_no_contractor_pay_rate(setup):
    value = deepcopy(setup.value)
    data = value['clients']['UMARK']
    data['hours'].append({'id': 'RECORDED', 'project_id': P1, 'hour_units': 250000,
        'work_start': '2026-06-04', 'work_end': '2026-06-04', 'date_basis': 'work_dates',
        'allocation_note': 'Separately reviewed attendance, not included in BILL1.',
        'description': 'Meeting', 'evidence_ids': ['evidence']})
    assert validate_balance_snapshot(value, setup.config, setup.clients) is value


def test_pending_work_is_unknown_hours_not_zero_or_money_inferred(setup):
    value = deepcopy(setup.value)
    pending = {'id': 'PENDING', 'supplier': 'Builder', 'invoice_number': 'BILL2',
        'evidence_date': '2026-10-02', 'note': 'Original invoice and hours require review.',
        'evidence_ids': ['evidence']}
    value['clients']['UMARK']['pending_work'] = [pending]
    assert validate_balance_snapshot(value, setup.config, setup.clients) is value
    pending['hour_units'] = 0
    with pytest.raises(TimesheetError, match='invalid_client_balance_snapshot'):
        validate_balance_snapshot(value, setup.config, setup.clients)


def test_explicit_historical_fallback_delivers_all_three_charts_with_one_cutoff(setup, monkeypatch):
    from matplotlib.figure import Figure
    from roo.studio_reports import total_units
    enable_recorded_hours(setup)
    original_source = setup.path.read_bytes()
    setup.service.allow_historical_snapshot = True
    now = NOW + timedelta(days=10)
    req = {**request('USAM', client='Mark', month='2026-06', months=5), 'requested_at': now.isoformat()}
    labels = []
    original_text = Figure.text
    def capture_text(figure, x, y, text, *args, **kwargs):
        labels.append(text)
        return original_text(figure, x, y, text, *args, **kwargs)
    monkeypatch.setattr(Figure, 'text', capture_text)
    report = setup.service.report_for_request(req)
    assert report['source_stale'] and report['hours_report']['source_stale']
    assert report['coverage_through'] == '2026-10-11'
    assert report['as_of'] == NOW.isoformat()
    assert report['used_hour_units'] == total_units(report['hours_report']) == 123010000
    charts = [p for p in report['parts'] if p.get('filename', '').endswith('.png')]
    assert {p['filename'] for p in charts} == {
        'studio-client-balance.png', 'studio-hours.png', 'studio-hours-projects.png'}
    assert all(base64.b64decode(p['content']).startswith(b'\x89PNG\r\n\x1a\n') for p in charts)
    assert sum('Historical snapshot · reviewed through 2026-10-11' in label for label in labels) == 3
    text = '\n'.join(p['content'] for p in report['parts'] if p['kind'] == 'message')
    assert text.index('Historical snapshot') < text.index('Invoice/recorded hour-units:')
    assert 'Later activity is unavailable' in text and 'through 2026-10-11' in text
    assert 'month to date' not in text
    with setup.service.requests.locked('frozen') as acquired:
        assert acquired
        setup.service.requests.write('frozen', report)
    assert setup.service.deliver_request(setup.service.requests.read('frozen'), req, 'HISTORICAL', now) == 'sent'
    assert setup.api.open_dm.call_args.args == ('USAM',)
    assert setup.api.upload_csv.call_count == 3
    assert setup.service.deliver_request(setup.service.requests.read('frozen'), req, 'HISTORICAL', now) == 'sent'
    assert setup.api.upload_csv.call_count == 3  # No duplicate uploads on retry.
    assert setup.path.read_bytes() == original_source
    setup.api.collect.assert_not_called()
    setup.api.verify.assert_not_called()


@pytest.mark.parametrize('kind', ['hours', 'client_balance'])
def test_historical_opt_in_cannot_send_an_unlabelled_current_snapshot_after_expiry(setup, kind):
    enable_recorded_hours(setup)
    req = {**request(), 'report_kind': kind}
    report = setup.service.report_for_request(req)
    assert report['source_stale'] is False
    setup.service.allow_historical_snapshot = True
    with pytest.raises(TimesheetError, match='source_stale'):
        setup.service.deliver_request(report, req, 'EXPIRED', NOW + timedelta(days=10))
    setup.api.open_dm.assert_not_called()


def test_historical_fallback_rechecks_scope_source_and_operator_flag(setup):
    setup.service.allow_historical_snapshot = True
    now = NOW + timedelta(days=10)
    req = {**request(), 'requested_at': now.isoformat()}
    report = setup.service.report_for_request(req)
    setup.service.allow_historical_snapshot = False
    with pytest.raises(TimesheetError, match='source_stale'):
        setup.service.deliver_request(report, req, 'REVOKED', now)
    setup.service.allow_historical_snapshot = True
    setup.dataset['limitations'].append('New review')
    setup.path.write_text(json.dumps(setup.value))
    with pytest.raises(TimesheetError, match='source_changed'):
        setup.service.deliver_request(report, req, 'CHANGED', now)
    setup.clients['UMARK']['project_ids'] = [P2]
    with pytest.raises(TimesheetError, match='client_access_changed'):
        setup.service.deliver_request(report, req, 'SCOPE', now)
    setup.api.open_dm.assert_not_called()


def test_future_review_still_blocks_historical_fallback(setup):
    setup.service.allow_historical_snapshot = True
    with pytest.raises(TimesheetError, match='source_stale'):
        setup.service.report_for_request({**request(), 'requested_at': (NOW - timedelta(hours=1)).isoformat()})
    setup.api.open_dm.assert_not_called()
    setup.api.verify_recipient.assert_not_called()


def test_historical_months_outside_coverage_show_unavailable_instead_of_zero(setup):
    from roo.studio_reports import summary
    enable_recorded_hours(setup)
    setup.service.allow_historical_snapshot = True
    now = NOW + timedelta(days=25)
    req = {**request(month='2026-10', months=2), 'requested_at': now.isoformat()}
    report = setup.service.report_for_request(req)
    hours = report['hours_report']
    assert [m['coverage_available'] for m in hours['monthly']] == [True, False]
    text = summary(hours)
    assert 'November 2026 — hours unavailable outside reviewed coverage' in text
    assert 'November 2026 — 0' not in text
    req['selector'].update(month='2026-11', months=1)
    uncovered = setup.service.report_for_request(req)
    assert uncovered['period_has_coverage'] is False
    assert 'Hours are unavailable for the requested period' in summary(uncovered['hours_report'])


def test_failed_companion_chart_cannot_send_a_partial_report_bundle(setup, monkeypatch):
    enable_recorded_hours(setup)
    setup.service.allow_historical_snapshot = True
    monkeypatch.setattr('roo.studio_report_worker.render_chart', Mock(side_effect=RuntimeError('render failed')))
    with pytest.raises(RuntimeError, match='render failed'):
        setup.service.report_for_request({**request(), 'requested_at': (NOW + timedelta(days=10)).isoformat()})
    setup.api.upload_csv.assert_not_called()
    setup.api.post_message.assert_not_called()
