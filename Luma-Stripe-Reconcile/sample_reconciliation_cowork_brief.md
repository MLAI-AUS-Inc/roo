# Stripe payout reconciliation brief

**For Claude Cowork.** Reconcile each Stripe deposit below in the Xero
bank feed. **Only these Stripe/Luma deposits — leave every other bank
line (transfers, PayID, etc.) untouched.**

For each: open the bank line → **Create** tab → fill Who / Why, then
**Add details** to split by event. Each split line sets **What** (account),
**Event Name**, **Project Name**, **Amount**, **Tax Rate**. The split total
must equal the bank line to the cent. **A human clicks OK to confirm.**

> Tax Rate is left as the income account's default — confirm it in Xero.

## Deposits to reconcile

| Payout | Arrived | Bank deposit | Events | Tickets |
|---|---|---|---|---|
| `po_06051` | 5 Jun 2026 | AUD 210.96 | MLAI Workshop — June | 3 |
| `po_06121` | 12 Jun 2026 | AUD 100.21 | MLAI Studio Mixer | 3 |

---

## po_06051 — AUD 210.96 received 5 Jun 2026

**Match the bank line:** Received **AUD 210.96** on/around **5 Jun 2026** (payer: Stripe).

- **Who:** Stripe Payments
- **Why:** Luma tickets — MLAI Workshop — June — 3 ticket(s) — payout po_06051

**Create → Add details (split lines):**

| What (account) | Event Name | Project Name | Amount | Tax Rate |
|---|---|---|---|---|
| Ticket Sales | AI Engineer | (set if used) | 220.00 | account default |
| Stripe Fees | — | — | -4.64 | account default |
| Luma Fees | — | — | -4.40 | account default |
| **TOTAL — must equal the bank line** | | | **210.96** | |

<details><summary>Buyers in this payout (audit — not entered per line)</summary>

- alice@example.com — MLAI Workshop — June / General — AUD 50.00 — 3 Jun 2026
- bob@example.com — MLAI Workshop — June / General — AUD 50.00 — 3 Jun 2026
- carol@example.com — MLAI Workshop — June / VIP — AUD 120.00 — 4 Jun 2026

</details>

> Cowork fills the form; **a human clicks OK to confirm the reconcile.**

## po_06121 — AUD 100.21 received 12 Jun 2026

**Match the bank line:** Received **AUD 100.21** on/around **12 Jun 2026** (payer: Stripe).

- **Who:** Stripe Payments
- **Why:** Luma tickets — MLAI Studio Mixer — 3 ticket(s) — payout po_06121

**Create → Add details (split lines):**

| What (account) | Event Name | Project Name | Amount | Tax Rate |
|---|---|---|---|---|
| Ticket Sales | ⚠ pick — Luma: MLAI Studio Mixer | (set if used) | 105.00 | account default |
| Stripe Fees | — | — | -2.69 | account default |
| Luma Fees | — | — | -2.10 | account default |
| **TOTAL — must equal the bank line** | | | **100.21** | |

<details><summary>Buyers in this payout (audit — not entered per line)</summary>

- dan@example.com — MLAI Studio Mixer / Entry — AUD 30.00 — 10 Jun 2026
- erin@example.com — MLAI Studio Mixer / Entry — AUD 30.00 — 11 Jun 2026
- frank@example.com — MLAI Studio Mixer / Entry+Drink — AUD 45.00 — 11 Jun 2026

</details>

> Cowork fills the form; **a human clicks OK to confirm the reconcile.**
