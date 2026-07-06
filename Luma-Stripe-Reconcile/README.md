# Luma → Stripe → Xero reconciliation

Pulls Luma ticket sales and Stripe payments, merges them, and writes a
**reconciliation pack**:

1. **`<out>.xlsx`** — the audit trail, two tabs:
   - **Sales detail** — one row per Stripe charge, linked to its Luma sale and payout.
   - **Payout summary** — one row per Stripe payout = the lump sum that hits your bank.
2. **`<out>_cowork_brief.md`** — the Claude Cowork hand-off. One section per payout,
   pre-filled with the Xero **Create** fields (Who / What / Why / Event Name /
   Project Name / Tax Rate) and split lines that total to the exact bank deposit.

The pull is **payout-driven**: it starts from every payout that arrived in the
window and pulls every charge inside it, so each payout's lines tie to the cent
against the bank line.

## Quick look (no keys needed)

    pip install -r requirements.txt
    python luma_stripe_merge.py --mock --out sample_reconciliation.xlsx

## Real run

1. `cp .env.example .env` and fill in your keys (LUMA_API_KEY, STRIPE_API_KEY restricted read-only).
2. Pick the window:
   - `python luma_stripe_merge.py --days 30 --out last30.xlsx`   (rolling last 30 days — the default)
   - `python luma_stripe_merge.py --since 2026-06-01 --until 2026-06-30 --out june.xlsx`
   - `python luma_stripe_merge.py --month 2026-06 --out june.xlsx`

## Event names → Xero
`cp event_map.example.json event_map.json` and map each Luma event name to the
exact Xero **Event Name** tracking option. Unmapped events show a `⚠ pick`
prompt in the brief so Cowork chooses the dropdown value manually. Edit the
account names (Ticket Sales / Stripe Fees / Luma Fees) at the top of
`luma_stripe_merge.py` to match your chart of accounts.

## How the match works
Each Stripe charge links to a Luma sale by (1) Luma order id in the Stripe description/metadata, else (2) buyer email + gross amount. A charge with no Luma match still appears (so the payout total ties), labelled `UNKNOWN (no Luma match)` — pick its event manually.

## Then reconcile in Xero
Booked as **gross income + fees**: each payout's bank line is split into ticket-income line(s) per event (gross) minus a Stripe-fee and a Luma-fee line, netting to the deposit. Cowork fills the Create form per the brief; **a human clicks OK to confirm.** Only Stripe/Luma deposits are touched — every other bank line is left alone. See ../Luma-Stripe-Xero-Reconciliation-Plan.md.

## Notes
- Live API calls follow the Luma/Stripe REST docs; validate field names on the first real run.
- Money is per-currency; no cross-currency conversion. Read-only: never writes to Luma/Stripe/Xero.
