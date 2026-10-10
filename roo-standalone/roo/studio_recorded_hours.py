"""Project/month views of the same reviewed work used by paid-credit reports.

No ticket estimates or contractor money conversions enter this projection.
Every microhour is allocated by the shared balance calculation exactly once.
"""
from collections import defaultdict

from .studio_client_balance import HOUR_UNITS, _allocated_days, build_balance_report
from .studio_reports import month_offset
from .timesheets import timestamp


def build_recorded_report(config, client, selector, dataset, now):
    balance = build_balance_report(client, selector, dataset, now)
    resolved = balance['selector']
    monthly = [{'month': month_offset(resolved['month'], i).strftime('%Y-%m'),
                'units': 0, 'unresolved': 0, 'allowance_units': None}
               for i in range(resolved['months'])]
    by_month = {row['month']: row for row in monthly}
    invoices = {row['id']: row for row in dataset['contractor_invoices']}
    rows = []
    for work in balance['hours']:
        units_by_month = defaultdict(int)
        for day, units in _allocated_days(work):
            if balance['period_has_coverage'] and balance['start'] <= day.isoformat() <= balance['end']:
                units_by_month[day.strftime('%Y-%m')] += units
        estimated = (work['date_basis'] != 'work_dates'
                     or work['work_start'][:7] != work['work_end'][:7])
        invoice = invoices.get(work.get('invoice_id'))
        for month, units in units_by_month.items():
            if not units:
                continue
            row = {'month': month, 'project_id': work['project_id'],
                   'project': config.projects[work['project_id']],
                   'builder_id': work['builder_id'], 'builder': config.builders[work['builder_id']]['name'],
                   'issue_id': work['id'] + ':' + month,
                   'identifier': invoice['number'] if invoice else work['id'],
                   'title': work['description'], 'completed_at': work['work_end'],
                   'work_start': work['work_start'], 'work_end': work['work_end'],
                   'size': '', 'units': units,
                   'source': 'reviewed_invoice' if invoice else 'reviewed_recorded_time',
                   'date_note': 'Source work period, not a ticket completion date.',
                   'month_allocation': 'estimated' if estimated else 'work_dates',
                   'allocation_note': work['allocation_note'] +
                       (' Exact source quantity preserved; cross-month daily allocation is estimated.'
                        if work['work_start'][:7] != work['work_end'][:7] else '')}
            rows.append(row)
            by_month[month]['units'] += units
            if estimated:
                by_month[month]['estimated_units'] = by_month[month].get('estimated_units', 0) + units
    assert sum(row['units'] for row in rows) == balance['used_hour_units']
    qualifications = list(dataset.get('qualifications', []))
    # Scope/timing qualifications are client-safe. Raw evidence and finance
    # records are not copied to the ordinary work-hours report.
    return {'selector': resolved, 'client': client['name'],
            'projects': {key: config.projects[key] for key in client['project_ids']},
            'monthly': monthly, 'rows': rows, 'exceptions': [],
            'generated_at': timestamp(now).isoformat(), 'complete': False,
            'basis': 'invoice_first_recorded_hours', 'unit_scale': HOUR_UNITS,
            'invoice_first_projects': list(client['project_ids']),
            'qualifications': qualifications, 'unallocated_units': 0,
            'estimated_month_units': sum(m.get('estimated_units', 0) for m in monthly),
            'recorded_snapshot': True, 'source_as_of': dataset['as_of'],
            'coverage_through': balance['coverage_through'], 'partial': balance['partial'],
            'limitations': list(dataset['limitations']), 'pending_work_count': balance['pending_work_count']}


def overlay_recorded_reports(report, reviewed):
    """Replace covered project rows, never add a second copy to legacy totals."""
    covered = {key for item in reviewed for key in item['projects']}
    old_scale = report['unit_scale']
    multiplier = HOUR_UNITS // old_scale
    if multiplier * old_scale != HOUR_UNITS:
        raise ValueError('Unsupported report precision')
    report['rows'] = [{**row, 'units': row['units'] * multiplier}
                      for row in report['rows'] if row['project_id'] not in covered]
    covered_names = {report['projects'][key] for key in covered}
    report['exceptions'] = [row for row in report['exceptions'] if row['project'] not in covered_names]
    report['qualifications'] = [row for row in report.get('qualifications', [])
                                if row['project_id'] not in covered]
    for item in reviewed:
        report['rows'].extend(item['rows'])
        report['qualifications'].extend(item['qualifications'])
    report['unit_scale'] = HOUR_UNITS
    by_month = {row['month']: row for row in report['monthly']}
    for month in by_month.values():
        month.update(units=0, unresolved=0, estimated_units=0)
        # Invoice hour-units cannot establish a remaining contractual allowance.
        month['allowance_units'] = None
    for row in report['rows']:
        if row['month']:
            month = by_month[row['month']]
            month['units'] += row['units']
            if row.get('month_allocation') == 'estimated':
                month['estimated_units'] += row['units']
    for row in report['exceptions']:
        by_month[row['month']]['unresolved'] += 1
    report['unallocated_units'] = sum(row['units'] for row in report['rows'] if not row['month'])
    report['estimated_month_units'] = sum(m['estimated_units'] for m in by_month.values())
    report['invoice_first_projects'] = sorted(set(report.get('invoice_first_projects', [])) | covered)
    report['basis'] = ('invoice_first_recorded_hours' if all(row.get('source') in
                        {'reviewed_invoice', 'reviewed_recorded_time'} for row in report['rows'])
                       else 'reviewed_invoices_and_completed_ticket_sizes')
    report.update(recorded_snapshot=True, complete=False,
                  source_as_of=min(item['source_as_of'] for item in reviewed),
                  coverage_through=min(item['coverage_through'] for item in reviewed),
                  partial=any(item['partial'] for item in reviewed),
                  limitations=list(dict.fromkeys(note for item in reviewed for note in item['limitations'])),
                  pending_work_count=sum(item['pending_work_count'] for item in reviewed))
    report['rows'].sort(key=lambda row: (row['month'], row['project'], row['completed_at'], row['identifier']))
    return report
