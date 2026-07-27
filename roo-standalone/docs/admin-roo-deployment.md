# Roo Admin production deployment

The existing Slack app and `docker-compose.yml` remain Public Roo. Admin Roo is
a second production Slack app, Compose project, bot token, signing secret,
receipt database, data volume, and scoped backend service principal on the same
production host. There is no separate staging environment.

## Create the production Slack app

1. In the MLAI Slack app console, create an app from
   `slack-app-manifests/roo-admin-production.yaml`.
2. Install it only in the MLAI workspace and store its bot token and signing
   secret in the repository secrets used by the production workflow.
3. Do not add channel-history, file, search, admin, user-token, or source-ingestion scopes. This app is the conversational Admin surface, not the Slack ingestion connector.
4. Invite it only to an approved private channel whose Slack ID begins with
   `G`. Record that channel ID in `ROO_ALLOWED_CHANNEL_IDS`, or record
   individual pilot Slack user IDs in `ROO_ALLOWED_DM_USER_IDS`. Public
   `C...` channels and raw `D...` conversation IDs are rejected at startup.

## Production shape

```bash
docker network create roo-admin-gateway
cp .env.admin.example .env.admin
# Fill the dedicated Slack credentials, scoped backend principal, one LLM key,
# and the exact approved private-channel/DM allowlists.
docker compose -f docker-compose.admin.yml up -d --build
curl http://127.0.0.1/admin/healthz/ready
```

`docker-compose.admin.yml` uses the dedicated `roo-admin` Compose project.
Public Roo keeps its existing project and data volume, so either deployment's
orphan cleanup cannot remove the other surface. The Admin container publishes
no host port. The existing production nginx joins `roo-admin-gateway` and
exposes only `/admin/slack/events`, `/admin/slack/actions`, and
`/admin/healthz/ready`; all other Admin paths return 404.

The production workflow writes `.env.admin` atomically and enforces:

```text
ROO_ENVIRONMENT=production
ROO_SURFACE=admin
ROO_ENABLED_SKILLS=admin-brain
ORG_BRAIN_ENABLED=true
ORG_BRAIN_ACTIONS_ENABLED=false
ROO_CONTEXTUAL_RESPONSES_ENABLED=false
ROO_CONTEXTUAL_SHADOW_MODE=false
```

Any mismatch between the effective allowlists and the exact current approval
blocks deployment before the service is restarted.

## Provision read-only access

Provision the service principal in `mlai-backend` after the pilot Slack
workspace, users, memberships, roles, and capabilities have been mapped:

```bash
python manage.py create_service_principal \
  --name roo-admin-pilot \
  --organization-domain mlai.au \
  --scope org_memory.read \
  --surface admin_roo
```

Store the one-time credential only in
`ADMIN_ROO_ORG_BRAIN_API_KEY`, then set the exact approved channel and user
IDs in the corresponding Admin Roo repository secrets. Never copy this
credential into Public Roo.

```text
ROO_ENABLED_SKILLS=admin-brain
ORG_BRAIN_ENABLED=true
ORG_BRAIN_API_KEY=mlai_sp_<credential-id>.<secret>
ORG_BRAIN_BACKEND_TIMEOUT_SECONDS=20
ORG_BRAIN_MAX_CONTEXT_TOKENS=6000
```

The production workflow compares the effective Roo allowlists with the exact
restricted approval manifest before starting the live container:

```bash
python scripts/check_admin_pilot_config.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/pilot-approval.json \
  --organization-domain mlai.au
```

The report contains only the approval hash, counts, and stable blocker codes.
It rejects expired/draft approvals, public channels, allowlist mismatches,
extra skills, and enabled controlled actions without printing Slack IDs or
credentials.

Restart Admin Roo and verify `/healthz/ready` reports `surface=admin`, only
`admin-brain` in `enabled_skills`, and `org_brain_enabled=true`. Then run the
credential-bound signed-request gate:

```bash
python scripts/check_admin_pilot_access.py \
  --env-file .env.admin \
  --approval-manifest /secure/operations/pilot-approval.json \
  --organization-domain mlai.au \
  --slack-team-id T_REPLACE
```

This sends no query and retrieves no memory. It signs fresh requests with the
real Admin Roo service-principal credential and proves that every approved
actor/private-channel pair and actor-bound DM reaches the backend's active
pilot probe. Representative unknown actors and unapproved private/public
contexts must return 401/403. The Public Roo client must remain unable to build
private-memory headers. Output contains only the approval hash, aggregate
expected/pass counts, and stable blocker codes. The backend still records its
normal credential-use timestamp, assertion replay receipt, and security audit
event for each probe.

After that automated gate, test a real Slack mention with each named pilot user
in each allowlisted DM/private channel. Confirm an unmapped user, unlisted
channel, Public Roo, and `/api/mention` cannot retrieve memory.

The pilot Admin deployment has:

- `ROO_SURFACE=admin`;
- only `admin-brain` in its explicit skill allowlist;
- a scoped `org_memory.read` credential unavailable to Public Roo;
- no Public Roo background workflows or commands; Admin Roo interactions are limited to answer-feedback controls;
- signed Slack events limited to allowlisted private contexts.

