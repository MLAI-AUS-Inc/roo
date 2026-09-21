# Coworking command implementation evidence

Historical evidence for the original slash-command version. The subsequently
approved mention entry point is documented in
[the current guide](../../../roo-standalone/docs/coworking-today-command.md).

Implemented on `codex/coworking-booking-command` in both repositories, starting
from current `origin/main` snapshots. Code commits:

- Roo: `58d2e03`, `14e6c2c` (base `9e9df0b`).
- Backend: `d814c99` (base `f4547f5`).

## Verification

- Roo full suite: **2,070 passed**, one upstream Starlette/AnyIO deprecation warning.
- Focused Roo command, signed Slack, surface, coworking admin and backend client
  tests: **195 passed**.
- Backend snapshot, existing coworking service, booking API, report API and
  permission tests: **59 passed**.
- Git whitespace checks: clean in both repositories.
- No new migrations. The user explicitly approved the existing migration set
  for the disposable SQLite test database. No production data or credentials
  were used.

The signed-route tests verify public-channel privacy, allowed/denied admin DMs,
replays, tampered signatures, denial without names, invalid arguments without
GitHub/AI routing, dedicated-key configuration, and a slow backend returning
before Slack's three-second deadline.

## Decisions made during execution

1. Used fresh isolated clones after the original checkouts returned filesystem
   errors. Risk: a different intended base would require rebasing.
2. Added the route in `roo/urls.py`, its current owner, instead of the global URL
   module. Risk: wrong routing would break the URL; resolver/API tests passed.
3. Implemented the independent command module while awaiting approval for
   backend test migrations. Risk: a contract mismatch would need correction;
   both sides now implement the same tested date/count/people contract.
4. Asserted GET routing rather than exact action-map equality because DRF adds
   HEAD at runtime. Risk: tests do not separately constrain HEAD; this endpoint
   is read-only.
5. Inferred direct-message context from the signed D-prefixed channel ID for
   this command only, so the existing admin DM allowlist works. Risk: incorrect
   classification; allowed and denied DM tests passed.
6. Deferred manifest registration until rollout supplies a verified HTTPS
   endpoint. The existing tracked manifest uses a legacy HTTP IP. Consequence:
   Slack registration and HTTPS verification remain rollout work.

## Release status

Code is committed locally; no push, merge, deployment, or Slack registration
has occurred. Staging smoke checks remain outstanding. See
[the rollout guide](../../../roo-standalone/docs/coworking-today-command.md).

## Independent review

A fresh read-only review found **no actionable Critical, Important or Minor
issues** and assessed the change as ready to merge. Broader regressions then
completed successfully with the counts above. No minor findings were deferred.

Reviewer exclusions were resolved as follows:

- Preserve existing bootstrap-admin policy; bootstrap access is removed through
  configuration, not by deactivating a PointsAdmin row.
- Keep live Slack registration, HTTPS selection and deployment latency checks
  as explicit release gates. The command is not live until those are completed.
- Preserve existing authorization and replay mechanisms rather than redesign
  them. This change does not claim to repair unrelated pre-existing weaknesses.

## Approved mention follow-up

The user subsequently approved replacing the slash-command entry point with
`@Roo coworking-today [YYYY-MM-DD]`. The backend contract is unchanged.

- Real leading bot mention and exact command token are required.
- Matched commands bypass AI/contextual routing; malformed arguments get usage.
- Channel and existing-thread results remain private; channel message copies
  and completed event retries do not trigger another reply.
- Failed private delivery releases the existing receipt for retry, never for
  public fallback. A stale slash registration only shows mention guidance.
- Focused tests: **119 passed**. Full Roo suite: **2,085 passed**, with the same
  upstream Starlette/AnyIO deprecation warning.
- Independent follow-up review: **no actionable findings**; ready to merge.

Live scopes/subscriptions/delivery remain staging checks. The review preserves
existing event-lease crash/uncertain-delivery behaviour and bootstrap-admin
policy; it does not claim exactly-once Slack delivery or redesign those shared
mechanisms. No new backend runtime changes, migrations, external messages,
production configuration, or deployments were made in this follow-up.
