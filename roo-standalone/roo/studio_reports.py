"""Private client reports: calendar-month, completed-ticket effort, no ledger writes."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from threading import Lock
from zoneinfo import ZoneInfo

from .payment_reminders import _escape
from .studio_report_backfill import apply_backfill, history_beginning, replacements
from .studio_report_clients import client_key
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


def resolve_period(params, now, *, beginning=None):
    """Relative periods are resolved in Melbourne, never in the model's timezone."""
    current = timestamp(now).astimezone(TZ).strftime('%Y-%m')
    count = params.get('months', 1)
    if type(count) is not int or not 1 <= count <= (120 if params.get('project_to_date') is True else 12):
        raise TimesheetError('invalid_month_count')
    month = params.get('month') or ('recent' if count > 1 else 'current')
    all_time = month == 'all' or params.get('project_to_date') is True
    if month == 'all':
        if beginning is None:
            # Public Roo has no private coverage data. The worker resolves this.
            selector, _, _ = resolve_period({**params, 'month': 'current', 'months': 1}, now)
            return {**selector, 'month': 'all'}, None, None
        month = beginning
        day = month_offset(month, 0)
        today = timestamp(now).astimezone(TZ)
        count = (today.year - day.year) * 12 + today.month - day.month + 1
        if not 1 <= count <= 120:
            raise TimesheetError('invalid_month_count')
    if month in {'current', 'last', 'recent', 'last_complete'}:
        offset = {'current': 0, 'last': -1, 'recent': 1 - count, 'last_complete': -count}[month]
        month = month_offset(current, offset).strftime('%Y-%m')
    start = month_offset(month, 0)
    end = month_offset(month, count)
    if start > timestamp(now) or month_offset(month, count - 1) > timestamp(now):
        raise TimesheetError('future_month')
    action = params.get('action', 'summary')
    if action not in {'summary', 'detailed'}:
        raise TimesheetError('invalid_report_action')
    selector = {'month': month, 'months': count, 'action': action}
    if all_time:
        selector['project_to_date'] = True
    if 'client' in params:
        client_key(params['client'])
        selector['client'] = ' '.join(params['client'].split())
    return selector, start, end


def allowance_units(value, scale=4):
    # An explicit null is an overview without a shared client budget. Omitted
    # configuration still defaults to 40; request parameters cannot change it.
    if value is None:
        return None
    try:
        number = Decimal(str(value)) * scale
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (ValueError, InvalidOperation) as exc:
        raise TimesheetError('invalid_monthly_allowance') from exc


