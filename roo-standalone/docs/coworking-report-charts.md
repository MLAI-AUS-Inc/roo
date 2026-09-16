# Coworking report charts

## Plan and data contract

1. Keep the existing permission checks, date presets, backend report and text summary.
2. Render the report's daily `booked_users` as a PNG, with a trailing seven-calendar-day average. Include weekends and explicit zero-booking days. Missing dates remain gaps; only complete seven-day windows have an average.
3. Upload the PNG into the request's Slack channel/thread using `files_upload_v2`. Render locally and upload bytes directly; no public image host or third-party chart service is needed.
4. Keep ordinary reports text-only. Include the chart only for explicit chart/graph/plot/trend-line requests or `include_chart: true`; `include_chart: false` and text-only requests suppress charts. Return the text report and a short explanation if rendering or upload fails.
5. Verify chart data, PNG output, Slack binary uploads, permissions, fallback behavior and routing metadata with offline tests. Inspect a rendered preview before delivery.

## What the chart measures

The current `/points/coworking/report/` API reports **active coworking bookings**, not door check-ins. Roo's admin check-in command creates a booking. Both the report and chart therefore say **booked people**; they must not claim independently verified attendance. Actual attendance charts would require a backend check-in data source.

`last 3 months` keeps the existing inclusive calendar-month calculation. On 2026-09-16 that is 2026-06-17 through 2026-09-16. The first six days have no seven-day average; averages include recorded zero days. Today's bookings may still change. For comparison requests the chart shows the primary range, while the text retains the period comparison.

The same chart supports `last 6 months` (2026-03-17 through 2026-09-16) and `last 12 months` / `last year` (2025-09-17 through 2026-09-16), up to 366 inclusive days. Roo generates only the requested range on demand; no recurring reports are scheduled.

## Usage and rollout

- `@Roo coworking report last 3 months` — text summary
- `@Roo show a daily coworking usage chart for the last 3 months` — summary and chart
- `@Roo show a daily coworking bookings chart for the last 6 months` — summary and chart
- `@Roo show a daily coworking bookings chart for the last 12 months` — summary and chart

Install the updated Python requirements and deploy Roo normally. The public Slack app manifest already includes `files:write`; the installed bot token must have that scope and the bot must be in the destination channel. If adding the scope to an existing installation, reinstall the Slack app. Reports still return their text if the permission is missing.

Slack documentation: [uploading files and replying in threads](https://docs.slack.dev/tools/python-slack-sdk/web/#uploading-files).

Tests use fake backend data and mocked Slack uploads; they do not publish reports to live Slack.

## Validation completed

- 176 tests passed across the chart renderer, Slack upload helper, points/report actions and reconciliation reports.
- All three router catalog checks passed, including the catalog size budget and action consistency.
- Coverage includes actual PNG generation through the report action for 3/6/12-month requests, text-only defaults for each lookback, explicit chart opt-in/opt-out, numerical trend windows, zero/missing days, short/year-long ranges, permission denial, comparison ranges, render failure, upload failure and missing Slack scope.
- A three-month preview with synthetic data was visually inspected for readable labels, clear daily/trend lines and unclipped content. No live daily counts were inferred from the example's weekly/monthly totals.
- On current main, the full points/report test module passes with synthetic Slack settings, including the Slack event tests.
- `git diff --check` passed. Deployment and a live Slack upload have not been performed.
