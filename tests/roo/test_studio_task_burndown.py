"""Task allowance curves use counted estimates, never payments or guessed work."""
from copy import deepcopy
import pytest

from roo.studio_reports import TZ, build_client_report, month_offset
from roo.studio_task_burndown import render_task_burndown
from tests.roo.test_studio_reports import NOW, setup, ticket
from roo.timesheets import TimesheetError, timestamp


@pytest.fixture
def plotted(monkeypatch):
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

    result = {'steps': [], 'text': []}
    step, text = Axes.step, Figure.text

    def capture_step(axes, dates, values, *args, **kwargs):
        result['axes'] = axes
        result['steps'].append((list(dates), list(values), kwargs))
        return step(axes, dates, values, *args, **kwargs)

    def capture_text(figure, x, y, value, *args, **kwargs):
        result['text'].append(value)
        return text(figure, x, y, value, *args, **kwargs)

    monkeypatch.setattr(Axes, 'step', capture_step)
    monkeypatch.setattr(Figure, 'text', capture_text)
    return result


def report(setup, data, **selector):
    return build_client_report(setup.config, setup.clients['UMARK'], selector, data, NOW)


@pytest.mark.parametrize('scale', [4, 100, 1_000_000])
def test_counted_completion_dates_burn_the_same_units_and_reset_each_month(setup, plotted, scale):
    value = report(setup, [ticket(1, size='Large (L)', completed='2026-08-10T01:00:00Z'),
                           ticket(2, size='Medium (M)', completed='2026-08-25T01:00:00Z'),
                           ticket(3, completed='2026-09-05T01:00:00Z')],
                   month='2026-08', months=2)
    for month in value['monthly']:
        month['units'] *= scale // 4
        month['allowance_units'] *= scale // 4
    for row in value['rows']:
        row['units'] *= scale // 4
    value['unit_scale'] = scale
    assert render_task_burndown(value).startswith(b'\x89PNG')
    august, september = plotted['steps']
    assert august[1] == [40, 37, 35, 35]
    assert september[1] == [40, 39, 39]
    assert [day.strftime('%Y-%m-%d') for day in august[0]] == [
        '2026-08-01', '2026-08-10', '2026-08-25', '2026-09-01']
    assert september[0][0] == august[0][-1]
    assert august[2]['where'] == september[2]['where'] == 'post'


def test_month_overrides_reset_without_carrying_over_unused_or_negative_hours(setup, plotted):
    setup.clients['UMARK']['monthly_allowances'] = {'2026-08': 2, '2026-09': 3}
    value = report(setup, [ticket(1, size='Extra Large (XL)', completed='2026-08-10T01:00:00Z'),
                           ticket(2, completed='2026-09-05T01:00:00Z')],
                   month='2026-08', months=2)
    render_task_burndown(value)
    assert plotted['steps'][0][1] == [2, -3, -3]
    assert plotted['steps'][1][1] == [3, 2, 2]
    text = '\n'.join(plotted['text'])
    assert 'rollover is excluded' in text
    assert 'not contractual entitlement' in text


def test_same_day_completion_quantities_are_aggregated_without_rounding(setup, plotted):
    value = report(setup, [ticket(1, size='Extra Small (XS)', completed='2026-09-10T01:00:00Z'),
                           ticket(2, size='Extra Small (XS)', completed='2026-09-10T02:00:00Z')])
    render_task_burndown(value)
    dates, values, _ = plotted['steps'][0]
    assert len(dates) == 3
    assert values == [40, 39.5, 39.5]
    assert dates[1].strftime('%Y-%m-%d') == '2026-09-10'


def test_current_month_stops_at_request_cutoff_and_never_plots_future_work(setup, plotted):
    value = report(setup, [ticket(1, completed='2026-09-22T03:00:00Z'),
                           ticket(2, completed='2026-09-22T05:00:00Z'),
                           ticket(3, completed='2026-09-23T01:00:00Z')])
    assert len(value['rows']) == 1
    render_task_burndown(value)
    dates, values, _ = plotted['steps'][0]
    cutoff = timestamp(value['generated_at']).astimezone(TZ)
    assert max(dates) == cutoff
    assert values == [40, 39, 39]
    assert dates[1].date() == cutoff.date()
    assert all(at <= cutoff for at in dates)
    assert 'As of 22 Sep 2026' in '\n'.join(plotted['text'])


