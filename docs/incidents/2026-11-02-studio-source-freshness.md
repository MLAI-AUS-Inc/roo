# Studio report freshness — 2 November 2026

Status: diagnosed in production; code fixes validated locally, not deployed.

The 06:00 UTC (17:00 Melbourne) request for Mark Ghiasy's Studio usage burndown
routed to `studio-hours:summary`. The worker marked delivery successful, but
its report was partial, had no coverage for the requested November period and
showed a historical source cutoff of 11 October. This was a delivered
unavailable-data report, not a successful current usage calculation.

## Causes verified from live state

- The private finance projection was last reviewed on 11 October and still
  covered only through that date. Mark uses recorded/invoice hours rather than
  the separate Linear-only source policy used by Study Nash.
- Refresh and historical fallback were both enabled. Scheduled Friday reports
  called source preparation; on-demand hours and payment reports did not.
- Friday source receipts reported `hours_coverage_missing`,
  `new_invoices_require_review` and `finance_sync_failed`. The mailbox scan
  identified 45 unreviewed message candidates. These are not necessarily 45
  distinct invoices or 45 new units of work.
- A scoped read inside the existing backend found Xero HTTP 429 with a
  `Retry-After` value of 21,287 seconds. The backend collapsed that error to
  `finance_sync_failed`, while Roo continued five-minute retries.

No credentials, accounting records, source review dates, access grants,
delivery receipts or schedules were changed during diagnosis. No messages were
sent and no migrations were run.

## Fix and validation

New on-demand requests now use the enabled source barrier for every authorized
finance owner. Signed readiness must match both the requested cutoff and the
snapshot's actual timestamp/coverage. A failed live preparation cannot fall back
to the historical file. Saved delivery retries keep their exact projection and
attachments. Notices distinguish review gaps, source failures and Xero throttling.

The companion backend change returns `finance_rate_limited` and a signed
`retry_at`, and persists a private cooldown across report cutoffs and restarts.
It respects both numeric and HTTP-date `Retry-After` values. Roo carries that
deadline into its durable request retry.

Validation: 722 Roo Studio/timesheet tests and 21 backend source tests passed,
using synthetic data and no provider/database access. Regression cases cover
new requests, old local snapshots, scoped multi-client reports, delivery retries,
bad signed projections, rate-limit persistence and recovery.

## Remaining source work and release

Deploy the backend rate-limit change and rebuild the separate Studio report
worker; the normal Public Roo deployment does not rebuild that worker. Neither
fix needs a migration. Preserve all private volumes and receipts.

Current recorded-hour charts still require review of the invoice candidates and
supported work records after 11 October. Restoring finance access alone must not
advance `reviewed_at` or `hours_complete_through`. Live Linear estimates can be
offered separately if that reporting basis is chosen; they must not become
recorded hours or paid-hour credit balances. Missing work cannot be recovered
by copying a newer timestamp into the manifest.
