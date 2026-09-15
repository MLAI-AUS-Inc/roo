# Studio fortnightly timesheets

Roo supports private, on-demand timesheets and an optional fortnightly delivery
to Sam. This is separate from the existing builder payment reminder. It does not
execute payments, change tickets, or implement client project reporting.

## Commands

| Request | Result |
| --- | --- |
| `@Roo timesheet` or DM `timesheet` | Latest completed fortnight |
| `@Roo timesheet 2026-09-11` | Saved report for that cutoff; generate it if missing |
| `@Roo timesheet current` | Provisional current-period draft |
| `/roo timesheet` or `/roo-dev timesheet` | Same command through the existing slash endpoint |

Only Dr Sam may request production reports, either by DMing Roo or mentioning
Roo in a channel or existing thread. Both signed command ingress and the worker
require the actor to equal `TIMESHEET_RECIPIENT_SLACK_ID`, which must be Dr Sam's
verified Slack ID. Additional requester allowlists cannot grant production access.
A channel/thread request gets an ephemeral acknowledgement; report contents and
CSVs are always DMed directly to Dr Sam. Scheduled reports use the same recipient. Production never posts a channel report. Roo never interprets natural-language text as a different requester
or destination. Existing workspace/surface/channel gates and Slack signature
validation remain in force. Slack retries share a durable request identity.

Each report includes a builder totals CSV, ticket details CSV with Linear links,
and an exceptions CSV when needed. Hours are **size-based hours**, not measured
time. A finalized timesheet is an allocation record, not proof of payment.

## Rules