Answers are rendered from the backend's permission-filtered response with
freshness, warnings, up to five citations, and Helpful/Incorrect/Stale/Missing
context feedback. Incorrect opens a correction form and enters human review;
it does not overwrite memory. If the backend fails, Admin Roo does not search
Slack, invoke another skill, or fabricate an answer.

The backend service-identity provisioning and rotation procedure lives in the
mlai-backend `docs/org-memory-service-identity.md` runbook. Its one-time
`mlai_sp_...` credential is valid only as `ORG_BRAIN_API_KEY`; never place it in
the Public Roo environment or reuse a legacy shared API key.

## Automatic production deployment

`Deploy Admin Roo production` runs for every reviewed commit pushed to `main`.
It can also redeploy a full commit SHA already contained in `main`. Configure
these repository secrets before merging:

- `DO_HOST`, `DO_USERNAME`, and `DO_SSH_KEY` (the existing Public Roo host);
- `ADMIN_ROO_SLACK_BOT_TOKEN`;
- `ADMIN_ROO_SLACK_SIGNING_SECRET`;
- `ADMIN_ROO_OPENAI_API_KEY`;
- `ADMIN_ROO_ORG_BRAIN_API_KEY`;
- `ADMIN_ROO_ALLOWED_CHANNEL_IDS` and/or `ADMIN_ROO_ALLOWED_DM_USER_IDS`;
- `ADMIN_ROO_SLACK_TEAM_ID`.
- `ADMIN_ROO_APPROVAL_MANIFEST`.

The workflow rejects unmerged commits, checks out the exact SHA in detached
mode, runs all Admin trust-boundary tests, validates exact approval/config
alignment, and atomically installs mode-0600 runtime files. It then deploys the
isolated `roo-admin` project, refreshes only the shared production nginx edge,
and verifies Public Roo containment plus enforced Admin readiness. Before
publishing Admin ingress it runs the aggregate-only signed-request gate against
the active backend. It never queries memory, creates an approval, issues a
backend credential, or enables controlled actions.

## Enable controlled actions after the read-only pilot

Keep actions disabled until the read-only release gates have passed. Rotate or
replace the Admin Roo principal with one carrying both scopes:

```bash
python manage.py create_service_principal \
  --name roo-admin-actions-pilot \
  --organization-domain mlai.au \
  --scope org_memory.read \
  --scope org_memory.actions \
  --surface admin_roo
```

Grant `approve_actions` only to named reviewers, then set:

```text
ROO_ENABLED_SKILLS=admin-brain,admin-actions
ORG_BRAIN_ENABLED=true
ORG_BRAIN_ACTIONS_ENABLED=true
```

The backend action gateway and Linear execution kill switches remain separate
and must also be deliberately enabled. Start with Linear execution disabled:
local Gmail, Slack, and Notion drafts can be exercised without sending or
posting anything. Then test two-person Linear approval in a private staging
channel. The proposer must not be able to approve their own action.

Action cards contain an opaque proposal UUID, risk, state, and a bounded
preview. Approve re-reads the authoritative proposal, refreshes live provider
preconditions, records the clicking reviewer, and executes only if the backend
still permits it. Reject opens a reason form. Replayed Slack requests and
repeated clicks are idempotent; ambiguous provider failures are never retried
automatically.

The initial UI does not offer email sending, direct Slack/Notion posting,
payments, finance changes, contracts, roles, permissions, governance changes,
or automatic reversal. Backend operators retain the controlled reversal API
and must reconcile provider state before using it.

## Production invariants

- Public Roo must fail startup if an organisational-brain key or private skill is present.
- Admin Roo must fail startup without a Slack context allowlist.
- Enabling Admin Brain requires `admin-brain` in `ROO_ENABLED_SKILLS` and a separate scoped `ORG_BRAIN_API_KEY`.
- Admin contextual responses and contextual shadow mode must remain disabled.
- Enabling controlled actions additionally requires `admin-actions`, `ORG_BRAIN_ACTIONS_ENABLED=true`, the `org_memory.actions` service scope, and named `approve_actions` reviewers.
- Never reuse Public Roo's bot token, signing secret, data volume, receipt database, or backend key.
- Keep `/api/mention` disabled on Admin Roo. On Public Roo it is disabled unless `INTERNAL_MENTION_API_KEY` is configured, then requires an exact bearer token.

## Rollback

First disable retrieval without stopping the Slack app:

```text
ORG_BRAIN_ENABLED=false
ORG_BRAIN_ACTIONS_ENABLED=false
ROO_ENABLED_SKILLS=
ORG_BRAIN_API_KEY=
```

Restart the Admin service. Public Roo and backend ingestion continue normally.
To stop Admin Roo entirely:

Stop or revoke the Admin app/deployment independently:

```bash
docker compose -f docker-compose.admin.yml down
```

This does not change Public Roo. If a credential may have leaked, revoke it in Slack and the backend before restarting.
