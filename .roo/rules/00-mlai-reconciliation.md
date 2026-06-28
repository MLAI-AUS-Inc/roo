# MLAI — Luma → Stripe → Xero reconciliation (project context)

You are working in MLAI's finance-ops reconciliation project. This file is loaded
into your context for every mode in this workspace — treat it as ground truth.

## What this project does
Each month: pull Luma ticket sales and Stripe payments, merge them into one
reconciliation worksheet, then reconcile the resulting Stripe payouts against the
Xero bank feed. Luma processes cards through MLAI's own Stripe account, so a Luma
sale and a Stripe charge are two views of the same transaction. Stripe settles
them to the bank as batched payouts — the payout is what gets reconciled.

## Key files
- Luma-Stripe-Xero-Reconciliation-Plan.md — full plan + monthly runbook. Read first.
- Luma-Stripe-Reconcile/luma_stripe_merge.py — the pull-and-merge script (real + --mock).
- Luma-Stripe-Reconcile/README.md — how to run it.
- Luma-Stripe-Reconcile/sample_reconciliation.xlsx — the target output shape.

## Output data model
- Sales detail — one row per ticket: date bought, event, ticket type, buyer, gross,
  Luma fee, Stripe fee, net, currency, stripe_charge_id, payout_id.
- Payout summary — one row per Stripe payout = the lump sum that hits the bank.
  The Net (bank deposit) column is the Xero matching key.

## Matching logic
Link each Luma sale to a Stripe charge by (1) Luma order id in the Stripe
description/metadata, else (2) buyer email + gross amount within ~2 days. Never
guess — unmatched rows are labelled UNMATCHED and surfaced for review.

## Xero reconciliation model
Use a Stripe clearing account: sales post in as income, each payout is a transfer
to the real bank, and the bank-feed payout reconciles 1:1 against that transfer.

## Guardrails (non-negotiable)
- Read-only against Luma, Stripe, and Xero. The pipeline never writes to them.
- Never move money, place a trade, or initiate a transfer.
- Never finalise a Xero reconcile, create/approve a transaction, or change account
  settings without explicit human confirmation. Prepare and propose; a human clicks confirm.
- Never print, log, or commit secrets. Keys live only in .env (gitignored).
- Validate the toolchain with --mock before any live run.
- Money is per-currency; never sum across currencies.
- If numbers don't tie out, stop and flag it — never "fix" it silently.
