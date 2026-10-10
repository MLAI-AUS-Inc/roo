"""Reviewed client work hours less hours purchased through actual receipts.

One hour is one million integer units. Each recorded hour and each receipt is
counted once; date allocations change the shape, never the supported total.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from threading import Lock

from .payment_reminders import _escape
from .studio_reports import TZ, resolve_period
from .timesheets import TimesheetError, csv_file, timestamp

HOUR_UNITS = 1_000_000
_chart_lock = Lock()


def money(cents):
    return ('−' if cents < 0 else '') + f'A${Decimal(abs(cents)) / 100:,.2f}'


def display_hours(units, *, precision=2):
    number = (Decimal(units) / HOUR_UNITS).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
    value = format(number, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def credit_hour_units(amount_cents, rate):
    if (type(amount_cents) is not int or amount_cents <= 0
            or type(rate) is not int or rate <= 0):
        raise TimesheetError('invalid_client_balance_payment')
    units, remainder = divmod(amount_cents * HOUR_UNITS, rate)
    if remainder:
        raise TimesheetError('inexact_client_balance_credit')
    return units


def _day(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise TimesheetError('invalid_client_balance_date') from exc


def _allocated_days(work):
    """Allocate every microhour once; remainder units go to earlier days."""
    start, end = _day(work['work_start']), _day(work['work_end'])
    count = (end - start).days + 1
    if count <= 0 or type(work['hour_units']) is not int or work['hour_units'] <= 0:
        raise TimesheetError('invalid_client_balance_hours')
    quotient, remainder = divmod(work['hour_units'], count)
    return [(start + timedelta(days=index), quotient + (index < remainder))
            for index in range(count)]


def build_balance_report(client, selector, dataset, now):
    """Compute recorded work less purchased hours, including historical carry-in.

    The caller verifies snapshot provenance and receipt attribution. This
    boundary also rejects work outside the verified project grant. Contractor
    payable amounts never enter the hour calculation.
    """
    rate = dataset['allocation_rate_cents_per_hour']
    if type(rate) is not int or rate <= 0:
        raise TimesheetError('invalid_client_balance_allocation_rate')
    coverage_start, coverage_end = (_day(dataset['coverage']['start']),
                                    _day(dataset['coverage']['through']))
    resolved, requested_start, requested_end = resolve_period(
        selector, now, beginning=coverage_start.strftime('%Y-%m'))
    current_day = timestamp(now).astimezone(TZ).date()
    source_day = timestamp(dataset['as_of']).astimezone(TZ).date()
    cutoff = min(coverage_end, current_day, source_day)
    start = max(coverage_start, requested_start.date())
    end = min(requested_end.date() - timedelta(days=1), cutoff)
    has_coverage = start <= end
    allowed = set(client['project_ids'])
    work_days, estimated_days = defaultdict(int), defaultdict(int)
    work_ids, payment_ids = set(), set()
    rows, payments = [], []
    for original in dataset['hours']:
        if original['project_id'] not in allowed:
            raise TimesheetError('client_balance_scope_mismatch')
        if original['id'] in work_ids:
            raise TimesheetError('duplicate_client_balance_hours')
        work_ids.add(original['id'])
        work = dict(original)
        allocated = _allocated_days(work)
        estimated = work['date_basis'] != 'work_dates' or len(allocated) > 1
        period_units = included_units = 0
        for day, units in allocated:
            if day <= cutoff:
                work_days[day] += units
                included_units += units
                if estimated:
                    estimated_days[day] += units
                if has_coverage and start <= day <= end:
                    period_units += units
        work.update(estimated_daily_allocation=estimated, included_hour_units=included_units,
                    period_hour_units=period_units,
                    daily_allocation=('Uniform daily allocation of the reviewed work period; exact hours preserved.'
                                      if len(allocated) > 1 else
                                      'Invoice issue date used as an estimated work date.'
                                      if work['date_basis'] == 'invoice_date' else
                                      'Reviewed estimated work date.' if estimated else 'Recorded work date.'))
        if included_units:
            rows.append(work)
    payment_days = defaultdict(list)
    for original in dataset['payments']:
        if original['id'] in payment_ids:
            raise TimesheetError('duplicate_client_balance_payment')
        payment_ids.add(original['id'])
        credit = credit_hour_units(original['amount_cents'], rate)
        day = _day(original['date'])
        if day <= cutoff:
            payment = {**original, 'credited_hour_units': credit}
            payments.append(payment)
            payment_days[day].append(payment)
    for day in payment_days:
        payment_days[day].sort(key=lambda payment: payment['id'])
    observed_end = end if has_coverage else cutoff
    lifetime_used = sum(units for day, units in work_days.items() if day <= observed_end)
    lifetime_credit = sum(payment['credited_hour_units'] for payment in payments
                          if _day(payment['date']) <= observed_end)
    lifetime_paid = sum(payment['amount_cents'] for payment in payments
                        if _day(payment['date']) <= observed_end)
    opening = (sum(units for day, units in work_days.items() if day < start)
               - sum(payment['credited_hour_units'] for payment in payments if _day(payment['date']) < start))
    events, points = [], []
    period_used = period_credit = period_paid = estimated_units = 0
    balance = opening
    if has_coverage:
        points.append({'date': start.isoformat(), 'balance_hour_units': balance, 'stage': 'opening'})
        day = start
        while day <= end:
            key = day.isoformat()
            units = work_days[day]
            if units:
                balance += units
                period_used += units
                estimated_units += estimated_days[day]
                events.append({'date': key, 'kind': 'work', 'id': key + ':recorded-work',
                               'hour_units': units, 'change_hour_units': units, 'balance_hour_units': balance,
                               'amount_cents': 0, 'invoice_ids': [], 'description': 'Allocated recorded work hours'})
            # Work precedes receipt credits on the same day. Repeated x values
            # before/after each actual receipt produce the vertical chart drop.
            points.append({'date': key, 'balance_hour_units': balance, 'stage': 'before_payment'})
            for payment in payment_days[day]:
                units, cents = payment['credited_hour_units'], payment['amount_cents']
                balance -= units
                period_credit += units
                period_paid += cents
                events.append({'date': key, 'kind': 'payment', 'id': payment['id'],
                               'hour_units': units, 'change_hour_units': -units, 'balance_hour_units': balance,
                               'amount_cents': cents, 'invoice_ids': list(payment['invoice_ids']),
                               'description': payment['description']})
                points.append({'date': key, 'balance_hour_units': balance, 'stage': 'after_payment',
                               'payment_id': payment['id'], 'amount_cents': cents, 'credited_hour_units': units})
            day += timedelta(days=1)
    else:
        # Show the last supported balance, without fabricating later activity.
        opening = balance = lifetime_used - lifetime_credit
    invoices = [dict(invoice) for invoice in dataset['invoices']
                if _day(invoice['date']) <= observed_end]
    invoice_status_historical = all('allocations' in payment for payment in dataset['payments'])
    if invoice_status_historical:
        paid_by_invoice = defaultdict(int)
        for payment in payments:
            if _day(payment['date']) <= observed_end:
                for allocation in payment['allocations']:
                    paid_by_invoice[allocation['invoice_id']] += allocation['amount_cents']
        for invoice in invoices:
            invoice['snapshot_paid_cents'] = invoice['paid_cents']
            invoice['snapshot_status'] = invoice['status']
            if invoice['status'] not in {'voided', 'draft'}:
                invoice['paid_cents'] = paid_by_invoice[invoice['id']]
                invoice['status'] = 'paid' if invoice['paid_cents'] >= invoice['amount_cents'] else 'authorised'
    invoices.sort(key=lambda invoice: (invoice['date'], invoice['number'], invoice['id']))
    limitations = list(dataset['limitations'])
    partial = (requested_start.date() < coverage_start or
               min(requested_end.date() - timedelta(days=1), current_day) > cutoff or not has_coverage)
    if partial:
        limitations.append(f"Reviewed coverage is {coverage_start.isoformat()} through {cutoff.isoformat()}; "
                           'activity outside that coverage is unavailable.')
    if estimated_units:
        limitations.append('The rise between receipts uses estimated daily timing from reviewed work periods; '
                           'it is not a daily timesheet.')
    contractor_invoices = [dict(invoice) for invoice in dataset.get('contractor_invoices', [])]
    pending_work = [dict(item) for item in dataset.get('pending_work', [])]
    return {
        'kind': 'client_balance', 'selector': resolved, 'client': client['name'], 'name': client['name'],
        'currency': dataset['currency'], 'basis': dataset['basis'], 'unit_scale': HOUR_UNITS,
        'allocation_rate_cents_per_hour': rate, 'scope_label': dataset.get('scope_label', ''),
        'invoice_status_as_of': observed_end.isoformat() if invoice_status_historical else source_day.isoformat(),
        'invoice_status_historical': invoice_status_historical,
        'start': start.isoformat(), 'end': end.isoformat(),
        'requested_start': requested_start.date().isoformat(),
        'requested_end': (requested_end.date() - timedelta(days=1)).isoformat(),
        'coverage_start': coverage_start.isoformat(), 'coverage_through': cutoff.isoformat(),
        'as_of': dataset['as_of'], 'generated_at': timestamp(now).isoformat(),
        'period_has_coverage': has_coverage, 'partial': partial,
        'opening_hour_units': opening, 'used_hour_units': period_used,
        'credited_hour_units': period_credit, 'payment_cents': period_paid, 'balance_hour_units': balance,
        'lifetime_used_hour_units': lifetime_used, 'lifetime_credited_hour_units': lifetime_credit,
        'lifetime_payment_cents': lifetime_paid, 'estimated_hour_units': estimated_units,
        'points': points, 'events': events, 'invoices': invoices,
        'unresolved_hour_units': sum(invoice['unresolved_hour_units'] for invoice in contractor_invoices),
        'contractor_invoices': contractor_invoices, 'pending_work': pending_work,
        'pending_work_count': len(pending_work), 'hours': rows,
        'payments': sorted(payments, key=lambda payment: (payment['date'], payment['id'])),
        'limitations': list(dict.fromkeys(limitations)),
    }


def _balance_text(units):
    if units > 0:
        return f'*{display_hours(units)}h used beyond purchased hours*'
    if units < 0:
        return f'*{display_hours(-units)}h of purchased hours remaining*'
    return '*Hours used equal hours purchased.*'


def summary(report):
    detailed = report['selector']['action'] == 'detailed'
    parts = [f"*Studio hours and paid credits · {_escape(report['client'])}*"]
    if report['period_has_coverage']:
        parts.extend([f"{report['start']} – {report['end']}",
                      f"Recorded hours used: *{display_hours(report['used_hour_units'])}h*",
                      f"Hours purchased: *{display_hours(report['credited_hour_units'])}h*"
                      f" · {money(report['payment_cents'])} received"])
        if report['opening_hour_units'] or detailed:
            parts.append(f"Hours used minus purchased, carried in: *{display_hours(report['opening_hour_units'])}h*")
        parts.append(_balance_text(report['balance_hour_units']) + f" · at {report['end']}")
    else:
        parts.extend([f"No reviewed activity is available for {report['requested_start']} – {report['requested_end']}.",
                      _balance_text(report['balance_hour_units']) + f" · last known at {report['coverage_through']}"])
    if report['scope_label']:
        parts.append(_escape(report['scope_label']))
    payments = [event for event in report['events'] if event['kind'] == 'payment']
    names = {invoice['id']: invoice['number'] for invoice in report['invoices']}
    parts.append('\n*Payments received · hours purchased on each date*')
    parts.extend(f"• {payment['date']} · *{money(payment['amount_cents'])} → {display_hours(payment['hour_units'])}h* · "
                 + _escape(', '.join(names.get(invoice, invoice) for invoice in payment['invoice_ids']) or 'Client payment')
                 for payment in payments)
    if not payments:
        parts.append('No payments received in the covered selected period.')
    visible_invoices = [invoice for invoice in report['invoices'] if invoice['status'] in {'paid', 'authorised'}]
    if detailed and visible_invoices:
        parts.append('\n*Client invoices · issue date / amount / paid / status*')
        parts.append('Invoice payment totals and status as of ' + report['invoice_status_as_of'] +
                     ('.' if report['invoice_status_historical'] else ' (snapshot status).'))
        parts.extend(f"• {invoice['date']} · {_escape(invoice['number'])} · {money(invoice['amount_cents'])} · "
                     f"paid {money(invoice['paid_cents'])} · {invoice['status']}" for invoice in visible_invoices)
    outstanding = [invoice for invoice in report['invoices'] if invoice['status'] == 'authorised'
                   and invoice['amount_cents'] > invoice['paid_cents']]
    if outstanding:
        parts.append('\n*Unpaid invoice amounts · excluded from purchased hours · as of ' + report['invoice_status_as_of'] + '*'
                     + ('' if report['invoice_status_historical'] else ' (snapshot status)'))
        parts.extend(f"• {_escape(invoice['number'])} · {money(invoice['amount_cents'] - invoice['paid_cents'])} unpaid"
                     for invoice in outstanding)
    if report['unresolved_hour_units']:
        parts.append('\n*' + display_hours(report['unresolved_hour_units']) + 'h pending confirmation* · excluded from the chart.')
    if report['pending_work_count']:
        parts.append(f"{report['pending_work_count']} later contractor invoices await confirmed hours and project scope; "
                     'excluded from the chart. Their hours are unknown.')
    if report['estimated_hour_units']:
        parts.append(f"{display_hours(report['estimated_hour_units'])}h have estimated daily timing.")
    source_date = timestamp(report['as_of']).astimezone(TZ).date().isoformat()
    parts.append(f"Purchased hours use {money(report['allocation_rate_cents_per_hour'])}/h · A$5,000 = "
                 f"{display_hours(credit_hour_units(500000, report['allocation_rate_cents_per_hour']))}h.")
    parts.append(f"Source snapshot: {source_date} · coverage through {report['coverage_through']}.")
    if report['partial']:
        parts.append(f"Reviewed coverage begins {report['coverage_start']}; activity outside that coverage is unavailable.")
    if detailed:
        parts.append('\n*Work timing assumptions and source limitations*')
        parts.extend('• ' + _escape(limitation) for limitation in report['limitations'])
    else:
        parts.append('Ask for a detailed report for invoice status, work records and timing assumptions.')
    return '\n'.join(parts)


def artifacts(report):
    """Detailed audit exports; source text is spreadsheet-formula escaped."""
    if report['selector']['action'] != 'detailed':
        return {}
    decimal_money = lambda cents: Decimal(cents) / 100
    decimal_hours = lambda units: (Decimal(units) / HOUR_UNITS).quantize(Decimal('0.000001'))
    names = {invoice['id']: invoice['number'] for invoice in report['invoices']}
    result = {
        'studio-hours-credits-ledger.csv': csv_file(
            ['date', 'event', 'source_id', 'hours', 'balance_change_hours', 'running_balance_hours',
             'client_payment_AUD', 'client_invoices', 'description', 'opening_balance_hours', 'coverage_through'],
            [[event['date'], event['kind'], event['id'], decimal_hours(event['hour_units']),
              decimal_hours(event['change_hour_units']), decimal_hours(event['balance_hour_units']),
              decimal_money(event['amount_cents']),
              ', '.join(names.get(invoice, invoice) for invoice in event['invoice_ids']), event['description'],
              decimal_hours(report['opening_hour_units']), report['coverage_through']] for event in report['events']]),
        'studio-client-invoices.csv': csv_file(
            ['invoice_id', 'invoice_number', 'issue_date', 'gross_amount_AUD', 'paid_to_date_AUD',
             'status', 'unpaid_authorised_amount_AUD', 'invoice_status_as_of'],
            [[invoice['id'], invoice['number'], invoice['date'], decimal_money(invoice['amount_cents']),
              decimal_money(invoice['paid_cents']), invoice['status'],
              decimal_money(max(0, invoice['amount_cents'] - invoice['paid_cents']))
              if invoice['status'] == 'authorised' else Decimal(0), report['invoice_status_as_of']]
             for invoice in report['invoices']]),
        'studio-work-hours-assumptions.csv': csv_file(
            ['work_id', 'contractor_invoice_id', 'project_id', 'recorded_hours',
             'included_through_coverage_hours', 'hours_in_selected_period', 'work_start', 'work_end',
             'date_basis', 'daily_timing_estimated', 'daily_allocation', 'allocation_note', 'description'],
            [[work['id'], work.get('invoice_id', ''), work['project_id'], decimal_hours(work['hour_units']),
              decimal_hours(work['included_hour_units']), decimal_hours(work['period_hour_units']),
              work['work_start'], work['work_end'], work['date_basis'], work['estimated_daily_allocation'],
              work['daily_allocation'], work['allocation_note'], work['description']] for work in report['hours']]),
    }
    if report['contractor_invoices']:
        result['studio-contractor-invoices.csv'] = csv_file(
            ['invoice_id', 'invoice_number', 'supplier', 'evidence_date', 'date_kind', 'invoice_hours',
             'excluded_from_client_hours', 'pending_confirmation_hours', 'allocation_note'],
            [[invoice['id'], invoice['number'], invoice['supplier'], invoice['date'],
              invoice.get('date_kind', 'invoice'), decimal_hours(invoice['hour_units']),
              decimal_hours(invoice['excluded_hour_units']), decimal_hours(invoice['unresolved_hour_units']),
              invoice['allocation_note']] for invoice in report['contractor_invoices']])
    if report['pending_work']:
        result['studio-work-pending-confirmation.csv'] = csv_file(
            ['source_id', 'supplier', 'invoice_number', 'evidence_date', 'hours_status', 'note'],
            [[item['id'], item['supplier'], item['invoice_number'], item['evidence_date'],
              'unknown; excluded from chart', item['note']] for item in report['pending_work']])
    return result


def render_chart(report):
    """Render privately; actual receipts buy hour credits and lower the line."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter
    import textwrap

    with _chart_lock:
        figure = Figure(figsize=(11, 7), dpi=150)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left=.12, right=.94, top=.69 if report['scope_label'] else .735, bottom=.25)
        figure.text(.06, .94, 'Studio hours and paid credits', fontsize=22, weight='bold', color='#142c3a')
        figure.text(.06, .875, textwrap.shorten(report['client'], width=75, placeholder='…'), fontsize=14, color='#334b58')
        if not report['period_has_coverage']:
            axes.text(.5, .5, 'No reviewed activity for the requested period\n'
                      f"Last available data: {report['coverage_through']}",
                      transform=axes.transAxes, ha='center', va='center', fontsize=14, color='#334b58')
            axes.set_axis_off()
        else:
            dates = [datetime.combine(_day(point['date']), datetime.min.time()) for point in report['points']]
            values = [point['balance_hour_units'] / HOUR_UNITS for point in report['points']]
            axes.plot(dates, values, color='#147d92', linewidth=2.2, label='Hours used minus hours purchased', zorder=4)
            axes.fill_between(dates, values, 0, where=[value >= 0 for value in values], interpolate=True,
                              color='#f3c994', alpha=.65, label='Hours used beyond purchased hours')
            axes.fill_between(dates, values, 0, where=[value <= 0 for value in values], interpolate=True,
                              color='#a7d9c4', alpha=.65, label='Purchased hours remaining')
            axes.axhline(0, color='#647580', linewidth=1)
            payments = [point for point in report['points'] if point['stage'] == 'after_payment']
            nearby_days = max(5, (dates[-1] - dates[0]).days * .09)
            payment_tiers, previous_at, tier = [], None, 0
            for point in payments:
                at = _day(point['date'])
                tier = (tier + 1) % 3 if previous_at and (at - previous_at).days < nearby_days else 0
                payment_tiers.append(tier)
                previous_at = at
            for point, tier in zip(payments, payment_tiers):
                at = datetime.combine(_day(point['date']), datetime.min.time())
                value = point['balance_hour_units'] / HOUR_UNITS
                axes.scatter([at], [value], color='#142c3a', s=25, zorder=5)
                axes.annotate(f"{at:%d %b}\n−{display_hours(point['credited_hour_units'])}h\n{money(point['amount_cents'])} received",
                              (at, value), xytext=(0, -25 - tier * 25), textcoords='offset points',
                              fontsize=8, ha='center', va='top', color='#142c3a',
                              arrowprops={'arrowstyle': '-', 'color': '#647580', 'linewidth': .6},
                              bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .92, 'pad': 2}, zorder=6)
            start, end = dates[0], dates[-1]
            padding = timedelta(days=max(.5, (end - start).days * .035))
            axes.set_xlim(start - padding, end + padding)
            span = max(10, max(values + [0]) - min(values + [0]))
            axes.set_ylim(min(values + [0]) - span * (.55 + max(payment_tiers, default=0) * .25),
                          max(values + [0]) + span * .16)
            locator = AutoDateLocator(minticks=4, maxticks=8)
            axes.xaxis.set_major_locator(locator)
            axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
            axes.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{"−" if value < 0 else ""}{abs(value):g}h'))
            axes.set_xlabel('Work / payment date')
            axes.set_ylabel('Hours used minus hours purchased')
            axes.grid(axis='y', color='#e8eef1', linewidth=.7)
            axes.set_axisbelow(True)
            for spine in axes.spines.values():
                spine.set_visible(False)
            axes.tick_params(length=0, pad=7)
            figure.legend(*axes.get_legend_handles_labels(), loc='lower left', bbox_to_anchor=(.11, .105),
                          frameon=False, fontsize=9)
        figure.text(.06, .82, (f"{report['start']} – {report['end']}" if report['period_has_coverage'] else
                              f"Requested: {report['requested_start']} – {report['requested_end']}"),
                    fontsize=11, color='#334b58')
        if report['period_has_coverage']:
            balance_label = 'Beyond purchased hours' if report['balance_hour_units'] >= 0 else 'Purchased hours remaining'
            figure.text(.06, .775, f"Hours used {display_hours(report['used_hour_units'])}h  ·  "
                        f"Hours purchased {display_hours(report['credited_hour_units'])}h  ·  "
                        f"{balance_label} {display_hours(abs(report['balance_hour_units']))}h", fontsize=10, color='#334b58')
        if report['scope_label']:
            figure.text(.06, .735, textwrap.shorten(report['scope_label'], width=118, placeholder='…'),
                        fontsize=10, color='#334b58')
        figure.text(.06, .065, f"Reviewed work hours · actual receipts buy hours at {money(report['allocation_rate_cents_per_hour'])}/h",
                    fontsize=9, color='#334b58')
        note = ('Daily work timing estimated. ' if report['estimated_hour_units'] else '')
        if report['unresolved_hour_units']:
            note += f"Pending confirmed quantity excluded: {display_hours(report['unresolved_hour_units'])}h. "
        if report['pending_work_count']:
            note += f"{report['pending_work_count']} later invoices have unconfirmed hours; excluded. "
        note += f"Data through {report['coverage_through']}."
        figure.text(.06, .03, '\n'.join(textwrap.wrap(note, width=118)), fontsize=8.5, color='#334b58')
        output = BytesIO()
        canvas.print_png(output)
        return output.getvalue()
