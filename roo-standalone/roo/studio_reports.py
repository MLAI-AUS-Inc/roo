"""Private client reports: calendar-month, completed-ticket effort, no ledger writes."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from threading import Lock
from zoneinfo import ZoneInfo

from .payment_reminders import _escape
from .timesheets import LABELS, UNITS, TimesheetError, csv_file, reconstruct, timestamp

TZ = ZoneInfo('Australia/Melbourne')
_chart_lock = Lock()


def month_offset(month, offset):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}', month):
            raise ValueError
        day = datetime.strptime(month, '%Y-%m')
        index = day.year * 12 + day.month - 1 + offset
        return datetime(index // 12, index % 12 + 1, 1, tzinfo=TZ)
    except (ValueError, TypeError, OverflowError) as exc:
        raise TimesheetError('invalid_month') from exc


def resolve_period(params, now):
    """Relative periods are resolved in Melbourne, never in the model's timezone."""
    current = timestamp(now).astimezone(TZ).strftime('%Y-%m')
    month = params.get('month') or 'current'
    if month in {'current', 'last'}:
        month = month_offset(current, -1 if month == 'last' else 0).strftime('%Y-%m')
    start = month_offset(month, 0)
    count = params.get('months', 1)
    if type(count) is not int or not 1 <= count <= 12:
        raise TimesheetError('invalid_month_count')
    end = month_offset(month, count)
    if start > timestamp(now) or month_offset(month, count - 1) > timestamp(now):
        raise TimesheetError('future_month')
    action = params.get('action', 'summary')
    if action not in {'summary', 'detailed'}:
        raise TimesheetError('invalid_report_action')
    return {'month': month, 'months': count, 'action': action}, start, end


def allowance_units(value):
    try:
        number = Decimal(str(value)) * 4
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (ValueError, InvalidOperation) as exc:
        raise TimesheetError('invalid_monthly_allowance') from exc


