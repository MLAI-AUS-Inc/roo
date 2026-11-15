# Studio meetings and builder payments

Ask Roo to add the meeting actions to Linear as usual. With project calendars
enabled, Roo links matching meetings and creates open tickets for upcoming
bookings. The private time card lets a builder confirm or edit minutes attended.
The treasurer approves those entries and the normal Linear completion workflow
still applies. Future bookings contribute zero approved time. A calendar outage
leaves ordinary action creation usable.

Use `check meetings YYYY-MM-DD to YYYY-MM-DD` privately to check a missed invoice
period of up to 31 days. Existing tickets and occurrence records are reused.
Use `check meeting time` to reopen confirmation or approval cards.

`@Roo check builder invoices` and `@Roo pay builder invoices` request the same
private review. An optional invoice number narrows the review. The public reply
only acknowledges private delivery. The private **Prepare payments** button
reserves approved work and customer coverage and returns a bank reference per
invoice. Transfer through the existing bank workflow using that reference.
After its reconciled Xero bank transaction is available, use
`record payment REVIEW-ID INVOICE-NUMBER BANK-TRANSACTION-ID` privately.
Payment is confirmed only after the backend verifies the amount, currency,
payee, reference, status and unused receipt identity. A manual Zeller transfer
can bypass Roo. Reconcile such transfers into the opening ledger before using
coverage for more payments.

Roo uses approved sizes where the agreement prices size credits and approved
actual time where it prices hourly work. The backend independently reads original
supplier invoices, completed Linear evidence and issued customer labour invoice
lines. Money and units must both fit. Shared customer pools include every
builder and project. A changed source or allowance requires a new review.
Customer invoices count once whether open or paid. GST and expenses provide
no labour credit. Carry-forward is only available through reviewed contract
dates and conversions. An unverified opening balance prevents a Ready result.

## Setup

Deploy the backend contract first and follow its `docs/studio-treasury.md`.
Configure `STUDIO_TREASURY_COMMANDS_ENABLED`, `STUDIO_TREASURY_SLACK_IDS` and
`STUDIO_TREASURY_QUEUE_DIR` in Public Roo. Enable
`STUDIO_MEETINGS_CALENDAR_ENABLED` for calendar enrichment. Use the existing
verified workspace and bot IDs. Share the queue directory with the dedicated
private worker, which owns `.env.treasury` and its separate backend key. The
public service receives neither that key nor the sealed invoice directory.

The private worker runs `python -m roo.studio_treasury_worker --env-file
.env.treasury`. `--once` is useful for an operator tick. The optional Compose
overlay mounts the timesheet final artifacts read-only. Existing member invoice
commands and sealed PDFs retain their existing paths and behavior.

The queue preserves completed backend results before Slack delivery. A `sending`
job is an uncertain Slack delivery and needs operator inspection before changing
its delivery status. Repeated approval clicks reuse the same backend review and
bank reference. A reserved or unknown transfer keeps its work and funding held.
Inspect the private review with `review invoices REVIEW-ID`. Ask for a new
preview if it expires or a provider changes. Provider outages return an exception
and never become zero hours or additional credit.

## Verification

Run `tests/roo/test_studio_treasury.py`, `test_invoice_ingress.py`,
`test_timesheet_ingress.py`, `test_studio_invoices.py`, `test_invoice_source.py`
and the existing Linear meeting action suites with synthetic credentials.
Enable one reviewed client in preview before activating its first bank handoff.
