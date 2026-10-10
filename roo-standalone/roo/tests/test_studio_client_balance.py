"""Synthetic work-hour and receipt evidence; no accounting or delivery writes."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import csv
from io import StringIO
from unittest.mock import patch

import pytest

from roo.studio_client_balance import (HOUR_UNITS, artifacts, build_balance_report, credit_hour_units,
                                      display_hours, money, render_chart, summary)
from roo.timesheets import TimesheetError, timestamp

NOW = timestamp('2026-10-11T01:00:00Z')
CLIENT = {'name': 'Mark Ghiasy', 'project_ids': ['project-a', 'project-b']}


def work(identifier='work-1', units=10_000_001, start='2026-06-01', end='2026-06-03', **extra):
    return {'id': identifier, 'invoice_id': 'contractor-1', 'project_id': 'project-a',
            'hour_units': units, 'work_start': start, 'work_end': end, 'date_basis': 'work_dates',
            'allocation_note': '', 'description': 'Reviewed work', **extra}


def payment(identifier='receipt-1', day='2026-06-02', amount=62500, **extra):
    return {'id': identifier, 'date': day, 'amount_cents': amount,
            'invoice_ids': ['client-invoice-1'], 'description': 'Actual payment', **extra}


def dataset(*, hours=None, payments=None, invoices=None, **extra):
    return {'coverage': {'start': '2026-06-01', 'through': '2026-10-11'},
            'as_of': '2026-10-11T01:00:00Z', 'currency': 'AUD', 'basis': 'reviewed_hours',
            'allocation_rate_cents_per_hour': 12500,
            'hours': [work()] if hours is None else hours,
            'payments': [payment()] if payments is None else payments,
            'invoices': [{'id': 'client-invoice-1', 'number': 'INV-101', 'date': '2026-06-01',
                          'amount_cents': 62500, 'status': 'paid', 'paid_cents': 62500}]
                        if invoices is None else invoices,
            'contractor_invoices': [], 'limitations': [], **extra}


def build(source=None, **selector):
    return build_balance_report(CLIENT, {'month': 'all', **selector}, source or dataset(), NOW)


def test_uniform_hour_allocation_preserves_every_microhour_and_labels_timing_estimation():
    report = build(dataset(payments=[]))
    assert [event['hour_units'] for event in report['events']] == [3333334, 3333334, 3333333]
    assert report['used_hour_units'] == report['balance_hour_units'] == 10000001
    assert report['estimated_hour_units'] == 10000001
    assert report['hours'][0]['estimated_daily_allocation'] is True
    assert 'Uniform daily allocation' in report['hours'][0]['daily_allocation']
    assert 'estimated daily timing' in summary(report)


def test_single_recorded_date_exact_but_invoice_date_basis_estimated():
    report = build(dataset(hours=[work(start='2026-06-01', end='2026-06-01')], payments=[]))
    assert report['estimated_hour_units'] == 0
    report = build(dataset(hours=[work(start='2026-06-01', end='2026-06-01', date_basis='invoice_date',
                                      allocation_note='No work date; invoice issued June 1.')], payments=[]))
    assert report['estimated_hour_units'] == 10000001
    assert 'Invoice issue date' in report['hours'][0]['daily_allocation']


@pytest.mark.parametrize('cents,units', [(500000, 40000000), (1000000, 80000000), (1, 80), (62500, 5000000)])
def test_exact_actual_payment_to_purchased_hours(cents, units):
    assert credit_hour_units(cents, 12500) == units
    report = build(dataset(hours=[], payments=[payment(amount=cents)]))
    assert report['credited_hour_units'] == units
    assert report['payment_cents'] == cents
    assert report['balance_hour_units'] == -units


def test_payment_is_single_hour_credit_drop_even_with_multiple_invoice_references():
    report = build(dataset(payments=[payment(invoice_ids=['client-invoice-1', 'client-invoice-2'])]))
    assert report['payment_cents'] == 62500
    assert report['credited_hour_units'] == 5000000
    drops = [point for point in report['points'] if point['stage'] == 'after_payment']
    assert len(drops) == 1
    before = [point for point in report['points'] if point['date'] == '2026-06-02'
              and point['stage'] == 'before_payment'][0]
    assert before['balance_hour_units'] == 6666668
    assert drops[0]['balance_hour_units'] == 1666668
    assert report['balance_hour_units'] == 5000001


def test_partial_receipts_each_buy_hours_once_and_invoices_do_not_add_credit():
    report = build(dataset(payments=[payment(amount=25000), payment('receipt-2', '2026-06-03', 37500)]))
    assert report['payment_cents'] == 62500
    assert report['credited_hour_units'] == 5000000
    assert [event['change_hour_units'] for event in report['events'] if event['kind'] == 'payment'] == [-2000000, -3000000]
    assert len([point for point in report['points'] if point['stage'] == 'after_payment']) == 2
    assert report['balance_hour_units'] == 5000001


def test_same_day_credit_order_deterministic_and_work_precedes_receipt():
    first, second = payment('z-receipt', amount=12500), payment('a-receipt', amount=25000)
    one = build(dataset(payments=[first, second]))
    two = build(dataset(payments=[second, first]))
    assert one['points'] == two['points']
    assert [event['id'] for event in one['events'] if event['kind'] == 'payment'] == ['a-receipt', 'z-receipt']
    assert one['events'][1]['kind'] == 'work'


def test_narrow_period_carries_historical_hour_balance_and_filters_events():
    source = dataset(hours=[work(), work('july-work', 1000000, '2026-07-01', '2026-07-01')],
                     payments=[payment(), payment('july-receipt', '2026-07-02', 25000)])
    report = build(source, month='2026-07')
    assert report['opening_hour_units'] == 5000001
    assert report['used_hour_units'] == 1000000
    assert report['credited_hour_units'] == 2000000
    assert report['payment_cents'] == 25000
    assert report['balance_hour_units'] == 4000001
    assert report['lifetime_used_hour_units'] == 11000001
    assert report['lifetime_credited_hour_units'] == 7000000
    assert report['lifetime_payment_cents'] == 87500
    assert all(event['date'].startswith('2026-07') for event in report['events'])


def test_negative_hour_balance_shows_purchased_hours_remaining():
    report = build(dataset(payments=[payment(amount=187500)]))
    assert report['balance_hour_units'] == -4999999
    assert '5h of purchased hours remaining' in summary(report)
    assert 'contractor costs' not in summary(report).lower()
    assert 'profit' not in summary(report).lower()
    assert min(point['balance_hour_units'] for point in report['points']) < 0


def test_unpaid_voided_and_draft_invoices_never_purchase_hours():
    invoices = [{'id': name, 'number': name.upper(), 'date': '2026-06-01', 'amount_cents': 500000,
                 'status': status, 'paid_cents': 0}
                for name, status in [('unpaid', 'authorised'), ('void', 'voided'), ('draft', 'draft')]]
    report = build(dataset(payments=[], invoices=invoices))
    assert report['balance_hour_units'] == 10000001
    assert report['credited_hour_units'] == 0
    assert not any(point['stage'] == 'after_payment' for point in report['points'])
    assert 'UNPAID · A$5,000.00 unpaid' in summary(report)
    assert 'VOID · A$5,000.00 unpaid' not in summary(report)
    assert 'DRAFT · A$5,000.00 unpaid' not in summary(report)


def test_future_work_days_receipts_and_invoices_excluded():
    source = dataset(hours=[work(units=1000000, start='2026-10-10', end='2026-10-13')],
                     payments=[payment(day='2026-10-12')],
                     invoices=[{'id': 'future', 'number': 'FUTURE', 'date': '2026-10-12',
                                'amount_cents': 62500, 'status': 'paid', 'paid_cents': 62500}])
    report = build(source)
    assert report['used_hour_units'] == 500000
    assert report['payment_cents'] == report['credited_hour_units'] == 0
    assert report['invoices'] == []
    assert report['hours'][0]['included_hour_units'] == 500000
    assert report['end'] == '2026-10-11'


def test_source_snapshot_caps_day_even_if_coverage_declares_later_days():
    source = dataset(as_of='2026-10-09T01:00:00Z',
                     hours=[work(units=1000000, start='2026-10-08', end='2026-10-11')], payments=[])
    report = build(source)
    assert report['used_hour_units'] == 500000
    assert report['end'] == report['coverage_through'] == '2026-10-09'
    assert report['partial']


def test_after_coverage_requests_show_partial_without_filling_unknown_days():
    source = dataset(coverage={'start': '2026-06-01', 'through': '2026-09-30'})
    report = build(source, month='recent', months=3)
    assert report['start'] == '2026-08-01'
    assert report['end'] == '2026-09-30'
    assert report['partial']
    assert 'activity outside that coverage is unavailable' in summary(report)
    assert max(point['date'] for point in report['points']) == '2026-09-30'
    after = build(source, month='current')
    assert after['period_has_coverage'] is False
    assert after['points'] == after['events'] == []
    assert after['balance_hour_units'] == 5000001
    assert 'No reviewed activity is available' in summary(after)
    assert 'last known at 2026-09-30' in summary(after)


def test_midmonth_coverage_clamps_start_and_discloses_partial():
    source = dataset(coverage={'start': '2026-06-15', 'through': '2026-10-11'},
                     hours=[work(units=1000000, start='2026-06-15', end='2026-06-16')], payments=[])
    report = build(source, month='2026-06')
    assert report['start'] == '2026-06-15'
    assert report['partial']
    assert report['used_hour_units'] == 1000000


@pytest.mark.parametrize('kind', ['hours', 'payments'])
def test_duplicate_evidence_fails_closed(kind):
    source = dataset()
    source[kind].append(deepcopy(source[kind][0]))
    with pytest.raises(TimesheetError, match='duplicate_client_balance'):
        build(source)


def test_outside_project_work_fails_closed():
    with pytest.raises(TimesheetError, match='client_balance_scope_mismatch'):
        build(dataset(hours=[work(project_id='other-client-project')]))


def test_detailed_exports_conserve_microhours_record_assumptions_and_escape_formulas():
    source = dataset(hours=[work(allocation_note='=FORMULA()', description='@name')],
                     payments=[payment(description='=BAD()')])
    report = build(source, action='detailed')
    files = artifacts(report)
    assert set(files) == {'studio-hours-credits-ledger.csv', 'studio-client-invoices.csv',
                          'studio-work-hours-assumptions.csv'}
    ledger = list(csv.DictReader(StringIO(files['studio-hours-credits-ledger.csv'])))
    assert sum(Decimal(row['hours']) for row in ledger if row['event'] == 'work') == Decimal('10.000001')
    assert sum(Decimal(row['hours']) for row in ledger if row['event'] == 'payment') == Decimal('5')
    assert ledger[-1]['running_balance_hours'] == '5.000001'
    assert ledger[1]['hours'] == '3.333334'
    assert next(row for row in ledger if row['event'] == 'payment')['hours'] == '5.000000'
    assert any(row['description'] == "'=BAD()" for row in ledger)
    assumptions = list(csv.DictReader(StringIO(files['studio-work-hours-assumptions.csv'])))
    assert assumptions[0]['daily_timing_estimated'] == 'True'
    assert assumptions[0]['allocation_note'] == "'=FORMULA()"
    assert assumptions[0]['description'] == "'@name"
    assert artifacts(build()) == {}


def test_money_and_hours_display_do_not_round_through_float():
    assert money(9007199254740991) == 'A$90,071,992,547,409.91'
    assert display_hours(9007199254740991, precision=6) == '9007199254.740991'
    assert display_hours(331067777) == '331.07'
    assert display_hours(10009999) == '10.01'
    assert display_hours(5500000) == '5.5'
    assert display_hours(40000000) == '40'


def test_chart_uses_hours_and_actual_receipt_credit_labels(tmp_path):
    from matplotlib.axes import Axes
    annotations, fills, plotted, ylabels = [], [], [], []
    original_annotate, original_fill, original_plot, original_ylabel = (Axes.annotate, Axes.fill_between,
                                                                     Axes.plot, Axes.set_ylabel)
    def annotate(self, text, *args, **kwargs):
        annotations.append(text)
        return original_annotate(self, text, *args, **kwargs)
    def fill(self, *args, **kwargs):
        fills.append(kwargs.get('label'))
        return original_fill(self, *args, **kwargs)
    def plot(self, *args, **kwargs):
        plotted.append(args)
        return original_plot(self, *args, **kwargs)
    def ylabel(self, text, *args, **kwargs):
        ylabels.append(text)
        return original_ylabel(self, text, *args, **kwargs)
    report = build(dataset(payments=[payment(amount=500000)]))
    with patch.object(Axes, 'annotate', annotate), patch.object(Axes, 'fill_between', fill), \
            patch.object(Axes, 'plot', plot), patch.object(Axes, 'set_ylabel', ylabel):
        image = render_chart(report)
    assert image.startswith(b'\x89PNG\r\n\x1a\n')
    assert '02 Jun\n−40h\nA$5,000.00 received' in annotations
    assert set(fills) == {'Hours used beyond purchased hours', 'Purchased hours remaining'}
    assert ylabels == ['Hours used minus hours purchased']
    dates, values = plotted[0][:2]
    june_two = [value for at, value in zip(dates, values) if at == datetime(2026, 6, 2)]
    assert june_two == [6.666668, -33.333332]
    (tmp_path / 'client-hours-balance.png').write_bytes(image)


def test_chart_handles_single_day_and_uncovered_period():
    source = dataset(hours=[], payments=[], coverage={'start': '2026-10-11', 'through': '2026-10-11'})
    assert render_chart(build(source)).startswith(b'\x89PNG')
    source['coverage'] = {'start': '2026-06-01', 'through': '2026-09-30'}
    assert render_chart(build(source, month='current')).startswith(b'\x89PNG')


def test_known_pending_invoice_hours_disclosed_and_excluded_from_used_hours():
    source = dataset(contractor_invoices=[{'id': 'pending-invoice', 'number': 'PENDING', 'supplier': 'Builder',
                    'date': '2026-06-01', 'hour_units': 25000000, 'unresolved_hour_units': 25000000,
                    'excluded_hour_units': 0, 'allocation_note': 'Scope confirmation needed.'}])
    report = build(source)
    assert report['unresolved_hour_units'] == 25000000
    assert report['balance_hour_units'] == 5000001
    assert '25h pending confirmation' in summary(report)
    files = artifacts(build(source, action='detailed'))
    invoices = list(csv.DictReader(StringIO(files['studio-contractor-invoices.csv'])))
    assert invoices[0]['pending_confirmation_hours'] == '25.000000'
    assert invoices[0]['included_client_hours'] == '0.000000'
    assert 'invoice_hours' not in invoices[0]


def test_unknown_later_invoice_hours_never_inferred_from_payable_money():
    pending = [{'id': name, 'invoice_number': name, 'supplier': 'Builder', 'evidence_date': '2026-10-01',
                'note': 'No confirmed hour quantity.', 'evidence_ids': ['evidence']}
               for name in ['48350', '0011']]
    source = dataset(pending_work=pending)
    report = build(source)
    assert report['pending_work_count'] == 2
    assert report['unresolved_hour_units'] == 0
    assert report['used_hour_units'] == 10000001
    assert '2 later contractor invoices await confirmed hours' in summary(report)
    assert 'Their hours are unknown' in summary(report)
    files = artifacts(build(source, action='detailed'))
    rows = list(csv.DictReader(StringIO(files['studio-work-pending-confirmation.csv'])))
    assert len(rows) == 2
    assert all(row['hours_status'] == 'unknown; excluded from chart' for row in rows)


def test_historical_invoice_status_uses_verified_receipt_allocations():
    source = dataset(hours=[], payments=[payment(day='2026-07-01', amount=500000,
                    allocations=[{'invoice_id': 'client-invoice-1', 'amount_cents': 500000}])],
                    invoices=[{'id': 'client-invoice-1', 'number': 'INV-101', 'date': '2026-06-01',
                               'amount_cents': 500000, 'paid_cents': 500000, 'status': 'paid'}])
    june = build(source, month='2026-06', action='detailed')
    assert june['payment_cents'] == june['credited_hour_units'] == 0
    assert june['invoice_status_as_of'] == '2026-06-30'
    assert june['invoices'][0]['paid_cents'] == 0
    assert june['invoices'][0]['status'] == 'authorised'
    assert 'INV-101 · A$5,000.00 unpaid' in summary(june)
    assert not any(point['stage'] == 'after_payment' for point in june['points'])
    invoice_export = list(csv.DictReader(StringIO(artifacts(june)['studio-client-invoices.csv'])))
    assert invoice_export[0]['paid_to_date_AUD'] == '0'
    assert invoice_export[0]['invoice_status_as_of'] == '2026-06-30'
    july = build(source, month='2026-07')
    assert july['invoices'][0]['status'] == 'paid'
    assert july['credited_hour_units'] == 40000000


def test_historical_multi_invoice_receipt_preserves_allocations_but_buys_hours_once():
    invoices = [{'id': name, 'number': name, 'date': '2026-06-01', 'amount_cents': 500000,
                 'paid_cents': paid, 'status': 'paid' if paid == 500000 else 'authorised'}
                for name, paid in [('one', 500000), ('two', 250000)]]
    source = dataset(hours=[], invoices=invoices, payments=[payment(amount=750000, invoice_ids=['one', 'two'],
                     allocations=[{'invoice_id': 'one', 'amount_cents': 500000},
                                  {'invoice_id': 'two', 'amount_cents': 250000}])])
    report = build(source, month='2026-06')
    assert report['credited_hour_units'] == 60000000
    assert [invoice['paid_cents'] for invoice in report['invoices']] == [500000, 250000]
    assert len([point for point in report['points'] if point['stage'] == 'after_payment']) == 1


def test_legacy_invoice_status_is_explicitly_labelled_as_snapshot_status():
    report = build(month='2026-06', action='detailed')
    assert not report['invoice_status_historical']
    assert 'Invoice payment totals and status as of 2026-10-11 (snapshot status)' in summary(report)


def test_scope_label_is_displayed_in_chart_and_summary():
    from matplotlib.figure import Figure
    text, original = [], Figure.text
    def capture(self, x, y, value, *args, **kwargs):
        text.append(value)
        return original(self, x, y, value, *args, **kwargs)
    label = 'Four configured projects · shared Sansoni package payments'
    report = build(dataset(scope_label=label))
    with patch.object(Figure, 'text', capture):
        assert render_chart(report).startswith(b'\x89PNG')
    assert label in text
    assert label in summary(report)


def test_overview_is_concise_and_detailed_preserves_full_source_limitations():
    limitation = 'A source qualification that needs the detailed audit report.'
    source = dataset(scope_label='Four configured projects · shared Sansoni package payments', limitations=[limitation])
    overview, detailed = summary(build(source)), summary(build(source, action='detailed'))
    assert len(overview.splitlines()) < 22
    assert 'Client invoices · issue date' not in overview
    assert limitation not in overview and limitation in detailed
    assert 'Client invoices · issue date' in detailed
    assert '10h have estimated daily timing' in overview
    assert 'Source snapshot: 2026-10-11 · coverage through 2026-10-11' in overview
    assert 'A$5,000 = 40h' in overview
    assert 'Ask for a detailed report' in overview


def test_source_snapshot_date_uses_client_reporting_timezone():
    assert 'Source snapshot: 2026-10-11' in summary(build(dataset(as_of='2026-10-10T23:00:00Z')))


def test_reviewed_hour_total_is_independent_of_contractor_payable_amounts():
    contractor = {'id': 'contractor-1', 'number': 'B001', 'supplier': 'Builder', 'date': '2026-06-01',
                  'hour_units': 241740000, 'excluded_hour_units': 0, 'unresolved_hour_units': 0,
                  'allocation_note': 'Count once.', 'amount_cents': 99999999999}
    source = dataset(hours=[work(units=241740000), work('additional', 5500000, invoice_id=None)],
                     contractor_invoices=[contractor],
                     payments=[payment('one', '2026-07-02', 500000), payment('two', '2026-08-13', 500000),
                               payment('three', '2026-09-09', 1000000)])
    report = build(source)
    assert report['used_hour_units'] == 247240000
    assert report['credited_hour_units'] == 160000000
    assert report['balance_hour_units'] == 87240000
    assert '247.24h' in summary(report)
    assert '87.24h used beyond purchased hours' in summary(report)
    assert 'cost_cents' not in report and 'balance_cents' not in report
    source['contractor_invoices'][0]['amount_cents'] = 1
    assert build(source)['used_hour_units'] == report['used_hour_units']


@pytest.mark.parametrize('rate,reason', [(0, 'invalid_client_balance_allocation_rate'),
                                         (True, 'invalid_client_balance_allocation_rate'),
                                         (12501, 'inexact_client_balance_credit')])
def test_invalid_or_inexact_credit_conversion_fails_closed(rate, reason):
    with pytest.raises(TimesheetError, match=reason):
        build(dataset(allocation_rate_cents_per_hour=rate))


def test_mixed_project_invoice_export_exposes_only_reviewed_client_hours():
    invoice = {'id': 'eva-00013', 'number': '00013', 'supplier': 'Eva', 'date': '2026-09-10',
               'hour_units': 60250000, 'excluded_hour_units': 5000000, 'unresolved_hour_units': 0,
               'allocation_note': '60.25h total includes 5h on a project outside the client grant.'}
    source = dataset(hours=[work(units=55250000)], contractor_invoices=[invoice])
    report = build(source, action='detailed')
    assert report['used_hour_units'] == 55250000
    assert report['contractor_invoices'][0]['hour_units'] == 60250000
    text = artifacts(report)['studio-contractor-invoices.csv']
    rows = list(csv.DictReader(StringIO(text)))
    assert rows[0]['included_client_hours'] == '55.250000'
    assert rows[0]['pending_confirmation_hours'] == '0.000000'
    assert rows[0]['invoice_number'] == '00013'
    assert rows[0]['supplier'] == 'Eva'
    assert rows[0]['evidence_date'] == '2026-09-10'
    assert 'invoice_hours' not in rows[0]
    assert 'excluded_from_client_hours' not in rows[0]
    assert '60.25' not in text
    assert ',5.000000,' not in text
    assert '5h' not in text
    assert 'Other work on this invoice is excluded' in rows[0]['review_note']
