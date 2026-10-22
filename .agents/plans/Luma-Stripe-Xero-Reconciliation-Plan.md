# MLAI — Luma → Stripe → Xero Reconciliation Plan

A monthly, repeatable workflow to pull Luma ticket sales, merge them with Stripe, and reconcile the resulting Stripe payouts in Xero.

## Decisions this plan is built on
- **Luma Plus is active** → we use the Luma API directly (richest data, automatable).
- **Payments arrive as batched Stripe payouts** → we reconcile via a **Stripe clearing account** in Xero, matching each payout to the ticket sales behind it.
- **Ongoing / monthly** → a reusable pull-and-merge script plus a saved monthly reconcile routine.

## Does the Luma API expose what we need? Yes.
- **When a ticket was bought** — registration/order timestamps on every guest and order, or live via the *Ticket Registered* / *Guest Registered* webhooks.
- **How much** — order amount, coupon, and currency on `event_ticket_orders`; price per ticket type on `ticket-types/list`. (Luma's Sales History CSV also breaks out gross / Luma fee / Stripe fee / net.)
- **Which event** — every guest, ticket, and order is tied to an `event_id`.

## Data flow
```
Luma API ─┐
          ├─►  merge (one row per ticket sale)  ─►  reconciliation worksheet  ─►  Xero reconcile
Stripe API ┘                                          (sales detail + payout summary)   (Cowork in browser)
```
Key fact that makes this work: Luma processes card payments through **your own** Stripe account (Stripe Connect), so Luma sales and Stripe charges are two views of the *same* transactions. Stripe then sweeps them to your bank as **batched payouts**, which is what we reconcile against.

## Prerequisites & credentials (one-time)
- **Luma API key** — from calendar settings (requires Luma Plus). Rate limits: 200 req/min per calendar key, 500 req/min per org key.
- **Stripe restricted API key** — read-only scopes: Charges, Balance transactions, Payouts, Customers.
- **Xero** — access to the org, plus a **Stripe clearing account** set up (see Phase 4).
- **Secret handling** — keys live in a local `.env` the script reads; they are never typed into chat, the sheet, or committed anywhere.

## Phase 1 — Pull Luma
1. **List events** — `GET /v1/calendar/list-events` → `event_id`, name, start date.
2. **Guests per event** — `GET /v1/event/get-guests?event_id=…` → guest summaries + `event_tickets` (paginate).
3. **Order detail per guest** — `GET /v1/event/get-guest` → `event_ticket_orders` (amount, coupon, currency).
4. **Ticket prices** — `GET /v1/event/ticket-types/list?event_id=…`.
Keep: event_id, event name, buyer email/name, ticket id, ticket type, registered_at, amount, currency, coupon.

## Phase 2 — Pull Stripe
1. **Payouts for the month** — `GET /v1/payouts` filtered by `arrival_date`.
2. **What's inside each payout** — `GET /v1/balance_transactions?payout={id}&expand[]=data.source`.
3. **Charge detail** — gross, created, Stripe fee, net, description/metadata, email.

## Phase 3 — Merge
One row per ticket sale, linked to its Stripe charge, rolled up to the payout that hit the bank.
Match keys: (1) Luma order ref in Stripe metadata/description; else (2) email + amount + timestamp window.
Output: **Sales detail** (per ticket) and **Payout summary** (per payout = the bank deposit; the reconciliation key).

## Phase 4 — Reconcile in Xero (batched payouts)
One-time: a **Stripe clearing account**. Ticket sales post in as income; each payout is a transfer from the clearing account to the real bank; the bank-feed payout reconciles 1:1 against that transfer.
Monthly: run the script → for each payout in the bank feed, match it (payout_id + amount + date) to its transfer using the Payout summary; confirm reconcile (human clicks final confirm); flag refunds/disputes/partials.
Guardrails: never finalise a reconcile, create a transaction, or move money without explicit confirmation.

## Why the Xero API alone can't do step 4
The available Xero connector is read-only (P&L, cash position, contacts) — no bank-reconciliation function. So the reconcile runs in the Xero web app (or via the browser), using the worksheet as the matching key.

## Ongoing automation
Keep the script in the repo; run monthly or schedule it. Optionally graduate to webhooks (Luma *Ticket Registered* + Stripe `payout.paid`).

## Risks & edge cases
Multi-currency (don't sum across currencies); refunds/chargebacks (negative balance txns); timezones (store UTC); fee split (Luma CSV is source of truth); cross-month payouts (reconcile by payout date).
