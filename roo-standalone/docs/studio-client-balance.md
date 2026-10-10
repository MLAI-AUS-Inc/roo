# Studio hours and purchased-hours

The opt-in `studio-client-balance` skill shows one client's accumulated hours used
minus purchased-hour credits. The y-axis is hours. Its curve rises as work hours
are allocated to work dates and drops vertically when actual client payments buy
hour credits at the reviewed client purchase rate. It combines the client's
permitted projects into one chart.

For a fully synthetic example, a client rate of A$100 per hour turns a received
A$1,000 payment into 10 purchased hours. If 8 hours have been used, the running
balance is −2h: two purchased hours remain. A positive balance means used hours
exceed purchased hours. This report does not calculate money owed, contractor
costs or profit. Contractor pay amounts and pay rates do not determine the curve.
Invoice issue dates and unpaid face values add no credit; unpaid amounts appear
separately in the report.

## Delivery plan and source handoff

1. The reconciliation agent reviews contractor invoice hours, work attribution,
   invoice revisions and payment evidence. The existing backend remains the sole
   owner of Xero OAuth credentials. The operator refreshes the evidence and exports
   a reviewed, read-only JSON projection with a coverage period and source timestamp.
2. The private Studio worker validates invoice-hour and payment conservation, exact
   client project scope, evidence references and source freshness. It constructs a
   daily ledger in integer millionths of an hour and renders the used-minus-purchased
   curve. Client invoice/payment money remains exact integer AUD cents.
3. Public Roo queues only the signed requester identity and safe report selectors.
   The worker checks access again and DMs the requester the summary, chart and,
   when requested, detailed CSVs. The public runtime receives no finance snapshot
   or Xero credentials.

The current implementation uses an **operator-reviewed snapshot**, with manual
refresh and publication. It does not automatically sync Xero in real time. The
report shows its source timestamp and reviewed coverage. A source older than seven
days fails closed by default; an operator must refresh the audit and publish a new
snapshot. An `as_of` timestamp should reflect a real review, not a file copy date.

## Ask Roo

```text
@Roo show Mark Ghiasy's used hours minus purchased hours as a chart
@Roo show my project-to-date hours used and paid-hour credits
@Roo chart Mark Ghiasy's used hours and purchased hours for the last three months
@Roo give me the detailed hours and payments behind that balance chart
```

The default is project-to-date (`month=all`). `recent` with `months=3` includes the
current Melbourne month to date and the previous two calendar months;
`last_complete` with `months=3` selects three completed months. A narrower period
includes the opening balance from earlier covered activity, so it does not reset
the client's hour-credit history to zero. Activity beyond reviewed coverage is
labelled unavailable. Detail follow-ups reuse only this hours/payment report's
prior period and client, independently of hours-only requests.

Reports select one client at a time. `client=all` is rejected. The summary includes
actual receipt dates and amounts with purchased-hour credits, client invoices and
their payment status, used and purchased hours and the running hour balance.
Detailed reports add the event ledger, client invoice list, contractor invoice
hour allocations and work-date assumptions.

## Access and scope

Existing Studio client access is necessary. A reviewed finance dataset for that
owner must also be present in the private snapshot. Publishing an owner dataset
is an explicit financial reporting scope: its `project_ids` must match the owner's
configured project list exactly. An ordinary hours-only client without that dataset
cannot receive financial records.

Staff need their existing explicit `report_client_ids` grant to select an owner.
That owner's full project list must already be within the staff member's project
access. A name, email, model parameter or new snapshot record cannot create a
grant. An unknown identity, changed project scope, changed source, or stale source
blocks delivery. Delivery is private to the verified requester.

Mark's permitted work hours cover `[Studio] Master App`, `[Studio] Aaron AI`,
`[Studio] Project Acquire` and `[Studio] Project Ironman`. Shared package receipts
may fund work beyond these four projects. The report must disclose that scope
difference rather than imply the chart is a complete package account. Work outside
the permitted project scope is excluded and accounted for in invoice allocations.

## Reviewed hours, payment and date rules

