# Points Admin management

Promotion, revocation and weekly allowance changes require one explicitly
configured Slack identity. This authority is separate from ordinary Points
Admin roles, bootstrap Points Admins, and Django superuser permissions.

## Environment configuration

Set the same values in Roo and mlai-backend for each environment:

```dotenv
POINTS_SUPER_ADMIN_SLACK_ID=
POINTS_SUPER_ADMIN_SLACK_TEAM_ID=
```

The user value must be a single Slack member ID starting with `U` or `W`;
the workspace value must be a single team ID starting with `T`. Remaining
characters must be uppercase letters or digits. Mentions, lists, whitespace,
missing values, and malformed values grant no authority. There is no runtime
fallback to a production user or to Django roles.

- Production: explicitly set `POINTS_SUPER_ADMIN_SLACK_ID=U05QPB483K9` to
  preserve the previously authorized user. Confirm and configure its production
  workspace ID before rollout.
- Staging: use the intended MLAI Dev member ID and MLAI Dev workspace ID.
  Leave management disabled until those real identifiers are available.
- Automated tests use synthetic Slack identities and never contact Slack.

These identifiers are configuration, not service credentials. Keep the existing
service keys private and distinct. Changing settings requires restarting the
corresponding service; neither service fetches these values from a settings API.

## Identity and API contract

Roo authorizes from `BackendActorContext`, populated by verified Slack ingress.
The context's actor must equal the requester, and its workspace must equal the
configured team. Model parameters and message text cannot supply that identity.
Missing context denies the action before looking up a target or calling backend
mutations.

Roo sends `requester_slack_id` and `slack_team_id` from that context for:

- `POST /api/v1/points/admins/` (also `target_slack_id`)
- `DELETE /api/v1/points/admins/{target}/`
- `PATCH` or `PUT /api/v1/points/admins/{target}/` (also `weekly_allowance`)

The backend independently matches the configured user/team pair. Mutations
require the isolated `ROO_API_KEY`; generic internal/website keys and Django
superuser sessions cannot bypass this check. The Roo key must differ from
`INTERNAL_API_KEY` and `MLAI_API_KEY`; `INTERNAL_API_KEY` must also be configured,
following the existing isolated-Roo permission contract. Roo uses its Roo key
rather than its legacy admin-header internal-key preference for these calls.
Read permissions remain unchanged.

The backend trusts identity asserted by the authenticated Roo service. It does
not independently query Slack membership. The workspace originates from Roo's
verified Slack envelope, not an arbitrary caller's untrusted workspace header.

## Rollout

1. Set identically named GitHub repository variables in both production
   repositories, preserving the production member ID above and using its
   confirmed workspace. Configure staging separately in its deployment
   environment. Do not copy production Slack credentials into staging.
2. Deploy the backend, then Roo. The new backend denies legacy management
   requests that omit workspace identity, so expect a brief management-only
   interruption until Roo is upgraded.
3. Restart both runtimes with matching settings. Wrong/missing configuration
   denies management; the production deployment scripts reject missing or
   malformed IDs before updating the service.
4. Verify staging with its intended member and an unauthorized member once the
   real identifiers are configured. Deployment and live Slack mutations are
   separate operational actions; the local tests do not perform them.

No database schema or Django superuser permission changes are required.

## Local verification

From `roo-standalone`, with the repository's synthetic test environment:

```sh
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au .venv/bin/python -m pytest \
  ../tests/roo/test_points_admin_authorization.py \
  ../tests/roo/test_mlai_backend_client.py -q
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au .venv/bin/python -m pytest \
  ../tests/roo/test_points_requests.py \
  -k 'promote_points_admin or revoke_points_admin or set_points_admin_allowance' -q
```
