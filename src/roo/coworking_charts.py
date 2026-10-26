"""Local, aggregate-only PNG charts for the coworking booking report."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO
from threading import Lock


# Matplotlib has shared font/cache state even when using independent Figures.
_render_lock = Lock()


@dataclass(frozen=True)
class CoworkingSeries:
    dates: tuple[date, ...]
    booked_people: tuple[int | None, ...]
    moving_average: tuple[float | None, ...]


def build_coworking_series(report: dict) -> CoworkingSeries:
    """Preserve zeros and gaps; never infer attendance or missing booking counts."""
    report_range = report.get("range", {})
    source = report_range.get("source")
    if source and source != "active_coworking_bookings":
        raise ValueError("Unsupported coworking report source")
    start = date.fromisoformat(report_range["start_date"])
    end = date.fromisoformat(report_range["end_date"])
    days = (end - start).days + 1
    if not 1 <= days <= 366:
        raise ValueError("Coworking charts require a range of 1 to 366 days")

    counts: dict[date, int] = {}
    for row in report.get("daily", []):
        day = date.fromisoformat(row["date"])
        count = row["booked_users"]
        if type(count) is not int or count < 0:
            raise ValueError("Daily booked users must be non-negative integers")
        if not start <= day <= end or day in counts:
            raise ValueError("Daily report dates must be unique and within the range")
        counts[day] = count
    if not counts:
        raise ValueError("Daily booking data is unavailable")

    dates = tuple(start + timedelta(days=offset) for offset in range(days))
    values = tuple(counts.get(day) for day in dates)
    averages = []
    for index in range(days):
        window = values[max(0, index - 6):index + 1]
        averages.append(
            sum(window) / 7
            if len(window) == 7 and all(value is not None for value in window)
            else None
        )
    return CoworkingSeries(dates, values, tuple(averages))


def render_coworking_chart(report: dict, *, today: date | None = None) -> bytes:
    """Render a readable Slack PNG with a daily line and seven-day trend."""
    series = build_coworking_series(report)
    with _render_lock:
        # Lazy imports let the text report survive a missing plotting dependency.
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import AutoDateLocator, DateFormatter
        from matplotlib.figure import Figure
        from matplotlib.ticker import MaxNLocator

        figure = Figure(figsize=(12, 6.5), dpi=160, facecolor="#f8fafc")
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_subplot(111)
        figure.subplots_adjust(left=0.075, right=0.97, bottom=0.22, top=0.70)
        axes.set_facecolor("#f8fafc")
        figure.text(0.075, 0.92, "Coworking usage", fontsize=23, weight="bold", color="#0f172a")
        figure.text(
            0.075, 0.865,
            f"{series.dates[0]:%d %b %Y} – {series.dates[-1]:%d %b %Y}",
            fontsize=12, color="#475569",
        )
        figure.text(0.075, 0.82, "Active bookings · not door check-ins", fontsize=11, color="#475569")

        # NaN values break the line at missing dates instead of connecting gaps.
        daily = [float("nan") if value is None else value for value in series.booked_people]
        trend = [float("nan") if value is None else value for value in series.moving_average]
        axes.plot(series.dates, daily, color="#64748b", linewidth=1.4, marker="o", markersize=2.7,
                  label="Daily booked people")
        if any(value is not None for value in series.moving_average):
            axes.plot(series.dates, trend, color="#0f766e", linewidth=3,
                      label="7-day moving average")
        axes.legend(loc="lower left", bbox_to_anchor=(0, 1.03), frameon=False,
                    ncol=2, borderaxespad=0, fontsize=10)
        axes.set_ylabel("Booked people", fontsize=10, color="#334155", labelpad=12)
        axes.set_ylim(bottom=0, top=max(1, max(value for value in series.booked_people if value is not None) * 1.15))
        axes.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
        if len(series.dates) <= 7:
            axes.set_xticks(series.dates)
        else:
            axes.xaxis.set_major_locator(AutoDateLocator(minticks=3, maxticks=8, interval_multiples=False))
        axes.xaxis.set_major_formatter(DateFormatter("%d %b"))
        if len(series.dates) == 1:
            axes.set_xlim(series.dates[0] - timedelta(days=1), series.dates[0] + timedelta(days=1))
        else:
            axes.set_xlim(series.dates[0], series.dates[-1])
        axes.grid(axis="y", color="#e2e8f0", linewidth=0.8)
        axes.set_axisbelow(True)
        axes.tick_params(axis="both", colors="#475569", labelsize=10, length=0, pad=9)
        for spine in axes.spines.values():
            spine.set_visible(False)

        notes = ["Trend uses complete 7-day windows, including recorded zero-booking days."]
        if len(series.dates) < 7:
            notes = ["Fewer than 7 days: daily counts only."]
        if None in series.booked_people:
            notes.append("Gaps indicate missing data, not zero bookings.")
        if today is not None and series.dates[0] <= today <= series.dates[-1]:
            notes.append("Today's bookings may still change.")
        figure.text(0.075, 0.085, "\n".join(notes), fontsize=9, color="#475569", linespacing=1.6)
        with BytesIO() as buffer:
            canvas.print_png(buffer)
            return buffer.getvalue()
