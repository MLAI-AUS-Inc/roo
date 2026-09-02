# Office Manager volunteer actions

Public Roo handles the Slack button for the backend-owned Office Manager of
the Day workflow. Roo does not choose the winner or mutate points locally: it
durably records each click, sends a stable `attempt_id` to `mlai-backend`, and
delivers the backend's authoritative result privately.

## Configuration

- `OFFICE_MANAGER_ACTIONS_ENABLED` is the Public Roo action-consumer gate. It
  defaults to `false`.
- `ROO_API_KEY` must be Roo's dedicated backend credential. It must not equal
  the backend's internal or MLAI service keys.
- `MLAI_BACKEND_URL` must identify the backend that owns the claim endpoint.
- Public Roo must use the same Slack application that owns the Office Manager
  button. Do not put this action or its credential on Admin Roo.

The readiness endpoint exposes a non-secret `office_manager` contract with the
gate state, backend base URL, claim path, and `Australia/Melbourne` timezone.
The backend deployment validates this contract before enabling its scheduler.

## Rollout

1. Deploy and approve the backend migrations and claim endpoint with
   `OFFICE_MANAGER_ENABLED=false`. Complete the backend's historical migration
   audit before changing persistent databases.
2. Configure the backend's Public Roo Slack token and coworking channel, and
   configure Roo with its dedicated `ROO_API_KEY` and the intended
   `MLAI_BACKEND_URL`.
3. Deploy this Roo release with `OFFICE_MANAGER_ACTIONS_ENABLED=false` and
   verify `/healthz/ready` reports the expected Office Manager contract.
4. Set the repository variable `OFFICE_MANAGER_ACTIONS_ENABLED=true` and
   redeploy Roo. Smoke-test one signed volunteer click, an exact retry of that
   click, and a genuinely new click.
5. Enable `OFFICE_MANAGER_ENABLED` on the backend only after Roo is ready.
   Verify the backend preflight accepts the real Slack app/channel and the Roo
   readiness contract, then monitor pending action age and terminal failures.

To roll back, disable new backend Office Manager creation and Roo actions, but
leave the backend scheduler running until committed Slack retractions have
drained. Existing non-terminal Roo action records remain durable while the
gate is disabled and resume only when re-enabled on the same Melbourne-local
date; stale records finish with non-actionable private feedback.

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

These tests cover replay identity, response loss, restart recovery, lease
expiry, date rollover, feature disable/re-enable, private result delivery, and
redacted logging without contacting Slack or the backend.
