# Client Studio hours

Clients can ask Roo for a fresh monthly report across their own projects, then
ask for a detailed work breakdown. Reports are private DMs, including when the
request comes from a channel. Public Roo queues authenticated request metadata;
a separate worker owns source credentials, verified client/project mappings,
snapshots and delivery receipts. The feature is disabled by default.

## Client experience

- “How many of my 40 Studio hours have we used this month?”
- “Break down my Studio hours by project and builder.”
- “Studio hours for August 2026.”
- “Show my monthly Studio hours from July to September 2026.”
- “Give me a high-level Studio hours report and chart for the last 3 months.”
- “Show my Studio hours for the last three full months.”
- “Give me the in-depth report on what those hours were spent on.”

The summary leads with **hours used out of the monthly allowance**, followed by
remaining hours or overage, project totals and builders within each project.
A private chart shows project usage, or month-by-month usage for a date range.
Zero-usage projects and months remain visible. The detailed follow-up includes
every counted work item's title, completion date, builder and size-based hours,
both in Slack and a CSV. Longer reports are split into readable messages.

“Last three months” includes the current month to date plus the previous two
calendar months (for example, July–September when requested in September).
“Last three full months” excludes the current month (June–August in that example).
Roo resolves these periods in Melbourne time using `month: recent` or
`month: last_complete`, with `months: 3`; the model does not calculate dates.
A month count without a starting month also ends in the current month.

Multi-month summaries lead with total usage, one short section per month, and
project/builder totals across the whole period. Project lists are shown once.
The chart compares monthly hours and marks the current month “to date.” Client
allowances stay separate for each month; staff overviews have no combined budget.

A detailed follow-up inherits the last requested period for the same verified
client, including when the summary was requested in a channel and the follow-up
comes in DM. Explicit months take precedence. Requests support 1–12 consecutive
calendar months, including the current month. Repeating a request with a new
Slack event reads fresh source data; Slack delivery retries reuse its saved
snapshot and rendered attachments.

The private worker reads fresh Linear candidates on every request, reuses only
unchanged, version-checked ticket evidence, and fetches missing evidence in small
batches. Complete reads persist under its private `linear-evidence/` directory,
so a rate limit or restart does not discard the entire scan. Changed tickets
and longer histories receive fresh, fully paginated reads; ownership is still
reconstructed and filtered separately for each client. This cache is never
mounted in Public Roo.

Linear rate limits (including GraphQL `RATELIMITED` responses with HTTP 400) keep
the request queued until the provider's retry time. Other safe read failures
wait at least a minute after the failure before retrying. Logs include sanitized
source error codes and read counts/durations, without provider response text.

Illustrative summary (synthetic data):

```text
Your Studio hours · Example client

September 2026 — 28.5 of 40 hours used
11.5 hours remaining · 71% used
Shared monthly allowance across all your projects.
• Project A: 18h
  Alice 12h · Bob 6h
• Project B: 10.5h
  Alice 2.5h · Bob 8h
```

## Hours and freshness

The existing Studio source provides **completed-ticket size hours**, not timer
logs. Reports use the same rubric as builder timesheets: XS 0.25h, S 1h, M 2h,
L 3h and XL 5h. Each report labels this basis and excludes work in progress.
“Fresh” means read on each request, as of its displayed request timestamp; it
does not mean an active timer. Detailed rows explain each ticket's allocated
hours; Roo never invents activities for individual clock hours.

One shared **40-hour monthly allowance per client** is the default. Explicit
`monthly_hours` and `monthly_allowances` overrides can represent other contracts
and dated allowance changes. Months use Australia/Melbourne midnight boundaries
and daylight saving. There is no implicit rollover, prorating or payment action.

For a staff overview spanning clients, explicitly set `monthly_hours` to JSON
`null`. This shows usage by month, project and builder without a combined budget,
remaining balance or overage. The same applies to a `null` monthly override;
omitting the setting retains the 40-hour client default. Staff access still uses
an explicit list of verified project IDs, with the same private delivery and
access checks. Adding staff access does not widen any client's project list.
New projects require an explicit configuration update before they are visible.

Each ticket counts once, in its **first completion month**, using its project,
assignee, title and effort label reconstructed at that completion. Reopening,
recompleting or moving a ticket does not count it again or expose another
client's historical work. Archived completed work remains eligible. Unknown
builders and missing/conflicting effort labels produce partial totals and an
exceptions CSV; remaining hours are withheld until those items are resolved.
Incomplete ownership/history evidence or a source outage blocks the report,
never silently replacing unavailable hours with zero.

