---
name: reconciliation-report
description: Admin event/Stripe reports and guarded preparation of outstanding Xero bank-feed transactions.
routing:
  use_when: >
    A Points Admin asks for an event/Stripe audit or to inspect, plan, code, match,
    or prepare outstanding Xero transactions using monthly and treasurer context.
  avoid_when: >
    Attendance only (luma-events), points (mlai-points), or unrelated accounting.
  examples:
    - {text: "audit all events in the last 6 months for ticket sales, sponsorship, catering and contractor costs", action: audit_event_finances}
    - {text: "go down all outstanding Xero transactions and plan the reconciliations", action: start_statement_reconciliation}
    - {text: "use the monthly updates and treasurer@mlai.au email to prepare every outstanding Xero line", action: start_statement_reconciliation}
    - {text: "preview reconciliation run xero-reconciliation-123", action: preview_statement_reconciliation, run_id: xero-reconciliation-123}
    - {text: "execute approved reconciliation run xero-reconciliation-123", action: execute_approved_reconciliation, run_id: xero-reconciliation-123}
  negative_examples:
    - {text: "how many people checked in?", instead: luma-events}
actions:
  - name: generate_report
    description: Build the Luma→Stripe pack.
    params:
      days: {type: integer}
      since: {type: string}
      until: {type: string}
  - name: audit_event_finances
    description: Audit event revenue/cost evidence.
    params:
      months: {type: integer}
      since: {type: string}
      until: {type: string}
  - name: start_statement_reconciliation
    description: Start a context-enriched preview plan for current Xero lines.
    params:
      domain: {type: string}
      instruction: {type: string}
      statement_line_ids: {type: array}
  - name: check_reconciliation_readiness
    description: Check queue/context/Xero readiness.
    params:
      domain: {type: string}
  - name: reconciliation_outcomes
    description: Show outcomes; read-only.
    params:
      domain: {type: string}
      limit: {type: integer}
  - name: decide_reconciliation_rule_candidate
    description: Decide reviewed rule.
    params:
      candidate_id: {type: string, required: true}
      candidate_version: {type: string, required: true}
      decision: {type: string, required: true, enum: [promote, reject]}
      reason: {type: string}
      domain: {type: string}
  - name: status_statement_reconciliation
    description: Check run.
    params:
      run_id: {type: string, required: true}
      domain: {type: string}
  - name: retry_statement_reconciliation
    description: Retry context analysis.
    params:
      run_id: {type: string, required: true}
      domain: {type: string}
  - name: preview_statement_reconciliation
    description: Show exact proposed Xero payloads.
    params:
      run_id: {type: string, required: true}
      domain: {type: string}
  - name: approve_ready_reconciliation
    description: Approve ready previews; no write.
    params:
      run_id: {type: string, required: true}
      domain: {type: string}
  - name: reject_reconciliation_suggestions
    description: Reject selected suggestions.
    params:
      run_id: {type: string, required: true}
      suggestion_ids: {type: array, required: true}
      reason: {type: string, required: true}
      domain: {type: string}
  - name: execute_approved_reconciliation
    description: Write approved matches; human finalises.
    params:
      run_id: {type: string, required: true}
      domain: {type: string}
      suggestion_ids: {type: array}
requires_auth: true
---

# Reconciliation Skill

Generates MLAI's Luma→Stripe report and controls a guarded Xero statement workflow.
The agent uses the latest monthly company update and source evidence from the
connected `treasurer@mlai.au` Gmail mailbox, Slack, Linear, Luma, Humanitix,
Stripe, Xero and startup memory to propose contact, account, tax, event/project
tracking and a short description. It cannot silently post: preview, admin
approval and execution are separate run-scoped actions.

## Capabilities

- Ask mlai-backend for the reconciliation report over a rolling window (default 30
  days) or an explicit `since`/`until` range.
- Summarize, per Stripe payout (= one bank deposit): the events behind it, the
  buyers, gross/fees, and the net that hit the bank.
- Upload the **Cowork brief** (markdown) and the **audit workbook** (xlsx) to the
  thread so they can be handed to Claude Cowork for the Xero reconcile.