def test_unresolved_tasks_have_no_invented_hours_or_confirmed_remaining_claim(setup, plotted):
    value = report(setup, [ticket(1), ticket(2, labels=[])])
    assert value['monthly'][0]['unresolved'] == 1
    render_task_burndown(value)
    assert plotted['steps'][0][1] == [40, 39, 39]
    assert plotted['steps'][0][2]['linestyle'] == '--'
    assert plotted['steps'][0][2]['label'] == 'Partial: allowance minus known estimates'
    text = '\n'.join(plotted['text'])
    assert 'Partial task data' in text
    assert 'no confirmed remaining balance' in text
    assert 'hours remaining' not in text
    assert 'remaining' not in plotted['axes'].get_ylabel().lower()


def test_unresolved_months_are_distinct_from_other_complete_months(setup, plotted):
    value = report(setup, [ticket(1, labels=[], completed='2026-08-10T01:00:00Z'),
                           ticket(2, completed='2026-09-05T01:00:00Z')],
                   month='2026-08', months=2)
    render_task_burndown(value)
    assert [step[2]['linestyle'] for step in plotted['steps']] == ['--', '-']
    assert plotted['steps'][0][1] == [40, 40]


def test_staff_overview_without_allowance_returns_none_before_rendering(setup, monkeypatch):
    setup.clients['UMARK']['monthly_hours'] = None
    value = report(setup, [ticket()])

    def forbidden(*args, **kwargs):
        pytest.fail('No-budget overview attempted chart rendering')

    monkeypatch.setattr('matplotlib.figure.Figure', forbidden)
    assert render_task_burndown(value) is None


def test_month_without_allowance_has_a_gap_instead_of_an_invented_balance(setup, plotted):
    setup.clients['UMARK']['monthly_allowances'] = {'2026-08': None}
    value = report(setup, [ticket(1, size='Extra Large (XL)', completed='2026-08-10T01:00:00Z'),
                           ticket(2, completed='2026-09-05T01:00:00Z')],
                   month='2026-08', months=2)
    render_task_burndown(value)
    assert len(plotted['steps']) == 1
    assert plotted['steps'][0][0][0].strftime('%Y-%m') == '2026-09'
    assert plotted['steps'][0][1] == [40, 39, 39]
    assert 'No configured allowance for 1 month' in ' '.join(' '.join(plotted['text']).split())
    assert 'Allowance not configured (no balance)' in plotted['axes'].get_legend_handles_labels()[1]


def test_ten_year_history_has_bounded_month_segments_and_rejects_longer_period(setup, plotted):
    value = report(setup, [])
    value['monthly'] = [{'month': month_offset('2016-10', index).strftime('%Y-%m'),
                         'units': 0, 'allowance_units': 160, 'unresolved': 0} for index in range(120)]
    value['selector'].update(month='2016-10', months=120, project_to_date=True)
    assert render_task_burndown(value).startswith(b'\x89PNG')
    assert len(plotted['steps']) == 120
    assert sum(len(step[0]) for step in plotted['steps']) == 240
    value['monthly'].insert(0, {'month': '2016-09', 'units': 0, 'allowance_units': 160, 'unresolved': 0})
    with pytest.raises(TimesheetError, match='task_burndown_period_too_long'):
        render_task_burndown(value)


@pytest.mark.parametrize('change', ['basis', 'row_source'])
def test_invoice_or_recorded_work_cannot_be_mislabeled_as_task_estimates(setup, plotted, change):
    value = deepcopy(report(setup, [ticket()]))
    if change == 'basis':
        value['basis'] = 'invoice_recorded_hours'
    else:
        value['rows'][0]['source'] = 'reviewed_invoice'
    assert render_task_burndown(value) is None
    assert not plotted['steps']
