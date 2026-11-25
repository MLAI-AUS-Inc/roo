"""Charts must reconcile to counted units without assigning unknown work dates."""
from datetime import date
from math import isnan

import pytest

from roo.studio_contributor_charts import render_contributor_chart
from roo.timesheets import TimesheetError


def task(person, project, date_time, units, month):
    return {'month': month, 'builder_id': person.lower(), 'builder': person,
            'project_id': project, 'completed_at': date_time, 'units': units}


def report():
    return {'client': 'Example Studio', 'projects': {'a': 'Master App', 'b': 'Member App', 'c': 'Third project'},
            'unit_scale': 4, 'basis': 'completed_ticket_size_hours', 'complete': True,
            'generated_at': '2026-10-16T00:00:00+11:00',
            'monthly': [{'month': '2026-09', 'units': 18}, {'month': '2026-10', 'units': 12}],
            'rows': [task('Shan', 'a', '2026-09-04T02:00:00Z', 14, '2026-09'),
                     task('Tom', 'b', '2026-09-09T02:00:00Z', 4, '2026-09'),
                     task('Shan', 'b', '2026-09-30T15:00:00Z', 4, '2026-10'),
                     task('Tom', 'a', '2026-10-10T02:00:00Z', 8, '2026-10')]}


@pytest.fixture
def draw(monkeypatch):
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    drawn = []
    original = FigureCanvasAgg.print_png

    def capture(canvas, *args, **kwargs):
        drawn.append(canvas.figure)
        return original(canvas, *args, **kwargs)

    monkeypatch.setattr(FigureCanvasAgg, 'print_png', capture)

    def render(data, view):
        assert render_contributor_chart(data, view).startswith(b'\x89PNG\r\n\x1a\n')
        return drawn[-1]

    return render


def test_stacked_projects_and_running_totals_reconcile_by_person(draw):
    from matplotlib.colors import to_rgba

    data = report()
    bars = draw(data, 'stacked').axes[0]
    assert [label.get_text() for label in bars.get_yticklabels()] == ['Master App', 'Member App', 'Third project']
    shan, tom = bars.containers
    assert [bar.get_width() for bar in shan] == [3.5, 1, 0]
    assert [bar.get_width() for bar in tom] == [2, 1, 0]
    assert [bar.get_x() for bar in tom] == [3.5, 1, 0]
    trend = draw(data, 'cumulative').axes[0]
    shan_line, tom_line = trend.lines
    assert shan_line.get_drawstyle() == 'steps-post'
    assert list(shan_line.get_ydata()) == [0, 3.5, 4.5, 4.5]
    assert list(tom_line.get_ydata()) == [0, 1, 3, 3]
    assert list(shan_line.get_xdata()) == [date(2026, 9, 1), date(2026, 9, 4), date(2026, 10, 1), date(2026, 10, 16)]
    for bars, line in [(shan, shan_line), (tom, tom_line)]:
        assert bars[0].get_facecolor() == to_rgba(line.get_color())
        assert sum(bar.get_width() for bar in bars) == line.get_ydata()[-1]


def test_monthly_claims_use_allocated_month_and_skip_gaps_and_undated_work(draw):
    data = report()
    data.update(unit_scale=1_000_000, basis='invoice_first_recorded_hours', complete=False,
                recorded_snapshot=True, coverage_through='2026-10-12', source_stale=True,
                estimated_month_units=1_125_000)
    data['monthly'][0].update(coverage_available=False, units=0)
    base = dict(source='reviewed_invoice', builder_id='tom', builder='Tom', project_id='a',
                work_start='2026-08-15', work_end='2026-10-01', completed_at='2026-10-01')
    data['rows'] = [{**base, 'month': '2026-10', 'units': 1_125_000, 'month_allocation': 'estimated'},
                    {**base, 'month': '', 'units': 2_500_000, 'month_allocation': 'unallocated'}]
    bars = draw(data, 'stacked')
    assert bars.axes[0].containers[0][0].get_width() == 3.625
    assert '2.5h have no assigned month and are included in project totals.' in bars.texts[-1].get_text()

    figure = draw(data, 'cumulative')
    line, = figure.axes[0].lines
    assert isnan(line.get_ydata()[0])
    assert list(line.get_ydata()[1:]) == [1.125]
    assert line.get_linestyle() == 'None'
    assert figure.axes[0].get_xlabel() == 'Cumulative hours by reporting month'
    notes = figure.texts[-1].get_text()
    for phrase in ['2.5h have no assigned month and are omitted', 'estimated month assignments',
                   'Historical snapshot', '2026-10-12', 'Shaded months']:
        assert phrase in notes


def test_single_month_single_day_recorded_rows_use_dated_steps_up_to_coverage(draw):
    data = report()
    data.update(basis='invoice_first_recorded_hours', recorded_snapshot=True,
                coverage_start='2026-10-02', coverage_through='2026-10-12')
    data['monthly'] = data['monthly'][1:]
    data['rows'] = [dict(source='submitted_linear', builder_id='tom', builder='Tom', project_id='a',
                         work_start=day, work_end=day, month='2026-10', units=units, month_allocation='work_dates')
                    for day, units in [('2026-10-03', 3), ('2026-10-09', 2)]]
    line, = draw(data, 'cumulative').axes[0].lines
    assert list(line.get_xdata()) == [date(2026, 10, i) for i in [2, 3, 9, 12]]
    assert list(line.get_ydata()) == [0, .75, 1.25, 1.25]


