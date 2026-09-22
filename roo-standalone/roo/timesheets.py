"""Deterministic, cutoff-bound Studio timesheets. No payment or ticket writes."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from .payment_reminders import ReceiptStore

PROJECTS = {
    'c0727962-6495-4584-96b5-0ad5b6ab469d': '[Studio] Master App',
    '2b091441-a02e-4491-94a7-517fcafc7b05': '[Studio] Cybertest',
    '37307700-0330-4237-804b-655fef443f01': '[Studio] Project Ironman',
    '0d499cd9-44d9-4b46-a669-a04d3bdb1850': '[Studio] Project Acquire',
    '86815a40-f7d6-45dd-adbb-fec564248628': '[Studio] Aaron AI',
}
UNITS = {'XS': 1, 'S': 4, 'M': 8, 'L': 12, 'XL': 20}
LABELS = dict(zip(('Extra Small (XS)', 'Small (S)', 'Medium (M)', 'Large (L)', 'Extra Large (XL)'), UNITS))
RUBRIC = 'studio-upper-bound-v1'


class TimesheetError(ValueError):
    """Fixed reason code, safe to log without provider text."""


class SourceRetryError(TimesheetError):
    """A source-wide failure with an absolute earliest retry time."""

    def __init__(self, code, retry_at):
        super().__init__(code)
        self.retry_at = retry_at


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise TimesheetError('invalid_timestamp') from exc


def flag(env, name):
    value = env.get(name, 'false').lower()
    if value not in {'true', 'false'}:
        raise TimesheetError('invalid_flag')
    return value == 'true'


@dataclass(frozen=True)
class TimesheetConfig:
    enabled: bool
    scheduled: bool
    team: str
    organization: str
    recipient: str
    allowed_users: tuple[str, ...]
    builders: dict
    projects: dict
    directory: Path
    queue: Path
    first: date = date(2026, 9, 11)
    tz: ZoneInfo = ZoneInfo('Australia/Melbourne')
    source: str = 'linear'

    @classmethod
    def from_env(cls, env):
        enabled = flag(env, 'TIMESHEETS_ENABLED')
        scheduled = flag(env, 'TIMESHEETS_SCHEDULE_ENABLED')
        source = env.get('TIMESHEET_SOURCE', 'linear')
        if source not in {'linear', 'plane'}:
            raise TimesheetError('invalid_timesheet_source')
        first = date.fromisoformat(env.get('TIMESHEET_FIRST_CUTOFF', '2026-09-11'))
        if first.weekday() != 4:
            raise TimesheetError('cutoff_must_be_friday')
        team = env.get('TIMESHEET_SLACK_TEAM_ID', '')
        recipient = env.get('TIMESHEET_RECIPIENT_SLACK_ID', '')
        # Legacy requester allowlists cannot grant additional production access.
        allowed = (recipient,) if recipient else ()
        raw = json.loads(env.get('TIMESHEET_BUILDERS_JSON', '[]'))
        if not isinstance(raw, list):
            raise TimesheetError('invalid_builder_mappings')
        builders = {}
        slack_ids = set()
        for item in raw:
            linear_id = str(UUID(item.get('builder_id') or item['linear_user_id']))
            slack_id = item['slack_user_id']
            if not re.fullmatch(r'[UW][A-Z0-9]+', slack_id) or linear_id in builders or slack_id in slack_ids:
                raise TimesheetError('invalid_builder_mappings')
            builders[linear_id] = {'slack_user_id': slack_id, 'name': item.get('name') or slack_id}
            slack_ids.add(slack_id)
        projects = json.loads(env.get('TIMESHEET_PROJECTS_JSON', json.dumps(PROJECTS)))
        if not isinstance(projects, dict) or not projects or any(str(UUID(k)) != k or not isinstance(v, str) or not v for k, v in projects.items()):
            raise TimesheetError('invalid_projects')
        org = env.get('TIMESHEET_LINEAR_ORGANIZATION_ID', '')
        if enabled:
            if (not re.fullmatch(r'T[A-Z0-9]+', team) or not re.fullmatch(r'[UW][A-Z0-9]+', recipient)
                    or not allowed or any(not re.fullmatch(r'[UW][A-Z0-9]+', x) for x in allowed)):
                raise TimesheetError('invalid_recipient_configuration')
            org = str(UUID(org))
        return cls(enabled, scheduled, team, org, recipient, allowed, builders, projects,
                   Path(env.get('TIMESHEET_DATA_DIR', '/app/timesheets/data')),
                   Path(env.get('TIMESHEET_QUEUE_DIR', '/app/timesheets/queue')), first, source=source)

    def cutoff(self, day):
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if (day - self.first).days < 0 or (day - self.first).days % 14:
            raise TimesheetError('not_a_payment_cutoff')
        return datetime.combine(day, time(12), self.tz)

    @property
    def beginning(self):
        return self.cutoff(self.first) - timedelta(days=14)

    def latest(self, now):
        local = timestamp(now).astimezone(self.tz)
        n = (local.date() - self.first).days // 14
        end = datetime.combine(self.first + timedelta(days=14*n), time(12), self.tz)
        if end > local:
            end -= timedelta(days=14)
        return end


def empty_ledger():
    return {'version': 1, 'periods': {}, 'allocated': {}, 'deferred': {}, 'corrections': []}


class Ledger(ReceiptStore):
    def load(self):
        result = self.read('ledger')
        if result is None:
            return empty_ledger()
        if (not isinstance(result, dict) or result.get('version') != 1
                or any(not isinstance(result.get(k), dict) for k in ('periods', 'allocated', 'deferred'))
                or not isinstance(result.get('corrections'), list)):
            raise TimesheetError('invalid_ledger')
        return result

    def save(self, data):
        self.write('ledger', data)


def reconstruct(issue, history, at):
    """Reverse post-cutoff changes, then identify the completed state at cutoff."""
    at = timestamp(at)
    if timestamp(issue['createdAt']) > at:
        return None
    history = sorted(history, key=lambda h: (timestamp(h['createdAt']), h['id']))
    result = dict(issue)
    result['labels'] = {x['id']: x['name'] for x in issue['labels']}
    assignees = set(issue['assignee_ids']) if 'assignee_ids' in issue else None
    for h in reversed(history):
        when = timestamp(h['createdAt'])
        if h.get('time_precision_seconds') and when <= at < when + timedelta(seconds=h['time_precision_seconds']):
            raise TimesheetError('activity_cutoff_time_ambiguous')
        if timestamp(h['createdAt']) <= at:
            continue
        if assignees is not None:
            assignees.difference_update(h.get('addedAssigneeIds') or [])
            assignees.update(h.get('removedAssigneeIds') or [])
        for field in ('Assignee', 'Project', 'State'):
            old, new = h.get('from' + field), h.get('to' + field)
            # A nullable transition to/from an unassigned value is still a change.
            if h.get('from' + field + 'Id') is not None or h.get('to' + field + 'Id') is not None:
                if old is None and h.get('from' + field + 'Id') is not None:
                    raise TimesheetError('missing_history_reference')
                if (result.get(field.lower()) or {}).get('id') != h.get('to' + field + 'Id'):
                    raise TimesheetError('inconsistent_history_chain')
                result[field.lower()] = old
        if h.get('fromTitle') is not None:
            result['title'] = h['fromTitle']
        for label_id in h.get('addedLabelIds') or []:
            result['labels'].pop(label_id, None)
        removed = {x['id']: x['name'] for x in (h.get('removedLabels') or [])}
        for label_id in h.get('removedLabelIds') or []:
            if label_id not in removed:
                raise TimesheetError('missing_label_history')
            result['labels'][label_id] = removed[label_id]
    state = result.get('state')
    if not isinstance(state, dict) or not state.get('type'):
        raise TimesheetError('missing_state_history')
    completed = [timestamp(h['createdAt']) for h in history
                 if (h.get('toState') or {}).get('type') == 'completed'
                 and (h.get('fromState') or {}).get('type') != 'completed'
                 and timestamp(h['createdAt']) <= at]
    if issue.get('completedAt') and timestamp(issue['completedAt']) <= at:
        completed.append(timestamp(issue['completedAt']))
    if state['type'] == 'completed' and not completed:
        raise TimesheetError('missing_completion_history')
    result['completion'] = max(completed).isoformat() if completed else None
    result['first_completion'] = min(completed).isoformat() if completed else None
    if assignees is not None:
        result['assignee_count'] = len(assignees)
        result['assignee'] = {'id': next(iter(assignees))} if len(assignees) == 1 else None
    return result


def build_report(config, end, dataset, ledger, *, draft=False, generated_at=None):
    start = config.latest(end) if draft else end - timedelta(days=14)
    if start < config.beginning:
        start = config.beginning
    rows, exceptions, deferred = [], [], {}
    for item in dataset:
        original = item['issue']
        issue_id = original['id']
        if issue_id in ledger['allocated']:
            continue
        try:
            if item.get('error'):
                raise TimesheetError(item['error'])
            issue = reconstruct(original, item['history'], end)
            if issue is None or (issue.get('project') or {}).get('id') not in config.projects:
                continue
            if issue['state']['type'] != 'completed':
                continue
            if issue.get('trashed'):
                raise TimesheetError('trashed_issue_requires_review')
            completed = timestamp(issue['completion'])
            first = timestamp(issue['first_completion'])
            if first <= timestamp(config.beginning):
                continue
            if completed <= timestamp(start) and issue_id not in ledger['deferred']:
                continue
            if issue.get('assignee_count', 1) != 1:
                raise TimesheetError('exactly_one_assignee_required')
            assignee = (issue.get('assignee') or {}).get('id')
            if assignee not in config.builders:
                raise TimesheetError('builder_mapping_required')
            sizes = [LABELS[name] for name in issue['labels'].values() if name in LABELS]
            if len(sizes) != 1:
                raise TimesheetError('exactly_one_effort_label_required')
            size = sizes[0]
            builder = config.builders[assignee]
            project = issue['project']['id']
            rows.append({'issue_id': issue_id, 'identifier': original['identifier'], 'url': original['url'],
                         'title': issue['title'], 'project_id': project, 'project': config.projects[project],
                         'builder_id': assignee, 'builder': builder['name'], 'slack_user_id': builder['slack_user_id'],
                         'completed_at': completed.isoformat(), 'size': size, 'units': UNITS[size],
                         'source': original.get('source', 'linear'),
                         'source_issue_id': original.get('source_issue_id', issue_id),
                         'rubric': RUBRIC, 'kind': 'ticket', 'reason': '', 'reference': ''})
        except (TimesheetError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, TimesheetError) else 'incomplete_issue_evidence'
            exceptions.append({'issue_id': issue_id, 'identifier': original.get('identifier', ''),
                               'url': original.get('url', ''), 'reason': reason})
            deferred[issue_id] = reason
    for correction in ledger['corrections']:
        # An operator adjustment belongs to the next open cutoff, even when
        # historical periods are generated later in a backlog catch-up.
        if not correction.get('period') and timestamp(correction['recorded_at']) < timestamp(end):
            rows.append(dict(correction['row']))
    rows.sort(key=lambda r: (r['builder'], r['project'], r['identifier'], r['kind']))
    exceptions.sort(key=lambda x: x['identifier'])
    totals = {}
    for row in rows:
        key = row['builder_id']
        total = totals.setdefault(key, {'builder_id': key, 'builder': row['builder'],
                                       'slack_user_id': row['slack_user_id'], 'ticket_units': 0,
                                       'adjustment_units': 0, 'tickets': 0})
        total['ticket_units' if row['kind'] == 'ticket' else 'adjustment_units'] += row['units']
        total['tickets'] += row['kind'] == 'ticket'
    return {'period': (config.latest(end) + timedelta(days=14)).date().isoformat() if draft else end.date().isoformat(),
            'start': start.isoformat(), 'end': end.isoformat(), 'draft': draft, 'rubric': RUBRIC,
            'generated_at': timestamp(generated_at or datetime.now(timezone.utc)).isoformat(),
            'source': config.source,
            'projects': dict(config.projects), 'builders': config.builders,
            'rows': rows, 'totals': list(totals.values()), 'exceptions': exceptions,
            'deferred': deferred, 'complete': not exceptions and config.source != 'plane',
            'coverage_verified': config.source != 'plane'}


def hours(units):
    return f'{units / 4:.2f}'


def safe_cell(value):
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def csv_file(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([safe_cell(value) if isinstance(value, str) else value for value in row])
    return stream.getvalue()


def artifacts(report):
    prefix = ('draft-' if report['draft'] else '') + 'timesheet-' + report['period']
    rows = report['rows']
    result = {
        prefix + '-builders.csv': csv_file(
            ['period_start', 'period_end', 'builder', 'builder_id', 'slack_user_id', 'tickets',
             'size_based_hours', 'adjustment_hours', 'total_hours', 'report_unresolved_tickets'],
            [[report['start'], report['end'], t['builder'], t['builder_id'], t['slack_user_id'], t['tickets'],
              Decimal(hours(t['ticket_units'])), Decimal(hours(t['adjustment_units'])), Decimal(hours(t['ticket_units'] + t['adjustment_units'])),
              len(report['exceptions'])] for t in report['totals']]),
        prefix + '-tickets.csv': csv_file(
            ['period_start', 'period_end', 'project', 'issue', 'url', 'title', 'builder', 'builder_id',
             'completed_at', 'size', 'size_based_hours', 'rubric', 'kind', 'correction_reason', 'reference', 'source', 'source_issue_id'],
            [[report['start'], report['end'], r['project'], r['identifier'], r['url'], r['title'], r['builder'],
              r['builder_id'], r['completed_at'], r['size'], Decimal(hours(r['units'])), r['rubric'], r['kind'],
              r['reason'], r['reference'], r.get('source', 'linear'), r.get('source_issue_id', r['issue_id'])] for r in rows]),
    }
    if report['exceptions']:
        result[prefix + '-exceptions.csv'] = csv_file(['issue', 'url', 'reason'],
            [[x['identifier'], x['url'], x['reason']] for x in report['exceptions']])
    return result


def summary(report):
    from .payment_reminders import _escape
    title = 'Draft—values may change before Friday’s cutoff' if report['draft'] else 'Finalized timesheet'
    if report.get('source') == 'plane':
        title = 'Plane compatibility preview — incomplete coverage'
    result = [f'*{title}*', f"Period: {report['start']} through {report['end']}",
              'Basis: size-based hours. Included in a timesheet does not mean paid.']
    if report.get('source') == 'plane':
        result.append('PLANE COMPATIBILITY PREVIEW ONLY: archived work and migration coverage are not verified. Do not use for payment.')
    result.extend(f"• {_escape(t['builder'])}: {hours(t['ticket_units'] + t['adjustment_units'])} hours ({t['tickets']} tickets)"
                  for t in report['totals'])
    result.append(f"Total: {hours(sum(r['units'] for r in report['rows']))} hours")
    if report['exceptions']:
        result.append(f"Incomplete totals: {len(report['exceptions'])} unresolved tickets; see exceptions CSV.")
    elif not report['rows']:
        result.append('No eligible completed tickets in this period.')
    text = '\n'.join(result)
    if len(text) > 3500:
        text = '\n'.join(result[:3] + ['Builder breakdown is in the attached CSV.'] + result[-2:])
    return text


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
