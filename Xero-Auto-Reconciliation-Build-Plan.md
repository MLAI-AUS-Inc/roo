# Xero Auto-Reconciliation — Comprehensive Build Plan

> **Audience:** an AI coding agent. Every phase lists exact files, patterns to copy
> (with file:line references verified by exploration on 2026-06-28), acceptance
> criteria, and guardrails. Two repos are involved:
>
> - `mlai-backend` — Django + DRF, at `/Users/samdonegan/Documents/Code/mlai-backend`.
>   Holds ALL secrets (Luma, Stripe, and the new Xero keys). All external API calls
>   happen here.
> - `roo-standalone` — FastAPI Slack agent, in THIS repo under `roo-standalone/`.
>   Holds no secrets; talks to mlai-backend via `roo/clients/mlai_backend.py`.

---

## 0. Goal & end-to-end flow

An MLAI **points admin** says to Roo in Slack, in plain language:

> "Roo, give me a report of the last 30 days of Luma events and Stripe sales"

Roo generates a **reconciliation report** (who bought tickets, for which event, how
much, when, and which Stripe bulk payout each sale landed in). The admin reviews it,
then says:

> "push those to Xero"

Roo (via mlai-backend) **creates one Receive Money bank transaction per Stripe
payout in Xero** — split into gross ticket income per event (with the *Event Name* /
*Project Name* tracking categories set), minus Stripe-fee and Luma-fee lines, netting
to the exact bank deposit. When anyone opens Xero's bank reconciliation screen, each
Stripe deposit shows a suggested **Match → one click OK**. Claude Cowork (Chrome,
authenticated into Xero) can do the clicking; a human confirms.

```
Slack admin ──"report last 30 days"──► Roo router ──► reconciliation-report skill
                                                            │
                                        mlai-backend  GET /integrations/reconciliation/report
                                          ├── Luma API   (sales: who/what/when/how much)
                                          ├── Stripe API (payouts + charges inside each)
                                          └── merge ──► brief.md + recon.xlsx ──► Slack files
                                                            │
Slack admin ──"push to Xero"──► Roo ──► POST /integrations/reconciliation/xero-push
                                          └── Xero API: create Receive Money txn per payout
                                                (split lines, tracking, reference=payout_id)
                                                            │
Xero bank rec screen: each Stripe deposit shows green "Match" ──► human/Cowork clicks OK
```

### The hard constraint that shapes this design (verified 2026-07-01)
Xero has **confirmed it will not allow reconciling bank statement lines via the API**,
nor expose unreconciled statement lines (they are regulated raw bank data in AU).
Therefore:
- ✅ The API **pre-creates the matching account transactions** (fully coded + tracked).
- ✅ Xero's rec screen then auto-suggests a 1:1 match per deposit.
- ❌ The final "Reconcile/OK" click **cannot** be automated via API — a human (or
  Cowork in Chrome with human confirmation) clicks it. This also satisfies MLAI's
  guardrail: *never finalise a reconcile without explicit human confirmation.*

### Xero auth model: Custom Connection (client_credentials)
Single-org, machine-to-machine, no OAuth authorization-code flow, no refresh tokens —
request a token with client id/secret whenever needed. Available in AU. This is a
**paid Xero feature**; if unavailable, fall back to a standard OAuth2 app with the
authorization-code flow + stored refresh token (adds a one-time browser consent and a
token-storage table; note this in Phase 2 but build for custom connections first).

Scopes needed: `accounting.transactions`, `accounting.settings.read`,
`accounting.contacts` (read/create the "Stripe Payments" contact).

---

## 1. What already exists (do NOT rebuild)

