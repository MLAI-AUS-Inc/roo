# Roo Studio invoice implementation

Spec: docs/superpowers/specs/2026-10-12-studio-invoices.md

Goal: implement the approved fillable invoice workflow in the existing private
timesheet worker and signed public ingress.

Architecture: deterministic invoice model and PDF generator, a worker service
composed with TimesheetService, authenticated exact command queue, and opt-in
configuration/skill. Store PDFs and snapshots privately; share queue metadata
only. Use existing quarter-hour units and sealed reports, never a second ledger.

Global constraints: no live Slack/Linear writes or deployment; no migrations;
synthetic test credentials; preserve signature, channel and actor gates and
Sam's timesheet permissions. Features default off. First invoice cutoff Oct23.

Review Focus: inspect identity rebinding, malformed/tampered queues, roster
changes, delayed drafts crossing Friday noon, partial delivery and process
crashes, cutoff reconstruction, negative corrections, source failures, long
Unicode work descriptions and multipage PDFs. Check the worker startup path
and queue volume mounts, not only isolated service methods.

## Task 1: Invoice model and fillable PDF

Interfaces: consumes TimesheetConfig/report rows; produces InvoiceConfig,
period selectors, build_invoice and render_pdf for Task 2. Integer cents fixed
at 1625 per quarter hour; drafts have the payment-Friday period identifier.

1. Write tests for amounts, boundaries, isolation, nonpayable and form persistence.
2. Run .venv/bin/python -m pytest roo/tests/test_studio_invoices.py -q.
   Expected: missing invoice implementation fails.
3. Implement deterministic model and PDF with blank editable personal fields,
   static work/totals, safe multipage wrapping and internal stable reference.
4. Run the same command. Expected: all tests pass; render and inspect sample.
5. Commit model, generator and tests.

## Task 2: Private service and schedule

Interfaces: consumes Task 1 model and TimesheetService report/delivery
transport; produces InvoiceService used by Task 3 worker startup.

1. Add tests using real ledgers/receipts and an offline provider boundary for
   immutable finals, draft nonallocation, carry-forward corrections, own-only
   access, Thursday/Friday cadence, missed drafts and durable retry recovery.
2. Run .venv/bin/python -m pytest roo/tests/test_invoice_worker.py -q.
   Expected: missing service fails.
3. Implement private invoice service with one final artifact per builder and
   period, persisted draft deliveries and exact PDF bytes, active verification
   and independent schedule flag. Catch per-member failures independently.
4. Run .venv/bin/python -m pytest roo/tests/test_studio_invoices.py roo/tests/test_invoice_worker.py roo/tests/test_timesheets.py -q.
   Expected: all pass, Sam's service retains permissions.
5. Commit service and tests.

## Task 3: Signed ingress, skill and operations

Interfaces: public exact command queue contains only signed envelope actor,
team, channel, source and selector; Task 2 validates private mappings. Worker
owns all artifacts and receipts. No inferred model identities.

1. Add tests for signed DM/mention/slash commands, HMAC rejection, retries,
   malformed commands, worker startup and opt-in skill catalog.
2. Run .venv/bin/python -m pytest roo/tests/test_invoice_ingress.py -q.
   Expected: missing invoice queue and intercept fails.
3. Add ingress/config, exact-command help skill, worker loop, queue mounts,
   environment examples and operating/recovery guide. Keep private credentials
   and reports absent from Public Roo.
4. Run .venv/bin/python -m pytest roo/tests/test_invoice_ingress.py roo/tests/test_timesheet_ingress.py roo/tests/test_router_catalog.py -q.
   Expected: pass. Run repo CI split suites and bare pytest; report any baseline
   legacy collection interference explicitly. Compile changed Python.
5. Commit integration/docs; prepare whole-branch review package and dispatch
   the one fresh gpt-6-astra reviewer. Fix Important/Critical findings in one
   RED-to-GREEN pass. Preserve review and verification evidence in handoff.