- Audit recent events for ticket-sale and sponsorship revenue plus catering and
  contractor costs, with a complete evidence attachment and no Xero writes.
- Start a preview-only analysis of the current, freshly imported Xero bank queue.
- Check queue freshness, monthly context, Xero write scopes and tracking setup.
- Preview ready and blocked suggestions with event/project allocation.
- Record a Points Admin's explicit approval for ready payloads.
- Create matching authorised Spend/Receive Money transactions, or bill payments
  for exact existing bills. A human still clicks green **Match/OK** in Xero.
- Confirm completed matches from the next full Xero queue import, retain their
  accounting and event/project allocation as historical evidence, and show
  repeated patterns for admin review without creating rules automatically.

## Parameters

- **action**: one of `generate_report`, `audit_event_finances`,
  `start_statement_reconciliation`,
  `check_reconciliation_readiness`, `reconciliation_outcomes`,
  `status_statement_reconciliation`,
  `retry_statement_reconciliation`,
  `decide_reconciliation_rule_candidate`,
  `preview_statement_reconciliation`,
  `approve_ready_reconciliation`, `reject_reconciliation_suggestions`, or
  `execute_approved_reconciliation`.
- **days**: Report window in days. Default 30, capped at 92.
- **months**: Event-audit calendar-month window. Default 6, capped at 24.
- **since** / **until**: Optional explicit window (`YYYY-MM-DD`).

## Statement workflow

1. Use `check_reconciliation_readiness`. Follow its blocker or recommended next
   action. A human imports the full current unreconciled Xero queue using the
   Chrome backfill; MLAI rejects stale or incomplete scans.
2. `start_statement_reconciliation` first applies unambiguous admin-verified
   rules deterministically. It sends only unresolved lines to the monthly-context
   agent. A matching outstanding Xero bill always bypasses a Spend/Receive Money
   rule and stays in the contextual bill-payment path. Repeating the same start
   request reuses its run and never dispatches duplicate analysis.
3. If all lines were resolved by verified rules the preview-only run completes
   immediately. Otherwise wait for the context agent, then use
   `preview_statement_reconciliation` and review the proposed contact,
   account/tax, description and event/project for each item.
   If Valley dispatch failed, `retry_statement_reconciliation` reuses the same
   durable run and deterministic suggestions. It refuses stale or changed Xero
   scans and will not enqueue a duplicate while the run is already queued.
4. Only an explicit `approve_ready_reconciliation` records approvals. Blocked
   suggestions remain unapproved. Use `reject_reconciliation_suggestions` with a
   reason for items that need a different allocation.
5. Only a later explicit `execute_approved_reconciliation` writes. MLAI rebuilds
   every preview and blocks stale hashes, changed fields, duplicates or lost
   scopes. Exact outstanding bills become bill payments instead of Spend Money.
6. The admin opens Xero and clicks green **Match/OK**; API execution does not
   finalise the bank reconciliation itself.
7. Import another **complete** Xero queue scan. A match-ready line absent from
   that scan is recorded as confirmed reconciled. `reconciliation_outcomes`
   reports those outcomes and repeated patterns that an admin may later turn
   into verified rules.
8. Review the candidate ID, version, exact accounting fields and allocation.
   `decide_reconciliation_rule_candidate` can then explicitly promote it to a
   verified rule or reject it with a reason. A changed version must be reviewed again.

## Security & guardrails

- **Points Admins only.** Non-admins are politely refused; mlai-backend re-checks.
- Reporting and analysis are read-only against Gmail, Slack, Linear, Luma and
  Stripe. The execute action can write matching transactions or bill payments to
  Xero, but cannot finalise a bank reconciliation.
- Never combine preview, approval and execution into one inferred action.
- Outcome learning is evidence only until an admin explicitly promotes the
  exact reviewed candidate version. Roo never activates a rule automatically.
- Approval is bound to run, statement source hash and exact Xero payload hash.
  Any change requires a fresh preview and approval.
- Roo holds no Luma/Stripe keys — mlai-backend owns API access and role enforcement.
- The report contains buyer PII (emails); never store it permanently or echo keys.