| Piece | Where | Status |
|---|---|---|
| Merge/report engine (standalone) | `Luma-Stripe-Reconcile/luma_stripe_merge.py` (this repo) | ✅ Working, payout-driven, produces `brief.md` + 2-tab `xlsx`; mock verified. **Port this logic to mlai-backend in Phase 1** |
| Cowork brief format | `Luma-Stripe-Reconcile/sample_reconciliation_cowork_brief.md` | ✅ Maps to Xero Create-tab fields (Who/What/Why/Event Name/Project Name/Tax Rate) |
| Skill pipeline (LLM tool-calling router) | `roo-standalone/roo/router.py` (`route()` L327, `build_tools()` L203), `roo/skills/loader.py` (`load_skills()` L75) | ✅ Skills auto-discovered from `skills/<dir>/SKILL.md`; no manual registration |
| Points-admin check (Roo side) | `roo-standalone/roo/clients/mlai_backend.py:1654` `is_admin()`; executor helpers `_is_full_points_admin_details` / `_full_points_admin_denial` (`executor.py` ~L6916–6933) | ✅ Roles `{admin, committee, portfolio_lead}`; copy the `create_task` gating pattern (executor.py ~L8729) |
| Points-admin check (backend side) | `mlai-backend/roo/permissions.py:34–53` `is_points_admin()` | ✅ Reuse as-is |
| Role-gated integration endpoint template | `mlai-backend/integrations/api_views_luma.py:18–97` `LumaAttendeeReportView` + URL at `integrations/api_urls.py:19` | ✅ Copy this shape exactly (HasRooApiKey, `slack_user_id` query param, permission gate, base64 file payload) |
| Luma service (no pricing yet) | `mlai-backend/integrations/services/luma.py:31–102` | ⚠️ Pulls guests/tickets/check-ins only — **no order amounts** |
| Stripe HTTP pattern | `mlai-backend/integrations/services/finance.py:343–363` `_stripe_collection` (raw `requests`, Bearer auth, `Stripe-Version` header) | ⚠️ Only invoices/subscriptions today — **no payouts/balance_transactions** |
| Secrets | `mlai-backend/mlai/settings.py`: `LUMA_API_KEY` (L684), `STRIPE_SECRET_KEY` (L543–547), `ROO_API_KEY` (L681) | ✅ Luma+Stripe already configured. **Xero keys are new** |
| Slack file upload from a skill | `luma_events` `export_attendee_csv` path (base64 → Slack file) | ✅ Reuse for delivering brief + xlsx |

### What is genuinely new
1. Luma **order amounts** fetch (Phase 1) — the one unvalidated external assumption.
2. Stripe **payouts + balance_transactions** fetch (Phase 1).
3. Reconciliation **report endpoint** (Phase 1).
4. **Xero client + push endpoint + audit model** (Phase 2) — all-new integration.
5. Roo **client methods** (Phase 3) and **skill** (Phase 4).

---

## Phase 0 — Prerequisites & validation (HUMAN + agent, do first)

**P0.1 (human, Sam):** In Xero Developer portal, create a **Custom Connection** app
for the MLAI org with scopes `accounting.transactions accounting.settings.read
accounting.contacts`. Put `XERO_CLIENT_ID` / `XERO_CLIENT_SECRET` into
`mlai-backend/.env`. If Custom Connections isn't available on the plan, create a
standard OAuth2 app instead and tell the agent — Phase 2 has a fallback note.

**P0.2 (agent): ✅ DONE 2026-07-01 — see `Luma-Stripe-Reconcile/PHASE-0.2-FINDINGS.md`.**
Live read-only probes confirmed: Luma exposes per-order **amounts** in integer cents
(`event_ticket_orders[].amount` + `currency`, via `get-guest` detail); Stripe payouts
+ balance_transactions work and **tie out to the cent**; and — critically — every
Stripe charge's `metadata` carries `event_api_id` + buyer `email` + `description`
(event name), so **the Luma↔Stripe match is DETERMINISTIC, not fuzzy**. Fees: only a
Stripe fee is deducted in the payout (no Luma application fee). Net effect: Stripe is
the spine, Luma is per-event enrichment, and the Xero split drops the Luma-fee line.
**Also fix:** `STRIPE_SECRET_KEY` is defined twice in `mlai-backend/.env` (a
`sk_test_` stub then the real `rk_live_`) — dedupe it.

**P0.3 (agent):** Read-only Xero sanity check: with the new credentials, call
`GET https://api.xro/2.0/Organisation`, `GET /Accounts`, `GET /TrackingCategories`.
Record (into `Luma-Stripe-Reconcile/xero_map.json`, committed):
- The **bank account** code/ID the Stripe payouts land in (match by name with Sam).
- Account codes for ticket income / Stripe fees / Luma fees (create in Xero UI if
  missing — human step; suggest names "Ticket Sales", "Stripe Fees", "Luma Fees").
