"""Reviewed invoice evidence, private to the client report worker (never payroll)."""
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from .timesheets import TimesheetError


def _text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError
    return value


def validate_backfill(value, config):
    """Reject the entire manifest if its provenance, scope or arithmetic is invalid."""
    try:
        version = value['version']
        if (type(version) is not int or version not in {1, 2} or value['team'] != config.team
                or value['organization'] != config.organization):
            raise ValueError
        scale = 100 if version == 2 else 4
        if version == 2:
            coverage = value['coverage']
            beginning, through = date.fromisoformat(coverage['start']), date.fromisoformat(coverage['through'])
            if beginning > through:
                raise ValueError
            projects = value['invoice_first_projects']
            if (not isinstance(projects, list) or any(not isinstance(p, str) or p not in config.projects for p in projects)
                    or len(set(projects)) != len(projects)):
                raise ValueError
            for note in value.get('qualifications', []):
                if note['project_id'] not in projects:
                    raise ValueError
                _text(note['reference'])
                _text(note['reason'])
        sources = value['sources']
        if (not isinstance(sources, dict) or not isinstance(value['entries'], list)
                or not isinstance(value['pending'], list) or len(value['entries']) > 10000):
            raise ValueError
        invoices, hashes = set(), set()
        totals = {}
        for source_id, source in sources.items():
            _text(source_id)
            kind = source.get('kind', 'invoice')
            if kind not in ({'invoice', 'recorded_time'} if version == 2 else {'invoice'}):
                raise ValueError
            _text(source['invoice'] if kind == 'invoice' else source['reference'])
            _text(source['message_id'])
            if not re.fullmatch('[a-f0-9]{64}', source['sha256']):
                raise ValueError
            invoice_key = (source['builder_id'], kind, source.get('invoice') if kind == 'invoice' else source['reference'])
            if source['builder_id'] not in config.builders or invoice_key in invoices or source['sha256'] in hashes:
                raise ValueError
            invoices.add(invoice_key)
            hashes.add(source['sha256'])
            billed = Decimal(source['hours'])
            if not billed.is_finite() or billed <= 0:
                raise ValueError
            totals[source_id] = Decimal(0)
        seen, covered = set(), {}
        for entry in value['entries']:
            key = _text(entry['id'])
            # An invoice line has one canonical ID, regardless of email copies.
            if key in seen or entry['source_id'] not in sources:
                raise ValueError
            if key != entry['source_id'] + ':' + _text(entry['line_id']):
                raise ValueError
            seen.add(key)
            project, builder = entry['project_id'], entry['builder_id']
            if project not in config.projects or builder not in config.builders:
                raise ValueError
            if builder != sources[entry['source_id']]['builder_id'] or not isinstance(entry['hours'], str):
                raise ValueError
            _text(entry['description'])
            _text(entry['review_note'])
            undated = version == 2 and entry.get('date_status') == 'unallocated'
            if undated:
                if entry['start'] is not None or entry['end'] is not None:
                    raise ValueError
                _text(entry['date_note'])
                month = ''
            else:
                if entry.get('date_status', 'work_period') != 'work_period':
                    raise ValueError
                start, end = date.fromisoformat(entry['start']), date.fromisoformat(entry['end'])
                if start > end or (version == 1 and start.strftime('%Y-%m') != end.strftime('%Y-%m')):
                    raise ValueError
                month = start.strftime('%Y-%m') if start.strftime('%Y-%m') == end.strftime('%Y-%m') else ''
            units = Decimal(entry['hours']) * scale
            if not units.is_finite() or units <= 0 or units != units.to_integral_value():
                raise ValueError
            entry['units'] = int(units)
            entry['month'] = month
            if 'allocation_month' in entry or 'allocation_note' in entry:
                # A reviewed reporting estimate never overwrites source dates.
                if version != 2 or month or project not in value['invoice_first_projects']:
                    raise ValueError
                allocation = entry['allocation_month']
                if not isinstance(allocation, str) or not re.fullmatch(r'\d{4}-\d{2}', allocation):
                    raise ValueError
                date.fromisoformat(allocation + '-01')
                if not beginning.strftime('%Y-%m') <= allocation <= through.strftime('%Y-%m'):
                    raise ValueError
                _text(entry['allocation_note'])
            totals[entry['source_id']] += Decimal(entry['hours'])
            if not isinstance(entry['replaces'], list):
                raise ValueError
            for issue_id in entry['replaces']:
                _text(issue_id)
                scope = (project, builder)
                if issue_id in covered and covered[issue_id] != scope:
                    raise ValueError
                covered[issue_id] = scope
        if any(total > Decimal(sources[key]['hours']) for key, total in totals.items()):
            raise ValueError
        for pending in value['pending']:
            if pending['project_id'] not in config.projects:
                raise ValueError
            _text(pending['reference'])
            _text(pending['reason'])
            if not pending['months']:
                raise ValueError
            for month in pending['months']:
                date.fromisoformat(month + '-01')
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise TimesheetError('invalid_invoice_backfill') from exc
    return value