- Version 1 supports **AUD gross client invoice/payment amounts**, including any
  GST. The operator-reviewed `allocation_rate_cents_per_hour` is the client's
  purchase rate; requests cannot set it. Paid-hour credits equal received cents
  divided by that rate. No contractor invoice pay amount, contractor pay rate,
  currency conversion or GST accounting enters the used-hour calculation.
- Count each current contractor invoice once. The operator reviews duplicate PDFs,
  invoice number suffixes and superseded versions; the importer does not guess which
  version is current. Read the reviewed invoice hours directly. Contractor bank
  payments settle invoices and do not become additional used hours.
- Client payment rows represent unique received bank/payment transactions. A batch
  receipt has one event with explicit allocations to its client invoices. Payment
  allocations must equal the event amount and each invoice's recorded paid amount.
  Draft/voided invoices cannot receive a payment allocation. Void and duplicated
  payment records must be excluded during source review.
- Used-hour rows retain a reviewed work window and date basis. Multi-day quantities
  are allocated uniformly over that window; remainder millionth-hour units go to
  earlier days so every unit is counted once. This daily shape is labelled estimated.
  A reviewed invoice issue date can be used as an estimated work date when needed.
- Every quantified contractor invoice hour must be classified as included work,
  excluded out-of-scope work, or unresolved work. Separate recorded work may have
  no `invoice_id`, but still needs reviewed dates, project attribution and provenance.
  Payment evidence with unknown hours/project/work dates goes into `pending_work`
  without a quantity and stays outside the curve. Never infer its hours from money.
- Retain provenance and limitations: approved surcharges, incomplete work detail,
  later uninvoiced activity and shared receipt scope must be disclosed. Accepted
  invoice hour quantities remain reviewed accounting hours, not a clock-time claim.
  Do not silently count missing evidence as zero or turn this curve into invoice debt.

## Snapshot version 1

The complete schema is enforced by
[`validate_balance_snapshot`](../roo/studio_client_balance_source.py). Client money
and purchase rates use positive integer cents; `paid_cents` may be zero. Work uses
integer `hour_units` at **1,000,000 units per hour**; `excluded_hour_units` and
`unresolved_hour_units` may be zero. Credits must be exactly representable at this
precision. Record IDs are unique within their collection; bank transaction IDs
and current invoice identities cannot be duplicated.

This example is fully synthetic and is not an access grant or production source:

```json
{
  "version": 1,
  "team": "TEXAMPLE",
  "organization": "00000000-0000-0000-0000-000000000020",
  "currency": "AUD",
  "basis": "gross",
  "evidence": {
    "review": {
      "reference": "Private reviewed invoice and receipt audit",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  },
  "clients": {
    "UEXAMPLECLIENT": {
      "project_ids": ["00000000-0000-0000-0000-000000000001"],
      "currency": "AUD",
      "basis": "gross",
      "allocation_rate_cents_per_hour": 10000,
      "coverage": {"start": "2025-01-01", "through": "2025-02-28"},
      "as_of": "2025-03-01T00:00:00Z",
      "scope_label": "Example permitted projects and included receipts",
      "limitations": ["Reviewed hours; daily work timing is estimated."],
      "invoices": [{
        "id": "CLIENT1", "number": "EXAMPLE-CLIENT-001", "date": "2025-01-02",
        "amount_cents": 100000, "paid_cents": 100000, "status": "paid",
        "evidence_ids": ["review"]
      }],
      "payments": [{
        "id": "RECEIPT1", "bank_transaction_id": "EXAMPLE-BANK-001",
        "date": "2025-01-20", "amount_cents": 100000,
        "invoice_ids": ["CLIENT1"], "description": "Received client invoice payment",
        "allocations": [{"invoice_id": "CLIENT1", "amount_cents": 100000}],
        "evidence_ids": ["review"]
      }],
      "contractor_invoices": [{
        "id": "BUILDER1", "supplier": "Example builder", "number": "EXAMPLE-BUILDER-001",
        "date": "2025-02-01", "date_kind": "invoice", "hour_units": 8000000,
        "excluded_hour_units": 0, "unresolved_hour_units": 0,
        "allocation_note": "Current invoice counted once.", "evidence_ids": ["review"]
      }],
      "hours": [{
        "id": "HOURS1", "invoice_id": "BUILDER1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "hour_units": 8000000, "work_start": "2025-01-21", "work_end": "2025-01-31",
        "date_basis": "estimated_work_period", "description": "Reviewed project work",
        "allocation_note": "Estimated invoice work window; exact invoice hours retained.",
        "evidence_ids": ["review"]
      }],
      "pending_work": [{
        "id": "PENDING1", "supplier": "Example builder", "invoice_number": "EXAMPLE-PENDING-001",
        "evidence_date": "2025-02-15", "note": "Original invoice hours and project need confirmation.",
        "evidence_ids": ["review"]
      }]
    }
  }
}
```

