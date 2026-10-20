# Event discounts with Roo Points

This opt-in member capability quotes a configured Luma event discount and
returns a coupon link after the member explicitly confirms the quoted spend.
Public Roo owns the conversation; `mlai-backend` owns identity mapping,
configured offers, quote expiry, point reservations, coupon creation and retry
recovery. Roo does not call Luma directly.

## Member flow

1. DM Public Roo: `I want to spend my points to get a discount to AI Meetup`.
2. Roo returns the backend's event details, exact benefit, points cost, eligible
   balance and short quote expiry. No points are spent for a quote.
3. Reply **in the same private thread** using the exact command Roo supplies:
   `confirm event discount <quote UUID>`.
4. Roo returns the unique coupon and full Luma link when the backend reports
   `fulfilled`. A `processing` response means coupon creation is still pending.
5. Ask `my event discounts` to recover an existing link or inspect progress.
   Repeating the same confirmation reuses the same operation and cannot create
   another spend for that quote.

Name an event or supply its canonical event ID (for example `evt-...`).
The MVP does not resolve a Luma URL into an event offer.

Requests from shared channels receive a prompt to DM Roo before any quote or
redemption request. A bare `yes`, quoted command, negated command, or model
argument such as `confirmed: true` cannot authorize spending. The MVP uses an
explicit text confirmation rather than an interactive card. The same-thread
requirement is enforced by the backend quote scope, including the Slack thread
root timestamp. Copying the command to another DM thread requires a fresh quote.

## Configuration and rollout

The feature is disabled by default:

```dotenv
ROO_SURFACE=public
ROO_EVENT_DISCOUNTS_ENABLED=false
ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID=TMLAI_WORKSPACE_ID
MLAI_BACKEND_URL=http://127.0.0.1:8001
ROO_API_KEY=replace-with-dedicated-roo-service-key
```

Use development-only credentials for local checks. Enable the Roo flag only
after the matching backend routes, approved schema, configured event offer and
Luma integration are ready. Existing history remains readable when the feature flag is disabled, provided
the workspace and service-key configuration is retained. New quotes and
redemptions require both feature flags enabled. The backend feature flag must also be enabled, with the same explicit
`ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID` workspace binding. This
capability uses the public Roo service key and never organisational-memory or
administrative credentials. It is not an implicit contextual action.

The backend enforces which events, tickets, discounts and points are eligible.
A unique single-use coupon is associated with the member's redemption record;
standard Luma coupons may still be shared before first use. Ticket quantity and
coupon expiry restrictions must be configured and verified in Luma for each
pilot offer.

## Service contract

All routes are under `/api/v1/points/event-discounts/`, with a trailing slash:

| Method | Route | Additional input |
| --- | --- | --- |
| POST | `quote/` | `event_query` |
| POST | `redeem/` | `quote_id`, `confirmed: true`, `request_id` |
| GET | `redemptions/` | none |
| GET | `redemptions/<UUID>/` | none |

Each request also includes `slack_user_id`, `slack_team_id`, `slack_channel_id`
and `slack_thread_ts` from Roo's verified `BackendActorContext`. POST uses JSON;
GET uses query parameters. Roo rejects missing/mismatched context before making
an HTTP call, and never derives actor identity from model parameters or message
mentions. Requests use `X-API-Key: <ROO_API_KEY>`.

A redeem operation uses the same value in `Idempotency-Key` and `request_id`:
`roo-event-discount-<sha256(team_id:user_id:quote_id)>`. The backend must check
actor ownership, exact quote conversation binding, explicit confirmation,
expiry, eligibility and idempotency before mutation. It must retain the same
redemption after an uncertain Luma response and reconcile before creating again.

A quote returns `quote_id`, `expires_at`, `event` (`id`, `name`, `url`,
`starts_at`), `points_cost`, `discount_description` and `eligible_balance`.
A redemption returns `id`, `status`, `event`, `points_cost`, `coupon_code`,
`checkout_url`, `expires_at`, `created_at`; list wraps records in
`{"redemptions": [...]}`. Initial states are `processing`, `fulfilled`, `failed`
and `cancelled`. `fulfilled` requires an HTTPS Luma link carrying exactly the
returned coupon in its `coupon` query parameter. Roo rejects malformed results,
untrusted destinations or mismatched coupon URLs.

The backend returns structured `{code, detail}` errors. Roo exposes a fixed
private explanation rather than raw backend responses or upstream details.

## Verification

From `roo-standalone`, with the isolated development virtualenv:

```bash
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
.venv/bin/python -m pytest roo/tests/test_event_discounts.py \
  roo/tests/test_router_catalog.py roo/tests/test_router_v2.py \
  roo/tests/test_routing_eval_gate.py -q
```

These checks use fake clients and a mocked HTTP boundary. They do not call
Slack, Luma, the model provider or a backend database. They cover strict consent,
verified private actor matching, disabled/admin surfaces, duplicate retry keys,
endpoint shape, unsafe coupon links, processing/recovery messages and routing.
They do not prove a live Luma coupon is created or applied at checkout. Verify a
designated test event with approved development credentials before pilot launch.