- Linear remains the source until the Plane migration is complete. The Plane
  compatibility adapter is preview-only; see [Plane cutover](#plane-cutover).
- Calendar cutoffs are Friday **12pm Australia/Melbourne**, every 14 days, with
  daylight saving handled by the timezone. Initial cutoff: **11 September 2026**;
  then 25 September and 9 October. First window is **after 28 August noon through
  11 September noon**, inclusive at the end. Older completions are excluded.
- Tickets must be in a selected project and in Linear's `completed` state category
  at cutoff. Moving to Done is approval. Late completions enter the next round.
- Exact effort labels map to fixed upper bounds: `Extra Small (XS)` = 0.25h,
  `Small (S)` = 1h, `Medium (M)` = 2h, `Large (L)` = 3h,
  `Extra Large (XL)` = 5h. Internal calculations use integer quarter hours.
- Reconstruct size, assignee, project and state at cutoff from Linear history.
  An edit after cutoff does not rewrite a finalized period.
- A ticket is allocated at most once, including after reopening and completing
  it again. Missing/conflicting labels, missing manual builder mappings and
  incomplete history appear as exceptions, never invented zero-hour rows.
  Unresolved tickets are carried forward for reevaluation in later periods.
- A source outage blocks report generation; it cannot produce a false zero total.
  A draft simulates any missing previous periods in memory, without finalizing or
  allocating anything. Draft request retries use the same saved draft.
- Closed reports are immutable. Generating a missing historical report also
  finalizes any missing earlier periods to preserve chronological allocation.

Selected projects are pinned by ID, independently of status or renaming:

| Project | Linear ID |
| --- | --- |
| Master App | `c0727962-6495-4584-96b5-0ad5b6ab469d` |
| Cybertest | `2b091441-a02e-4491-94a7-517fcafc7b05` |
| Project Ironman | `37307700-0330-4237-804b-655fef443f01` |
| Project Acquire | `0d499cd9-44d9-4b46-a669-a04d3bdb1850` |
| Aaron AI | `86815a40-f7d6-45dd-adbb-fec564248628` |

Studynash, Present Studio and MLAI Studio operations are excluded. Overrides must
be explicit ID-to-name JSON. All ticket/history/label pages are read, including
archived work. The read also discovers recently changed tickets outside the
allowlist so moving a ticket out after cutoff does not hide it.

## Local testing without external calls

From `roo-standalone`, install `requirements.txt` into a Python 3.11+ environment.
Run the tests with synthetic values, without loading a real `.env`:

```bash
SLACK_BOT_TOKEN=synthetic SLACK_SIGNING_SECRET=synthetic OPENAI_API_KEY=synthetic \
python -m pytest roo/tests/test_timesheets.py roo/tests/test_timesheet_linear.py \
  roo/tests/test_timesheet_ingress.py roo/tests/test_payment_reminders.py \
  roo/tests/test_slack_security.py roo/tests/test_timesheet_plane.py -q
```

These exercise signed HTTP requests, the real command queue, paginated Linear
adapter, cutoff/history calculations, CSVs, private delivery, replay, disk
failures and uncertain external outcomes, using synthetic provider responses.
No Slack messages, ticket writes or payments occur.

## Fake-data Roo Dev test in Slack

For a live Slack test without Linear, copy `.env.timesheet-demo.example` to
ignored `.env.timesheet-demo`. Use Roo Dev's bot token and signing secret, set
`TIMESHEET_DEMO_ENABLED=true`, and explicitly allow the test requester's Slack ID
in `TIMESHEET_DEMO_ALLOWED_SLACK_IDS`. Start from an empty demo data directory.

```bash
python -m roo.timesheet_demo --env-file .env.timesheet-demo --port 8012
```

This separate local entry point checks the actual Roo Dev app/bot/workspace
identity, accepts signed mentions only from configured testers in private
`#roo-testing` (`C0BRM181EDV`), and uses the same queue, report engine and durable
delivery checkpoints with a hard-coded synthetic Linear adapter. It refuses
production mode and cannot call Linear. It does not import the AI agent or run
other jobs. Production access rules are unaffected by its tester allowlist.

Connect Roo Dev's event callback to this server's `/slack/events` through a local
tunnel. Mention the actual bot in the channel or an existing thread:

```text
@Roo Dev timesheet
@Roo Dev timesheet current
@Roo Dev timesheet 2026-09-11
```

The response and `FAKE-DATA-` CSV uploads stay in the original thread. Every
response starts with:

> **Roo Dev test — FAKE DATA.** This report is replying in your thread for testing.
> **In production, Roo will DM the report directly to Dr Sam, rather than reply
> in this thread.**

Default fixture clock is 13 September 2026: the finalized first fortnight totals
Alice **3.25h** and Bob **5h**, with one missing-size exception. The current draft
contains Bob's **3h** late ticket and still flags the exception. Set
`TIMESHEET_DEMO_AS_OF=2026-09-26T12:00:00+10:00` and restart to simulate the second
round: Bob totals **5h**, including the repaired size. Already counted tickets
stay excluded. Keep the same demo data directory when testing replay/carryover.

To share an existing ngrok hostname without replacing its local Roo server,
`python scripts/timesheet_dev_proxy.py` listens on port 8013. Route the tunnel to
8013: `/timesheet-dev/*` forwards to the demo on 8012 and other paths forward to
the existing Roo on 8000. Set only Roo Dev's event callback to
`https://TUNNEL_HOST/timesheet-dev/slack/events`; preserve other manifest fields.
Back up the original manifest and tunnel configuration first and restore those
settings before stopping the proxy. A shared Roo Dev app has one event callback,
so its events use this timesheet test handler while connected. This mode supports
mentions in the test channel; slash commands and DMs remain outside this demo.

## Read-only development preview

Copy `.env.timesheets.example` to ignored `.env.timesheets`. Fill in a dedicated
read-only **development** Linear key, organization ID, Roo Dev Slack token and
workspace ID, verified recipient/requester IDs, and the manual builder map.
Set `TIMESHEETS_ENABLED=true` and keep `TIMESHEETS_SCHEDULE_ENABLED=false`.
Do not guess Sam's Slack ID. For an isolated development fixture use a verified
test recipient and a separate data directory; production identity is never reused.
Relative paths resolve from `roo-standalone`.

```bash
python -m roo.timesheet_worker --env-file .env.timesheets --preflight
python -m roo.timesheet_worker --env-file .env.timesheets --preview current
python -m roo.timesheet_worker --env-file .env.timesheets --preview 2026-09-11
```

Preflight reads workspace, organization, project and user identities. Preview
exports local CSVs to ignored `timesheet-preview/`; it sends no messages and does
not write the allocation ledger. It can create lock files. No public `.env` is
implicitly loaded by the worker. Preview needs source access but no file uploads.

## Connect Roo Dev commands when ready for a live development test

Configure Public Roo's command settings in its development `.env`:

```dotenv
TIMESHEET_COMMANDS_ENABLED=true
TIMESHEET_SLACK_TEAM_ID=T_VERIFIED_DEV_WORKSPACE
TIMESHEET_SLACK_BOT_USER_ID=U_VERIFIED_ROO_DEV_BOT
TIMESHEET_RECIPIENT_SLACK_ID=U_VERIFIED_TEST_RECIPIENT
TIMESHEET_FIRST_CUTOFF=2026-09-11
TIMESHEET_QUEUE_DIR=./data/timesheet-queue
```

Replace the illustrative IDs with actual Slack IDs. Public Roo and the worker
must use the same absolute queue directory, team, cutoff anchor and single
recipient ID. In production this must be Dr Sam; he alone can request reports. Public Roo receives no Linear
worker credential or report data volume. The worker polls durable requests:

```bash
python -m roo.timesheet_worker --env-file .env.timesheets
```

This worker command **can send DMs for queued requests**, even with scheduling
disabled. Start it only when testing those sends is intended. Run the development
Public Roo server separately with its existing signed Slack event endpoint and
Roo Dev app configuration, then DM `timesheet current`. A new slash command is not
required if Roo's existing `/roo-dev` registration already points to
`/slack/commands`. The app needs its existing `app_mention`/DM events and command
registration, `chat:write`, `im:write`, `users:read`, and `files:write` for CSV
delivery. Scope changes may require reinstalling the development app.

Optional Compose setup, once live development delivery is intended:

```bash
docker compose -p roo-standalone -f docker-compose.yml up -d --build
docker compose -p roo-timesheets -f docker-compose.timesheets.yml up -d --build
```

The base public configuration and worker resolve the same `./data/timesheet-queue` bind mount. Worker snapshots,
ledger and delivery receipts use the separate `timesheet-data` volume. Normal
Public and Admin-driven Public Roo restarts retain the queue mount; the old
command override is optional and remains compatible. Do not share a data directory between dev
and production, switch storage between native/Compose mid-test, delete the worker
volume, or run `down -v` to retry a report. Back up the entire worker data directory
together. Two processes on one local filesystem serialize ledger writes with
file locks; network filesystems and separate unsynchronized replicas are unsupported.

## Cadence and delivery recovery

Only after the development outputs are checked, configure Sam and enable
`TIMESHEETS_SCHEDULE_ENABLED=true`. The separate worker polls every ten seconds
and catches up missed completed fortnights. Every finalized period is persisted
before delivery. Confirmed message/file parts are never retried; known rate limits
retry after the requested delay. A timeout after an attempted send becomes
`needs_review`. A pending command receives a fixed private failure notice; raw
provider errors, credentials and report contents are not logged.

If a draft has no saved snapshot and its period has since been finalized, Roo
rejects it with a private request to fetch the finalized report or request a
fresh draft. It never silently reports zero hours using later allocations.
Already saved draft snapshots retain their original content on delivery retries.

The following commands inspect/export local state without network calls:

```bash
python -m roo.timesheet_worker --env-file .env.timesheets --status
python -m roo.timesheet_worker --env-file .env.timesheets --export 2026-09-11
```

`--status` shows period keys, allocation/deferred counts and each delivery's part
statuses. Inspect the private receipt file for the filename/recipient, check Slack,
then resolve an uncertain part with **one** of:

```bash
python -m roo.timesheet_worker --env-file .env.timesheets --recover DELIVERY_KEY --part 1 --delivered-reference F_CONFIRMED_FILE
python -m roo.timesheet_worker --env-file .env.timesheets --recover DELIVERY_KEY --part 1 --confirmed-absent
```

Part numbers are zero-based. Confirm absence before retrying; do not delete the
receipt. A crash between Slack success and receipt persistence is intentionally
ambiguous and requires this recovery. `--once` processes pending commands and
enabled scheduled delivery once; it is **not** a dry run.

## Corrections

Create a private JSON file referencing an original finalized ticket row:

```json
{
  "id": "unique-adjustment-id",
  "period": "2026-09-11",
  "issue_id": "original-linear-issue-id",
  "builder_id": "mapped-linear-user-id",
  "units": -1,
  "reason": "Correct agreed effort by a quarter hour"
}
```

Run `python -m roo.timesheet_worker --env-file .env.timesheets --correct adjustment.json`.
The adjustment enters the next open fortnight after it is recorded. It does not
rewrite the original, change Linear, or execute a payment. Reusing the same ID and
content is idempotent; different content with the same ID is rejected. To reassign
hours, record separate negative and positive corrections for the two builders.

## Evidence limits

Linear must expose the relevant history and project/user references. Missing or
inconsistent evidence is flagged, including trashed issues that are returned by
the API. Permanently deleted/inaccessible tickets cannot always be discovered;
there is no claim to recover data the source no longer exposes. Linear label
names are read from label objects; historically renamed/deleted label definitions
may require operator reconciliation. Avoid changing the effort rubric labels
in place. Reports generated long after cutoff depend on retained source history.
Finalized snapshots retain their evidence and a hash, the rubric version, selected
projects and builder mappings so later configuration changes cannot rewrite them.

## Plane cutover

The public instance metadata at `https://plane.mlai.au/api/instances/` reported
**Plane Community Edition 1.4.0** on 14 September 2026. Workspace URL:
`https://plane.mlai.au/mlai`. No authenticated workspace data was inspected while
building this adapter: no Plane API key was available.

`TIMESHEET_SOURCE=linear` remains the default. The Plane adapter uses the v1
`work-items` and activity endpoints with `X-API-Key`, only GET requests, and no
redirects or private/admin-session fallback. It reads every cursor page and
normalizes Plane data for the existing calculation engine. Report CSVs use the
provider-neutral `builder_id` column and include `source` and `source_issue_id`.
Existing finalized ledger snapshots remain unchanged.

Current Plane support:

- The same exact five effort labels and upper-bound hours. Preserve those labels
  during migration. `estimate_point` is an ID/index into an estimate scale; Roo
  **does not treat it as hours**. Using Plane's native estimate selector will
  require an explicitly verified definition-to-size mapping after we inspect it.
- Completed state **group**, independent of its display name.
- Exactly one assignee at cutoff. Multiple or missing assignees become exceptions;
  Roo neither credits each person with all the hours nor invents a split.
- Size, assignee, title and state history. Activity `epoch` is the action time;
  activity `created_at` may reflect delayed background processing. CE writes
  second-precision epochs, so an action in the exact cutoff second is flagged
  rather than guessed.
- Explicit project/user ID maps. An imported Plane item can retain its original
  Linear issue UUID as its ledger identity, so previously allocated work stays
  excluded. No title or name matching. Imports with source metadata but no identity
  map become exceptions. Imports whose metadata was lost need reconciliation;
  they cannot be proven new simply because Plane assigned new IDs.
- Separate Plane IDs for native work and provider-specific task links. Deferred
  tickets which disappear from API results remain visible as exceptions.

### Why Plane finalization is not enabled yet

In [CE v1.4.0's issue manager](https://github.com/makeplane/plane/blob/v1.4.0/apps/api/plane/db/models/issue.py),
the normal issue query excludes archived tickets, archived projects, drafts and
triage work. The [v1 work-item API](https://github.com/makeplane/plane/blob/v1.4.0/apps/api/plane/api/views/issue.py)
uses that manager. The [v1 routes](https://github.com/makeplane/plane/blob/v1.4.0/apps/api/plane/api/urls/work_item.py)
do not provide an archived-work-item listing. Activity reads also exclude archived
projects. A normal API result therefore cannot prove that every payable ticket
was included. Moves outside selected projects and missing imported history add
similar coverage gaps. Stable item reads also cannot prove all asynchronous
activities have already been written.

For that reason the worker rejects Plane finalization, Slack report delivery and
scheduled Plane reports with `plane_preview_only_pending_coverage_verification`.
Plane previews explicitly say **incomplete coverage; do not use for payment**,
and never modify the allocation ledger. There is no environment flag that silently
waives this limitation. Enabling the live source needs a reviewed coverage solution,
such as a verified archive-inclusive read endpoint/export plus a migration baseline
and retained history. Do not unarchive or edit tickets just to make a report pass.

### Prepare and compare a read-only preview

Copy `.env.timesheets-plane.example` to ignored `.env.timesheets-plane`. Configure
the API key locally, the actual workspace UUID, the five project ID mappings,
builder mappings and original Linear issue UUIDs for migrated work. Preserve the
existing logical organization ID and use a **copy** of the original ledger.
Set `TIMESHEETS_ENABLED=true`; keep the schedule disabled.

```bash
python -m roo.timesheet_worker --env-file .env.timesheets-plane --preflight
python -m roo.timesheet_worker --env-file .env.timesheets-plane --preview current --output-dir timesheet-preview/plane
```

`--preflight` verifies identity/project access; it is not a certification of full
archive/history coverage. A date preview is supported for an unfinalized period;
requesting an already finalized period refuses to masquerade its saved Linear
snapshot as a fresh Plane read. Keep using the normal export command to retrieve
the saved original.

Before switching live reporting, reconcile Linear vs Plane across the selected
projects: every migrated ticket and assigned builder, sizes, completed/open states,
timestamps, retained history, archived/moved tickets, deferred exceptions and all
previous allocations. Preserve the ledger; do not start a new payment ledger on
Plane. Choose an explicit Friday-noon cutover after this comparison passes. The
existing production recipient stays Dr Sam by DM. The separately running Roo Dev
fake-data thread demo and its tester permissions do not change.
