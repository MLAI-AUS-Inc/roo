# Studio member invoices

Roo generates builder invoices **to MLAI**, using the existing Linear timesheet
process. Done/completed approves work; scoped projects, verified builder
mappings, quarter-hour allocation, deferred evidence and corrections are all
owned by the existing private timesheet worker. Sam's report permissions remain
Sam-only. Invoice commands never grant access to another member.

| Size | Credited hours | Payable AUD |
|---|---:|---:|
| XS | 0.25 | $16.25 |
| S | 1 | $65.00 |
| M | 2 | $130.00 |
| L | 3 | $195.00 |
| XL | 5 | $325.00 |

The rate is $65/hour, with no additional GST. The PDF title is Invoice and
does not infer GST registration. Work amounts are static PDF content. Builder
business/contact/address, ABN, BSB, account details and invoice number are blank
AcroForm text fields. Issue date is automatically filled from the saved
generation timestamp in Melbourne time and remains editable. Due date and
payment notes are omitted. Saved finals keep their original issue date when
retrieved. Complete and save personal fields locally before issuing the invoice;
never send personal fields to
Roo or upload completed forms. The fixed internal reference remains separate
from the builder's editable invoice number. The fixed bill-to details identify
MLAI with ABN **94 807 394 137**. The builder's **Your ABN** field remains blank
and editable. Send the completed final invoice to **treasurer@mlai.au**, as
shown in the PDF's submission instructions.

## Commands and cadence

- `@Roo invoice` in a permitted Slack channel, or `invoice` in a DM: current
  payment-fortnight draft, privately delivered to the authenticated member.
- `invoice latest`: most recent payable final since rollout; skips fortnights
  with no payable invoice. If none exists, privately explains that outcome.
- `invoice YYYY-MM-DD`: that payment Friday. Current open period produces a
  draft; closed periods produce/retrieve the sealed final. Future periods,
  off-cadence dates and dates before **23 October 2026** are excluded.
- `/roo invoice` and `/roo-dev invoice` use the same signed queue path.

Channel mentions receive an initial queued acknowledgement visible to everyone
in the originating channel thread. DM acknowledgements remain private, and slash
command responses remain ephemeral. Once the private
message and PDF are confirmed delivered, Roo replies in the originating thread:
“I’ve sent your invoice privately. Check your DMs.” Amounts, work items and
personal fields stay private. If no invoice is payable, the channel reply confirms
a private update instead. DM requests and ephemeral slash commands do not add
a public reply. Confirmation receipts survive restarts; a delayed confirmation
does not generate or deliver another invoice. Uncertain confirmation outcomes
use the existing operator review workflow.

Drafts run every second Thursday **17:00 Australia/Melbourne**, starting
**22 October 2026**. Finals run after the following Friday's **12:00** cutoff,
starting **23 October**. The next draft is **5 November**, final **6 November**.
The worker's regular tick evaluates local dates, including daylight saving;
there is no UTC cron drift. Draft catchup ends at Friday noon. After restarting
later, skip obsolete drafts and deliver any outstanding finals from rollout.
Each active manually mapped Slack member receives only their own rows.

Drafts never persist allocations. They preview the current period using the
existing engine's simulated chronological catchup. Finals seal that engine's
snapshot, then store one invoice artifact per workspace/organization, builder
and payment fortnight. Prior timesheet reports may need chronological catchup,
but no pre-rollout invoice is generated or delivered. Saved PDFs retain exact
bytes, fixed rate, source rows and source-report hash. Repeated commands retrieve
the same final; a new request may deliver that same artifact again privately.
Late draft requests return the final for their original period after cutoff.
The selected final period is pinned privately before transport starts, including
`latest`, so crossing a later cutoff during retry or recovery does not switch
invoices. Draft expiry is rechecked before each delivery operation, including
the external upload protocol's final file-sharing step. Sent and ambiguous
parts are preserved; unsent expired parts are marked expired.

No positive payable amount produces a private explanation, not an empty PDF.
Incomplete evidence is identified as excluded work needing review; provider
failures are never interpreted as zero. A final explanation also supersedes a
prior positive draft if the sealed payable amount is now zero or negative.

## Scoped source preparation and recovery

The private worker uses an indexed invoice reader. The primary query applies
the approved project IDs, their verified Linear team IDs and the requested
payment period in Linear itself. Names and prefixes never expand the payable
project list. Preflight resolves every project's teams, including pagination;
an unverified relation blocks generation. `TIMESHEET_SLACK_TEAM_ID` remains a
Slack workspace ID, not a Linear team ID.