- The two tracking categories and their exact names (screenshot showed **"Event
  Name"** and **"Project Name"**) + the current option lists.
- Whether the org is GST-registered (`Organisation.PaysTax`) — informs tax handling.

**P0.4 (human, Sam):** Confirm the mapping file: which income/fee accounts to use,
and the Luma-event-name → Xero *Event Name* option mapping (extend
`event_map.json`). New Luma events will need new tracking options — decide policy:
agent may **create tracking options via API** (allowed by `accounting.settings`
scope) or flag for manual creation. Default: create automatically, log it.

**Acceptance:** `xero_map.json` committed; P0.2 output confirms amounts exist;
Sam has signed off on account codes + tracking policy.

---

## Phase 1 — mlai-backend: reconciliation report endpoint

> Branch: `feature/luma-stripe-reconciliation` in mlai-backend.

**1.1 `integrations/services/reconciliation.py` (new)** — `LumaStripeReconciliationService`.
Port the logic from this repo's `Luma-Stripe-Reconcile/luma_stripe_merge.py`:

- **Stripe side (payout-driven — this ordering matters):**
  `GET /v1/payouts?arrival_date[gte]=&arrival_date[lt]=` then per payout
  `GET /v1/balance_transactions?payout={id}&expand[]=data.source` (paginate with
  `starting_after`, `limit=100`). Include `type=charge` rows (gross, fee, net,
  email, description, created); collect `type=refund`/`adjustment` rows separately
  and surface them (do not silently drop — they change the payout total).
  Mirror the HTTP style of `finance.py:_stripe_collection` (raw `requests`, `Bearer
  {settings.STRIPE_SECRET_KEY}`, `Stripe-Version`, `timeout=(3,20)`), but note the
  key comes from settings, not a `connection.access_token`.
- **Luma side (ENRICHMENT ONLY — Stripe is the spine; see PHASE-0.2-FINDINGS §3):**
  the Stripe charge already carries `metadata.event_api_id`, buyer `email`, and the
  event name in `description`, so the core report needs no Luma call. Use Luma only to
  enrich (ticket-type names, coupon detail, canonical event name): map each distinct
  `event_api_id` seen in the payouts → one Luma event (`/calendar/list-events` or a
  by-id lookup). Only drill to per-guest `/v1/event/get-guest` (which holds
  `event_ticket_orders[].amount` in **cents**) if per-order ticket-type/coupon detail
  is wanted — this is the N+1 path, so gate it behind a flag and throttle
  (200 req/min, retry-on-429). Header `x-luma-api-key`, matching `services/luma.py`.
- **Merge (one row per Stripe charge):** join **deterministically** on
  `charge.metadata.event_api_id` → Luma event; refine to a specific Luma order by
  `metadata.email` (+ amount if a buyer has multiple orders in one event). A charge
  whose `event_api_id` doesn't resolve is still kept and labelled
  `UNKNOWN (no Luma match)` — never guessed, never dropped (the payout must tie to
  the cent). Gross/fee/net/currency/created come from the Stripe charge, not Luma.
- **Outputs** (all returned in one JSON payload, files base64 like the attendee
  report's CSV block, `luma.py:90–96` pattern):
  - `payouts`: structured JSON — per payout: id, arrival date, currency, net,
    gross/fee breakdown, per-event splits, per-charge rows (buyer, event, ticket
    type, amount, bought_at), refunds/adjustments, match warnings.
  - `brief_md`: the Cowork/human-readable brief (port `write_brief`).
  - `workbook_xlsx`: 2-tab audit workbook (port `write_xlsx`; add `openpyxl` to
    mlai-backend `requirements.txt`).
- Money handling: integer **cents** internally, per-currency, never cross-sum.

**1.2 `integrations/api_views_reconciliation.py` (new)** — `ReconciliationReportView`.
Copy `LumaAttendeeReportView` (`api_views_luma.py:18–97`) exactly in shape:
`authentication_classes = []`, `permission_classes = [HasRooApiKey]`,
`slack_user_id` from query params, gate with **`is_points_admin`**
(`roo/permissions.py:34`) → 403 with a clear message. Params: `days` (default 30,
max 92), or `since`/`until` (ISO dates), `include_xlsx` (default true).
Map service exceptions to 502/503/429 like the Luma view (L54–62).

**1.3 `integrations/api_urls.py`:** add
`path('reconciliation/report', ReconciliationReportView.as_view(), name='reconciliation_report')`.

**1.4 Tests `integrations/tests_reconciliation.py`:** mirror
`integrations/tests_luma.py:217–290` — patch `HasRooApiKey.has_permission`, create
`PointsAdmin` fixtures, mock Stripe+Luma HTTP. Cases: non-admin 403; happy path
(2 payouts, multi-event split, totals tie); unmatched charge labelled; refund in
payout surfaced; pagination; per-currency separation; Luma 429 retry.

**Acceptance:** tests green; a real dry call (Sam-triggered, real keys) returns
payouts whose nets equal actual bank deposits.

---

## Phase 2 — mlai-backend: Xero client, push endpoint, audit model

> Same branch or `feature/xero-push`, stacked on Phase 1.

**2.1 Settings (`mlai/settings.py`):** `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`
(pattern: `_env_first` like `STRIPE_SECRET_KEY` L543), plus
`XERO_BANK_ACCOUNT_CODE`, `XERO_TICKET_INCOME_CODE`, `XERO_STRIPE_FEE_CODE`,
`XERO_LUMA_FEE_CODE`, `XERO_EVENT_TRACKING_NAME` (default `"Event Name"`),
`XERO_PROJECT_TRACKING_NAME` (default `"Project Name"`) — all env-overridable,
seeded from Phase 0's `xero_map.json`.

**2.2 `integrations/services/xero.py` (new)** — `XeroClient` (raw `requests`, matching
repo convention — do not add the `xero-python` SDK):
- `_get_token()`: `POST https://identity.xero.com/connect/token`,
  `grant_type=client_credentials`, basic auth with client id/secret, scope string.
  Cache in-process until expiry (~30 min). Custom-connection tokens are bound to the
  single org, so **no `Xero-Tenant-Id` header is required**. *(Fallback if standard
  OAuth2 app: add a `XeroToken` model storing refresh token, refresh on use, and set
  `Xero-Tenant-Id` from the connections endpoint.)*
- Reads: `get_organisation()`, `get_accounts()`, `get_tracking_categories()`,
  `find_bank_transactions(reference)` — `GET /api.xro/2.0/BankTransactions?where=Reference=="{ref}"`.
- Writes:
  - `ensure_contact("Stripe Payments")` → find-or-create.
  - `ensure_tracking_option(category_id, option_name)` → find-or-create (per P0.4
    policy).
  - `create_receive_money(payout)` → `PUT /api.xro/2.0/BankTransactions` with:
    `Type=RECEIVE`, `BankAccount={Code: settings.XERO_BANK_ACCOUNT_CODE}`,
    `Contact={Name: "Stripe Payments"}`, `Date=payout arrival`,
    `Reference="Luma payout {payout_id}"` (the idempotency key),
    `LineAmountTypes=Inclusive`, and LineItems:
    - one **positive** line per event: `Description="Luma tickets — {event} — N
      tickets"`, `AccountCode=income`, `Tracking=[{Event Name: option}, {Project
      Name: option?}]`, `LineAmount=gross for that event`
    - one **negative** line `AccountCode=Stripe Fees, LineAmount=-stripe_fee`
    - **(No Luma-fee line — Phase 0.2 confirmed no Luma application fee is deducted
      from the payout. gross − Stripe fee = net = deposit.)** Keep the code path
      behind a config flag in case a future account has a Luma Connect fee.
    - Omit `TaxType` on every line → Xero applies each **account's default tax
      rate** (matches Sam's decision). Total must equal payout net **exactly**; if
      rounding drifts by cents, add a rounding line to the income account and flag.
- Error surface: raise typed exceptions (`XeroConfigurationError`, `XeroAPIError`)
  mapped in the view like the Luma ones.

**2.3 Audit + idempotency model (`integrations/models.py` + migration):**
```python
class XeroReconciliationPush(models.Model):
    payout_id = models.CharField(max_length=64, unique=True)   # hard idempotency
    xero_bank_transaction_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(...)   # created | skipped_existing | error | dry_run
    currency = models.CharField(max_length=3)
    net_amount_cents = models.BigIntegerField()
    payload_summary = models.JSONField()      # events, splits, warnings
    requested_by_slack_id = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
```
Belt-and-braces idempotency: check this table **and** query Xero by `Reference`
before any create. Re-runs must be safe.

**2.4 `ReconciliationXeroPushView`** (in `api_views_reconciliation.py`):
`POST /integrations/reconciliation/xero-push`. Body:
`{slack_user_id, payout_ids: [...] | days: 30, dry_run: bool=true, confirm: bool=false}`.
- Gate: `is_points_admin` → 403 otherwise.
- **Writes happen ONLY when `dry_run=false` AND `confirm=true`.** Anything else
  returns the full preview (what would be created, per payout) without touching Xero.
- Skips: already-pushed payouts (both idempotency checks), non-AUD payouts unless
  the target bank account currency matches, payouts containing unresolved
  `UNKNOWN (no Luma match)` charges **unless** `allow_unknown=true` (they then post
  to income with tracking left blank + a warning).
- Response per payout: `status`, `xero_bank_transaction_id`, deep link
  `https://go.xero.com/Bank/ViewTransaction.aspx?bankTransactionID={id}`, warnings.

**2.5 URL:** `path('reconciliation/xero-push', ReconciliationXeroPushView.as_view(), name='reconciliation_xero_push')`.

**2.6 Tests:** mock all Xero HTTP. Cases: dry-run default returns preview + zero
writes; `confirm` without `dry_run=false` still doesn't write; idempotent re-push
skips; reference collision in Xero skips; totals-mismatch aborts that payout with
error status; tracking option auto-created; non-admin 403; unknown-charge payout
blocked without `allow_unknown`.

**Acceptance:** tests green; one real payout pushed with `dry_run=false confirm=true`
against the live org, verified visually in Xero (correct splits/tracking, and the
bank rec screen shows the green Match for that deposit).

---

## Phase 3 — roo-standalone: client methods

> Branch in THIS repo, `roo-standalone/roo/clients/mlai_backend.py`. Mirror
> `get_luma_attendee_report` (L1097–1126).

```python
async def get_reconciliation_report(self, slack_user_id, *, days=30, include_xlsx=True) -> dict
    # GET {base}/integrations/reconciliation/report

async def push_reconciliation_to_xero(self, slack_user_id, *, payout_ids=None,
                                      days=None, dry_run=True, confirm=False) -> dict
    # POST {base}/integrations/reconciliation/xero-push
```
Same auth header the client already uses (`ROO_API_KEY`); 30s timeout; surface
backend error text verbatim so denials read cleanly in Slack.

---

## Phase 4 — roo-standalone: the skill (plain-language trigger)

**4.1 `skills/reconciliation_report/SKILL.md` (new).** Must pass loader validation
(`roo/skills/loader.py:196–234`): ≥3 examples, ≥1 negative example.

```yaml
---
name: reconciliation-report
description: Generate the Luma→Stripe reconciliation report and optionally push
  Receive Money transactions to Xero for one-click bank-rec matching. Points
  admins only.
routing:
  use_when: >
    An admin asks for a reconciliation / ticket-sales / payout report combining
    Luma and Stripe (e.g. "last 30 days of Luma events and Stripe sales"), asks
    which payments are inside Stripe bank payouts, or asks to push/prepare those
    payouts in Xero.
  avoid_when: >
    Attendee counts or check-ins only (luma-events). General data questions
    (mlai-data-query). Points balances or tasks (mlai-points). Anything about
    invoices unrelated to Luma ticket payouts.
  examples:
    - {text: "give me a report of the last 30 days of luma events and stripe sales", action: generate_report}
    - {text: "which payments are in the stripe payouts that hit our bank?", action: generate_report}
    - {text: "reconciliation report for June please", action: generate_report}
    - {text: "push those payouts to xero", action: push_to_xero}
  negative_examples:
    - {text: "how many people checked in to the AI Engineer event?", instead: luma-events}
    - {text: "what's my points balance", instead: mlai-points}
actions:
  - name: generate_report
    description: Build the Luma×Stripe reconciliation pack and upload it to the thread.
    params:
      days: {type: integer, description: "Rolling window in days (default 30, max 92)"}
  - name: push_to_xero
    description: Create Receive Money transactions in Xero for the reported payouts.
      ALWAYS a dry-run preview first; writes only after the admin explicitly confirms.
    params:
      confirm: {type: boolean, description: "True only when the admin has explicitly confirmed the previewed push"}
      days: {type: integer, description: "Window to push (default: same as last report)"}
requires_auth: true
---
# (markdown body: workflow, response style, guardrails — mirror mlai_points body style)
```

**4.2 Executor handler** (`roo/skills/executor.py`): add dispatch
`elif skill.name == "reconciliation-report"` → `_execute_reconciliation_report()`:
1. **Admin gate first** — copy the `create_task` pattern (~L8729):
   `admin_details = await client.get_admin_details(user_id)`;
   `if not self._is_full_points_admin_details(admin_details): return
   self._full_points_admin_denial(admin_details, "generate reconciliation reports")`.
2. `generate_report`: call client → decode base64 `brief_md` + `workbook_xlsx` →
   upload both to the thread (same Slack file mechanics as the Luma CSV export) →
   post a short summary message (payout count, total, UNMATCHED count, "say *push
   to xero* when ready").
3. `push_to_xero`: **two-turn confirmation, enforced in code, not just prompt:**
   - First call (or `confirm` param false): call backend with `dry_run=true`,
     render the preview (per-payout: amount, events, what will be created), end
     with "Reply **confirm push** to create these in Xero."
   - Only when router extracts `confirm=true` from an explicit confirmation
     message: call with `dry_run=false, confirm=true`, then report per-payout
     results + Xero deep links + "open the bank rec screen — each deposit now has
     a one-click Match."
4. Never echo keys; on backend 403 relay the denial text.

**4.3 Roo tests** (`roo/tests/test_reconciliation_report.py`): routing fixtures
(each SKILL.md example routes here; negatives route away — reuse the routing_eval
harness), admin-denied path, report path uploads 2 files, push path requires the
two-turn confirm (a single "push to xero and confirm" first message must still
dry-run — assert the handler ignores `confirm=true` unless a prior dry-run preview
exists in the thread state, or simply always dry-runs on the first push call per
thread).

**Acceptance:** in a dev Slack workspace — non-admin politely denied; admin gets
files; push previews then writes only after explicit confirm.

---

## Phase 5 — The last mile in Xero (human/Cowork, minimal now)

After a push, reconciliation is one click per deposit:
1. Open Xero → Bank account → Reconcile.
2. Each Stripe deposit shows a suggested **Match** against the pushed Receive Money
   transaction (same amount/date/reference). Click **OK**.
3. Lines with no green match = something's off (refund-bearing payout, unknown
   charge, cents drift) → check the brief's warnings; do NOT force it.

Cowork's brief (Phase 1 `brief_md`) should be updated to this simpler runbook: a
table of payout → amount → expected match reference, plus the warnings list.
Cowork in Chrome may click the OKs, **with a human watching/confirming** per MLAI
guardrails. Non-Stripe bank lines are never touched.

---

## Phase 6 — Rollout order & definition of done

1. Phase 0 (validation) → 2. Phase 1 PR (mlai-backend) → 3. Phase 2 PR
   (mlai-backend) → 4. Phases 3+4 PR (this repo) → 5. First supervised
   end-to-end run on ONE payout → 6. Full 30-day run.

**Definition of done:** an admin can, entirely from Slack, (a) get the who/what/
when/how-much report with payout groupings, (b) push all clean payouts to Xero,
and (c) reconcile the month in Xero with one click per deposit; non-admins are
refused; re-runs never duplicate; every Xero write is audited in
`XeroReconciliationPush` with the requesting admin's Slack ID.

## Standing guardrails (encode in code, not just prompts)
- Read-only against Luma and Stripe, always.
- Xero writes: dry-run by default; write only with `dry_run=false AND confirm=true`
  from a points admin; idempotent by payout Reference + DB unique key.
- The reconcile click itself is always human-confirmed (API cannot and should not
  do it).
- Per-currency; integer cents; if a payout doesn't tie to the cent, flag — never
  silently fix.
- Secrets only in mlai-backend env/settings; never in Roo, Slack, logs, or commits.
