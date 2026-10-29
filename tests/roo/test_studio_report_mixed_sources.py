"""Reviewed project cutoffs must not relabel fresh, authorized task estimates."""
from copy import deepcopy

import pytest

from roo.studio_reports import render_chart, reviewed_cutoff_notice, summary
from tests.roo.test_studio_report_linear_policy import (
    devashi, finance_setup, request, task_client,
)


@pytest.fixture
def reports(task_client):
    mixed = task_client.service.report_for_request(request('USAM', client='all'))
    canonical = task_client.service.report_for_request(request('UMARK'))
    for report in (mixed, canonical):
        # Mark's June work stays within its reviewed interval; the later task
        # rows in the mixed report remain fresh through the October request.
        report.update(source_as_of='2026-09-15T01:00:00+00:00',
                      coverage_through='2026-09-15', source_stale=True, partial=True)
    return mixed, canonical


@pytest.fixture
def chart_text(monkeypatch):
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

    captured = {'text': [], 'monthly_labels': []}
    original_text, original_bar = Figure.text, Axes.bar

    def text(figure, x, y, value, *args, **kwargs):
        captured['text'].append(value)
        return original_text(figure, x, y, value, *args, **kwargs)

    def bar(axes, x, height, *args, **kwargs):
        captured['axes'] = axes
        captured['monthly_labels'].extend(x)
        return original_bar(axes, x, height, *args, **kwargs)

    monkeypatch.setattr(Figure, 'text', text)
    monkeypatch.setattr(Axes, 'bar', bar)
    return captured


def test_mixed_summary_scopes_history_to_reviewed_projects_and_dates_fresh_task_reads(reports):
    mixed, _ = reports
    text = summary(mixed)
    assert 'Historical reviewed projects · [Studio] Master App · reviewed through 2026-09-15' in text
    assert 'Later activity for these reviewed projects is unavailable.' in text
    assert 'Linear task projects ([Studio] Study Nash)' in text
    assert 'read fresh through 11 Oct 2026, 11:00 AM AEDT' in text
    assert 'reviewed hour-units and ticket-size estimates across 5 months' in text
    assert 'Reviewed project source: 2026-09-15 · coverage through 2026-09-15' in text
    assert 'This view uses the same reviewed work records' not in text
    assert 'Requested activity outside reviewed coverage is unavailable' not in text
    assert 'not verified clock time or confirmed billable entitlement' in text
    # The October subtotal includes current tasks and must not inherit Mark's
    # September review cutoff as though it applied to the combined subtotal.
    assert 'October 2026 — 3h reviewed hour-units and ticket-size estimates' in text
    assert '· through 2026-09-15' not in next(line for line in text.splitlines() if 'September 2026 —' in line)


def test_pure_canonical_report_retains_existing_history_and_basis_wording(reports):
    _, canonical = reports
    assert reviewed_cutoff_notice(canonical) == (
        'Historical snapshot · reviewed through 2026-09-15. '
        'Last review: 15 Sep 2026 (Melbourne). Later activity is unavailable.')
    text = summary(canonical)
    assert 'This view uses the same reviewed work records as the paid-credit chart.' in text
    assert 'Source review: 2026-09-15 · coverage through 2026-09-15' in text
    assert 'Linear task projects' not in text


@pytest.mark.parametrize('stale', [False, True])
def test_mixed_monthly_chart_labels_both_sources_and_keeps_current_task_month_to_date(reports, chart_text, stale):
    mixed, _ = reports
    mixed['source_stale'] = stale
    assert render_chart(mixed).startswith(b'\x89PNG')
    text = '\n'.join(chart_text['text'])
    assert ('Historical reviewed projects: [Studio] Master App' if stale else
            'Reviewed projects: [Studio] Master App') in text
    assert 'Review coverage through 2026-09-15; later reviewed activity unavailable.' in text
    assert 'Linear task projects fresh through 11 Oct 2026, 11:00 AM AEDT.' in text
    assert 'reviewed hour-units and ticket-size estimates' in text
    assert 'Historical snapshot' not in text
    assert ('Historical reviewed projects' in text) is stale
    assert chart_text['axes'].get_ylabel() == 'Reviewed hour-units and ticket-size estimates'
    assert 'Oct 2026\n(to date)' in chart_text['monthly_labels']
    assert not any('(through 15 Sep)' in label for label in chart_text['monthly_labels'])


@pytest.mark.parametrize('breakdown', ['projects', 'clients'])
def test_mixed_total_charts_do_not_apply_review_cutoff_to_fresh_task_totals(reports, monkeypatch, breakdown):
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

    mixed, _ = reports
    texts, axes_seen = [], []
    original_text, original_xlabel = Figure.text, Axes.set_xlabel

    def text(figure, x, y, value, *args, **kwargs):
        texts.append(value)
        return original_text(figure, x, y, value, *args, **kwargs)

    def xlabel(axes, value, *args, **kwargs):
        axes_seen.append((axes, value))
        return original_xlabel(axes, value, *args, **kwargs)

    monkeypatch.setattr(Figure, 'text', text)
    monkeypatch.setattr(Axes, 'set_xlabel', xlabel)
    assert render_chart(mixed, breakdown=breakdown).startswith(b'\x89PNG')
    subtitle = next(value for value in texts if 'total hours by' in value)
    assert subtitle.endswith('· to date')
    assert '· through 2026-09-15' not in subtitle
    assert axes_seen[-1][1] == 'Reviewed hour-units and ticket-size estimates'


def test_pure_canonical_chart_retains_existing_snapshot_header_and_units(reports, chart_text):
    _, canonical = reports
    assert render_chart(canonical).startswith(b'\x89PNG')
    text = '\n'.join(chart_text['text'])
    assert 'Historical snapshot · reviewed through 2026-09-15' in text
    assert 'Later activity unavailable · source refresh required for a current report' in text
    assert 'Linear task projects' not in text
    assert chart_text['axes'].get_ylabel() == 'Invoice/recorded hour-units'


def test_current_mixed_summary_still_identifies_each_source_without_a_stale_warning(reports):
    mixed, _ = reports
    current = deepcopy(mixed)
    current['source_stale'] = False
    assert reviewed_cutoff_notice(current) == ''
    text = summary(current)
    assert 'Historical' not in text
    assert 'Reviewed projects ([Studio] Master App)' in text
    assert 'Linear task projects ([Studio] Study Nash)' in text
    assert 'read fresh through 11 Oct 2026, 11:00 AM AEDT' in text
