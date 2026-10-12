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
