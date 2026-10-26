"""Bounded usage includes reviewed quantities only with contained work dates."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .timesheets import TimesheetError, timestamp

TZ = ZoneInfo('Australia/Melbourne')


def _date_window(start, end):
    """Inclusive source dates express whole days, never an inferred clock time."""
    return (datetime.combine(date.fromisoformat(start), time.min, TZ),
            datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min, TZ))


def apply_weekly_backfill(report, backfill, config, now):
    """Retain exact source quantities; ambiguous weekly splits remain partial."""
    if not backfill:
        return report
    start = timestamp(report['selector']['start']).astimezone(TZ)
    end = timestamp(report['selector']['end']).astimezone(TZ)
    as_of = timestamp(now).astimezone(TZ)
    period_name = 'month-to-date' if report['selector'].get('period') == 'month_to_date' else 'weekly'
    by_month = {month['month']: month for month in report['monthly']}
    report['unallocated_units'] = 0
    report['estimated_month_units'] = 0
    invoice_first = set(backfill.get('invoice_first_projects', [])) & set(report['projects'])
    report['invoice_first_projects'] = sorted(invoice_first)
    report['qualifications'] = [note for note in backfill.get('qualifications', [])
                                if note['project_id'] in report['projects']]

    def overlapping_months(first, through):
        return [month for month in by_month if first[:7] <= month <= through[:7]]

    def exception(project, reference, months, reason):
        for month in months:
            by_month[month]['unresolved'] += 1
            report['exceptions'].append({'month': month, 'project': report['projects'][project],
                                         'identifier': reference, 'reason': reason})

    for entry in ([] if start == end else backfill['entries']):
        project = entry['project_id']
        if project not in report['projects']:
            continue
        # A future source work window is invalid as-of evidence, even if a
        # different weekly request would exclude it. Match monthly fail-closed
        # behavior rather than hiding a changed/invalid reviewed manifest.
        if entry['end'] and date.fromisoformat(entry['end']) > as_of.date():
            raise TimesheetError('future_invoice_work')
        allocation = entry.get('allocation_month')
        if allocation and (date.fromisoformat(allocation + '-01') > as_of.date() or
                           (not entry['end'] and date.fromisoformat(backfill['coverage']['through']) > as_of.date())):
            raise TimesheetError('future_invoice_work')
        undated = not entry['start']
        window = (backfill['coverage'] if undated else
                  {'start': entry['start'], 'through': entry['end']})
        if date.fromisoformat(window['through']) > as_of.date():
            raise TimesheetError('future_invoice_work')
        first, through = _date_window(window['start'], window['through'])
        if first >= end or through <= start:
            continue
        source = backfill['sources'][entry['source_id']]
        reference = source.get('invoice', source.get('reference'))
        relevant = overlapping_months(window['start'], window['through'])
        if undated:
            exception(project, reference, relevant,
                      f'Excluded from this {period_name} subtotal; source work dates are unconfirmed. '
                      f'A monthly allocation cannot establish {period_name} hours.')
            continue
        if first < start or through > end:
            exception(project, reference, relevant,
                      f'Excluded from this {period_name} subtotal; the source work period extends outside '
                      'the reporting cutoff or lacks sufficient time precision. Hours are not prorated.')
            continue
        # Group a fully contained aggregate under its actual final work month.
        # The original dates are preserved; estimated month assignments do not
        # become evidence of a weekly split or change the reviewed quantity.
        month = entry['end'][:7]
        report['rows'].append({
            'month': month, 'project_id': project, 'project': report['projects'][project],
            'builder_id': entry['builder_id'], 'builder': config.builders[entry['builder_id']]['name'],
            'issue_id': entry['id'], 'identifier': reference, 'title': entry['description'],
            'completed_at': entry['end'], 'work_start': entry['start'], 'work_end': entry['end'],
            'size': '', 'units': entry['units'],
            'source': 'reviewed_recorded_time' if source.get('kind') == 'recorded_time' else 'reviewed_invoice',
            'date_note': entry.get('date_note', ''), 'month_allocation': 'work_dates', 'allocation_note': '',
        })
        by_month[month]['units'] += entry['units']

    # Pending evidence only identifies a month. Every touched week in that
    # month remains qualified until supported dates establish a narrower scope.
    for pending in ([] if start == end else backfill['pending']):
        project = pending['project_id']
        if project in report['projects']:
            exception(project, pending['reference'], [month for month in pending['months'] if month in by_month],
                      pending['reason'])
    report['complete'] = not report['exceptions'] and not report['qualifications']
    report['basis'] = ('invoice_first_recorded_hours' if invoice_first == set(report['projects']) and
                       all(row.get('source') in {'reviewed_invoice', 'reviewed_recorded_time'} for row in report['rows']) else
                       'reviewed_invoices_and_completed_ticket_sizes')
    report['rows'].sort(key=lambda row: (row['month'], row['project'], row['completed_at'], row['identifier']))
    return report