Prepare historical coverage explicitly before activating this reader:

```bash
python -m roo.timesheet_worker --env-file .env.timesheets --preflight
python -m roo.timesheet_worker --env-file .env.timesheets --prepare-source
```

Preparation reads from the existing 28 August noon source beginning and saves
evidence. It creates no report, allocation, invoice or Slack delivery. Until
preparation completes, member requests stay queued and receive a private
waiting notice. Repeat preparation after its reported cooldown to resume saved
pages and evidence batches. It is a one-time historical pass, with a new pass
required after changes to source credentials/organization, team/project scope,
first cutoff, timezone, rubric or reader version.

Normal reads refresh scoped period candidates, retained unfinished/deferred
work and lightweight changes since the last successful refresh, with a
five-minute overlap. A compact project-history check for new/changed outsiders
protects work moved out of Studio, including moves across teams. Unchanged
unrelated workspace issues cause no history reads. Relevant histories are
batched and reused after fresh candidate checks; label names are refreshed
even when the issue timestamp did not change. Full histories retain the first
completion so reopened old work cannot be paid again. Missing prior periods
are still simulated chronologically for drafts and persisted only for finals.
Resumed report reads finish saved work, then require a fresh closing pass in
the current attempt before returning financial inputs. If that closing pass
cannot finish within the budget, keep the request waiting and review the source
workload; never increase limits or delete receipts simply to force a payment.
Historical preparation can finish over multiple attempts and retains its
original refresh-start watermark; it produces no financial snapshot, and the
next report always performs the mandatory fresh refresh. A task changed during
compact-history checking invalidates its stale discovery checkpoint so the
next attempt rediscovers it. Finals require stable closing discovery before
sealing. The API provides no
global transactional snapshot, and never-observed hard-deleted tasks remain an
upstream visibility limit. Loss of access to known required work blocks coverage.

One captured command/schedule batch builds one all-builder report, then applies
each member's ownership checks and private projection. Later requests refresh
changes; no draft is reused indefinitely. Saved finals and saved request
artifacts can be delivered during source outages without a Linear read.

Private storage, relative to `TIMESHEET_DATA_DIR`:

- `linear-invoice-index/<scope fingerprint>/manifest.json`: complete coverage
  and refresh-start watermark; `progress.json`: resumable work, never a report.
- `linear-invoice-index/<scope fingerprint>/full-pages/`: version-bound history
  page receipts for interrupted long-history reads.
- `linear-evidence/<credential/organization/workspace fingerprint>/`: verified
  full issue/history evidence and digests.
- `linear-cooldowns/<fingerprint>.json`: shared provider/local retry deadline.
- `invoices/notices/`: original private wait reason, text and deadline, pinned
  before delivery. Confirmed/ambiguous notices are never resent automatically.

HTTP 429 and GraphQL `RATELIMITED` at HTTP 200/400 honor the provider's reset,
including millisecond request/complexity headers. Corrupt cooldowns fail closed.
Each source attempt permits at most 200 HTTP calls and 45 seconds before the
next request (one in-flight request can reach the existing HTTP timeout).
Exhaustion saves progress and shares a wait of at least 60 seconds; another
builder cannot immediately start another scan. Provider waits show the earliest
retry in Melbourne time, not a completion ETA. Source delays never create empty
invoices or success acknowledgements. The separate client-report deployment
has its own volume and may still consume the same provider quota.

Aggregate `invoice_linear_read` logs expose candidate/page/hydration counts,
compact move checks, evidence cache hits/batches and elapsed time; no issue
bodies or credentials. Measure preparation, cold period, unchanged refresh,
one changed issue and a 19-builder batch separately. Under one minute for a
typical healthy prepared request is a target pending live measurement.

For example, on 12 October `invoice` previews work after 9 October noon for
the 23 October payment fortnight; `latest` remains a saved closed-period
selector, with member invoices restricted to rollout dates.

The private worker must be updated separately from Public Roo. After separate
production authorization: build the new private worker image, run that image's
read-only scope preflight and preparation against the existing private volume,
then activate it as the long-running reader. The already enabled invoice flags
need no changes. A public GitHub deployment alone does not rebuild this worker.
Keep all ledger, queues, saved artifacts and delivery receipts intact; never
delete them to force a retry. Live MLAI team/project relations and timing must
be verified before activation; the browser account available during this change
did not have access to the MLAI workspace.