@pytest.mark.parametrize('view', ['stacked', 'cumulative'])
def test_no_coverage_and_out_of_scope_rows_cannot_show_hours(draw, view):
    data = report()
    data.update(rows=[], period_has_coverage=False)
    figure = draw(data, view)
    assert 'Hours unavailable for this period.' in [t.get_text() for t in figure.axes[0].texts]
    data['rows'] = [task('Other client person', 'other', '2026-09-04T02:00:00Z', 4, '2026-09')]
    with pytest.raises(TimesheetError, match='contributor_report_scope_mismatch'):
        render_contributor_chart(data, view)


def test_different_owner_coverage_keeps_later_recorded_hours(draw):
    data = report()
    data.update(basis='invoice_first_recorded_hours', recorded_snapshot=True, coverage_through='2026-09-12',
                reviewed_coverage_varies=True)
    data['rows'] = [{**row, 'source': 'reviewed_invoice', 'month_allocation': 'work_dates',
                     'work_start': row['month'] + '-10', 'work_end': row['month'] + '-10'}
                    for row in data['rows']]
    figure = draw(data, 'cumulative')
    assert [line.get_ydata()[-1] for line in figure.axes[0].lines] == [4.5, 3]
    assert 'Reviewed coverage varies by project' in figure.texts[-1].get_text()
    assert figure.axes[0].get_xlabel() == 'Cumulative hours by reporting month'


def test_people_with_the_same_name_and_undated_only_work_stay_distinct(draw):
    data = report()
    data['rows'] = [task('Sam', 'a', '2026-10-04T02:00:00Z', 8, '2026-10'),
                    dict(builder_id='second-sam', builder='Sam', project_id='a', month='', units=14)]
    bars = draw(data, 'stacked')
    assert [entry.get_text() for entry in bars.legends[0].texts] == ['Sam (1) · 2h', 'Sam (2) · 3.5h']
    trend = draw(data, 'cumulative')
    assert [entry.get_text() for entry in trend.legends[0].texts] == ['Sam (1) · 2h']
    assert '3.5h have no assigned month' in trend.texts[-1].get_text()

def test_legacy_projects_retain_later_activity_in_a_mixed_portfolio(draw):
    data = report()
    data.update(canonical_only=False, recorded_snapshot=True, coverage_through='2026-09-12',
                basis='reviewed_invoices_and_completed_ticket_sizes')
    early = data['rows'][0]
    early.update(source='reviewed_invoice', month_allocation='work_dates',
                 work_start='2026-09-04', work_end='2026-09-04')
    figure = draw(data, 'cumulative')
    assert all(line.get_xdata()[-1] == date(2026, 10, 16) for line in figure.axes[0].lines)
    assert [line.get_ydata()[-1] for line in figure.axes[0].lines] == [4.5, 3]
    assert 'Other project sources use the report cutoff' in figure.texts[-1].get_text()


def test_one_month_aggregate_does_not_flatten_dated_steps_or_create_new_hours(draw):
    data = report()
    data.update(basis='reviewed_invoices_and_completed_ticket_sizes', monthly=data['monthly'][1:])
    dated_step = task('Alice', 'a', '2026-10-04T02:00:00Z', 8, '2026-10')
    recorded = dict(source='submitted_linear', builder_id='alice', builder='Alice', project_id='a',
                    work_start='2026-10-09', work_end='2026-10-09', month='2026-10',
                    units=7, month_allocation='work_dates')
    # A range can have a populated 'completed_at' for display. It is not a
    # supported single work day, and neither endpoint may receive the hours.
    aggregate = dict(recorded, units=12, work_start='2026-10-01', work_end='2026-10-11',
                     completed_at='2026-10-11T09:00:00+11:00')
    separate_person = dict(aggregate, builder_id='bob', builder='Bob', units=16)
    data['rows'] = [dated_step, recorded, aggregate, separate_person]
    data['monthly'][0]['units'] = sum(row['units'] for row in data['rows'])
    result = draw(data, 'cumulative')
    daily, monthly = result.axes
    line, = daily.lines
    assert line.get_drawstyle() == 'steps-post'
    assert list(line.get_xdata()) == [date(2026, 10, i) for i in (1, 4, 9, 16)]
    assert list(line.get_ydata()) == [0, 2, 3.75, 3.75]
    assert daily.get_ylabel() == 'Dated hours only'
    assert [list(dot.get_ydata()) for dot in monthly.lines] == [[6.75], [4]]
    assert all(dot.get_linestyle() == 'None' for dot in monthly.lines)
    assert sum(dot.get_ydata()[-1] for dot in monthly.lines) == data['monthly'][0]['units'] / data['unit_scale']
    assert [entry.get_text() for entry in result.legends[0].texts] == ['Alice · 6.75h', 'Bob · 4h']
    assert '7h lack a supported single day' in result.texts[-1].get_text()


def test_month_only_totals_never_draw_an_interpolated_line(draw):
    data = report()
    data.update(basis='invoice_first_recorded_hours', monthly=data['monthly'][1:])
    data['rows'] = [dict(source='reviewed_invoice', builder_id='alice', builder='Alice', project_id='a',
                         work_start='2026-10-02', work_end='2026-10-15', month='2026-10',
                         units=12, month_allocation='work_dates')]
    data['monthly'][0]['units'] = 12
    chart = draw(data, 'cumulative')
    axes, = chart.axes
    dot, = axes.lines
    assert [label.get_text() for label in axes.get_xticklabels()] == ['Oct 26']
    assert list(dot.get_ydata()) == [3] and dot.get_linestyle() == 'None'
    assert 'Reporting-month markers show totals, not dates worked.' in chart.texts[-1].get_text()
