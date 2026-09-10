# Builder payment reminders

An opt-in Roo worker sends each configured builder a private Slack reminder on
the Thursday before a fortnightly payment Friday. The first payment date defaults
to **11 September 2026**, followed by 25 September, 9 October, and so on.
Thursday's reminder time defaults to **09:00 Australia/Melbourne**. This is a
delivery time, not an assumed payment cutoff: all user-facing copy says **Friday**.
Until the cutoff time is known, reminders run on the preceding day rather than
claiming to run exactly 24 hours before the cutoff.

For the first cycle the message starts:

> **Payment reminder:** First payments are this Friday! Please get all your hours
> in and mark your completed Linear tasks as done by Friday so your completed work
> is included in this payment run.

Later cycles say “Payments are this Friday!” Each reminder contains the **full
list** of that builder's assigned, open Linear tickets: identifier, clickable
link, full title, and current status. Backlog and older tickets are included;
there is no project, team, cycle, updated-at, or “likely finished” filter.
Completed and cancelled status categories and archived tickets are excluded.
The worker follows every API page and divides long lists into additional DMs.
Builders without open tickets receive no reminder. It never updates a ticket,
logs hours, calculates a payment, or infers that work is complete.

## Runtime and identity boundaries

The existing backend Linear reader is bound to a Slack channel and configured
queue. Reusing it here would omit builders' work outside that queue. This worker
instead has a dedicated **read-only Linear API key** and runs separately from
both Public Roo's interactive agent and Admin Roo. It has no inbound endpoint,
LLM calls, skill routing, backend credentials, or administrative capability.

Use the Public Roo bot's token to preserve Roo's DM identity. Required bot scopes
are `chat:write`, `im:write`, and `users:read`. No history permission is needed.
The worker verifies the Slack workspace and Linear organization before checking
builders, and checks each mapped account is active. Provide a manually verified,
one-to-one roster of Slack user IDs and Linear user UUIDs. It does not infer paid
builder eligibility from all Slack members or guess identities from names.

Provision the Linear key with read access to **every team containing these
builders' work**. The API cannot report tickets hidden from its credential; an
incompletely scoped key will produce an incomplete list. Keep this key only in
the dedicated worker environment, not in the Public Roo web or Admin environment.

API references: [Linear queries and archived resources](https://linear.app/developers/graphql),
[Linear pagination](https://linear.app/developers/pagination), and
[Slack message delivery](https://docs.slack.dev/reference/methods/chat.postMessage/).

## Configure and preview

From `roo-standalone`, copy `.env.payment-reminders.example` to the ignored
`.env.payment-reminders`. Fill in the dedicated credentials, Slack workspace ID,
Linear organization UUID, and verified `PAYMENT_BUILDERS_JSON` roster. No live
IDs or credentials are committed. Set `PAYMENT_REMINDERS_ENABLED=true` when ready
to preview. The feature is disabled by default and is not started by either
existing Public Roo or Admin Roo deployment.

For a development preview, use **development credentials and users only**:

```bash
docker compose -f docker-compose.payment-reminders.yml run --rm payment-reminders \
  python -m roo.payment_reminder_worker --dry-run
```

The preview respects the Thursday delivery window. Outside it, it reports
`not_due` and makes no external calls. During the window it reads live Linear and
Slack account information and prints the exact messages, but does not open a DM,
send a message, or create receipt files. Keep preview output private because it
contains task titles. Automated tests use mocked services and cover other dates.

## Activate

After configuring the approved production roster and credentials, start the
separate worker from the deployed checkout:

```bash
docker compose -f docker-compose.payment-reminders.yml up -d --build
docker compose -f docker-compose.payment-reminders.yml logs -f payment-reminders
```

The worker polls the local delivery window every minute. If it starts late on
Thursday, it catches up that day. It stops sending at the start of Friday and
never backfills an expired cycle. A restart on Friday therefore cannot issue a
late “first payments” reminder. A missed first cycle does not shift the cadence.
The clock is checked again before each message; an API call already in flight at
the end of Thursday can still complete. Changing `PAYMENT_REMINDER_HOUR` changes
only Thursday's send time. `PAYMENT_FIRST_FRIDAY` defines the actual payment
calendar; do not move it forward merely to make an old first-payment message run.

The named `payment-reminder-data` volume must persist across restarts and
deployments. Do not use `down -v`, delete receipts, or run replicas backed by
separate volumes: that removes duplicate protection. The local filesystem lock
supports multiple processes sharing the same volume on one Docker host. Multiple
hosts with independent volumes are unsupported.

One receipt per workspace, payment Friday, and builder stores the prepared message
parts and each delivery result. Successful parts are never resent. Rate-limited
parts retry after Slack's delay, at least 60 seconds later. A partial delivery
resumes the original prepared list, so it is a snapshot of the initial check.
Builders with no open work are recorded as checked for that cycle.

## Failures and recovery

Normal logs contain builder IDs and outcomes, not task text or API responses.
Monitor for `error`, `needs_review`, and `pending` that remains unresolved as
Thursday ends. `--once` performs one scheduled check, returns a nonzero status
for failures/pending deliveries, and is useful for operational checks.

Linear failures (including partial GraphQL results and broken pagination) send
no partial list. Workspace verification failures stop that tick. A failure for
one builder does not stop others. A changed account mapping after a message was
prepared requires operator review rather than delivering the old user's tasks.

If Slack delivery times out or the process crashes after posting, the part stays
`sending`. Later runs report `needs_review` and do not guess whether it arrived.
Explicit non-rate-limit Slack rejections leave the part `failed`. To recover:

1. Stop the worker with `docker compose -f docker-compose.payment-reminders.yml stop`.
2. Inspect the affected builder's DM and the receipt JSON in the persistent volume.
3. If the part arrived, set that part's status to `sent` and record its Slack `ts`.
   Only when confirmed absent, set it to `pending` after fixing the failure.
   Preserve the receipt key, identity, text, and all already-sent parts.
4. Restart the worker on Thursday to resume. After that delivery window, record
   the missed reminder for manual follow-up; automated delivery will not backfill.

Disable the worker with `docker compose -f docker-compose.payment-reminders.yml stop`.
This does not affect the Public Roo web service or Admin Roo and preserves receipts.

## Offline verification

```bash
python -m pytest roo/tests/test_payment_reminders.py -q
```

Tests exercise every-page retrieval, status categories, all-task rendering,
long-list splitting, escaping, identity checks, first/subsequent payment copy,
fortnightly/DST scheduling, dry runs, restarts, concurrent workers, rate limits,
partial sends, and uncertain delivery. No database migration is required.
