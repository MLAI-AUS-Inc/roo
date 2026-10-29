"""Local task-estimate burndowns, separate from purchased-hour payment reports."""
from collections import defaultdict
from datetime import datetime, time
from io import BytesIO
from threading import Lock
import textwrap

from .studio_reports import TZ, month_offset
from .timesheets import TimesheetError, timestamp

_chart_lock = Lock()


def render_task_burndown(report) -> bytes | None:
    """Return a monthly base-allowance chart, or None without a client budget.

    Only counted ticket estimates are plotted, on their completion dates. Each
    calendar month starts from its own allowance; no rollover or cash credits
    enter this view. Partial months show the allowance less known estimates,
    never a confirmed remaining balance.
    """
    monthly = report['monthly']
    if (not monthly or all(month['allowance_units'] is None for month in monthly)
            or report.get('basis', 'completed_ticket_size_hours') != 'completed_ticket_size_hours'):
        return None
    if len(monthly) > 120:
        raise TimesheetError('task_burndown_period_too_long')
    scale = report.get('unit_scale', 4)
    if type(scale) is not int or scale <= 0:
        raise TimesheetError('invalid_task_burndown_units')
    cutoff = timestamp(report['generated_at']).astimezone(TZ)
    by_month = {month['month']: defaultdict(int) for month in monthly}
    for row in report['rows']:
        if row.get('source', 'completed_ticket_size') != 'completed_ticket_size':
            return None
        if type(row['units']) is not int or row['units'] < 0:
            raise TimesheetError('invalid_task_burndown_units')
        completed = timestamp(row['completed_at']).astimezone(TZ)
        key = completed.strftime('%Y-%m')
        if completed <= cutoff and key in by_month:
            # At most one point per actual completion date keeps a ten-year
            # report bounded to about 3,720 points, without invented daily work.
            by_month[key][completed.date()] += row['units']

    partial = {month['month'] for month in monthly if month.get('unresolved')}
    partial.update(item['month'] for item in report.get('exceptions', []) if item.get('month'))
    if report.get('complete') is False and not partial:
        partial.update(by_month)
    segments, missing = [], []
    for month in monthly:
        key = month['month']
        start, end = month_offset(key, 0), min(month_offset(key, 1), cutoff)
        if start > end:
            continue
        allowance = month['allowance_units']
        if allowance is None:
            missing.append((start, end))
            continue
        if type(allowance) is not int or allowance <= 0:
            raise TimesheetError('invalid_task_burndown_allowance')
        dates, values, used = [start], [allowance], 0
        for day, units in sorted(by_month[key].items()):
            used += units
            dates.append(datetime.combine(day, time.min, TZ))
            values.append(allowance - used)
        dates.append(end)
        values.append(allowance - used)
        segments.append((key, dates, values))
    if not segments:
        return None

    with _chart_lock:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
        from matplotlib.figure import Figure
        from matplotlib.ticker import FuncFormatter

        figure = Figure(figsize=(11, 7.5), dpi=130)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left=.12, right=.96, bottom=.29, top=.735)
        figure.text(.065, .94, 'Estimated task usage burndown', fontsize=22,
                    weight='bold', color='#142c3a')
        figure.text(.065, .885, textwrap.shorten(str(report['client']), width=75, placeholder='…'),
                    fontsize=14, color='#334b58', parse_math=False)
        first = month_offset(monthly[0]['month'], 0)
        last = min(month_offset(monthly[-1]['month'], 1), cutoff)
        figure.text(.065, .84, f'{first:%b %Y} – {last:%d %b %Y} · monthly allowance resets',
                    fontsize=11, color='#334b58')
        figure.text(.065, .79,
                    'Partial task data: unresolved tasks excluded; no confirmed remaining balance.' if partial else
                    'Each drop uses counted completed-ticket size estimates on its completion date.',
                    fontsize=10, weight='bold' if partial else 'normal',
                    color='#9a4a00' if partial else '#334b58')
        labels = set()
        for key, dates, values in segments:
            uncertain = key in partial
            label = ('Partial: allowance minus known estimates' if uncertain else
                     'Base allowance minus counted task estimates')
            axes.step(dates, [value / scale for value in values], where='post',
                      color='#b86e22' if uncertain else '#147d92',
                      linestyle='--' if uncertain else '-', linewidth=2.2,
                      label=label if label not in labels else '_nolegend_')
            labels.add(label)
        for index, (start, end) in enumerate(missing):
            axes.axvspan(start, end, facecolor='#e8edf0', alpha=.8, hatch='//',
                         edgecolor='#a8b4bc', linewidth=0,
                         label='Allowance not configured (no balance)' if index == 0 else '_nolegend_')
        for month in monthly[1:]:
            boundary = month_offset(month['month'], 0)
            if boundary <= cutoff:
                axes.axvline(boundary, color='#b8c5cc', linestyle=':', linewidth=.7)
        axes.axhline(0, color='#647580', linewidth=.8)
        if first < last:
            axes.set_xlim(first, last)
        locator = AutoDateLocator(minticks=3, maxticks=8)
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        axes.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{value:g}h'))
        axes.set_xlabel('Completion date · Australia/Melbourne')
        axes.set_ylabel('Base allowance minus counted task estimates (hours)')
        axes.grid(axis='y', color='#e8eef1', linewidth=.7)
        axes.set_axisbelow(True)
        axes.tick_params(length=0, pad=7)
        for spine in axes.spines.values():
            spine.set_visible(False)
        figure.legend(*axes.get_legend_handles_labels(), loc='lower left',
                      bbox_to_anchor=(.11, .15), frameon=False, fontsize=9)
        note = 'Base monthly allowance only; rollover is excluded. This is not contractual entitlement.'
        if missing:
            note += (f' No configured allowance for {len(missing)} '
                     + ('month' if len(missing) == 1 else 'months') + '; shaded gaps have no balance.')
        figure.text(.065, .095, '\n'.join(textwrap.wrap(note, width=118)), fontsize=9, color='#334b58')
        figure.text(.065, .035, 'Completed-ticket size estimates; work in progress and clocked time excluded.\n'
                    + cutoff.strftime('As of %d %b %Y, %I:%M %p %Z.'), fontsize=9, color='#334b58')
        output = BytesIO()
        canvas.print_png(output)
        return output.getvalue()
