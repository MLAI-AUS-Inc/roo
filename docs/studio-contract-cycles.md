# Studio agreement allowance cycles

New client projects use the **explicit commencement date in the fully signed
agreement** saved in Google Drive. They renew on that monthly anniversary, not
the first of the calendar month or a fixed 30-day interval. Mark Ghiasy's client
mapping and all projects under it are explicitly exempt and retain their
existing reporting, source and allowance behavior.

## Onboard a client or project

1. Verify the existing client identity and explicit Linear project grant.
   Contract text never grants project access.
2. Open the fully signed Drive agreement and verify its audit/signature record.
   Read the commencement date, included hours, monthly renewal and rollover
   clauses, including any Schedule 1 variations. Do not substitute the signing
   date, upload date, project creation date or first completed task for a missing
   commencement date. Obtain a review when terms are missing or conflict.
3. Record the reviewed `contract_terms` in the owner's private
   `STUDIO_REPORTS_CLIENTS_JSON` entry. The [Study Nash example](studio-devashi-reports.md)
   shows the schema; replace provenance placeholders with the signed Drive file
   ID, SHA-256 of its bytes, signature-completion timestamp, review timestamp
   and verified reviewer ID. Keep source documents and credentials private.
4. Set `hours_source: "linear"` for this completed-task usage view. Set a verified
   `history_start_month` separately if project-to-date requests are needed.
   The terms' `project_ids` must exactly match the reviewed owner's project scope.
   Adding a project requires reviewing whether it shares this agreement's
   allocation; it does not automatically purchase another 40 hours.
5. Keep `STUDIO_REPORTS_REQUIRE_CONTRACT_TERMS=true` in production. Set
   `STUDIO_REPORTS_CONTRACT_EXEMPT_CLIENT_IDS_JSON` to the verified **Mark owner
   ID only**. Staff entries with `report_client_ids` are overviews, not another
   ownership declaration. The worker rejects missing terms for all other owners,
   changed project scopes without matching terms, and terms on an exempt owner.
6. Test and preview the configured client report, then deploy the reviewed private
   configuration. A preview reads sources and produces private artifacts without
   sending Slack messages. This change does not add a recurring schedule.

No Drive watcher or model interpretation runs on report requests. Contract
extraction and review happen at onboarding, then deterministic code uses the
saved terms. Each subsequent agreement/amendment needs a new review; preserve
the previous private configuration and source version. Version 1 represents a
single agreement from commencement: mid-contract rate/allowance amendments,
fixed-day renewal, extra purchased blocks and contract termination require a
separate supported policy before activation, not silent edits to past terms.
Finance/payment reporting remains separate.

## Calculation and output

- Cycles start at Melbourne midnight, inclusive, and end at the next anniversary,
  exclusive. Day 29/30/31 clamps to the last day of a shorter month, returning to
  the original day in subsequent months. DST follows Australia/Melbourne.
- Each first-completed task uses its reviewed historical size, project and
  builder. Work before commencement is included only in requested history,
  not in the contract allowance. Reopened tasks never consume twice.
- Incoming rollover is used before the current base allocation. Unused incoming
  rollover expires at the next boundary. The configured percentage of the unused
  **base allocation** carries forward, up to the cap. Overage does not create
  negative rollover. Decimal hours retain fractional rollover without rounding
  quarter-hour tasks up or discarding half-quarter-hour credits.
- The worker reads complete scoped evidence from commencement through the request
  cutoff. A current-month request therefore includes earlier usage when computing
  its cycle's opening balance. Usage charts and task CSVs keep calendar filtering;
  the burndown shows the full intersecting allowance cycles through the cutoff.
- Summary, burndown and `studio-hours-allowance-cycles.csv` share one calculation.
  The CSV records inclusive start, exclusive end, cutoff, base allowance, incoming
  rollover, usage, estimated remaining, unused base and expired rollover.
  `rollover_out_hours` is **conditional on usage through the cutoff** for an open
  cycle; no future allocation is added to the chart before renewal.
- Missing task sizes leave known usage visible, withhold the remaining balance
  and prevent unknown rollover from becoming confirmed credit in subsequent
  cycles. `studio-hours-cycle-unresolved.csv` includes relevant earlier-cycle
  exceptions even when they fall outside the requested calendar month. Broken
  ownership/history evidence blocks the report rather than assuming zero usage.
- Client/staff grants, source isolation, private delivery and scope revalidation
  remain unchanged. Updating reviewed contract terms invalidates saved deliveries.
  Staff aggregates remain usage-only without a combined contract budget.

These balances use completed-task size estimates, not clocked time. The
implementation supports monthly renewal, a whole-number rollover percentage,
a quarter-hour cap and expiry after one cycle, with rollover consumed first.
Unsupported terms fail validation. See the
[regression tests](../tests/roo/test_studio_contract_cycles.py).
