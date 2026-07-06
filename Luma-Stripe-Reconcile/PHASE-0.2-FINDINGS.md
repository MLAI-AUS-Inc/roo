# Phase 0.2 — Live API validation findings

**Date:** 2026-07-01 · **Method:** read-only probes against the live Luma + Stripe
APIs using the keys already in `mlai-backend/.env`. Buyer PII was redacted in all
output; no data was written to Luma or Stripe. Probe scripts are throwaway (kept in
the session scratchpad, not committed).

## Verdict: GO. Both make-or-break assumptions hold, and the hardest risk (the Luma↔Stripe match) is resolved.

---

## 1. Luma API exposes per-order amounts ✅

- Auth header `x-luma-api-key`; base `https://public-api.luma.com/v1`.
- `GET /calendar/list-events` → events with `api_id` (`evt-…`), `name`, `start_at`,
  `timezone`. (Returned 45 events for MLAI.)
- `GET /event/get-guests?event_api_id=evt-…` → guest list, paginated (page size 50).
  **The guest-list rows do NOT contain populated order amounts** (`event_tickets: []`,
  `event_ticket: null`).
- `GET /event/get-guest?event_api_id=…&guest_api_id=gst-…` → **this** is where the
  paid order lives, as `event_ticket_orders[]`:

  ```json
  {
    "amount": 15000,          // integer CENTS
    "amount_discount": 0,
    "amount_tax": 0,
    "currency": "aud",        // lower-case; free events default to "usd"
    "coupon_info": null,
    "is_captured": false,
    "api_id": "evttktord-…",
    "id": "evttktord-…"
  }
  ```

  Real confirmations: an order of `amount: 15000 currency: aud` (A$150.00) and
  `amount: 1000 aud` (A$10.00). Free events show `amount: 0`.

**Consequence:** amounts require the per-guest detail call → **N+1 requests** (one
`get-guest` per attendee). Luma rate limit is ~200 req/min per calendar key, so a
month of events × ~50 guests each needs throttling + pagination. **But see §3 — we
mostly don't need this loop.**

**Not in the Luma order object:** any Stripe id, and any fee breakdown (no Luma fee,
no Stripe fee). Fees come from Stripe.

---

## 2. Stripe payouts + tie-out ✅

- `GET /v1/payouts` works with the restricted `rk_live_` key. 8 recent AUD payouts,
  all `status: paid` (e.g. `105.54`, `516.04`, `1085.31` AUD).
- `GET /v1/balance_transactions?payout={id}&expand[]=data.source` returns the charges
  inside a payout.
- **Tie-out confirmed empirically:** for the `105.54 AUD` payout, the 4 charges'
  `net` summed to **exactly 105.54** (the payout also appears as a `type: payout`
  balance-txn of `-105.54`, so the set nets to 0). `charge.gross − charge.fee = net`;
  Σ`net` = the bank deposit. **The payout-driven reconciliation math is correct.**

---

## 3. The Luma↔Stripe match is DETERMINISTIC ✅ (biggest risk, resolved)

Every Stripe charge carries Luma attribution in `metadata` **and** `description`:

| Field | Example (PII redacted) | Use |
|---|---|---|
| `metadata.event_api_id` | `evt-eo0U89lNwQ6wQyD` | **Exact join to the Luma event** (same id as list-events `api_id`) |
| `metadata.email` | `<buyer email>` | Join to the specific Luma guest |
| `metadata.luma_payment_started_api_id` | `paystart-FOOqkxCwEHMu4eG` | Luma payment ref |
| `metadata.payment_type` | `registration` | Filter ticket sales vs other |
| `description` | `How to Start a Startup \| Melbourne` | Human-readable event name |
| `statement_descriptor` | `MLAI AUS` | — |

**Implication — Stripe is the spine, Luma is enrichment.** The report ("who bought
what event, how much, when, in which payout") can be built almost entirely from
Stripe: payout → charges → each charge already knows its **event** (`event_api_id`
+ name), **buyer** (`email`), **gross/fee/net**, **currency**, and **created**
timestamp. Luma is only needed to enrich: ticket-type names, coupon detail, canonical
event name, buyer name. That enrichment can be fetched **per event**
(map `event_api_id` → Luma event once), largely avoiding the per-guest N+1 loop.

Match key precedence for Phase 1:
1. `charge.metadata.event_api_id` → Luma event (deterministic).
2. `charge.metadata.email` (+ amount if a buyer has multiple orders in one event) →
   specific Luma order, only when ticket-type/coupon enrichment is wanted.

---

## 4. Fee model is simpler than the original plan assumed

The only deduction between charge gross and the bank payout is the **Stripe fee**
(observed ~$0.55 on $14.60, ~$1.40 on $64.79). **No Luma application/platform fee is
taken out of the payout** (balance-transaction types were only `charge` and
`payout`; no `application_fee`). So the Xero split simplifies to:

```
+ gross ticket income per event      (from Stripe charge gross, attributed by event)
−  Stripe fee                        (from Stripe charge fee)
=  net = the bank deposit            (payout amount)
```

The planned **"Luma fee" line is unnecessary** for this account (drop it unless the
accountant says Luma invoices a fee separately, which would be a separate bill, not
part of the payout). GST: `amount_tax` is available on the Luma order if needed, but
tax handling stays "account default" per the earlier decision.

---

## 5. Environment caveats (fix before Phase 1 build)

- **Luma key** in `mlai-backend/.env` is **live** and working (that's why the probe
  pulled real data). ✅
- **`STRIPE_SECRET_KEY` is defined twice** in `mlai-backend/.env`: a `sk_test_…`
  stub first, then the real `rk_live_…` restricted key (107 chars). Dotenv/shell
  last-wins makes the live key effective, but the duplicate is a footgun — **dedupe
  it** and confirm the deploy env uses the live restricted key.
- The `rk_live_` restricted key already has the scopes we need (payouts,
  balance_transactions, charges read all returned 200). ✅

---

## 6. Still unvalidated (carry into Phase 1 as smaller risks)

- **Refunds / disputes / partial payouts** were not in the sampled payouts — the
  service must still handle `type: refund`/`adjustment` balance-txns (they change the
  payout total) and surface them.
- **Coupons:** every sampled `coupon_info` was `null`; the populated shape is unseen.
- **Whether Luma adds a service fee on top of the base ticket price** (i.e. does
  Stripe `gross` = Luma order `amount`, or `amount + luma_service_fee`?) — a
  revenue-recognition nuance, not a bank-reconciliation blocker. Verify by matching
  one specific order across both systems during Phase 1.
- **Multi-currency:** all sampled payouts were AUD; keep per-currency handling.

---

## Net effect on the build plan
- Phase 1 Luma fetch: keep amounts capability, but **drive from Stripe**; use Luma
  per-event enrichment, not a mandatory per-guest sweep. Lower cost, fewer rate-limit
  concerns.
- Phase 1 merge: **deterministic** join on `metadata.event_api_id` (+ email), not
  fuzzy email+amount+time. Far fewer UNMATCHED rows expected.
- Phase 2 Xero split: **two line types** (income per event, Stripe fee) — drop the
  Luma-fee line.
- Phase 0 prerequisites: dedupe the Stripe env var; the Luma-amount validation
  (P0.2) is **complete and positive**.
