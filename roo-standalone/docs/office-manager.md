# Office Manager volunteer actions

Public Roo handles the Slack button for the backend-owned Office Manager of
the Day workflow. Roo does not choose the winner or mutate points locally: it
durably records each click, sends a stable `attempt_id` to `mlai-backend`, and
delivers the backend's authoritative result privately.

Each current announcement encodes a positive integer claim epoch in its button
value, for example `{"date":"2026-08-03","generation":2}`. Dates must use
the exact `YYYY-MM-DD` representation. Buttons created
before the epoch field existed are generation 1. Roo rejects booleans, strings,
floats, zero, negative values, and integers outside SQLite's signed range. It
persists the validated generation with the attempt and sends that exact value
on every retry; a reopened day therefore cannot accept a click from an older
announcement.

## Configuration

- `OFFICE_MANAGER_ACTIONS_ENABLED` gates admission of new Public Roo clicks and
  defaults to `false`. The durable retry worker still runs while disabled so
  previously accepted attempts and staged private results can finish.
- `ROO_API_KEY` must be Roo's dedicated backend credential. It must not equal
  the backend's internal or MLAI service keys.
- `MLAI_BACKEND_URL` must be the backend's root origin with no path, query, or
  fragment, for example `https://api.mlai.au`. Roo appends the versioned Office
  Manager claim path itself; do not configure a value ending in `/api/v1`.
- In production, credentialed Meeting Room and Office Manager mutations are
  restricted to the reviewed `https://api.mlai.au` authority. Roo refuses to
  start rather than sending `ROO_API_KEY` to an arbitrary host.
- Public Roo must use the same Slack application that owns the Office Manager
  button. Do not put this action or its credential on Admin Roo.

The readiness endpoint exposes a non-secret `office_manager` contract with the
gate state, backend base URL, claim path, authenticated backend contract,
Slack `team_id`, `bot_id`, and bot `user_id`,
worker heartbeat, and content-free outbox health. Startup calls the exact
backend preflight with `ROO_API_KEY`; a wrong credential, route, contract, or
timezone fails closed before the action worker starts. A credential rejection
seen after startup keeps the original action pending and makes readiness fail
with `office_manager_backend_auth_failed` until that attempt recovers. A
Slack app credential rejection behaves the same way and reports
`office_manager_slack_auth_failed`; it is not discarded as a member-specific
target failure. A
permanent, redacted Slack-target failure remains visible as an Office Manager
warning, but does not take core Roo readiness offline.

The `office-manager-v1` preflight must advertise both
`claim_generation_supported: true` and `claim_generation_required: true`.
This Roo release always sends the persisted `generation`, and the backend
rejects claims that omit it. Roo requires every successful response and every
recognized terminal error to echo the exact persisted `attempt_id` and integer
`generation`. A missing or mismatched binding remains pending for authoritative
reconciliation.

## Rollout

1. Disable Office Manager actions, then deploy and approve the backend
   migrations and claim endpoint with `OFFICE_MANAGER_ENABLED=false`. Confirm
   preflight reports both generation capabilities as `true`; older Roo
   instances cannot claim through this strict contract during the rollout.
   Complete the backend's historical migration audit before changing persistent
   databases.
2. Configure the backend's Public Roo Slack token and coworking channel, and
   configure Roo with its dedicated `ROO_API_KEY` and the intended
   `MLAI_BACKEND_URL`.
3. Deploy this Roo release with `OFFICE_MANAGER_ACTIONS_ENABLED=false` and
   verify `/healthz/ready` reports the expected Office Manager contract.
4. Set the repository variable `OFFICE_MANAGER_ACTIONS_ENABLED=true` and
   redeploy Roo. Deployment builds the candidate image and runs its exact
   authenticated Office Manager preflight before replacing the running Roo
   container. The preflight may report the backend creation gate disabled
   during this staged interval; retries remain durable.
5. Enable `OFFICE_MANAGER_ENABLED` on the backend only after Roo is ready.
   Verify the backend preflight accepts the real Slack app/channel, matches its
   team and bot identity to Roo readiness, and accepts the Roo
   readiness contract, then smoke-test one signed volunteer click, an exact
   retry of that click, and a genuinely new click. Monitor pending action age
   and terminal failures.

To roll back, disable new backend Office Manager creation and Roo actions, but
leave the backend scheduler running until committed Slack retractions have
drained. Roo continues reconciling existing non-terminal action records with
their original attempt ID while the gate is disabled, including after the
Melbourne-local date changes; historical results name the original date and
explicitly say that no current action is required. Deployment rollback also
restores and health-checks the separate Slack bridge from the previous source
revision before declaring rollback complete.

## Local verification

From `roo-standalone`, use test credentials only:

```bash
SLACK_BOT_TOKEN=test \
SLACK_SIGNING_SECRET=test \
OPENAI_API_KEY=test \
OFFICE_MANAGER_ACTIONS_ENABLED=false \
pytest roo/tests/test_office_manager_actions.py \
  roo/tests/test_mlai_backend_client.py -q
```

These tests cover replay identity, response loss followed by Slack's
`duplicate_message` acknowledgement, restart recovery, lease expiry, date
rollover and generation fencing, feature disable/re-enable, private result
delivery, and redacted logging without contacting Slack or the backend.
