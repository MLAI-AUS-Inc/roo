"""Reviewed agreement terms and deterministic allowance cycles for task reports.

Terms live only in the private owner's configuration. A report selector cannot
set them. Calendar usage and contract balances deliberately have separate bounds.
"""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from zoneinfo import ZoneInfo

from .timesheets import TimesheetError, flag, timestamp

TZ = ZoneInfo('Australia/Melbourne')


def hours(value):
    value = Decimal(value)
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def quantity(value, *, positive=False):
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or (positive and not number)
                or number * 4 != (number * 4).to_integral_value()):
            raise ValueError
        return number
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TimesheetError('invalid_contract_terms') from exc


def commencement(terms):
    try:
        raw = terms['commencement_date']
        if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError
        return datetime.combine(date.fromisoformat(raw), time.min, TZ)
    except (KeyError, ValueError, TypeError) as exc:
        raise TimesheetError('invalid_contract_terms') from exc


def validate_terms(client):
    terms = client.get('contract_terms')
    if terms is None:
        return
    required = {'version', 'project_ids', 'commencement_date', 'renewal', 'included_hours',
                'rollover_percent', 'rollover_cap_hours', 'rollover_cycles', 'rollover_first', 'source'}
    if (not isinstance(terms, dict) or set(terms) != required
            or type(terms['version']) is not int or terms['version'] != 1
            or client.get('hours_source') != 'linear' or 'report_client_ids' in client
            or terms['renewal'] != 'monthly' or terms['rollover_first'] is not True
            or type(terms['rollover_cycles']) is not int or terms['rollover_cycles'] != 1
            or type(terms['rollover_percent']) is not int or not 0 <= terms['rollover_percent'] <= 100
            or not isinstance(terms['project_ids'], list)
            or any(not isinstance(key, str) for key in terms['project_ids'])
            or len(terms['project_ids']) != len(set(terms['project_ids']))
            or set(terms['project_ids']) != set(client['project_ids'])):
        raise TimesheetError('invalid_contract_terms')
    commencement(terms)
    quantity(terms['included_hours'], positive=True)
    quantity(terms['rollover_cap_hours'])
    source = terms['source']
    if (not isinstance(source, dict) or set(source) != {
            'drive_file_id', 'sha256', 'signed_at', 'reviewed_at', 'reviewed_by'}
            or not isinstance(source['drive_file_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', source['drive_file_id'])
            or not isinstance(source['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', source['sha256'])
            or not isinstance(source['reviewed_by'], str)
            or not re.fullmatch(r'[UW][A-Z0-9]+', source['reviewed_by'])):
        raise TimesheetError('invalid_contract_terms')
    try:
        if timestamp(source['signed_at']) > timestamp(source['reviewed_at']):
            raise TimesheetError('invalid_contract_terms')
    except (TimesheetError, TypeError) as exc:
        raise TimesheetError('invalid_contract_terms') from exc


def validate_contract_policy(clients, env):
    """Future owners require reviewed terms; explicitly exempt owners stay legacy."""
    try:
        exempt = json.loads(env.get('STUDIO_REPORTS_CONTRACT_EXEMPT_CLIENT_IDS_JSON', '[]'))
    except (ValueError, TypeError) as exc:
        raise TimesheetError('invalid_contract_exemptions') from exc
    if (not isinstance(exempt, list) or any(not isinstance(key, str) for key in exempt)
            or len(exempt) != len(set(exempt))
            or any(key not in clients or 'report_client_ids' in clients[key] for key in exempt)):
        raise TimesheetError('invalid_contract_exemptions')
    required = flag(env, 'STUDIO_REPORTS_REQUIRE_CONTRACT_TERMS')
    projects = {}
    for actor, client in clients.items():
        validate_terms(client)
        if 'report_client_ids' in client:
            continue
        terms = client.get('contract_terms')
        if actor in exempt and terms is not None:
            raise TimesheetError('contract_terms_on_exempt_client')
        if required and actor not in exempt and terms is None:
            raise TimesheetError('contract_terms_required')
        for project in client['project_ids']:
            if project in projects and projects[project] != terms:
                raise TimesheetError('conflicting_project_contracts')
            projects[project] = terms


def anniversary(start, index):
    """Clamp short months without losing the original day (31 Jan -> 28 Feb -> 31 Mar)."""
    month = start.year * 12 + start.month - 1 + index
    year, month = month // 12, month % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, monthrange(year, month)[1]))


def build_cycles(terms, rows, exceptions, *, scale, visible_start, cutoff, include_cutoff=False):
    start = commencement(terms)
    included = quantity(terms['included_hours'], positive=True)
    cap = quantity(terms['rollover_cap_hours'])
    rate = Decimal(terms['rollover_percent']) / 100
    carry = Decimal(0)
    cycles = []
    for index in range(121):
        begin, end = anniversary(start, index), anniversary(start, index + 1)
        if begin > cutoff or (begin == cutoff and not include_cutoff):
            break
        if index == 120:
            raise TimesheetError('contract_history_too_long')
        work = [row for row in rows if begin <= timestamp(row['completed_at']) < end
                and (timestamp(row['completed_at']) < cutoff
                     or include_cutoff and timestamp(row['completed_at']) == cutoff)]
        unresolved = [row for row in exceptions if begin <= timestamp(row['completed_at']) < end]
        used = sum((Decimal(row['units']) / scale for row in work), Decimal(0))
        complete = carry is not None and not unresolved
        available = included + carry if carry is not None else None
        base_unused = max(Decimal(0), included - max(Decimal(0), used - carry)) if complete else None
        outgoing = min(cap, base_unused * rate) if complete else None
        # Unused incoming rollover expires here. Only this cycle's unused base
        # allocation can roll forward, and incoming rollover is consumed first.
        expired = max(Decimal(0), carry - used) if complete else None
        points = []
        if available is not None:
            running = available
            points.append({'at': begin.isoformat(), 'hours': hours(running)})
            daily = {}
            for row in work:
                day = timestamp(row['completed_at']).astimezone(TZ).date()
                daily[day] = daily.get(day, Decimal(0)) + Decimal(row['units']) / scale
            for day, amount in sorted(daily.items()):
                running -= amount
                points.append({'at': datetime.combine(day, time.min, TZ).isoformat(), 'hours': hours(running)})
            points.append({'at': min(end, cutoff).isoformat(), 'hours': hours(running)})
        if end > visible_start:
            cycles.append({
                'start': begin.isoformat(), 'end': end.isoformat(),
                'as_of': min(end, cutoff).isoformat(), 'included_hours': hours(included),
                'carry_in_hours': hours(carry) if carry is not None else None,
                'used_hours': hours(used),
                'remaining_hours': hours(available - used) if complete else None,
                'base_unused_hours': hours(base_unused) if complete else None,
                'expired_rollover_hours': hours(expired) if complete else None,
                'rollover_out_hours': hours(outgoing) if complete else None,
                'complete': complete, 'unresolved': len(unresolved), 'points': points,
            })
        carry = outgoing
    return {
        'commencement_date': terms['commencement_date'], 'renewal_day': start.day,
        'rollover_percent': terms['rollover_percent'], 'rollover_cap_hours': hours(cap),
        'cycles': cycles, 'complete': all(cycle['complete'] for cycle in cycles),
        'exceptions': exceptions,
    }


def cycle_summary(report):
    contract = report.get('contract_allowance')
    if contract is None:
        return []
    parts = ['\n*Contract allowance · estimated from completed tasks*']
    if not contract['cycles']:
        parts.append(f"Allowance starts {contract['commencement_date']}; earlier work is history only.")
    for cycle in contract['cycles']:
        start, end = timestamp(cycle['start']).astimezone(TZ), timestamp(cycle['end']).astimezone(TZ)
        label = f'{start:%d %b %Y} – {end - timedelta(days=1):%d %b %Y}'
        parts.append(f"*{label}* · {cycle['included_hours']}h new allowance"
                     + (f" + {cycle['carry_in_hours']}h rollover" if cycle['carry_in_hours'] is not None else
                        ' · incoming rollover unconfirmed'))
        parts.append(f"{cycle['used_hours']}h counted in this allowance cycle; "
                     + (f"*{cycle['remaining_hours']}h estimated remaining*." if cycle['complete'] else
                        'remaining hours and rollover are unconfirmed because earlier or current tasks need review.'))
        parts.append(f'Next renewal: {end:%d %b %Y}.')
    parts.append(f"Monthly renewal on day {contract['renewal_day']} (last day for shorter months). "
                 f"{contract['rollover_percent']}% of unused base hours rolls into the next cycle, "
                 f"capped at {contract['rollover_cap_hours']}h. Rollover is used first and expires after that cycle.")
    parts.append('Calendar-month usage is separate from the allowance cycle. '
                 'Pre-commencement tasks do not consume this allowance. These are task estimates, not clocked time.')
    return parts


def cycle_artifacts(report):
    from .timesheets import csv_file
    contract = report.get('contract_allowance')
    if contract is None:
        return {}
    fields = ['start', 'end', 'as_of', 'included_hours', 'carry_in_hours', 'used_hours',
              'remaining_hours', 'base_unused_hours', 'expired_rollover_hours', 'rollover_out_hours', 'complete']
    result = {'studio-hours-allowance-cycles.csv': csv_file(
        ['cycle_start_inclusive', 'cycle_end_exclusive', 'usage_cutoff', *fields[3:]],
        [[cycle[field] if cycle[field] is not None else '' for field in fields] for cycle in contract['cycles']])}
    if contract['exceptions']:
        fields = ['completed_at', 'project', 'identifier', 'reason']
        result['studio-hours-cycle-unresolved.csv'] = csv_file(
            fields, [[row[field] for field in fields] for row in contract['exceptions']])
    return result
