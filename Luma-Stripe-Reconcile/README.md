# Luma → Stripe → Xero reconciliation

Pulls Luma ticket sales and Stripe payments, merges them, and writes a
two-tab Xero reconciliation worksheet:

- **Sales detail** — one row per ticket sale, linked to its Stripe charge and payout.
- **Payout summary** — one row per Stripe payout = the lump sum that hits your bank. This is the line you reconcile in Xero.

## Quick look (no keys needed)

    pip install -r requirements.txt
    python luma_stripe_merge.py --mock --out sample_reconciliation.xlsx

## Real run

1. `cp .env.example .env` and fill in your keys (LUMA_API_KEY, STRIPE_API_KEY restricted read-only).
2. `python luma_stripe_merge.py --month 2026-06 --out june_2026.xlsx`  (defaults to last month).

## How the match works
Each Luma sale links to a Stripe charge by (1) Luma order id in the Stripe description/metadata, else (2) buyer email + gross amount. Unmatched rows are flagged `UNMATCHED` (causes: refunds, typo'd email, cross-month payout).

## Then reconcile in Xero
Set up a Stripe clearing account: sales post in as income, each payout is a transfer to your real bank, and the bank-feed payout reconciles 1:1 against that transfer. Match on the Payout summary's `Net (bank deposit)`. See ../Luma-Stripe-Xero-Reconciliation-Plan.md.

## Notes
- Live API calls follow the Luma/Stripe REST docs; validate field names on the first real run.
- Money is per-currency; no cross-currency conversion. Read-only: never writes to Luma/Stripe/Xero.
