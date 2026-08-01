# Unified Roo Admin Brain production deployment

MLAI uses one Slack app and one user-facing identity: the existing `@Roo`.
There is no `@Roo Admin` app and no separate staging environment. Two runtime
processes preserve the security boundary:

- Public Roo verifies Slack, routes requests, runs all existing public skills,
  and posts responses. It has a route-only backend principal and never the
  organisational-memory credential.
- The internal Admin worker has no Slack or LLM credentials and no public
  ingress. It alone holds the read-only memory principal.

## Runtime flow

1. Slack signs a mention to the existing `/slack/events` endpoint.
2. Roo routes the task by intent. Points and other existing tasks stay Public,
   including when the caller is a committee member.
3. For an internal-memory intent, Public Roo first rejects public `C...`
   channels locally, then calls the backend's content-free eligibility endpoint.
4. The backend permits the route only when the verified caller has active
   identity, membership, capability, pilot actor/context approval, and an
   active `PointsAdmin` record with exact role `committee`.
5. Public Roo sends a short-lived, HMAC-signed, single-use envelope to the
   internal worker. It is bound to the Slack team, user, channel, thread,
   event, request kind, and payload.
6. The Admin worker calls the memory backend with its own `org_memory.read`
   principal. Public Roo posts the answer only if the returned destination
   exactly matches the verified Slack request.

Mixed private-memory and public-action requests produce a clarification asking
the user to send two requests. Denied or unavailable Admin routes fail closed;
they do not search Slack, invoke another skill, or fabricate an answer.

## Provision the two backend principals

Create the Admin worker principal:

```bash
python manage.py create_service_principal \
  --name roo-admin-production \
  --organization-domain mlai.au \
  --scope org_memory.read \
  --surface admin_roo
```

Store its one-time token only in `ADMIN_ROO_ORG_BRAIN_API_KEY`.

Create the Public Roo routing principal separately:

```bash
python manage.py create_service_principal \
  --name roo-public-admin-router \
  --organization-domain mlai.au \
  --scope org_memory.route \
  --surface roo_gateway
```

Store it only in `ROO_ADMIN_ROUTER_API_KEY`. The two tokens must be distinct.
Generate a separate random dispatch secret of at least 32 characters and store
the same value in `ROO_ADMIN_DISPATCH_SECRET` for both runtimes.

## Required production secrets

The `Deploy internal Admin Brain production` workflow uses:

- the existing `DO_HOST`, `DO_USERNAME`, and `DO_SSH_KEY`;
- `ADMIN_ROO_ORG_BRAIN_API_KEY` for the internal worker;
- `ROO_ADMIN_ROUTER_API_KEY` for Public Roo eligibility only;
- `ROO_ADMIN_DISPATCH_SECRET` for private runtime dispatch;
- `ADMIN_ROO_SLACK_TEAM_ID` for the signed pilot probe; and
- `ADMIN_ROO_APPROVAL_MANIFEST` for the current production pilot policy.

Do not configure Admin Slack bot/signing secrets, an Admin OpenAI key, or local
Admin Slack allowlists. The existing Public Roo app remains the only Slack app,
and the backend pilot manifest is the authoritative actor/context allowlist.

Each eligible caller must also have an active `PointsAdmin` record with exact
role `committee`. The `admin`, `partner`, and `portfolio_lead` classes are
deliberately denied unless their stored class is changed to `committee`.

## Deployment behavior

The workflow accepts only a full reviewed commit SHA already on `main`. It:

1. runs the trust-boundary, dispatch, worker, interaction, and policy tests;
2. checks out that exact SHA in `/root/roo-admin` and `/root/roo`;
3. validates the current approval and exercises allowed/denied backend actor
   and private-context combinations through both backend principals without
   retrieving memory;
4. starts `roo.admin_worker:app` on the private `roo-admin-gateway` network;
5. verifies the worker reports internal-only, read-only readiness;
6. atomically adds the route principal, internal URL, dispatch secret, and
   `ROO_UNIFIED_ADMIN_ROUTING_ENABLED=true` to Public Roo; and
7. restarts Public Roo and verifies unified routing is live with shadow mode off.

If any post-start check fails, the workflow restores the previous Public Roo
environment, restarts Public Roo, and stops the new Admin worker. No Admin
endpoint is added to nginx. `/admin/slack/events`, `/admin/slack/actions`, and
`/admin/healthz/ready` remain 404 at the public edge.

## Enforced worker shape

The internal environment is documented in `.env.admin.example` and must keep:

```text
ROO_ENVIRONMENT=production
ROO_SURFACE=admin
ROO_ADMIN_INTERNAL_ONLY=true
ROO_ENABLED_SKILLS=admin-brain
ORG_BRAIN_ENABLED=true
ORG_BRAIN_ACTIONS_ENABLED=false
ROO_CONTEXTUAL_RESPONSES_ENABLED=false
ROO_CONTEXTUAL_SHADOW_MODE=false
```

Startup rejects Slack credentials on the internal worker, private-memory
credentials on Public Roo, missing dispatch controls, extra Admin skills, and
enabled Admin actions. Dispatch receipts are stored durably in the Admin data
volume so a signed envelope cannot be replayed across workers or restarts.

Answers show the `🔒 Internal organisational memory` label, freshness and
warnings, up to five citations, and Helpful/Incorrect/Stale/Missing feedback.
Feedback returns through the same signed worker boundary. Incorrect feedback
enters human review and never overwrites memory directly.

## Verification

Before enabling the route, the workflow runs:

```bash
python scripts/check_admin_pilot_config.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/pilot-approval.json \
  --organization-domain mlai.au

python scripts/check_admin_pilot_access.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/pilot-approval.json \
  --organization-domain mlai.au \
  --slack-team-id T_REPLACE

UNIFIED_ROUTE_PROBE_TOKEN=mlai_sp_REPLACE \
python scripts/check_unified_admin_route.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/pilot-approval.json \
  --organization-domain mlai.au \
  --slack-team-id T_REPLACE
```

After deployment, verify in Slack:

- an eligible committee member can ask an internal-memory question in a DM or
  approved private channel;
- that same member's points request still uses the normal Public Roo flow;
- other PointsAdmin classes and unmapped users receive a generic denial;
- public-channel internal-memory requests are denied before backend retrieval;
- a mixed memory-plus-action request asks to split the tasks; and
- normal Public Roo behavior is unchanged.

## Rollback

Disable unified routing by restoring Public Roo's previous `.env` and
restarting its `roo` service, then stop the worker:

```bash
docker compose -f docker-compose.admin.yml stop roo-admin
```

Revoke the route principal for a routing-boundary incident. Revoke the Admin
worker principal first for any possible memory-credential incident. Neither
action affects backend ingestion or ordinary Public Roo skills.

Controlled Admin actions remain disabled. Enabling them later requires a
separate design and release gate; this read-only deployment must not be widened
by adding `admin-actions` or `org_memory.actions` scopes.
