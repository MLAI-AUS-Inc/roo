# Roo Admin development deployment

The existing Slack app and `docker-compose.yml` remain Public Roo. Admin Roo is a second Slack app, hostname, deployment, bot token, signing secret, receipt database, data volume, and scoped backend service principal.

## Create the development Slack app

1. In the MLAI Slack app console, create an app from `slack-app-manifests/roo-admin-development.yaml`.
2. Replace `admin-roo-dev.example.invalid` with the dedicated development hostname before enabling events or interactivity.
3. Install it only in the MLAI workspace and copy its bot token and signing secret into `.env.admin`.
4. Do not add channel-history, file, search, admin, user-token, or source-ingestion scopes. This app is the conversational Admin surface, not the Slack ingestion connector.
5. Invite it only to a private development channel whose Slack ID begins with
   `G`. Record that channel ID in `ROO_ALLOWED_CHANNEL_IDS`, or record
   individual pilot Slack user IDs in `ROO_ALLOWED_DM_USER_IDS`. Public
   `C...` channels and raw `D...` conversation IDs are rejected at startup.

## Start the isolated deployment

```bash
cp .env.admin.example .env.admin
# Fill Slack credentials, one LLM key, and an explicit Admin channel/DM allowlist.
docker compose -f docker-compose.admin.yml up -d --build
curl http://127.0.0.1:8081/healthz/ready
```

`docker-compose.admin.yml` uses the dedicated `roo-admin` Compose project.
Public Roo keeps its existing project and data volume, so either deployment's
orphan cleanup cannot remove the other surface.

For a Slack-envelope smoke test, leave `ORG_BRAIN_ENABLED=false` and
`ROO_ENABLED_SKILLS` empty. The service will accept only signed, allowlisted
mentions/DMs but cannot retrieve memory.

## Enable the read-only pilot

Provision the service principal in `mlai-backend` after the pilot Slack
workspace, users, memberships, roles, and capabilities have been mapped:

```bash
python manage.py create_service_principal \
  --name roo-admin-pilot \
  --organization-domain mlai.au \
  --scope org_memory.read \
  --surface admin_roo
```

Store the one-time credential only in `.env.admin`, then set:

```text
ROO_ENABLED_SKILLS=admin-brain
ORG_BRAIN_ENABLED=true
ORG_BRAIN_API_KEY=mlai_sp_<credential-id>.<secret>
ORG_BRAIN_BACKEND_TIMEOUT_SECONDS=20
ORG_BRAIN_MAX_CONTEXT_TOKENS=6000
```

Before starting a live read-only container, compare the effective Roo
allowlists with the exact restricted approval manifest:

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

## Manual staging deployment

The `Deploy Admin Roo staging` GitHub Actions workflow is manual-only and uses
the protected `admin-roo-staging` environment. Configure these repository or
environment secrets before running it:

- `ADMIN_ROO_DO_HOST`;
- `ADMIN_ROO_DO_USERNAME`;
- `ADMIN_ROO_DO_SSH_KEY`;
- `ADMIN_ROO_SLACK_TEAM_ID`.

The target host must already contain a separate Roo checkout at
`/root/roo-admin`, a mode-0600
`/root/roo-admin/roo-standalone/.env.admin`, and the restricted approval at
`/root/roo-admin-operations/pilot-approval.json`. Start the workflow with the
full reviewed commit SHA after that commit has been merged into `main`. The
workflow rejects unmerged commits, checks out that exact SHA in detached mode,
runs all Admin trust-boundary tests against it, validates exact approval/config
alignment inside a one-off container, deploys only the isolated `roo-admin`
project, and verifies that readiness exposes only the read-only `admin-brain`
skill. It then runs the aggregate-only signed-request gate against the deployed
backend. It never queries memory, creates an approval, issues a backend
credential, enables the backend query flag, or deploys Public Roo.

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
- Enabling controlled actions additionally requires `admin-actions`, `ORG_BRAIN_ACTIONS_ENABLED=true`, the `org_memory.actions` service scope, and named `approve_actions` reviewers.
- Never reuse Public Roo's bot token, signing secret, hostname, data volume, receipt database, or backend key.
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
