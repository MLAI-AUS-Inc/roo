---
name: reconciliation-report
description: Generate Luma→Stripe reconciliation reports or audit recent events for expected ticket sales, sponsorship, catering, and contractor evidence in Xero.
routing:
  use_when: >
    A Points Admin asks for a reconciliation or ticket-sales/payout report that
    combines Luma and Stripe, or asks to audit events for expected finance
    categories: ticket sales and sponsorship revenue, plus catering and contractor
    costs. Examples include "reconcile the last 30 days", "which payments are in
    our Stripe payouts", and "audit all events in the last six months and tell me
    which are missing ticket sales, sponsorship, catering, or contractors".
  avoid_when: >
    Attendee registration counts or check-ins only (luma-events). General curated
    data questions (mlai-data-query). Points balances, tasks, awards, or coworking
    reports (mlai-points). General accounting questions that do not ask for event
    reconciliation, event finance completeness, or Stripe payout evidence.
  examples:
    - {text: "give me a report of the last 30 days of luma events and stripe sales", action: generate_report}
    - {text: "which payments are in the stripe payouts that hit our bank account?", action: generate_report}
    - {text: "reconciliation report for June please", action: generate_report}
    - {text: "reconcile luma ticket sales against stripe for the last month", action: generate_report}
    - {text: "audit all events in the last 6 months for ticket sales, sponsorship, catering and contractor costs", action: audit_event_finances}
    - {text: "which recent events are missing sponsorship revenue or contractor expenses?", action: audit_event_finances}
  negative_examples:
    - {text: "how many people checked in to the AI Engineer event?", instead: luma-events}
    - {text: "what's my points balance?", instead: mlai-points}
    - {text: "how many people used the coworking space this week", instead: mlai-points}
actions:
  - name: generate_report
    description: Build the Luma→Stripe reconciliation pack and upload it to the thread.
    params:
      days: {type: integer, description: "Rolling window in days (default 30, max 92)."}
      since: {type: string, description: "Optional window start YYYY-MM-DD (overrides days)."}
      until: {type: string, description: "Optional window end YYYY-MM-DD."}
  - name: audit_event_finances
    description: Audit recent events for expected ticket sales, sponsorship, catering, and contractor evidence and upload the full findings.
    params:
      months: {type: integer, description: "Rolling calendar-month window (default 6, max 24)."}
      since: {type: string, description: "Optional window start YYYY-MM-DD (overrides months)."}
      until: {type: string, description: "Optional window end YYYY-MM-DD (default today)."}
requires_auth: true
---

# Reconciliation Report Skill

Generates MLAI's monthly Luma→Stripe reconciliation report and a separate
event-finance completeness audit for Points Admins. The audit checks each selected
Luma event, Humanitix event, and event found through Xero tracking for evidence of
the categories MLAI normally expects.

## Capabilities

- Ask mlai-backend for the reconciliation report over a rolling window (default 30
  days) or an explicit `since`/`until` range.
- Summarize, per Stripe payout (= one bank deposit): the events behind it, the
  buyers, gross/fees, and the net that hit the bank.
- Upload the **Cowork brief** (markdown) and the **audit workbook** (xlsx) to the
  thread so they can be handed to Claude Cowork for the Xero reconcile.
- Audit a rolling six-calendar-month window (or explicit dates) for ticket sales,
  sponsorship revenue, catering costs, and contractor costs.
- Upload a PII-minimized markdown audit showing every event, present/missing
  categories, and the evidence behind each present category.

## Parameters

- **action**: `generate_report` or `audit_event_finances`.
- **days**: Rolling window size in days. Default 30, capped at 92.
- **months**: Audit window in calendar months. Default 6, capped at 24.
- **since** / **until**: Optional explicit window (`YYYY-MM-DD`); `since` overrides the rolling window.

## Workflow

1. Confirm the requester is a Points Admin (mlai-backend also enforces this).
2. Ask mlai-backend for the requested report or event audit with the requester's Slack ID.
3. Post a concise Slack summary and upload the full evidence to the thread.
4. State that "missing" means no tracked evidence in the selected period, not
   proof that the category did not exist.

## Security & guardrails

- **Points Admins only.** Non-admins are politely refused; mlai-backend re-checks.
- **Read-only** against Luma, Humanitix, Stripe, and Xero. This skill never moves
  money, writes to a provider, or finalises a Xero reconcile.
- Roo holds no Luma/Stripe keys — mlai-backend owns API access and role enforcement.
- The report contains buyer PII (emails); never store it permanently or echo keys.
