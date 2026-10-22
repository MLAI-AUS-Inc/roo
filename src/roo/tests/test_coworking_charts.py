"""Numerical and PNG contract tests; no backend, LLM or Slack calls."""
import struct
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.coworking_charts import build_coworking_series, render_coworking_chart


def report_for(counts, *, start=date(2026, 6, 17)):
    return {
        "range": {
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=len(counts) - 1)).isoformat(),
            "source": "active_coworking_bookings",
        },
        "daily": [
            {"date": (start + timedelta(days=i)).isoformat(), "booked_users": count}
            for i, count in enumerate(counts) if count is not None
        ],
    }


def test_daily_counts_are_sorted_and_trend_includes_weekend_zeros():
    report = report_for([7, 14, 0, 0, 7, 14, 7, 0])
    report["daily"].reverse()
    series = build_coworking_series(report)
    assert series.dates == tuple(date(2026, 6, 17) + timedelta(days=i) for i in range(8))
    assert series.booked_people == (7, 14, 0, 0, 7, 14, 7, 0)
    assert series.moving_average == (None,) * 6 + (7.0, 6.0)


def test_missing_days_remain_gaps_until_a_complete_window_is_available():
    series = build_coworking_series(report_for([7, None] + [7] * 7))
    assert series.booked_people[1] is None
    assert series.moving_average == (None,) * 8 + (7.0,)


@pytest.mark.parametrize("counts", [[0], [1, 2, 3], [0] * 92, [1, None, 3, 0, 5, 6, 7], [2] * 366])
def test_render_is_a_readable_png_for_short_empty_sparse_and_long_ranges(counts):
    png = render_coworking_chart(report_for(counts), today=date(2026, 6, 17))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1920, 1040)
    assert 10_000 < len(png) < 2_000_000


@pytest.mark.parametrize("count", [-1, 1.5, True, "3", None])
def test_invalid_counts_are_not_silently_plotted(count):
    report = report_for([1])
    report["daily"][0]["booked_users"] = count
    with pytest.raises(ValueError, match="non-negative integers"):
        build_coworking_series(report)


def test_duplicate_day_is_not_double_counted():
    report = report_for([1])
    report["daily"].append(report["daily"][0])
    with pytest.raises(ValueError, match="unique"):
        build_coworking_series(report)


def test_no_daily_data_does_not_fabricate_a_zero_chart():
    with pytest.raises(ValueError, match="unavailable"):
        build_coworking_series(report_for([None] * 7))


def test_chart_rejects_a_different_source():
    report = report_for([1])
    report["range"]["source"] = "door_checkins"
    with pytest.raises(ValueError, match="source"):
        build_coworking_series(report)


def test_chart_range_is_bounded():
    with pytest.raises(ValueError, match="1 to 366"):
        build_coworking_series(report_for([1] * 367))
