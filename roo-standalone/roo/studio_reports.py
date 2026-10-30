"""Private monthly and weekly client usage reports, without payroll ledger writes."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
import re
from threading import Lock
from zoneinfo import ZoneInfo

from .payment_reminders import _escape
from .studio_report_backfill import apply_backfill, history_beginning, replacements
from .studio_report_clients import client_key
from .studio_report_weekly_backfill import apply_weekly_backfill
from .timesheets import UNITS, TimesheetError, csv_file, reconstruct, timestamp

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
    if params.get('period') == 'weekly':
        return resolve_week(params, now)
    if params.get('period') == 'month_to_date':
        return resolve_month_to_date(params, now)
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


def resolve_week(params, now):
    """Freeze an explicit half-open weekly window, including DST boundaries."""
    try:
        start = timestamp(params['start']).astimezone(TZ)
        end = timestamp(params['end']).astimezone(TZ)
    except (KeyError, TimesheetError) as exc:
        raise TimesheetError('invalid_report_week') from exc
    if not start < end or end - start > timedelta(days=7) or end > timestamp(now):
        raise TimesheetError('invalid_report_week')
    action = params.get('action', 'summary')
    if action not in {'summary', 'detailed'}:
        raise TimesheetError('invalid_report_action')
    last = end - timedelta(microseconds=1)
    count = (last.year - start.year) * 12 + last.month - start.month + 1
    selector = {'period': 'weekly', 'start': start.isoformat(), 'end': end.isoformat(),
                'month': start.strftime('%Y-%m'), 'months': count, 'action': action}
    if 'client' in params:
        client_key(params['client'])
        selector['client'] = ' '.join(params['client'].split())
    return selector, start, end


def resolve_month_to_date(params, now):
    """The companion month starts in Melbourne and ends at the frozen cutoff."""
    try:
        end = timestamp(params['end']).astimezone(TZ)
    except (KeyError, TimesheetError) as exc:
        raise TimesheetError('invalid_report_month_to_date') from exc
    start = month_offset(end.strftime('%Y-%m'), 0)
    if start > end or end > timestamp(now):
        raise TimesheetError('invalid_report_month_to_date')
    action = params.get('action', 'summary')
    if action not in {'summary', 'detailed'}:
        raise TimesheetError('invalid_report_action')
    selector = {'period': 'month_to_date', 'start': start.isoformat(), 'end': end.isoformat(),
                'month': start.strftime('%Y-%m'), 'months': 1, 'action': action}
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
    if scale > 100:
        number = (Decimal(units) / scale).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        return format(number, 'f').rstrip('0').rstrip('.')
    return format(Decimal(units) / scale, 'f').rstrip('0').rstrip('.') if units % scale else str(units // scale)


def total_units(report):
    return sum(row['units'] for row in report['rows'])


def build_client_report(config, client, selector, dataset, now, backfill=None, *, effort_resolutions=None):
    from .studio_report_policy import resolve_report_effort
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
                        'allowance_units': None if selector.get('period') in {'weekly', 'month_to_date'} else
                            allowance_units(client.get('monthly_allowances', {}).get(
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
            if project_id in invoice_first:
                # Invoice quantities and explicitly reviewed logs are authoritative.
                # Delayed completion is not evidence of additional work after
                # the invoice cutoff. Keep unmatched estimates out at all dates.
                raise TimesheetError('ticket_not_added_to_invoice_total_may_overlap_or_need_recorded_hours')
            if missing_effort_history:
                raise TimesheetError('historical_effort_labels_unavailable')
            if issue.get('trashed') or issue['state']['type'] != 'completed':
                raise TimesheetError('completion_needs_review')
            builder_id = (issue.get('assignee') or {}).get('id')
            if issue.get('assignee_count', 1) != 1 or builder_id not in config.builders:
                raise TimesheetError('builder_mapping_required')
            size, reviewed_effort = resolve_report_effort(issue, history, completed, effort_resolutions)
            units = UNITS[size] * (scale // 4)
            rows.append({'month': month, 'project_id': project_id, 'project': projects[project_id],
                         'builder_id': builder_id, 'builder': config.builders[builder_id]['name'],
                         'issue_id': original['id'], 'identifier': original['identifier'],
                         'title': issue['title'], 'completed_at': completed.astimezone(TZ).isoformat(),
                         'size': size, 'units': units,
                         **({'effort_basis': 'reviewed_completion_label'} if reviewed_effort else {})})
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
    if selector.get('period') in {'weekly', 'month_to_date'}:
        return apply_weekly_backfill(report, backfill, config, as_of.astimezone(TZ))
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
        units = sum(row['units'] for row in rows)
        uncertain = any(item.get('project') == project_name for item in report.get('exceptions', []))
        amount = ('Unavailable' if uncertain and not units else
                  display_hours(units, report.get('unit_scale', 4)) + ('h + unverified' if uncertain else 'h'))
        parts.append(f"• *{_escape(project_name)}: {amount}*")
        if builders:
            parts.append('  ' + ' · '.join(f'{_escape(name)} {display_hours(units, report.get("unit_scale", 4))}h'
                for (_, name), units in sorted(builders.items(), key=lambda pair: (-pair[1], pair[0]))))
        elif not uncertain:
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


def mixed_source_projects(report):
    """Return reviewed/task project labels only when both source types are used."""
    if not report.get('recorded_snapshot') or not report.get('linear_project_ids'):
        return None
    projects = report['projects']
    tasks = set(report['linear_project_ids']) & set(projects)
    reviewed = (set(report.get('invoice_first_projects', [])) & set(projects)) - tasks
    names = lambda keys: ', '.join(projects[key] for key in sorted(keys, key=lambda key: (projects[key].casefold(), key)))
    return names(reviewed), names(tasks)


def task_source_timestamp(report):
    return timestamp(report['generated_at']).astimezone(TZ).strftime('%d %b %Y, %I:%M %p %Z')


def reviewed_cutoff_notice(report):
    """Keep the review cutoff visible when a historical fallback is rendered."""
    if not report.get('source_stale'):
        return ''
    source = report.get('source_as_of', report.get('as_of'))
    reviewed = timestamp(source).astimezone(TZ).strftime('%d %b %Y')
    mixed = mixed_source_projects(report)
    if mixed:
        return (f"Historical reviewed projects · {_escape(mixed[0])} · reviewed through {report['coverage_through']}. "
                f"Last review: {reviewed} (Melbourne). Later activity for these reviewed projects is unavailable. "
                f"Linear task projects ({_escape(mixed[1])}) were read fresh through {task_source_timestamp(report)}.")
    return (f"Historical snapshot · reviewed through {report['coverage_through']}. "
            f"Last review: {reviewed} (Melbourne). Later activity is unavailable.")


def summary(report):
    if report['selector'].get('period') == 'weekly':
        return weekly_summary(report)
    if report['selector'].get('period') == 'month_to_date':
        return month_to_date_summary(report)
    hours = lambda units: display_hours(units, report.get('unit_scale', 4))
    mixed = mixed_source_projects(report)
    invoice_first = report['basis'] == 'invoice_first_recorded_hours'
    quantity_label = ('reviewed hour-units and ticket-size estimates' if mixed else
                      'invoice/recorded hour-units' if report.get('recorded_snapshot') else 'recorded hours')
    detailed = report['selector']['action'] == 'detailed'
    heading = 'Studio hours' if report['selector'].get('client') else 'Your Studio hours'
    parts = [f"*{heading} · {_escape(report['client'])}*"]
    if reviewed_cutoff_notice(report):
        parts.append('*' + reviewed_cutoff_notice(report) + '*')
    if report.get('period_has_coverage') is False:
        parts.append('Hours are unavailable for the requested period; reviewed coverage ends '
                     + report['coverage_through'] + '. Missing activity has not been counted as zero.')
        return '\n'.join(parts)
    many = len(report['monthly']) > 1
    current = timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m')
    if many:
        monthly = report['monthly']
        parts.append(f"{month_label(monthly[0]['month'])} – {month_label(monthly[-1]['month'])}")
        total = hours(total_units(report))
        parts.append(f"*{total} {quantity_label if invoice_first or mixed else 'hours used'} across {len(monthly)} months*"
                     + (' · invoice-first total; qualifications below' if invoice_first and not mixed else
                        ' · partial total' if not report['complete'] else ''))
        if invoice_first and not report.get('client_groups'):
            parts.append('\n*Projects · total for this period*')
            parts.extend(project_breakdown(report, report['rows']))
    if report.get('unallocated_units'):
        parts.append(f"*{hours(report['unallocated_units'])}h included in this total have no confirmed monthly split.* "
                     'They are shown separately from monthly usage, not assigned to invoice/payment dates.')
    if report.get('estimated_month_units'):
        parts.append(f"*{hours(report['estimated_month_units'])}h have estimated month assignments.* "
                     'These redistribute existing hours; the detailed report explains each invoice/work-date assumption.')
    for month in report['monthly']:
        if month.get('coverage_available') is False:
            parts.append(f"\n*{month_label(month['month'])} — hours unavailable outside reviewed coverage*")
            continue
        used, allowance = month['units'], month['allowance_units']
        usage = (f'{hours(used)} hours used' if allowance is None else
                 f'{hours(used)} of {hours(allowance)} hours used')
        if mixed:
            usage = f'{hours(used)}h reviewed hour-units and ticket-size estimates'
        elif report['basis'] == 'invoice_first_recorded_hours':
            usage = f'{hours(used)}h ' + ('allocated total' if month.get('estimated_units') else 'dated subtotal')
            usage += f' · {hours(allowance)}h allowance' if allowance is not None else ''
        parts.append(f"\n*{month_label(month['month'])} — {usage}*"
                     + (f" · through {report['coverage_through']}" if report.get('recorded_snapshot') and not mixed
                        and month['month'] == report['coverage_through'][:7] and report.get('partial') else
                        ' · month to date' if many and month['month'] == current else ''))
        if month.get('estimated_units'):
            parts.append(f"Includes {hours(month['estimated_units'])}h assigned using estimated work months."
                         + (" Remaining hours and overages aren't confirmed."
                            if allowance is not None and not month['unresolved'] else ''))
        if month['unresolved']:
            kind = 'work items or invoice records' if report['basis'] != 'completed_ticket_size_hours' else 'completed work items'
            parts.append(('Some recorded work still needs review.' if invoice_first else
                          f"⚠️ Partial total: {month['unresolved']} {kind} need review.")
                         + (" Remaining hours aren't confirmed yet." if allowance is not None else ''))
        elif allowance is not None and used > allowance and not month.get('estimated_units'):
            parts.append(f"*{hours(used - allowance)} hours over your allowance.*")
        elif allowance is not None and not month.get('estimated_units'):
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
    if report.get('recorded_snapshot'):
        if mixed:
            parts.append(f'\nReviewed projects ({_escape(mixed[0])}) use the same reviewed work records as the paid-credit chart. '
                         'These amounts are invoice/recorded hour-units, not verified clock time or confirmed billable entitlement.')
            parts.append('Reviewed project source: ' + report['source_as_of'][:10] +
                         ' · coverage through ' + report['coverage_through'] + '.')
            parts.append(f'Linear task projects ({_escape(mixed[1])}) use completed-ticket size estimates read fresh through '
                         + task_source_timestamp(report) + '.')
        else:
            parts.append('\nThis view uses the same reviewed work records as the paid-credit chart. '
                         'Amounts are invoice/recorded hour-units, not verified clock time or confirmed billable entitlement.')
            parts.append('Source review: ' + report['source_as_of'][:10] +
                         ' · coverage through ' + report['coverage_through'] + '.')
        if report.get('partial'):
            parts.append('Activity for the reviewed projects outside their coverage is unavailable; it is not counted as zero.' if mixed else
                         'Requested activity outside reviewed coverage is unavailable; it is not counted as zero.')
        if report.get('pending_work_count'):
            parts.append(str(report['pending_work_count']) + ' work sources remain unquantified and excluded.')
    if report['basis'] == 'completed_ticket_size_hours':
        parts.append('\nHours are based on completed-ticket sizes; work in progress and clocked time are not included.')
        reviewed_effort = sum(row.get('effort_basis') == 'reviewed_completion_label' for row in report['rows'])
        if reviewed_effort:
            parts.append(f'{reviewed_effort} completed work items use reviewed completion-time sizes to resolve conflicting labels.')
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
            parts.append((f'Ticket estimates are excluded for the reviewed projects ({_escape(mixed[0])}), including later ticket completions. '
                          'Their recorded totals are not a complete clock-time total through today.') if mixed else
                         'Ticket estimates are excluded for invoice-first projects, including later ticket completions. Recorded totals are not a complete clock-time total through today.')
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


def week_label(selector):
    start = timestamp(selector['start']).astimezone(TZ)
    end = timestamp(selector['end']).astimezone(TZ)
    return (start.strftime('%d %b %Y, %I:%M %p %Z') + ' – '
            + end.strftime('%d %b %Y, %I:%M %p %Z'))


def weekly_summary(report):
    scale = report.get('unit_scale', 4)
    hours = lambda units: display_hours(units, scale)
    unavailable = report.get('weekly_has_coverage') is False and not report['rows']
    quantity = 'invoice/recorded hour-units' if report.get('recorded_snapshot') else 'hours'
    parts = [f"*Weekly Studio hours · {_escape(report['client'])}*",
             *(['*' + reviewed_cutoff_notice(report) + '*'] if reviewed_cutoff_notice(report) else []),
             week_label(report['selector']) + ' · end time excluded',
             '*Weekly hours unavailable — reviewed records do not cover this week.*' if unavailable else
             f"*{hours(total_units(report))} {quantity} counted this week*"
             + (' · partial total' if not report['complete'] else '')]
    if unavailable:
        parts.extend(f"• *{_escape(name)}*: weekly hours unavailable." for name in report['projects'].values())
    else:
        parts.extend(project_breakdown(report, report['rows']))
    if not report['complete']:
        parts.append('This total has unresolved work or source qualifications. '
                     'Hours without supported weekly dates are excluded; see the notes below and any attached exceptions.')
    if report['basis'] == 'completed_ticket_size_hours':
        parts.append('Hours use completed-ticket size estimates at first completion. '
                     'Work in progress and clocked time are not included.')
    else:
        invoices = sum(row['units'] for row in report['rows'] if row.get('source') == 'reviewed_invoice')
        recorded = sum(row['units'] for row in report['rows'] if row.get('source') == 'reviewed_recorded_time')
        tickets = sum(row['units'] for row in report['rows'] if row.get('source', 'completed_ticket_size') == 'completed_ticket_size')
        if not unavailable:
            parts.append(f'Includes {hours(invoices)}h from reviewed invoices, {hours(recorded)}h from '
                         f'recorded work and {hours(tickets)}h from completed-ticket estimates. Matched work counts once.')
        parts.append('Invoice and recorded hours count only when their full work dates fit this week. '
                     'Aggregate and undated hours are not divided into guessed daily amounts.')
        if report.get('invoice_first_projects'):
            parts.append('Ticket estimates are excluded for invoice-first projects, including later ticket completions. '
                         'Recorded totals are not a complete clock-time total through today.')
    if report.get('recorded_snapshot'):
        parts.append('This view uses the same reviewed work records as the paid-credit report. '
                     'Amounts are invoice/recorded hour-units; clock time and billable entitlement are unverified.')
        parts.append('Source review: ' + report['source_as_of'][:10] +
                     ' · coverage through ' + report['coverage_through'] + '.')
        if report.get('partial'):
            parts.append('Activity outside reviewed coverage is unavailable and is not counted as zero.')
        if report.get('pending_work_count'):
            parts.append(str(report['pending_work_count']) + ' work sources remain unquantified and excluded.')
    for note in report.get('qualifications', []):
        parts.append(f"• {_escape(report['projects'][note['project_id']])}: {_escape(note['reason'])}")
    parts.append('Report cutoff: ' + timestamp(report['generated_at']).astimezone(TZ).strftime('%d %b %Y, %I:%M %p %Z') + '.')
    if report['selector']['action'] == 'detailed':
        parts.append('The attached breakdown lists the counted work, builders, dates and hours.')
    return '\n'.join(parts)


def month_to_date_summary(report):
    """A compact companion to the weekly text, using the chart's exact rows."""
    scale = report.get('unit_scale', 4)
    unavailable = report.get('period_has_coverage') is False
    title = month_label(report['selector']['month']) + ' · month to date'
    quantity = 'invoice/recorded hour-units' if report.get('recorded_snapshot') else 'hours'
    parts = [f'*{title}*', week_label(report['selector']) + ' · end time excluded']
    if reviewed_cutoff_notice(report):
        parts.insert(1, '*' + reviewed_cutoff_notice(report) + '*')
    parts.append('Reviewed records do not cover this period; monthly hours are unavailable.' if unavailable else
                 f'*{display_hours(total_units(report), scale)} {quantity} counted*'
                 + (' · partial total' if not report['complete'] else ''))
    parts.append('Month-to-date starts on the first day of this month and uses the same Friday cutoff.')
    if report['selector']['start'] == report['selector']['end']:
        parts.append('The month has just started; no time has elapsed in this reporting window.')
    if report.get('recorded_snapshot'):
        parts.append('Source review: ' + report['source_as_of'][:10] +
                     ' · coverage through ' + report['coverage_through'] + '.')
    if not report['complete']:
        parts.append('Unsupported dates and allocations are excluded; the total is qualified by source coverage and review notes.')
    if report['basis'] != 'completed_ticket_size_hours':
        parts.append('Month-to-date uses the same reviewed allocation as calendar-month reports; '
                     'this is not a daily clock-time log. Cutoff-day work needs supported times.')
        if report.get('estimated_month_units'):
            parts.append(f"Includes {display_hours(report['estimated_month_units'], scale)}h with estimated monthly allocation; exact source quantities are preserved.")
    for note in report.get('qualifications', []):
        parts.append(f"• {_escape(report['projects'][note['project_id']])}: {_escape(note['reason'])}")
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
        if row.get('month_allocation') == 'estimated':
            period += ' · estimated allocation to ' + month_label(row['month'])
        lines.append(f"• {period} · {_escape(row['builder'])} · *{display_hours(row['units'], report.get('unit_scale', 4))}h*"
                     f" — {_escape(row['title'])} ({_escape(row['identifier'])})")
        if row.get('allocation_note'):
            lines.append('  Allocation assumption: ' + _escape(row['allocation_note']))
        if row.get('effort_basis') == 'reviewed_completion_label':
            lines.append('  Size confirmed from the first-completion label change.')
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
             'source', 'work_period_start', 'work_period_end', 'date_note', 'month_allocation', 'allocation_note', 'effort_basis'],
            [[r['month'], r['project'], r['builder'], r['completed_at'], r['identifier'], r['title'],
              r['size'], Decimal(r['units']) / report.get('unit_scale', 4), r.get('source', 'completed_ticket_size'),
              r.get('work_start', ''), r.get('work_end', ''), r.get('date_note', ''),
              r.get('month_allocation', 'work_dates'), r.get('allocation_note', ''),
              r.get('effort_basis', '')] for r in report['rows']])
    if report['exceptions']:
        result['studio-hours-unresolved.csv'] = csv_file(['month', 'project', 'issue', 'reason'],
            [[r['month'], r['project'], r['identifier'], r['reason']] for r in report['exceptions']])
    return result


