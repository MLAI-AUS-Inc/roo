"""Local contributor charts from the counted rows in a scoped Studio hours report."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from io import BytesIO
from itertools import accumulate
import textwrap

from .studio_reports import TZ, _chart_lock, display_hours, month_offset
from .timesheets import TimesheetError, timestamp


def _work_day(row):
    """Only use daily precision when the counted quantity has a single supported day."""
    if row.get('source', 'completed_ticket_size') == 'completed_ticket_size':
        return timestamp(row['completed_at']).astimezone(TZ).date()
    if (row.get('month_allocation') == 'work_dates' and row.get('work_start')
            and row['work_start'] == row.get('work_end')):
        return date.fromisoformat(row['work_end'])
    return None


def _contributors(rows):
    names = {row['builder_id']: row['builder'] for row in rows}
    name_counts = Counter(names.values())
    seen = Counter()
    labels = {}
    for person in sorted(names, key=lambda key: (names[key].casefold(), key)):
        name = names[person]
        seen[name] += 1
        labels[person] = f'{name} ({seen[name]})' if name_counts[name] > 1 else name
    return labels


def _composition(axes, report, people, colors, *, monthly=False):
    from matplotlib.colors import to_rgb

    scale = report.get('unit_scale', 4)
    if monthly:
        labels = {m['month']: month_offset(m['month'], 0).strftime('%b %Y') for m in report['monthly']}
        if any(not row['month'] for row in report['rows']):
            labels[''] = 'Month unallocated'
        uncertain = _unavailable_months(report['monthly'])
    else:
        labels = report['projects']
        uncertain_names = {entry['project'] for entry in report.get('exceptions', [])}
        uncertain = {key for key, name in labels.items() if name in uncertain_names}
    counts = defaultdict(Counter)
    totals = Counter()
    for row in report['rows']:
        key = (row['month'] or '') if monthly else row['project_id']
        counts[row['builder_id']][key] += row['units']
        totals[key] += row['units']
    order = list(labels) if monthly else sorted(labels, key=lambda key: (-totals[key], labels[key].casefold(), key))
    positions = range(len(order))
    left = [0] * len(order)
    longest = max([scale, *totals.values()]) / scale
    for person, name in people.items():
        units = [counts[person][key] for key in order]
        bars = axes.barh(positions, [n / scale for n in units], left=[n / scale for n in left],
                         color=colors[person], height=.58, label=f'{name} · {display_hours(sum(units), scale)}h')
        r, g, b = to_rgb(colors[person])
        ink = '#142c3a' if .299 * r + .587 * g + .114 * b > .62 else 'white'
        for bar, amount in zip(bars, units):
            label = display_hours(amount, scale)
            if amount / scale >= longest * (.025 + .013 * len(label)):
                axes.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2,
                          label, ha='center', va='center', color=ink, fontsize=9)
        left = [prior + value for prior, value in zip(left, units)]
    axes.set_yticks(positions, [textwrap.fill(labels[key], width=30) for key in order], parse_math=False)
    for y, key in enumerate(order):
        units = totals[key]
        label = display_hours(units, scale) + 'h'
        if key in uncertain or report.get('complete') is False:
            label = label + ' counted' if units else 'No counted hours'
        if monthly and not units:
            label = 'No hours attributed' if key not in uncertain else 'Hours unavailable'
        axes.annotate(label, (units / scale, y), xytext=(5, 0), textcoords='offset points',
                      va='center', fontsize=9, weight='bold', color='#334b58')
    axes.set_xlim(0, longest * 1.3)
    axes.set_ylim(len(order) - .5, -.5)
    axes.set_xlabel('Hours counted')


def _only_reviewed_projects(report):
    return (report.get('canonical_only', True) and not report.get('linear_project_ids')
            and report['basis'] == 'invoice_first_recorded_hours')


def _unavailable_months(monthly):
    return {month['month'] for month in monthly if month.get('coverage_available') is False
            or (month.get('unresolved') and not month['units'])}


def _trend(axes, report, people, colors, start, end):
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    from matplotlib.ticker import MaxNLocator

    scale = report.get('unit_scale', 4)
    dated = [row for row in report['rows'] if row['month']]
    if not dated:
        axes.set_axis_off()
        axes.text(.5, .5, 'No dated hours to plot for this period.', transform=axes.transAxes, ha='center')
        return
    gaps = _unavailable_months(report['monthly'])
    days = [_work_day(row) for row in dated]
    if report.get('recorded_snapshot') and _only_reviewed_projects(report) and not report.get('reviewed_coverage_varies'):
        end = min(end, date.fromisoformat(report['coverage_through']))
        if report.get('coverage_start'):
            start = max(start, date.fromisoformat(report['coverage_start']))
    daily = (not gaps and not report.get('reviewed_coverage_varies')
             and all(day is not None and start <= day <= end for day in days))
    counts = defaultdict(Counter)
    for row, day in zip(dated, days):
        counts[row['builder_id']][day if daily else row['month']] += row['units']

    if daily:
        axes.set_xlabel('Work or task completion date · Melbourne')
        locator = AutoDateLocator(minticks=3, maxticks=8)
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        if start < end:
            axes.set_xlim(start, end)
        else:
            axes.set_xlim(start - timedelta(days=1), end + timedelta(days=1))
    else:
        months = [month['month'] for month in report['monthly']]
        positions = range(1, len(months) + 1)
        labels = ['Start'] + [month_offset(key, 0).strftime('%b %y') for key in months]
        stride = max(1, (len(months) + 6) // 7)
        shown = sorted({0, *range(1, len(labels), stride), len(months)})
        axes.set_xticks(shown, [labels[i] for i in shown])
        axes.set_xlim(-.05, len(months) + .05)
        axes.set_xlabel('Cumulative hours by reporting month')
        for index, key in zip(positions, months):
            if key in gaps:
                axes.axvspan(index - .45, index + .45, facecolor='#e6e9eb', hatch='//',
                             edgecolor='#aab6bc', linewidth=0)

    for person, name in people.items():
        if person not in counts:
            continue  # No dated quantity for this contributor.
        amounts = counts[person]
        label = f'{name} · {display_hours(sum(amounts.values()), scale)}h'
        if daily:
            keys = sorted(amounts)
            x = [start, *keys, end]
            y = [0, *accumulate(amounts[key] for key in keys), sum(amounts.values())]
            axes.step(x, [n / scale for n in y], where='post', color=colors[person],
                      linewidth=2.1, marker='.', markersize=5, label=label)
        else:
            y = [0 if months[0] not in gaps else float('nan')]
            for key, total in zip(months, accumulate(amounts[key] for key in months)):
                y.append(total / scale if key not in gaps else float('nan'))
            axes.plot(range(len(y)), y, color=colors[person], linewidth=2.1, marker='o',
                      markersize=4, label=label)
    axes.set_ylim(bottom=0)
    axes.yaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    axes.set_ylabel('Cumulative hours counted')


def _notes(report, breakdown):
    basis = report['basis']
    if basis == 'completed_ticket_size_hours':
        notes = ['Completed-ticket size estimates. Work in progress is excluded.']
    elif basis == 'invoice_first_recorded_hours':
        notes = ['Invoice and recorded work quantities. Clock time and billable entitlement are unverified.']
    else:
        notes = ['Includes invoice and recorded work quantities and completed-ticket size estimates.']
    if report.get('work_reviewed_through'):
        notes.append(f"Recorded work review complete through {report['work_reviewed_through']}.")
        if report.get('latest_included_work_date'):
            notes.append(f"Latest included work period ends {report['latest_included_work_date']}.")
    elif report.get('recorded_snapshot'):
        prefix = 'Historical snapshot. ' if report.get('source_stale') else ''
        if report.get('reviewed_coverage_varies'):
            line = prefix + 'Reviewed coverage varies by project. Earliest cutoff ' + report['coverage_through'] + '.'
        else:
            line = prefix + 'Reviewed work coverage through ' + report['coverage_through'] + '.'
        if not _only_reviewed_projects(report):
            line += ' Other project sources use the report cutoff.'
        notes.append(line)
    if report.get('complete') is False:
        notes.append('Some work is unresolved. Only attributable hours are shown.' if breakdown == 'monthly'
                     else 'Only counted work is shown. See the report for coverage and unresolved work.')
    scale = report.get('unit_scale', 4)
    unallocated = sum(row['units'] for row in report['rows'] if not row['month'])
    if unallocated:
        treatment = {'stacked': 'included in project totals', 'monthly': 'shown separately',
                     'cumulative': 'omitted from the lines'}[breakdown]
        notes.append(f'{display_hours(unallocated, scale)}h have no assigned month and are {treatment}.')
    if report.get('estimated_month_units'):
        label = ('estimated dates or month allocations' if report.get('work_reviewed_through')
                 else 'estimated month assignments')
        notes.append(f'{display_hours(report["estimated_month_units"], scale)}h use {label}.')
    return notes


def render_contributor_chart(report, breakdown):
    """Render one PNG from the report's counted units, without a source reread."""
    from matplotlib import colormaps
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    if breakdown not in {'stacked', 'cumulative', 'monthly'}:
        raise ValueError('Unknown contributor chart')
    if any(row['project_id'] not in report['projects'] for row in report['rows']):
        raise TimesheetError('contributor_report_scope_mismatch')
    people = _contributors(report['rows'])
    palette = ('#147d92', '#c55d12', '#7465ab', '#008462', '#ad465f', '#286aac',
               '#976816', '#925b9b', '#536a38', '#575757')
    colors = dict(zip(people, palette if len(people) <= len(palette) else
                      colormaps['turbo'].resampled(len(people)).colors))
    monthly = report['monthly']
    start = month_offset(monthly[0]['month'], 0).date()
    end = min(timestamp(report['generated_at']).astimezone(TZ).date(),
              month_offset(monthly[-1]['month'], 1).date() - timedelta(days=1))
    projects = list(report['projects'].values())
    scope = projects[0] if len(projects) == 1 else f'{len(projects)} projects'
    subtitle = f'{start:%d %b %Y} – {end:%d %b %Y} · {report["client"]} · {scope}'
    if breakdown == 'monthly':
        subtotal = display_hours(sum(row['units'] for row in report['rows']), report.get('unit_scale', 4))
        if report['rows'] or report.get('complete') is True:
            subtitle += f' · {subtotal}h counted'
    notes = _notes(report, breakdown)
    if breakdown == 'cumulative' and _unavailable_months(monthly) and any(row['month'] for row in report['rows']):
        notes.append('Shaded months have no supported subtotal.')
    with _chart_lock:
        footer = .38 + .20 * len(notes)
        legend_rows = (len(people) + 2) // 3
        label_lines = max((len(textwrap.wrap(name, width=32)) for name in people.values()), default=1)
        legend_space = (.27 + .17 * (label_lines - 1)) * legend_rows
        header = 1.25 + legend_space
        project_lines = max((len(textwrap.wrap(name, width=30)) for name in projects), default=1)
        plot_height = max(2, len(projects) * max(.64, .19 * project_lines)) if breakdown == 'stacked' else 3.3
        if breakdown == 'monthly':
            plot_height = max(2, .46 * (len(monthly) + int(any(not row['month'] for row in report['rows']))))
        height = header + plot_height + footer + .55
        figure = Figure(figsize=(11, height), dpi=135)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left={'stacked': .28, 'monthly': .17, 'cumulative': .11}[breakdown], right=.95,
                               top=1 - header / height, bottom=(footer + .55) / height)
        title = 'Project hours by contributor' if breakdown == 'stacked' else 'Cumulative hours by contributor'
        if breakdown == 'monthly':
            title = 'Monthly hours by contributor'
        figure.text(.06, 1 - .43 / height, title, fontsize=21, weight='bold', color='#142c3a')
        figure.text(.06, 1 - .73 / height, textwrap.shorten(subtitle, width=115, placeholder='…'),
                    fontsize=11, color='#334b58', parse_math=False)
        if report.get('period_has_coverage') is False:
            axes.set_axis_off()
            axes.text(.5, .5, 'Hours unavailable for this period.', transform=axes.transAxes, ha='center')
        elif breakdown in {'stacked', 'monthly'}:
            _composition(axes, report, people, colors, monthly=breakdown == 'monthly')
        else:
            _trend(axes, report, people, colors, start, end)
        axes.set_axisbelow(True)
        axes.grid(axis='y' if breakdown == 'cumulative' else 'x', color='#e5eaed')
        axes.tick_params(length=0, labelsize=9, pad=6)
        for spine in axes.spines.values():
            spine.set_visible(False)
        handles, labels = axes.get_legend_handles_labels()
        if handles:
            legend = figure.legend(handles, [textwrap.fill(name, width=32) for name in labels],
                                   loc='upper left', bbox_to_anchor=(.06, 1 - .85 / height),
                                   ncols=min(3, len(handles)), frameon=False, fontsize=9)
            for label in legend.get_texts():
                label.set_parse_math(False)
        figure.text(.06, .20 / height, '\n'.join(notes), fontsize=9, color='#465d67', linespacing=1.45)
        with BytesIO() as output:
            canvas.print_png(output)
            return output.getvalue()