def display_hours(units, scale=4):
    return format(Decimal(units) / scale, 'f').rstrip('0').rstrip('.') if units % scale else str(units // scale)


def total_units(report):
    return sum(row['units'] for row in report['rows'])


def build_client_report(config, client, selector, dataset, now, backfill=None):
    beginning = history_beginning(backfill, client['project_ids']) if selector.get('month') == 'all' else None
    selector, start, end = resolve_period(selector, now, beginning=beginning)
    as_of = timestamp(now)
    projects = {key: config.projects[key] for key in client['project_ids']}
    scale = 100 if (backfill or {}).get('version') == 2 else 4
    invoice_first = set((backfill or {}).get('invoice_first_projects', []))
    monthly = []
    for index in range(selector['months']):
        key = month_offset(selector['month'], index).strftime('%Y-%m')
        monthly.append({'month': key, 'units': 0, 'unresolved': 0,
                        'allowance_units': allowance_units(client.get('monthly_allowances', {}).get(
                            key, client.get('monthly_hours', 40)), scale)})
    by_month = {item['month']: item for item in monthly}
    replaced = replacements(backfill)
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
        missing_effort_history = False
        try:
            issue = reconstruct(original, history, completed)
        except TimesheetError as exc:
            if str(exc) != 'missing_label_history':
                raise TimesheetError('source_evidence_incomplete') from exc
            # Deleted labels prevent proving effort, but do not automatically
            # invalidate ownership. Reconstruct every non-label field before
            # exposing even an exception; this ticket will contribute no hours.
            try:
                issue = reconstruct({**original, 'labels': []},
                    [{**event, 'addedLabelIds': [], 'removedLabelIds': [], 'removedLabels': []}
                     for event in history], completed)
            except (TimesheetError, KeyError, TypeError) as ownership_error:
                raise TimesheetError('source_evidence_incomplete') from ownership_error
            missing_effort_history = True
        except (KeyError, TypeError) as exc:
            raise TimesheetError('source_evidence_incomplete') from exc
        if issue is None:
            raise TimesheetError('source_evidence_incomplete')
        project_id = (issue.get('project') or {}).get('id')
        if project_id not in projects:
            continue
        if original['id'] in replaced:
            # Invoice ownership is independently reviewed, but a linked ticket
            # must still prove the same historical project and builder.
            if replaced[original['id']] != (project_id, (issue.get('assignee') or {}).get('id')):
                raise TimesheetError('invoice_ticket_scope_mismatch')
            continue
        month = completed.astimezone(TZ).strftime('%Y-%m')
        try:
            if (project_id in invoice_first and
                    completed.astimezone(TZ).date().isoformat() <= backfill['coverage']['through']):
                # Invoice quantities and explicitly reviewed logs are authoritative.
                # Never add potentially overlapping ticket estimates to this basis.
                raise TimesheetError('ticket_not_added_to_invoice_total_may_overlap_or_need_recorded_hours')
            if missing_effort_history:
                raise TimesheetError('historical_effort_labels_unavailable')
            if issue.get('trashed') or issue['state']['type'] != 'completed':
                raise TimesheetError('completion_needs_review')
            builder_id = (issue.get('assignee') or {}).get('id')
            if issue.get('assignee_count', 1) != 1 or builder_id not in config.builders:
                raise TimesheetError('builder_mapping_required')
            sizes = [LABELS[name] for name in issue['labels'].values() if name in LABELS]
            if len(sizes) != 1:
                raise TimesheetError('exactly_one_effort_label_required')
            units = UNITS[sizes[0]] * (scale // 4)
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
    report = {'selector': selector, 'client': client['name'], 'projects': projects,
            'monthly': monthly, 'rows': rows, 'exceptions': exceptions,
            'generated_at': as_of.astimezone(TZ).isoformat(), 'complete': not exceptions,
            'basis': 'completed_ticket_size_hours', 'unit_scale': scale}
    return apply_backfill(report, backfill, config, as_of.astimezone(TZ))


def month_label(key):
    return month_offset(key, 0).strftime('%B %Y')


def project_breakdown(report, selected, project_ids=None):
    parts = []
    for project_id, project_name in report['projects'].items():
        if project_ids is not None and project_id not in project_ids:
            continue
        rows = [row for row in selected if row['project_id'] == project_id]
        builders = {}
        for row in rows:
            key = (row['builder_id'], row['builder'])
            builders[key] = builders.get(key, 0) + row['units']
        parts.append(f"• *{_escape(project_name)}: {display_hours(sum(row['units'] for row in rows), report.get('unit_scale', 4))}h*")
        if builders:
            parts.append('  ' + ' · '.join(f'{_escape(name)} {display_hours(units, report.get("unit_scale", 4))}h'
                for (_, name), units in sorted(builders.items(), key=lambda pair: (-pair[1], pair[0]))))
        else:
            parts.append('  No counted work recorded.')
    return parts


def client_breakdown(report):
    parts = ['\n*Clients and projects · total for this period*']
    for group in report['client_groups']:
        rows = [row for row in report['rows'] if row['project_id'] in group['project_ids']]
        total = display_hours(sum(row['units'] for row in rows), report.get('unit_scale', 4))
        parts.append(f"\n*{_escape(group['name'])} · {total}h*")
        parts.extend(project_breakdown(report, rows, group['project_ids']))
    return parts


def summary(report):
    hours = lambda units: display_hours(units, report.get('unit_scale', 4))
    invoice_first = report['basis'] == 'invoice_first_recorded_hours'
    detailed = report['selector']['action'] == 'detailed'
    heading = 'Studio hours' if report['selector'].get('client') else 'Your Studio hours'
    parts = [f"*{heading} · {_escape(report['client'])}*"]
    many = len(report['monthly']) > 1
    current = timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m')
    if many:
        monthly = report['monthly']
        parts.append(f"{month_label(monthly[0]['month'])} – {month_label(monthly[-1]['month'])}")
        total = hours(total_units(report))
        parts.append(f"*{total} {'recorded hours' if report['basis'] == 'invoice_first_recorded_hours' else 'hours used'} across {len(monthly)} months*"
                     + (' · invoice-first total; qualifications below' if invoice_first else
                        ' · partial total' if not report['complete'] else ''))
        if invoice_first and not report.get('client_groups'):
            parts.append('\n*Projects · total for this period*')
            parts.extend(project_breakdown(report, report['rows']))
    if report.get('unallocated_units'):
        parts.append(f"*{hours(report['unallocated_units'])}h included in this total have no confirmed monthly split.* "
                     'They are shown separately from monthly usage, not assigned to invoice/payment dates.')
    for month in report['monthly']:
        used, allowance = month['units'], month['allowance_units']
        usage = (f'{hours(used)} hours used' if allowance is None else
                 f'{hours(used)} of {hours(allowance)} hours used')
        if report['basis'] == 'invoice_first_recorded_hours':
            usage = f'{hours(used)}h dated subtotal' + (f' · {hours(allowance)}h allowance' if allowance is not None else '')
        parts.append(f"\n*{month_label(month['month'])} — {usage}*"
                     + (' · month to date' if many and month['month'] == current else ''))
        if month['unresolved']:
            kind = 'work items or invoice records' if report['basis'] != 'completed_ticket_size_hours' else 'completed work items'
            parts.append(('Monthly allocation is incomplete.' if invoice_first else
                          f"⚠️ Partial total: {month['unresolved']} {kind} need review.")
                         + (" Remaining hours aren't confirmed yet." if allowance is not None else ''))
        elif allowance is not None and used > allowance:
            parts.append(f"*{hours(used - allowance)} hours over your allowance.*")
        elif allowance is not None:
            parts.append(f"*{hours(allowance - used)} hours remaining* · {used / allowance:.0%} used")
        if not many:
            parts.append('Project overview · no combined monthly allowance.' if allowance is None
                         else 'Shared monthly allowance across all your projects.')
            if not report.get('client_groups'):
                parts.extend(project_breakdown(report, report['rows']))
    if many:
        parts.append('Monthly allowances are shared across projects and reset each month; no rollover.'
                     if any(month['allowance_units'] is not None for month in report['monthly']) else
                     'Project overview · no combined monthly allowance.')
        if not report.get('client_groups') and not invoice_first:
            parts.append('\n*Projects · total for this period*')
            parts.extend(project_breakdown(report, report['rows']))
    if report.get('client_groups'):
        parts.extend(client_breakdown(report))
    if report['basis'] == 'completed_ticket_size_hours':
        parts.append('\nHours are based on completed-ticket sizes; work in progress and clocked time are not included.')
    else:
        invoiced = sum(row['units'] for row in report['rows'] if row.get('source') == 'reviewed_invoice')
        recorded = sum(row['units'] for row in report['rows'] if row.get('source') == 'reviewed_recorded_time')
        estimated = sum(row['units'] for row in report['rows'] if row.get('source', 'completed_ticket_size') == 'completed_ticket_size')
        components = [f'{hours(invoiced)}h from reviewed invoices']
        if recorded:
            components.append(f'{hours(recorded)}h from additional recorded work')
        if estimated or not report.get('invoice_first_projects'):
            components.append(f'{hours(estimated)}h from completed-ticket estimates')
        parts.append('\nIncludes ' + ' and '.join(components) + '. Matched work is counted once.')
        if report.get('invoice_first_projects'):
            parts.append('Ticket estimates are excluded from the reviewed invoice period. Recorded totals are not a complete clock-time total through today.')
        else:
            parts.append('Invoice hours use the work period; unresolved dates or allocations are excluded from the backfill.')
    for note in report.get('qualifications', []):
        parts.append(f"• {_escape(report['projects'][note['project_id']])}: {_escape(note['reason'])}")
    parts.append('Updated ' + timestamp(report['generated_at']).astimezone(TZ).strftime('%d %b %Y, %I:%M %p %Z') + '.')
    if detailed:
        parts.append('The attached breakdown lists every counted work item, completion date, builder and hours, grouped by month and project.')
    else:
        selector = report['selector']
        parts.append(f'Ask “Studio hours detailed for {selector["month"]}'
                     + (f' for {selector["months"]} months' if selector['months'] > 1 else '')
                     + (f' for client {_escape(selector["client"])}' if selector.get('client') else '')
                     + '” for the full work breakdown.')
    return '\n'.join(parts)


def detail_messages(report):
    lines = ['*Work breakdown*', 'Entries identify invoice/recorded work or full ticket size estimates and supported dates. Invoice hour-units may include billing adjustments; no hourly activity log is inferred.']
    previous = None
    for row in report['rows']:
        group = (row['month'], row['project'])
        if group != previous:
            lines.append(f"\n*{month_label(row['month']) if row['month'] else 'Work month unallocated'} · {_escape(row['project'])}*")
            previous = group
        period = row['completed_at'][:10]
        if row.get('source') in {'reviewed_invoice', 'reviewed_recorded_time'}:
            period = row['work_start'] + (' – ' + row['work_end'] if row['work_end'] != row['work_start'] else '')
            period = (period or 'Work month unconfirmed') + (' · invoice' if row['source'] == 'reviewed_invoice' else ' · recorded work')
        lines.append(f"• {period} · {_escape(row['builder'])} · *{display_hours(row['units'], report.get('unit_scale', 4))}h*"
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
            ['month', 'project', 'builder', 'completion_date_or_work_period_end', 'issue_or_invoice', 'work', 'size', 'hours',
             'source', 'work_period_start', 'work_period_end', 'date_note'],
            [[r['month'], r['project'], r['builder'], r['completed_at'], r['identifier'], r['title'],
              r['size'], Decimal(r['units']) / report.get('unit_scale', 4), r.get('source', 'completed_ticket_size'),
              r.get('work_start', ''), r.get('work_end', ''), r.get('date_note', '')] for r in report['rows']])
    if report['exceptions']:
        result['studio-hours-unresolved.csv'] = csv_file(['month', 'project', 'issue', 'reason'],
            [[r['month'], r['project'], r['identifier'], r['reason']] for r in report['exceptions']])
    return result


def render_chart(report, breakdown=None):
    """Local chart: client data never goes to a third-party chart service."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import textwrap

    with _chart_lock:
        scale = report.get('unit_scale', 4)
        monthly = report['monthly']
        many = len(monthly) > 1 and breakdown is None
        project_ids = list(report['projects'])
        bars = len(report.get('client_groups', [])) if breakdown == 'clients' else len(project_ids)
        figure = Figure(figsize=(10, 4.8 if many else max(4.8, 2.8 + bars * .65)), dpi=150)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left=.34 if not many else .10, right=.92, top=.76, bottom=.22)
        figure.text(.06, .93, 'Studio hours' if report['selector'].get('client') else 'Your Studio hours',
                    fontsize=22, weight='bold', color='#142c3a')
        if report['selector'].get('client'):
            figure.text(.06, .86, textwrap.shorten(report['client'], width=90, placeholder='…'), fontsize=12, color='#334b58')
        if many:
            labels = [month_offset(m['month'], 0).strftime('%b %Y') for m in monthly]
            current = timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m')
            axis_labels = [label + ('\n(to date)' if month['month'] == current else '')
                           for label, month in zip(labels, monthly)]
            values = [m['units'] / scale for m in monthly]
            allowances = [m['allowance_units'] / scale if m['allowance_units'] is not None else None for m in monthly]
            configured = [value for value in allowances if value is not None]
            colors = ['#65aebb' if month['month'] == current else '#147d92' for month in monthly]
            if report.get('unallocated_units'):
                axis_labels.append('Month\nunallocated')
                values.append(report['unallocated_units'] / scale)
                colors.append('#ad5514')
            axes.bar(axis_labels, values, color=colors, width=.55)
            if configured:
                axes.plot(axis_labels[:len(monthly)], allowances, color='#ad5514',
                          marker='_', linestyle='--', label='Monthly allowance')
                axes.legend(frameon=False)
            axes.set_ylabel('Hours recorded' if report['basis'] == 'invoice_first_recorded_hours' else 'Hours used')
            axes.tick_params(axis='x', labelrotation=30 if len(monthly) > 6 else 0)
            for index, value in enumerate(values):
                label = ('No dated\nhours' if value == 0 and index < len(monthly) and monthly[index]['unresolved']
                         else f'{value:g}h')
                axes.annotate(label, (index, value), xytext=(0, 5),
                              textcoords='offset points', ha='center')
            axes.set_ylim(0, max([1, *values, *configured]) * 1.2)
            subtitle = f"{labels[0]} – {labels[-1]} · " + (
                'one shared allowance each month' if len(configured) == len(monthly) else
                'monthly usage and configured allowances' if configured else 'monthly project usage')
        else:
            if breakdown:
                period = month_offset(monthly[0]['month'], 0).strftime('%b %Y')
                if len(monthly) > 1:
                    period += ' – ' + month_offset(monthly[-1]['month'], 0).strftime('%b %Y')
                subtitle = f'{period} · total hours by {"client" if breakdown == "clients" else "project"}'
                if monthly[-1]['month'] == timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m'):
                    subtitle += ' · to date'
            else:
                month = monthly[0]
                used, allocated = month['units'], month['allowance_units']
                usage = (f'{display_hours(used, scale)} hours used' if allocated is None else
                         f'{display_hours(used, scale)} of {display_hours(allocated, scale)} hours used')
                subtitle = f"{month_label(month['month'])} · {usage}"
            groups = (report['client_groups'] if breakdown == 'clients' else
                      [{'name': report['projects'][key], 'project_ids': [key]} for key in project_ids])
            groups = sorted(groups, key=lambda group: -sum(r['units'] for r in report['rows'] if r['project_id'] in group['project_ids']))
            values = [sum(r['units'] for r in report['rows'] if r['project_id'] in group['project_ids']) / scale for group in groups]
            names = ['\n'.join(textwrap.wrap(group['name'], 29)) for group in groups]
            axes.barh(range(len(names)), values, color='#147d92', height=.5)
            axes.set_yticks(range(len(names)), names)
            axes.invert_yaxis()
            axes.set_xlabel('Hours used by client' if breakdown == 'clients' else 'Hours used by project')
            if report['basis'] == 'invoice_first_recorded_hours':
                axes.set_xlabel('Recorded hours by project')
            axes.set_xlim(0, max([1, *values]) * 1.23)
            for index, value in enumerate(values):
                axes.text(value + max([1, *values]) * .025, index, f'{value:g}h', va='center', weight='bold')
        figure.text(.06, .805 if report['selector'].get('client') else .845, subtitle, fontsize=12, color='#334b58')
        for spine in axes.spines.values():
            spine.set_visible(False)
        axes.tick_params(length=0, pad=8)
        basis = ('Reviewed invoice/recorded hours + completed-ticket estimates' if report['basis'] != 'completed_ticket_size_hours'
                 else 'Completed-ticket size hours · excludes work in progress')
        if report['basis'] == 'invoice_first_recorded_hours':
            basis = f"{display_hours(total_units(report), scale)}h supported total · invoice hour-units + additional recorded work"
        caution = ('\nMonthly allocation incomplete; see report qualifications.' if report.get('unallocated_units') else
                   '\nPartial totals: some completed work needs review.' if not report['complete'] else '')
        figure.text(.06, .065, basis
                    + caution,
                    fontsize=10, color='#465d67')
        with BytesIO() as output:
            canvas.print_png(output)
            return output.getvalue()