This is a live client usage view, separate from payroll's immutable fortnightly
allocation ledger. Payroll corrections and carry-forward rules are not client
charges and are not imported. Repaired historical source evidence can change a
new client report. Linear is currently supported; the unverified Plane coverage
adapter cannot serve client reports. Permanently deleted/inaccessible work cannot
be reconstructed from evidence the source no longer exposes.

## Configure and preview

1. Copy `.env.studio-reports.example` to ignored `.env.studio-reports`.
2. Configure dedicated development source and Roo Dev credentials, the verified
   workspace/organization IDs, project IDs, and builder mappings.
3. Set `STUDIO_REPORTS_CLIENTS_JSON` to verified owner Slack IDs and their explicit
   project IDs. For Mark Ghaisy, verify his Slack ID and every owned project ID;
   never grant access by matching his name or guessing his projects.
4. Set `STUDIO_REPORTS_WORKER_ENABLED=true` for the worker. Keep the existing
   payroll worker's directories separate. Source reads and report delivery do
   not mutate its ledger or source tickets.
5. Run a local preview when source reads are authorized:

```bash
python -m roo.studio_report_worker --env-file .env.studio-reports \
  --preview current --actor VERIFIED_CLIENT_SLACK_ID --detailed
```

Preview verifies Slack/source identities and reads Linear. It writes local
private text/CSV/PNG artifacts under `studio-report-preview/` and sends no Slack
messages. All charts render locally; no chart service receives client data.

For command activation, configure Public Roo:

```dotenv
STUDIO_REPORTS_ENABLED=true
STUDIO_REPORTS_SLACK_TEAM_ID=T_VERIFIED_WORKSPACE
STUDIO_REPORTS_QUEUE_DIR=./data/studio-report-queue
```

If `ROO_ENABLED_SKILLS` is explicitly configured, add `studio-hours`. Otherwise
the feature flag adds it to the public skill catalog. Existing signature,
workspace, channel, DM and explicit-action gates still apply. Requests made on
behalf of another client never change the authenticated actor. Unknown owners
receive a private setup message without any source read. Delivery checks current
ownership again, so revoked/changed access invalidates an old queued snapshot.

Start the dedicated worker only when Slack delivery is intended:

```bash
python -m roo.studio_report_worker --env-file .env.studio-reports
# Or, alongside the regular Public Roo Compose project:
docker compose -f docker-compose.studio-reports.yml up -d --build
```

Public Roo and worker must share the same queue directory. The Compose profiles
mount `./data/studio-report-queue`; only the worker mounts report data. Required
Slack scopes are `chat:write`, `im:write`, `users:read`, and `files:write`.
The worker polls every ten seconds. No recurring client reports are scheduled.
Disable `STUDIO_REPORTS_ENABLED` and stop the separate worker to roll back.

## Delivery recovery

Snapshots and rendered parts persist before sending. Confirmed messages/files
are not resent; rate limits retry after the indicated delay. A timeout after an
attempted send remains uncertain and requires operator review. Missing chart
rendering does not suppress text totals. Attachment upload failures retain the
already-delivered text and follow the same delivery recovery rules.

Inspect private worker `deliveries/*.json` receipts, confirm the outcome in
Slack, then resolve the zero-based part number with one of:

```bash
python -m roo.studio_report_worker --env-file .env.studio-reports \
  --recover DELIVERY_KEY --part 1 --confirmed-absent
python -m roo.studio_report_worker --env-file .env.studio-reports \
  --recover DELIVERY_KEY --part 1 --delivered-reference VERIFIED_SLACK_REFERENCE
```

These recovery commands only update local receipts. Do not delete receipts or
the worker volume to retry a send. Back up the worker directory together. Use a
single shared local filesystem; separate unsynchronized replicas are unsupported.

## Tests

From `roo-standalone`, with synthetic credentials only:

```bash
SLACK_BOT_TOKEN=synthetic SLACK_SIGNING_SECRET=synthetic OPENAI_API_KEY=synthetic \
.venv/bin/python -m pytest roo/tests/test_studio_reports.py \
  roo/tests/test_studio_report_source.py \
  roo/tests/test_timesheets.py roo/tests/test_timesheet_linear.py \
  roo/tests/test_studio_report_routing.py roo/tests/test_router_catalog.py -q
```

Tests exercise shared budgets, monthly/DST boundaries, historical ownership,
reopened work, incomplete data, fresh reads, scoped private delivery, retries,
access revocation, CSV escaping, charts and conversational routing without
external calls.