def render_chart(report, breakdown=None):
    """Local chart: client data never goes to a third-party chart service."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    import textwrap

    weekly = report['selector'].get('period') == 'weekly'
    month_to_date = report['selector'].get('period') == 'month_to_date'
    bounded = weekly or month_to_date
    if bounded and breakdown is None:
        breakdown = 'projects'
    mixed = mixed_source_projects(report)

    with _chart_lock:
        scale = report.get('unit_scale', 4)
        monthly = report['monthly']
        monthly_view = breakdown is None
        project_ids = sorted(report['projects'], key=lambda key: (report['projects'][key].casefold(), key))
        palette = ('#147d92', '#e69f00', '#7465ab', '#009e73', '#d55e00',
                   '#0072b2', '#cc79a7', '#6b7136', '#575757', '#56b4e9')
        project_colors = {key: palette[index % len(palette)] for index, key in enumerate(project_ids)}
        bars = len(report.get('client_groups', [])) if breakdown == 'clients' else len(project_ids)
        legend_rows = (len(project_ids) + int(any(m['allowance_units'] is not None for m in monthly)) + 1) // 2
        height = 5.4 + .3 * legend_rows if monthly_view else max(4.8, 2.8 + bars * .65)
        figure = Figure(figsize=(10, height), dpi=150)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        bottom = (1.15 + .3 * legend_rows) / height if monthly_view else .22
        figure.subplots_adjust(left=.10 if monthly_view else .34, right=.92, top=.76, bottom=bottom)
        warning = reviewed_cutoff_notice(report)
        if mixed:
            figure.subplots_adjust(top=.66)
            figure.text(.06, .765,
                        ('Historical reviewed projects: ' if warning else 'Reviewed projects: ')
                        + textwrap.shorten(mixed[0], width=65, placeholder='…') + '\n'
                        + f"Review coverage through {report['coverage_through']}; later reviewed activity unavailable.\n"
                        + 'Linear task projects fresh through ' + task_source_timestamp(report) + '.',
                        fontsize=9, weight='bold' if warning else 'normal', color='#9a4a00' if warning else '#334b58',
                        parse_math=False, va='top')
        elif warning:
            figure.subplots_adjust(top=.66)
            figure.text(.06, .745, f"Historical snapshot · reviewed through {report['coverage_through']}\n"
                        'Later activity unavailable · source refresh required for a current report',
                        fontsize=10, weight='bold', color='#9a4a00')
        figure.text(.06, .93, 'Weekly Studio hours' if weekly else
                    'Month-to-date Studio hours' if month_to_date else
                    'Studio hours' if report['selector'].get('client') else 'Your Studio hours',
                    fontsize=22, weight='bold', color='#142c3a')
        if report['selector'].get('client'):
            figure.text(.06, .86, textwrap.shorten(report['client'], width=90, placeholder='…'), fontsize=12, color='#334b58')
        if monthly_view:
            labels = [month_offset(m['month'], 0).strftime('%b %Y') for m in monthly]
            current = timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m')
            axis_labels = [label + ('*' if month.get('estimated_units') else '')
                           + (f"\n(through {report['coverage_through'][8:10]} {month_offset(month['month'], 0):%b})"
                              if report.get('recorded_snapshot') and report.get('partial') and not mixed
                              and month['month'] == report['coverage_through'][:7] else
                              '\n(to date)' if month['month'] == current and (not warning or mixed) else '')
                           for label, month in zip(labels, monthly)]
            values = [m['units'] / scale for m in monthly]
            allowances = [m['allowance_units'] / scale if m['allowance_units'] is not None else None for m in monthly]
            configured = [value for value in allowances if value is not None]
            month_keys = [month['month'] for month in monthly]
            if report.get('unallocated_units'):
                axis_labels.append('Month\nunallocated')
                month_keys.append('')
                values.append(report['unallocated_units'] / scale)
            month_index = {key: index for index, key in enumerate(month_keys)}
            project_units = {key: [0] * len(month_keys) for key in project_ids}
            for row in report['rows']:
                if row['project_id'] in project_units and row['month'] in month_index:
                    project_units[row['project_id']][month_index[row['month']]] += row['units']
            bottom_units = [0] * len(month_keys)
            for project_id in project_ids:
                units = project_units[project_id]
                axes.bar(axis_labels, [value / scale for value in units],
                         bottom=[value / scale for value in bottom_units],
                         color=project_colors[project_id], width=.55, edgecolor='white', linewidth=.5,
                         label=textwrap.shorten(report['projects'][project_id], width=40, placeholder='…'))
                bottom_units = [base + value for base, value in zip(bottom_units, units)]
            if configured:
                axes.plot(axis_labels[:len(monthly)], allowances, color='#35434a',
                          marker='_', linestyle='--', label='Monthly allowance')
            handles, legend_labels = axes.get_legend_handles_labels()
            figure.legend(handles, legend_labels, loc='upper left',
                          bbox_to_anchor=(.09, (.6 + .3 * legend_rows) / height),
                          ncol=2, frameon=False, fontsize=9)
            axes.set_xlabel('Month')
            axes.set_ylabel('Reviewed hour-units and ticket-size estimates' if mixed else
                            'Invoice/recorded hour-units' if report.get('recorded_snapshot') else
                            'Hours recorded' if report['basis'] == 'invoice_first_recorded_hours' else 'Hours used')
            axes.tick_params(axis='x', labelrotation=30 if len(monthly) > 6 else 0)
            for index, value in enumerate(values):
                label = ('Unavailable' if index < len(monthly) and monthly[index].get('coverage_available') is False else
                         'No dated\nhours' if value == 0 and index < len(monthly) and monthly[index]['unresolved']
                         else display_hours(round(value * scale), scale) + 'h')
                axes.annotate(label, (index, value), xytext=(0, 5),
                              textcoords='offset points', ha='center', zorder=4,
                              bbox={'facecolor': 'white', 'edgecolor': 'none', 'pad': 1})
            axes.set_ylim(0, max([1, *values, *configured]) * 1.2)
            period = labels[0] + (f' – {labels[-1]}' if len(labels) > 1 else '')
            subtitle = f'{period} · monthly hours by project'
        else:
            period = week_label(report['selector']) if bounded else month_offset(monthly[0]['month'], 0).strftime('%b %Y')
            if len(monthly) > 1 and not bounded:
                period += ' – ' + month_offset(monthly[-1]['month'], 0).strftime('%b %Y')
            subtitle = f'{period} · total hours by {"client" if breakdown == "clients" else "project"}'
            if not bounded and report.get('recorded_snapshot') and report.get('partial') and not mixed:
                subtitle += ' · through ' + report['coverage_through']
            elif not bounded and monthly[-1]['month'] == timestamp(report['generated_at']).astimezone(TZ).strftime('%Y-%m'):
                subtitle += ' · to date'
            groups = (report['client_groups'] if breakdown == 'clients' else
                      [{'name': report['projects'][key], 'project_ids': [key]} for key in project_ids])
            groups = sorted(groups, key=lambda group: -sum(r['units'] for r in report['rows'] if r['project_id'] in group['project_ids']))
            values = [sum(r['units'] for r in report['rows'] if r['project_id'] in group['project_ids']) / scale for group in groups]
            names = []
            for group in groups:
                lines = textwrap.wrap(group['name'], 29)
                if (bounded or breakdown == 'projects') and len(lines) > 2:
                    lines = [lines[0], lines[1][:28] + '…']
                names.append('\n'.join(lines))
            colors = '#147d92' if breakdown == 'clients' else [project_colors[group['project_ids'][0]] for group in groups]
            axes.barh(range(len(names)), values, color=colors, height=.5)
            axes.set_yticks(range(len(names)), names)
            axes.invert_yaxis()
            axes.set_xlabel('Hours used by client' if breakdown == 'clients' else 'Hours used by project')
            if mixed:
                axes.set_xlabel('Reviewed hour-units and ticket-size estimates')
            elif report['basis'] == 'invoice_first_recorded_hours':
                axes.set_xlabel('Invoice/recorded hour-units by project' if report.get('recorded_snapshot') else 'Recorded hours by project')
            axes.set_xlim(0, max([1, *values]) * 1.23)
            for index, value in enumerate(values):
                uncertain = any(item.get('project') in {report['projects'][key] for key in groups[index]['project_ids']}
                                for item in report.get('exceptions', []))
                label = ('Unavailable' if uncertain and not value else
                         display_hours(round(value * scale), scale) + ('h + unverified' if uncertain else 'h'))
                axes.text(value + max([1, *values]) * .025, index, label, va='center', weight='bold')
        if report.get('period_has_coverage', report.get('weekly_has_coverage')) is False:
            axes.clear()
            axes.set_position([.10, .22, .82, .40 if warning else .52])
            axes.set_axis_off()
            axes.text(.5, .65, 'Reviewed hours unavailable',
                      transform=axes.transAxes, ha='center', fontsize=17, weight='bold', color='#334b58')
            coverage = 'Source coverage through ' + report.get('coverage_through', 'an unconfirmed date')
            axes.text(.5, .47, coverage + '\nMissing records have not been counted as zero.',
                      transform=axes.transAxes, ha='center', fontsize=12, color='#465d67')
        figure.text(.06, .805 if report['selector'].get('client') else .845, subtitle, fontsize=12, color='#334b58')
        for spine in axes.spines.values():
            spine.set_visible(False)
        axes.tick_params(length=0, pad=8)
        basis = ('Reviewed invoice/recorded hours + completed-ticket estimates' if report['basis'] != 'completed_ticket_size_hours'
                 else 'Completed-ticket size hours · excludes work in progress')
        if report['basis'] == 'invoice_first_recorded_hours':
            basis = f"{display_hours(total_units(report), scale)}h supported total · invoice hour-units + additional recorded work"
        if mixed:
            basis = f"{display_hours(total_units(report), scale)}h · reviewed hour-units and ticket-size estimates · clock time unverified"
        elif report.get('recorded_snapshot'):
            basis = f"{display_hours(total_units(report), scale)} invoice/recorded hour-units · clock time and billable entitlement unverified"
        caution = ('\nMonthly allocation incomplete; see report qualifications.' if report.get('unallocated_units') else
                   '\nPartial totals: some completed work needs review.' if not report['complete'] else '')
        if weekly and not report['complete']:
            caution = '\nPartial weekly total; see date exceptions and source qualifications.'
        if month_to_date and not report['complete']:
            caution = '\nPartial month-to-date total; see date exceptions and source qualifications.'
        if report.get('period_has_coverage', report.get('weekly_has_coverage')) is False:
            basis = 'Hours unavailable · reviewed records do not cover this period'
            caution = ''
        if report.get('estimated_month_units'):
            caution = '\n* Includes estimated month assignments; see the detailed report for assumptions.'
            if report.get('unallocated_units'):
                caution += ' Some hours remain unallocated.'
        figure.text(.06, .065, basis
                    + caution,
                    fontsize=10, color='#465d67')
        with BytesIO() as output:
            canvas.print_png(output)
            return output.getvalue()
