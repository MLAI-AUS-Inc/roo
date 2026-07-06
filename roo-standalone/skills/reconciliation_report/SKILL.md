---
name: reconciliation-report
description: Generate the Luma→Stripe reconciliation report — who bought tickets for which event, how much, and which Stripe payout each landed in — for Points Admins to reconcile in Xero.
routing:
  use_when: >
    A Points Admin asks for a reconciliation or ticket-sales/payout report that
    combines Luma and Stripe: e.g. "reconcile the last 30 days of Luma events and
    Stripe sales", "which payments are inside our Stripe payouts / bank deposits",
    "monthly ticket reconciliation report". Produces a brief + workbook to
    reconcile the Stripe payouts against the bank feed in Xero.
  avoid_when: >
    Attendee registration counts or check-ins only (luma-events). General curated
    data questions (mlai-data-query). Points balances, tasks, awards, or coworking
    reports (mlai-points). Anything not about Luma ticket payments settling to the
    bank via Stripe payouts.
  examples:
    - {text: "give me a report of the last 30 days of luma events and stripe sales", action: generate_report}
    - {text: "which payments are in the stripe payouts that hit our bank account?", action: generate_report}
    - {text: "reconciliation report for June please", action: generate_report}
    - {text: "reconcile luma ticket sales against stripe for the last month", action: generate_report}
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
requires_auth: true
---

# Reconciliation Report Skill

Generates MLAI's monthly Luma→Stripe reconciliation report so a Points Admin can
reconcile the batched Stripe payouts against the Xero bank feed. Because Luma
processes card payments through MLAI's own Stripe account, a Luma ticket sale and
a Stripe charge are two views of the same transaction; Stripe settles them to the
bank as batched payouts — the payout is what gets reconciled.

## Capabilities

- Ask mlai-backend for the reconciliation report over a rolling window (default 30
  days) or an explicit `since`/`until` range.
- Summarize, per Stripe payout (= one bank deposit): the events behind it, the
  buyers, gross/fees, and the net that hit the bank.
- Upload the **Cowork brief** (markdown) and the **audit workbook** (xlsx) to the
  thread so they can be handed to Claude Cowork for the Xero reconcile.

## Parameters

- **action**: `generate_report`.
- **days**: Rolling window size in days. Default 30, capped at 92.
- **since** / **until**: Optional explicit window (`YYYY-MM-DD`); `since` overrides `days`.

## Workflow

1. Confirm the requester is a Points Admin (mlai-backend also enforces this).
2. Ask mlai-backend for the report with the requester's Slack ID.
3. Post a concise Slack summary: number of payouts, total deposited, and any
   payouts with warnings (refunds, unmatched charges).
4. Upload the brief (`.md`) and, when present, the workbook (`.xlsx`) to the thread.
5. Tell the admin they can hand the brief to Claude Cowork to reconcile in Xero —
   the API only prepares; a human clicks the final confirm in Xero.

## Security & guardrails

- **Points Admins only.** Non-admins are politely refused; mlai-backend re-checks.
- **Read-only** against Luma and Stripe. This skill never moves money, writes to
  Stripe/Luma, or finalises a Xero reconcile.
- Roo holds no Luma/Stripe keys — mlai-backend owns API access and role enforcement.
- The report contains buyer PII (emails); never store it permanently or echo keys.