Client invoice `status` is `paid`, `authorised`, `voided` or `draft`. Work
`date_basis` is `work_dates`, `estimated_work_period` or `invoice_date`; the last
requires a single date equal to the contractor invoice issue date. Contractor
invoice `date_kind` distinguishes `invoice` (default) from `payment_evidence`;
unconfirmed payment-only records with no reliable hours belong in `pending_work`.
Every record requires nonempty `evidence_ids` that resolve to the evidence map.
Keep raw invoices, source hashes, bank identifiers and the real snapshot out of
Git and out of the public runtime.

## Configure, check and publish

Public Roo uses `STUDIO_CLIENT_BALANCE_ENABLED=false` by default. It shares the
authenticated request queue and `STUDIO_REPORTS_SLACK_TEAM_ID` with hours reporting,
but does not require `STUDIO_REPORTS_ENABLED=true`.

In the separate private worker's `.env.studio-reports`, configure:

```dotenv
STUDIO_REPORTS_WORKER_ENABLED=true
STUDIO_CLIENT_BALANCE_WORKER_ENABLED=false
STUDIO_CLIENT_BALANCE_FILE=/app/studio-reports/data/client-balance.json
STUDIO_CLIENT_BALANCE_MAX_AGE_DAYS=7
```

`STUDIO_CLIENT_BALANCE_MAX_AGE_DAYS` accepts 1–31. Keep the snapshot inside
`STUDIO_REPORTS_DATA_DIR`, mounted only by the private worker. Existing verified
workspace, organization, builder/project mappings and client grants are reused.
Public Roo holds no Xero token, invoice attachment directory or financial snapshot.

From `roo-standalone`, validate the operator-reviewed source offline:

```bash
python scripts/install_studio_client_balance.py \
  --env-file .env.studio-reports \
  --source /private/reviewed/client-balance.json --check
```

The default without either action flag is also `--check`. Validation reads only
the specified JSON and explicit worker configuration; it makes no external calls
or file changes. It checks source structure and conservation, not whether the
source evidence is true or currently fresh. The worker enforces freshness at report
generation and delivery.

After review, run the same command with `--install`. The only allowed destination
is the configured `STUDIO_CLIENT_BALANCE_FILE` resolved inside the worker data
directory. Publication writes a temporary mode-600 file, fsyncs it and atomically
replaces the destination. Invalid snapshots and paths leave the previous file
unchanged. Output contains only status, owner IDs, record counts and the source
SHA-256, without money amounts, hour quantities or source text.

Enable the private balance flag and the public skill flag only after the reviewed
snapshot, access and deployment are ready. For a worker preview, use its existing
`--preview all --balance --actor VERIFIED_REQUESTER --client "Client name"`
options. Preview renders locally and does not send a message, but uses the normal
Slack recipient verification read; use synthetic dependencies for unit tests.
Never use production tokens for local tests.

For updates, retain a private copy of the previous approved source, refresh the
reconciliation audit, recheck allocations/scope and republish with the CLI. Changing
the source during a prepared delivery blocks the old report and requires a new
request. To roll back, disable the two balance flags and republish the previous
approved snapshot only if its scope and freshness are still valid. Queue/delivery
receipts must not be deleted to force resends. No database migration is required.
