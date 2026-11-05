# Accurate Roo award results

Approved: Native execution, deploy and verify; keep the allowance unchanged.

## Spec and constraints

The backend correctly rejected a 30-point award with only 5 allowance points
remaining. Roo must report confirmed successes, definite rejections, and unknown
outcomes accurately. Preserve authorization, amounts, recipients, request-derived
idempotency keys, and replay DM suppression. Never publish raw HTTP exceptions,
response bodies, URLs, or private balances. Do not change allowance policy or
credit the rejected award. No production award calls or Slack sends for testing.

## Task 1: Regression tests and focused reporting fix

Files: `tests/roo/test_slack_award_results.py`,
`roo-standalone/roo/skills/executors/mlai_points.py`,
`.github/workflows/deploy.yml`, `IMPROVEMENT_LOG.md`.

1. Add offline tests for allowance rejection, all-success, mixed results,
   malformed/non-JSON errors, timeout/5xx, replay suppression, and DM failure.
2. Run the new tests before changing production code. Expected: reporting tests
   fail on the misleading success header and raw exception strings.
3. Extract safe rejection details and build summaries from actual outcomes.
   Keep unknown outcomes explicitly unconfirmed and preserve award execution.
4. Run focused tests and related award/client/security tests using the existing
   CI process boundaries. Expected: all pass. Add the new test to the CI gate.
5. Record verification in the improvement log and commit.

## Final review and release

One fresh gpt-6-astra whole-branch review; resolve important findings with tests.
Use the established release path after required checks, verifying migration
gates do not apply migrations. Verify deployed revision, source, health and
unchanged allowance with read-only checks. Keep an available rollback image.

## Review focus

Privacy of backend error details; unknown outcomes after a possible commit;
partial successes; request identity and repeated recipient notifications;
no allowance, identity, or award policy changes.