def load_backfill(path, config):
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise TimesheetError('invoice_backfill_unavailable') from exc
    return validate_backfill(value, config)


def replacements(backfill):
    """Explicit matches only; never guess that similar titles are the same work."""
    return {issue: (entry['project_id'], entry['builder_id'])
            for entry in (backfill or {}).get('entries', []) for issue in entry['replaces']}


def history_beginning(backfill, projects):
    if not backfill:
        raise TimesheetError('all_time_coverage_not_configured')
    dates = [entry['start'] for entry in backfill['entries'] if entry['project_id'] in projects and entry['start']]
    if set(projects) & set(backfill.get('invoice_first_projects', [])):
        dates.append(backfill['coverage']['start'])
    if not dates:
        raise TimesheetError('all_time_coverage_not_configured')
    return min(dates)[:7]


def apply_backfill(report, backfill, config, now):
    if not backfill:
        return report
    by_month = {month['month']: month for month in report['monthly']}
    first, last = min(by_month), max(by_month)
    report['unallocated_units'] = 0
    report['estimated_month_units'] = 0
    invoice_first = set(backfill.get('invoice_first_projects', [])) & set(report['projects'])
    report['invoice_first_projects'] = sorted(invoice_first)
    report['qualifications'] = [note for note in backfill.get('qualifications', []) if note['project_id'] in report['projects']]

    def exception(entry, months, reason):
        source = backfill['sources'][entry['source_id']]
        for month in months:
            by_month[month]['unresolved'] += 1
            report['exceptions'].append({'month': month, 'project': report['projects'][entry['project_id']],
                'identifier': source.get('invoice', source.get('reference')), 'reason': reason})

    for entry in backfill['entries']:
        project = entry['project_id']
        allocation = entry.get('allocation_month')
        month = allocation or entry['month']
        if project not in report['projects']:
            continue
        if entry['end'] and date.fromisoformat(entry['end']) > now.date():
            raise TimesheetError('future_invoice_work')
        if allocation:
            if (date.fromisoformat(allocation + '-01') > now.date() or
                    (not entry['end'] and date.fromisoformat(backfill['coverage']['through']) > now.date())):
                raise TimesheetError('future_invoice_work')
        if not month:
            window = backfill['coverage'] if not entry['start'] else {'start': entry['start'], 'through': entry['end']}
            if date.fromisoformat(window['through']) > now.date():
                raise TimesheetError('future_invoice_work')
            relevant = [key for key in by_month if window['start'][:7] <= key <= window['through'][:7]]
            if not relevant:
                continue
            contained = first <= window['start'][:7] and last >= window['through'][:7]
            reason = ('Included in period total; work month unallocated.' if contained else
                      'Excluded from this subtotal; hours span or may belong outside the requested months.')
            exception(entry, relevant, reason)
            if not contained:
                continue
        elif month not in by_month:
            continue
        source = backfill['sources'][entry['source_id']]
        report['rows'].append({
            'month': month, 'project_id': project, 'project': report['projects'][project],
            'builder_id': entry['builder_id'], 'builder': config.builders[entry['builder_id']]['name'],
            'issue_id': entry['id'], 'identifier': source.get('invoice', source.get('reference')), 'title': entry['description'],
            'completed_at': entry['end'] or '', 'work_start': entry['start'] or '', 'work_end': entry['end'] or '',
            'size': '', 'units': entry['units'],
            'source': 'reviewed_recorded_time' if source.get('kind') == 'recorded_time' else 'reviewed_invoice',
            'date_note': entry.get('date_note', ''),
            'month_allocation': 'estimated' if allocation else 'unallocated' if not month else 'work_dates',
            'allocation_note': entry.get('allocation_note', ''),
        })
        if month:
            by_month[month]['units'] += entry['units']
            if allocation:
                by_month[month]['estimated_units'] = by_month[month].get('estimated_units', 0) + entry['units']
                report['estimated_month_units'] += entry['units']
        else:
            report['unallocated_units'] += entry['units']
    for pending in backfill['pending']:
        if pending['project_id'] not in report['projects']:
            continue
        for month in pending['months']:
            if month not in by_month:
                continue
            by_month[month]['unresolved'] += 1
            report['exceptions'].append({'month': month, 'project': report['projects'][pending['project_id']],
                'identifier': pending['reference'], 'reason': pending['reason']})
    report['complete'] = not report['exceptions'] and not report['qualifications'] and not report['estimated_month_units']
    report['basis'] = ('invoice_first_recorded_hours' if invoice_first == set(report['projects']) and
                       all(row.get('source') in {'reviewed_invoice', 'reviewed_recorded_time'} for row in report['rows']) else
                       'reviewed_invoices_and_completed_ticket_sizes')
    report['rows'].sort(key=lambda r: (r['month'], r['project'], r['completed_at'], r['identifier']))
    return report
