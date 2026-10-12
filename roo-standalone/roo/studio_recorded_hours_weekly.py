"""Exact weekly views of canonical reviewed work; no daily proration."""
from datetime import date, datetime, time, timedelta

from .studio_client_balance import HOUR_UNITS
from .studio_reports import TZ, month_offset, resolve_period
from .timesheets import TimesheetError, timestamp


def _window(first, last):
    return (datetime.combine(date.fromisoformat(first), time.min, TZ),
            datetime.combine(date.fromisoformat(last) + timedelta(days=1), time.min, TZ))


def build_weekly_recorded_report(config, client, selector, dataset, now):
    """Count a supported source quantity once only when its full dates fit.

    The private caller validates provenance and freshness. Estimated source
    dates cannot establish weekly timing, and inclusive dates imply full days.
    Financial records and raw evidence never enter this ordinary hours view.
    """
    resolved, start, end = resolve_period(selector, now)
    if resolved.get('period') != 'weekly':
        raise TimesheetError('invalid_report_week')
    as_of = timestamp(dataset['as_of']).astimezone(TZ)
    current = timestamp(now).astimezone(TZ)
    coverage_start, coverage_end = _window(dataset['coverage']['start'], dataset['coverage']['through'])
    supported_end = min(coverage_end, as_of, current)
    has_coverage = max(start, coverage_start) < min(end, supported_end)
    coverage_partial = start < coverage_start or end > supported_end or not has_coverage
    projects = {key: config.projects[key] for key in client['project_ids']}
    monthly = [{'month': month_offset(resolved['month'], i).strftime('%Y-%m'),
                'units': 0, 'unresolved': 0, 'allowance_units': None}
               for i in range(resolved['months'])]
    by_month = {row['month']: row for row in monthly}
    invoices = {row['id']: row for row in dataset['contractor_invoices']}
    rows, exceptions, seen = [], [], set()
    for work in dataset['hours']:
        project = work['project_id']
        if project not in projects:
            raise TimesheetError('client_balance_scope_mismatch')
        if work['id'] in seen:
            raise TimesheetError('duplicate_client_balance_hours')
        seen.add(work['id'])
        first, through = _window(work['work_start'], work['work_end'])
        if first >= end or through <= start:
            continue
        invoice = invoices.get(work.get('invoice_id'))
        reference = invoice['number'] if invoice else work['id']
        reason = None
        if work['date_basis'] != 'work_dates':
            reason = ('Excluded from this weekly subtotal; source work dates are estimated or use an invoice date. '
                      'These dates cannot establish weekly hours; quantities are not prorated.')
        elif first < start or through > end:
            reason = ('Excluded from this weekly subtotal; the full source work period extends outside the weekly '
                      'cutoff or lacks sufficient time precision. Quantities are not prorated.')
        elif first < coverage_start or through > supported_end:
            reason = ('Excluded from this weekly subtotal; the full source work period is not supported by the '
                      'reviewed coverage and source timestamp. Unavailable hours are not counted as zero.')
        if reason:
            for month in by_month:
                if work['work_start'][:7] <= month <= work['work_end'][:7]:
                    exceptions.append({'month': month, 'project': projects[project],
                                       'identifier': reference, 'reason': reason})
                    by_month[month]['unresolved'] += 1
            continue
        builder = work['builder_id']
        if builder not in config.builders:
            raise TimesheetError('builder_mapping_required')
        # Preserve the entire canonical quantity, including millionth-hour
        # precision, under its actual final work month. There is no day split.
        month = work['work_end'][:7]
        rows.append({'month': month, 'project_id': project, 'project': projects[project],
                     'builder_id': builder, 'builder': config.builders[builder]['name'],
                     'issue_id': work['id'], 'identifier': reference,
                     'title': work['description'], 'completed_at': work['work_end'],
                     'work_start': work['work_start'], 'work_end': work['work_end'],
                     'size': '', 'units': work['hour_units'],
                     'source': 'reviewed_invoice' if invoice else 'reviewed_recorded_time',
                     'date_note': 'Full supported source work period; no daily allocation or clock time is inferred.',
                     'month_allocation': 'work_dates', 'allocation_note': work['allocation_note']})
        by_month[month]['units'] += work['hour_units']
    rows.sort(key=lambda row: (row['month'], row['project'], row['completed_at'], row['identifier']))
    limitations = list(dataset['limitations'])
    coverage_through = min(date.fromisoformat(dataset['coverage']['through']), as_of.date(), current.date()).isoformat()
    if coverage_partial:
        limitations.append('Requested activity outside reviewed coverage is unavailable; it is not counted as zero. '
                           'Source review: ' + dataset['as_of'] + '; coverage through ' + coverage_through + '.')
    if exceptions:
        limitations.append('Work without fully supported weekly dates is excluded; aggregate quantities are not prorated.')
    return {'selector': resolved, 'client': client['name'], 'projects': projects,
            'monthly': monthly, 'rows': rows, 'exceptions': exceptions,
            'generated_at': current.isoformat(), 'complete': False,
            'basis': 'invoice_first_recorded_hours', 'unit_scale': HOUR_UNITS,
            'invoice_first_projects': list(client['project_ids']),
            'qualifications': [{key: note[key] for key in ('project_id', 'reason', 'reference')}
                               for note in dataset.get('qualifications', []) if note['project_id'] in projects],
            'unallocated_units': 0, 'estimated_month_units': 0,
            'recorded_snapshot': True, 'source_as_of': dataset['as_of'],
            'coverage_through': coverage_through, 'coverage_start': dataset['coverage']['start'],
            'period_has_coverage': has_coverage, 'weekly_has_coverage': has_coverage,
            'partial': coverage_partial or bool(exceptions),
            'limitations': list(dict.fromkeys(limitations)),
            'pending_work_count': len(dataset.get('pending_work', []))}