## Reviewed deployment configuration

The feature defaults off. Deployment is a separate reviewed action. Public Roo
needs only these non-secret settings, plus its existing Slack credentials:

```dotenv
STUDIO_INVOICE_COMMANDS_ENABLED=true
STUDIO_INVOICE_QUEUE_DIR=/app/timesheets/invoice-queue
TIMESHEET_SLACK_TEAM_ID=T...verified-team
TIMESHEET_SLACK_BOT_USER_ID=U...verified-roo-bot
TIMESHEET_FIRST_CUTOFF=2026-09-11
```

If Public Roo has an explicit `ROO_ENABLED_SKILLS` allowlist, add
`studio-invoices` to it for help routing. Exact authenticated commands are
handled before inference; the help skill cannot authorize generation through
model-supplied actor or recipient parameters.

Keep `.env.timesheets` private. Retain its existing verified organization,
Sam recipient, one-to-one builders and approved project scope, and enable:

```dotenv
TIMESHEETS_ENABLED=true
TIMESHEET_SOURCE=linear
STUDIO_INVOICES_ENABLED=true
STUDIO_INVOICES_SCHEDULE_ENABLED=true
STUDIO_INVOICE_QUEUE_DIR=/app/timesheets/invoice-queue
```

`TIMESHEETS_SCHEDULE_ENABLED` independently controls Sam's reports. Invoice
scheduling does not require changing that flag. Do not move source credentials
or builder mappings into Public Roo. Use the existing
`docker-compose.timesheet-commands.yml` override for Public Roo and the separate
`docker-compose.timesheets.yml` worker project. Both mount only the shared
`./data/invoice-queue` metadata directory; the named private `timesheet-data`
volume contains allocations, invoice artifacts and delivery receipts. Maintain
the same worker Slack team, first-cutoff cadence and manual mapping identities.
Changing a builder's Slack mapping does not transfer saved invoices to a new
account; it requires reviewed identity reconciliation.

Before enabling production, run the existing worker **preflight** with explicit
approved development/staging credentials. It verifies workspace, source scope,
Sam and every mapped Slack/Linear member. Verify the shared queue mounts,
persistent worker volume and required Slack file-upload/DM scopes. Existing
preflight/preview read sources but do not send or seal. Do not run `--once`
against production as a harmless preview: enabled schedules and queued requests
will deliver and finals will allocate.

## Adjustments and delivery recovery

Use the [timesheet correction workflow](studio-timesheets.md) to record a
reviewed quarter-hour adjustment referencing the original row. It appears in
the next payable fortnight; neither the original sealed report nor invoice is
rewritten. Negative adjustments reduce the total; a nonpositive net amount
receives an explanation and remains in the allocation ledger for review.
The existing engine consumes a negative adjustment in one fortnight. It does
not automatically carry a negative net balance into another later fortnight;
Sam must reconcile a nonpositive outcome before later payment, using reviewed
adjustments as appropriate. This feature does not introduce a payment/debt
ledger or redefine that existing financial policy.

Invoice receipts use `invoice-...` keys in the **existing** deliveries directory.
`python -m roo.timesheet_worker --env-file .env.timesheets --status` lists them.
Confirmed rate-limit failures retry only pending parts. A persisted `sending`
or definite nonretryable `failed` part requires operator review. After verifying
Slack's actual outcome, use the existing recovery command, for example:

```bash
python -m roo.timesheet_worker --env-file .env.timesheets \
  --recover invoice-DELIVERY_KEY --part 1 --confirmed-absent
# Or, only after finding the actual Slack delivery:
python -m roo.timesheet_worker --env-file .env.timesheets \
  --recover invoice-DELIVERY_KEY --part 1 --delivered-reference FVERIFIED
```

Part 0 is the private message, part 1 the PDF when payable. Never confirm absent
without checking Slack; an ambiguous upload might already have succeeded.
The next worker tick/request retries the pending part with the stored artifact.
Restarts preserve receipts, exact PDF content and allocation state.

## Verification

Offline tests cover fixed amounts, Melbourne/DST cutoffs, canonical AcroForm
fields and appearance streams after save/reopen, multipage work tables, draft
nonallocation, immutable finals, correction carry-forward, mapping rebinding,
authenticated ingress and retry handoff, member isolation and receipt recovery.
Preview and Acrobat Reader desktop save/reopen checks must also be recorded
before rollout; automated field-tree checks alone do not certify every reader.
