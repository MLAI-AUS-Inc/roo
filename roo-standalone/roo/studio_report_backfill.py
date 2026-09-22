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
        if (value['version'] != 1 or value['team'] != config.team
                or value['organization'] != config.organization):
            raise ValueError
        sources = value['sources']
        if (not isinstance(sources, dict) or not isinstance(value['entries'], list)
                or not isinstance(value['pending'], list) or len(value['entries']) > 10000):
            raise ValueError
        invoices, hashes = set(), set()
        totals = {}
        for source_id, source in sources.items():
            _text(source_id)
            _text(source['invoice'])
            _text(source['message_id'])
            if not re.fullmatch('[a-f0-9]{64}', source['sha256']):
                raise ValueError
            invoice_key = (source['builder_id'], source['invoice'])
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
            start, end = date.fromisoformat(entry['start']), date.fromisoformat(entry['end'])
            # Never assign a cross-month aggregate to its invoice/payment month.
            if start > end or start.strftime('%Y-%m') != end.strftime('%Y-%m'):
                raise ValueError
            units = Decimal(entry['hours']) * 4
            if not units.is_finite() or units <= 0 or units != units.to_integral_value():
                raise ValueError
            entry['units'] = int(units)
            entry['month'] = start.strftime('%Y-%m')
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


def apply_backfill(report, backfill, config, now):
    if not backfill:
        return report
    by_month = {month['month']: month for month in report['monthly']}
    for entry in backfill['entries']:
        project, month = entry['project_id'], entry['month']
        if project not in report['projects'] or month not in by_month:
            continue
        if date.fromisoformat(entry['end']) > now.date():
            raise TimesheetError('future_invoice_work')
        source = backfill['sources'][entry['source_id']]
        report['rows'].append({
            'month': month, 'project_id': project, 'project': report['projects'][project],
            'builder_id': entry['builder_id'], 'builder': config.builders[entry['builder_id']]['name'],
            'issue_id': entry['id'], 'identifier': source['invoice'], 'title': entry['description'],
            'completed_at': entry['end'], 'work_start': entry['start'], 'work_end': entry['end'],
            'size': '', 'units': entry['units'], 'source': 'reviewed_invoice',
        })
        by_month[month]['units'] += entry['units']
    for pending in backfill['pending']:
        if pending['project_id'] not in report['projects']:
            continue
        for month in pending['months']:
            if month not in by_month:
                continue
            by_month[month]['unresolved'] += 1
            report['exceptions'].append({'month': month, 'project': report['projects'][pending['project_id']],
                'identifier': pending['reference'], 'reason': pending['reason']})
    report['complete'] = not report['exceptions']
    report['basis'] = 'reviewed_invoices_and_completed_ticket_sizes'
    report['rows'].sort(key=lambda r: (r['month'], r['project'], r['completed_at'], r['identifier']))
    return report