def display_hours(units):
    return format(Decimal(units) / 4, 'f').rstrip('0').rstrip('.') if units % 4 else str(units // 4)


def build_client_report(config, client, selector, dataset, now):
    selector, start, end = resolve_period(selector, now)
    as_of = timestamp(now)
    projects = {key: config.projects[key] for key in client['project_ids']}
    monthly = []
    for index in range(selector['months']):
        key = month_offset(selector['month'], index).strftime('%Y-%m')
        monthly.append({'month': key, 'units': 0, 'unresolved': 0,
                        'allowance_units': allowance_units(client.get('monthly_allowances', {}).get(
                            key, client.get('monthly_hours', 40)))})
    by_month = {item['month']: item for item in monthly}
    rows, exceptions, seen = [], [], set()
    for item in dataset:
        original = item['issue']
        if original['id'] in seen:
            raise TimesheetError('duplicate_issue_evidence')
        seen.add(original['id'])
        # A broken history could hide a project move or earlier completion. Never
        # expose a row or an apparent zero total when ownership cannot be proved.
        if item.get('error'):
            raise TimesheetError('source_evidence_incomplete')
        history = item['history']
        completions = [timestamp(h['createdAt']) for h in history
                       if (h.get('toState') or {}).get('type') == 'completed'
                       and (h.get('fromState') or {}).get('type') != 'completed']
        if original.get('completedAt'):
            completions.append(timestamp(original['completedAt']))
        future_completion = any(at > as_of for at in completions)
        completions = [at for at in completions if at <= as_of]
        if not completions:
            if (original.get('state') or {}).get('type') == 'completed' and not future_completion:
                raise TimesheetError('source_evidence_incomplete')
            continue
        completed = min(completions)  # Reopen/recomplete never consumes hours twice.
        if not start <= completed < end:
            continue
        try:
            issue = reconstruct(original, history, completed)
        except (TimesheetError, KeyError, TypeError) as exc:
            raise TimesheetError('source_evidence_incomplete') from exc
        if issue is None:
            raise TimesheetError('source_evidence_incomplete')
        project_id = (issue.get('project') or {}).get('id')
        if project_id not in projects:
            continue
        month = completed.astimezone(TZ).strftime('%Y-%m')
        try:
            if issue.get('trashed') or issue['state']['type'] != 'completed':
                raise TimesheetError('completion_needs_review')
            builder_id = (issue.get('assignee') or {}).get('id')
            if issue.get('assignee_count', 1) != 1 or builder_id not in config.builders:
                raise TimesheetError('builder_mapping_required')
            sizes = [LABELS[name] for name in issue['labels'].values() if name in LABELS]
            if len(sizes) != 1:
                raise TimesheetError('exactly_one_effort_label_required')
            units = UNITS[sizes[0]]
            rows.append({'month': month, 'project_id': project_id, 'project': projects[project_id],
                         'builder_id': builder_id, 'builder': config.builders[builder_id]['name'],
                         'issue_id': original['id'], 'identifier': original['identifier'],
                         'title': issue['title'], 'completed_at': completed.astimezone(TZ).isoformat(),
                         'size': sizes[0], 'units': units})
            by_month[month]['units'] += units
        except TimesheetError as exc:
            by_month[month]['unresolved'] += 1
            exceptions.append({'month': month, 'project': projects[project_id],
                               'identifier': original['identifier'], 'reason': str(exc)})
    rows.sort(key=lambda row: (row['month'], row['project'], row['completed_at'], row['identifier']))
    return {'selector': selector, 'client': client['name'], 'projects': projects,
            'monthly': monthly, 'rows': rows, 'exceptions': exceptions,
            'generated_at': as_of.astimezone(TZ).isoformat(), 'complete': not exceptions,
            'basis': 'completed_ticket_size_hours'}


def month_label(key):
    return month_offset(key, 0).strftime('%B %Y')


def summary(report):
    detailed = report['selector']['action'] == 'detailed'
    parts = [f"*Your Studio hours · {_escape(report['client'])}*"]
    for month in report['monthly']:
        used, allowance = month['units'], month['allowance_units']
        parts.append(f"\n*{month_label(month['month'])} — {display_hours(used)} of {display_hours(allowance)} hours used*")
        if month['unresolved']:
            parts.append(f"⚠️ Partial total: {month['unresolved']} completed work items need review. Remaining hours aren't confirmed yet.")
        elif used > allowance:
            parts.append(f"*{display_hours(used - allowance)} hours over your allowance.*")
        else:
            parts.append(f"*{display_hours(allowance - used)} hours remaining* · {used / allowance:.0%} used")
        parts.append('Shared monthly allowance across all your projects.')
        selected = [row for row in report['rows'] if row['month'] == month['month']]
        for project_id, project_name in report['projects'].items():
            rows = [row for row in selected if row['project_id'] == project_id]
            builders = {}
            for row in rows:
                key = (row['builder_id'], row['builder'])
                builders[key] = builders.get(key, 0) + row['units']
            parts.append(f"• *{_escape(project_name)}: {display_hours(sum(row['units'] for row in rows))}h*")
            if builders:
                parts.append('  ' + ' · '.join(f'{_escape(name)} {display_hours(units)}h'
                    for (_, name), units in sorted(builders.items(), key=lambda pair: (-pair[1], pair[0]))))
            else:
                parts.append('  No counted completed work.')
    parts.append('\nHours are based on completed-ticket sizes; work in progress and clocked time are not included.')
    parts.append('Updated ' + timestamp(report['generated_at']).astimezone(TZ).strftime('%d %b %Y, %I:%M %p %Z') + '.')
    if detailed:
        parts.append('The attached breakdown lists every counted work item, completion date, builder and hours, grouped by month and project.')
    else:
        selector = report['selector']
        parts.append(f'Ask “Studio hours detailed for {selector["month"]}'
                     + (f' for {selector["months"]} months' if selector['months'] > 1 else '')
                     + '” for the full work breakdown.')
    return '\n'.join(parts)


def detail_messages(report):
    lines = ['*Work breakdown*', 'Each entry shows the full ticket’s size-based hours; no hourly activity log is inferred.']
    previous = None
    for row in report['rows']:
        group = (row['month'], row['project'])
        if group != previous:
            lines.append(f"\n*{month_label(row['month'])} · {_escape(row['project'])}*")
            previous = group
        lines.append(f"• {row['completed_at'][:10]} · {_escape(row['builder'])} · *{display_hours(row['units'])}h*"
                     f" — {_escape(row['title'])} ({_escape(row['identifier'])})")
    if not report['rows']:
        lines.append('No counted completed work in this period.')
    return split_messages('\n'.join(lines))


def split_messages(text, limit=3500):
    chunks, current = [], ''
    for line in text.splitlines():
        # Labels and task titles are untrusted, and may exceed a Slack message.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ''
        current += ('\n' if current else '') + line
    if current:
        chunks.append(current)
    return chunks


def artifacts(report):
    result = {}
    if report['selector']['action'] == 'detailed':
        result['studio-hours-work.csv'] = csv_file(
            ['month', 'project', 'builder', 'completed_at_melbourne', 'issue', 'work', 'size', 'size_based_hours'],
            [[r['month'], r['project'], r['builder'], r['completed_at'], r['identifier'], r['title'],
              r['size'], Decimal(r['units']) / 4] for r in report['rows']])
    if report['exceptions']:
        result['studio-hours-unresolved.csv'] = csv_file(['month', 'project', 'issue', 'reason'],
            [[r['month'], r['project'], r['identifier'], r['reason']] for r in report['exceptions']])
    return result


def render_chart(report):
    """Local chart: client data never goes to a third-party chart service."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import textwrap

    with _chart_lock:
        monthly = report['monthly']
        many = len(monthly) > 1
        project_ids = list(report['projects'])
        figure = Figure(figsize=(10, 4.8 if many else max(4.8, 2.8 + len(project_ids) * .65)), dpi=150)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left=.34 if not many else .10, right=.92, top=.76, bottom=.22)
        figure.text(.06, .93, 'Your Studio hours', fontsize=22, weight='bold', color='#142c3a')
        if many:
            labels = [month_offset(m['month'], 0).strftime('%b %Y') for m in monthly]
            values = [m['units'] / 4 for m in monthly]
            axes.bar(labels, values, color='#147d92', width=.55)
            axes.plot(labels, [m['allowance_units'] / 4 for m in monthly], color='#ad5514',
                      marker='_', linestyle='--', label='Monthly allowance')
            axes.legend(frameon=False)
            axes.set_ylabel('Hours used')
            axes.tick_params(axis='x', labelrotation=30 if len(monthly) > 6 else 0)
            for index, value in enumerate(values):
                axes.annotate(f'{value:g}h', (index, value), xytext=(0, 5),
                              textcoords='offset points', ha='center')
            axes.set_ylim(0, max([1, *values, *[m['allowance_units'] / 4 for m in monthly]]) * 1.2)
            subtitle = f"{labels[0]} – {labels[-1]} · one shared allowance each month"
        else:
            month = monthly[0]
            used, allocated = month['units'], month['allowance_units']
            subtitle = f"{month_label(month['month'])} · {display_hours(used)} of {display_hours(allocated)} hours used"
            values = [sum(r['units'] for r in report['rows'] if r['project_id'] == key) / 4 for key in project_ids]
            names = ['\n'.join(textwrap.wrap(report['projects'][key], 29)) for key in project_ids]
            axes.barh(range(len(names)), values, color='#147d92', height=.5)
            axes.set_yticks(range(len(names)), names)
            axes.invert_yaxis()
            axes.set_xlabel('Hours used by project')
            axes.set_xlim(0, max([1, *values]) * 1.23)
            for index, value in enumerate(values):
                axes.text(value + max([1, *values]) * .025, index, f'{value:g}h', va='center', weight='bold')
        figure.text(.06, .845, subtitle, fontsize=12, color='#334b58')
        for spine in axes.spines.values():
            spine.set_visible(False)
        axes.tick_params(length=0, pad=8)
        figure.text(.06, .065, 'Completed-ticket size hours · excludes work in progress'
                    + ('\nPartial totals: some completed work needs review.' if not report['complete'] else ''),
                    fontsize=10, color='#465d67')
        with BytesIO() as output:
            canvas.print_png(output)
            return output.getvalue()
