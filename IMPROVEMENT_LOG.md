# Repository improvement log

A short record of repository cleanup, testing, security, and delivery changes.
Each entry states what changed and how it was checked. Findings without fixes
are labelled as such.

## 2026-10-30

- Repaired Study Nash project-name selectors after production routing passed
  the project title as the client filter. Added explicit verified project aliases
  to the private owner mapping without changing project access, and regression
  checks for the exact queue-to-DM route and cross-client denial (106 targeted
  tests passed). Recovered the latest original rejected request with its actor,
  selector and earlier notice receipt preserved; Slack confirmed the summary,
  usage/burndown charts and exceptions export delivered privately.
- Added an explicit, private Linear source policy and project-history boundary
  for Study Nash usage reports, with monthly base-allowance task burndowns.
  Preserved other clients' reviewed sources, private delivery and access checks;
  missing task evidence remains partial or blocks the report.
- Adapted the new reporting tests and documentation to the consolidated
  repository layout. All 551 reporting, chart, balance and routing regressions
  passed with synthetic credentials; added the new source-policy and burndown
  suites to the isolated reporting CI group (768 passed) and validated
  documentation links.
- Activated the approved single-project mapping and reporting grant, preserving
  other client mappings, schedules, queue/data volumes and Public Roo's current
  application image. The updated worker and restarted Public Roo passed health
  checks; a report preview reads Linear without posting a Slack test message.

## 2026-10-28

- Fixed the Roo account-link rollout gate to query dependency health inside
  the running container with an eight-second timeout. It no longer relies on
  host Python or a private health route being exposed by NGINX. Preserved
  capability validation and rollback, checked the running release and flag,
  and added a public dependency-health 404 check.
- Verified the deployment probe with offline shell/HTTP boundary tests,
  including disabled flags, transport and contract failures, and mismatched
  runtime release/flag values: 26 deployment tests and 45 account-link tests
  passed. Full isolated regression checks run in the release workflow.

## 2026-10-22

- Moved the coding-agent guide, Roo Code reconciliation rule, and Roo agent
  setup note into `agents/`. Kept `AGENTS.md` and
  `.roo/rules/00-mlai-reconciliation.md` as symlinks so agent discovery and Roo
  Code rule loading still work. Updated the README link.
- Reviewed `.github/workflows/deploy-admin-production.yml` (findings only; no
  workflow change): add SSH host fingerprint verification, pin the SSH action,
  constrain the dispatch secret to a single-line format, and make Public Roo
  environment updates atomic.
- Classified the routing, bridge, and Slack-to-Linear plans and routing audit
  as historical design records rather than coding-agent instructions. The
  plans now live in `agents/plans/`; the audit lives in `docs/`.
- Moved the Luma–Stripe–Xero reconciliation plan to `agents/plans/`, updated
  its references, and instructed coding agents to put new plans there.
- Removed the unused root `pyproject.toml` and `uv.lock`. They were untracked,
  had no application dependencies, and specified Python 3.12 while Roo runs on
  Python 3.11.
- Moved the historical routing probe to `scripts/`, marked its limitations,
  and updated references. Its cases remain in the current routing eval set.
- Scoped the Roo Code reconciliation rule to the reconciliation workspace and
  updated its setup instructions and file paths.
- Added an opt-in, localhost-only NGINX proxy for testing the authenticated
  Public Roo mention endpoint with Postman.
- Fixed the Postman proxy mount, disabled background automations for local
  testing, generated a local endpoint key, and applied the approved Roo SQLite
  v3 migration. Roo readiness and proxy authentication checks pass. Started
  local backend web/PostgreSQL and applied its 364 migrations after approval;
  backend readiness and the no-pending-migrations check pass. Verified an
  authenticated Postman greeting returns HTTP 200 using the development
  OpenAI key, with no Slack reply.
- Added an importable local Postman collection with readiness, agent, and
  authentication checks, parameterized URLs, and an empty API-key variable.
- Documented Postman startup, import, variables, troubleshooting, and shutdown
  in `postman/README.md`.
- Added `roo-standalone/scripts/run_evals.sh` with offline routing gates and explicit live
  routing/addressing modes, plus an eval guide describing datasets and limits.
  Verified shell syntax and all three offline routing gates in Docker.
- Repaired the root `AGENTS.md` symlink after the instructions folder moved to
  `.agents/`; updated the README entry point.
- Consolidated application/bridge tests and routing/addressing evals under
  `tests/`, with a shared pytest configuration and `tests/run_evals.sh`.
  Moved Compose and NGINX into `infra/`; updated workflows, operations scripts,
  documentation, and relative mounts. All 90 test modules pass individually;
  eight Compose configurations render and production NGINX syntax passes.
  Deployment caveat: an existing server checkout using `roo-standalone/`
  needs a transition for local env/data paths and rollback before deployment.
- Moved quest documentation to `docs/QUESTS.md` and marked its handler as
  inactive. Verified linked source paths; no runtime changes.
- Moved the application Dockerfile into `infra/`, preserving the `roo-standalone/` build
  context; updated all six Compose builds, the security test reference, and docs.
  Consolidated application/bridge ignore rules into root `.gitignore`, including
  local env files, caches, and editor files while retaining env templates.
  Validated all six rendered build paths, ignore behavior, and diff whitespace.
- Rebuilt the relocated Dockerfile and restarted local Roo/Postman successfully.
  Offline evals: 3 passed. Public CI test groups: 2,013 passed; Admin: 213;
  payment reminders: 85. The Docker-only queue test skipped inside the image
  passed separately against the host Compose CLI. Python compilation passes.
  Roo readiness returns 200, missing authentication returns 401, unknown proxy
  routes return 404, and NGINX syntax passes; backend/database remain healthy.
  No migrations, live model evals, Slack actions, or production deployment run.
- Fixed the reported coworking clarification for “Thursday, 22 October from
  1pm to 5pm”: the parser incorrectly counted weekday and calendar date as
  separate dates. Matching weekday/date pairs now resolve together; conflicting
  weekdays, invalid dates, and multiple-date requests still require clarification.
  All 180 date/booking regression tests pass offline, including the exact report
  with absent, ISO, and natural-language router dates. No real booking made.

- Restored the application directory name to `roo-standalone` and updated CI,
  Compose, test imports, scripts, and documentation. Kept tests and infrastructure
  in their dedicated folders. Deployment now discovers the previous Compose
  layout when capturing rollback state and restarting the bridge.
  Validation: 41 targeted deployment/skill tests passed; all Compose profiles
  rendered with empty test environments; workflow YAML and SSH shell syntax
  checks passed. No deployment or live service calls performed.

- Updated article quote consent PR #302 for current `main`: preserved the
  executor facade and ported the gate into `ContentFactoryExecutor`; moved its
  documentation/tests into the current repository layout and retained the
  isolated CI consent group. Article/client/Chat regressions: 269 passed;
  executor regressions: 34 passed; surface boundary checks: 30 passed; Slack,
  internal-mention and backend-identity regressions: 58 passed. Skill
  configuration lint and Python compilation pass. Verified the quote/replay
  contract and Chat bridge action in backend deployment `faaeac1`, plus the
  desktop/mobile action allowlists on Chat main. Sam authorised admin merges
  of #311 and #302 on 30 October; no database migration is part of this change.
