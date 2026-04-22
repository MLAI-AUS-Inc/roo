# 2026-04-22 Roo Coworking Booking Timeouts

## Summary

On 2026-04-22, Roo replied to multiple coworking booking requests with an MLAI backend timeout message. The user-facing failure mode was ambiguous: Roo could not confirm whether the booking had been created, and the original Roo container logs were not a complete source of truth after deployment.

The immediate recovery used Slack channel history plus backend database verification. The permanent Roo-side fix is a durable coworking booking intent queue. Roo now persists each booking intent before calling MLAI backend, leases it while processing, and retries automatically after backend transport failures. The MLAI backend unique active booking constraint remains the hard duplicate guard.

## Impact

Affected Slack requests in `#cowork-and-chill` on 2026-04-22 Melbourne time:

- Jasper Mondares: replayed and booked
- Juan Bernal: replayed and booked
- Christopher/CJ Moss: replayed and booked
- Alex Shiell: replayed and booked
- Cate Trotter: replayed and booked
- Kaey-Lib Tan: already booked, no duplicate created
- Melody Wu: not replayed because MLAI backend had no matching Slack user
- Aaron: not replayed because the original message did not include a Slack identity for Aaron

## Evidence

- Roo posted timeout replies for coworking booking requests.
- Some failed probes did not reach Django request logging, which placed the stall before normal Django request handling.
- MLAI backend workers showed stuck socket/fd behavior during investigation, including `CLOSE_WAIT` growth.
- The backend DB did not have the live `unique_active_booking_per_user_date` partial unique index before the incident fix.
- After backend hardening and migration, production smoke testing showed 100 successful backend readiness requests, zero failures, zero partial responses, and `CLOSE_WAIT` stable at `0 -> 0`.
- Production DB verification after replay showed exactly one active coworking booking per recovered user/date.

## Root Cause

MLAI backend became unavailable to Roo during booking requests because gunicorn workers stalled before requests reliably reached Django. The observed `CLOSE_WAIT` sockets were a symptom of workers not closing sockets promptly under the failure mode; they were not treated as the only root cause.

The user-facing reliability gap was Roo-side: booking intents only existed in Slack messages and transient process logs until MLAI backend accepted the booking. If the backend timed out, Roo had no durable local record to retry from.

## Backend Fixes

- Applied the live DB partial unique index `unique_active_booking_per_user_date`.
- Kept backend idempotency at user/date level as the replay and retry concurrency guard.
- Changed gunicorn to sync workers with explicit worker count, keep-alive, timeout, graceful timeout, and max request recycling.
- Added runtime hardening tests for gunicorn config, healthcheck behavior, migration guard, and deployment smoke checks.

## Roo Fixes

- Added `roo.coworking_booking_intents`.
- Persists coworking booking intents in SQLite under `data/coworking_booking_intents.db`.
- Uses the Docker `roo-data` volume, so queued intents survive container restarts.
- Records one idempotency key per Slack user/date: `coworking:<slack_user_id>:<booking_date>`.
- Persists the intent before calling MLAI backend.
- Leases an intent while processing to avoid duplicate in-process work.
- Marks retryable backend failures as `pending_retry` with exponential backoff.
- Runs a startup retry worker that claims due intents and confirms them through MLAI backend.
- Posts a thread confirmation once a queued retry succeeds.
- Marks non-retryable failures, such as missing backend users, as `blocked` and posts the reason.

## Change Inventory

MLAI backend:

- `docker-compose.yml`: web runtime starts gunicorn only; migrations are a deploy step, not container startup work.
- `core/middleware.py`: request duration and worker pid logging.
- `roo/migrations/0018_ensure_coworking_unique_active_booking.py`: live partial unique index guard.
- `ops/backend-socket-smoke.sh`: readiness/load smoke with socket-state checks.
- `ops/docker-health-watchdog.sh`: bounded health restart behavior.
- `scripts/check-http-wrapper.sh`: CI guard against raw outbound HTTP calls.
- `tests/test_runtime_hardening.py`: migration/config/smoke-script coverage.

Roo:

- `roo/coworking_booking_intents.py`: durable SQLite intent queue and retry worker.
- `roo/skills/executor.py`: persist coworking intent before backend booking call.
- `roo/agent.py`: fast-path coworking booking uses the same durable path.
- `roo/main.py`: starts/stops the retry worker with the FastAPI lifespan.
- `roo/config.py`: queue DB path and retry poll settings.
- `ops/replay_failed_coworking.py`: legacy incident replay tool for pre-queue requests.
- `roo/tests/test_coworking_booking_intents.py`: queue/retry tests.
- `roo/tests/test_points_requests.py`: booking timeout queue tests.

## Deployment Workflow

Normal workflow:

1. Make the change locally in the owning repo.
2. Run focused tests, then the relevant full test suite.
3. Commit the code and push to `main`.
4. Let GitHub Actions deploy from `main`.
5. Verify production health after deploy.

Roo deployment:

- Repo: `MLAI-AUS-Inc/roo`
- Trigger: push to `main`
- GitHub Action: `.github/workflows/deploy.yml`
- Server behavior: SSH to the Roo droplet, `git pull origin main`, then `docker compose up -d --build --remove-orphans` in `/root/roo/roo-standalone`.
- Roo does not need a manual DB migration for this queue; SQLite schema is created automatically in the `roo-data` Docker volume.

MLAI backend deployment:

- Repo: `MLAI-AUS-Inc/mlai-backend`
- Trigger: push to `main`
- GitHub Action: `.github/workflows/deploy.yml`
- Checks: migration check plus targeted Django tests.
- Deploy script: `deploy.sh`
- Server behavior: rsync repo contents, build images, stop web traffic, run `python manage.py migrate --noinput`, verify `migrate --check`, verify `unique_active_booking_per_user_date`, then start web/scheduler/bridge-worker.
- Any required one-off DB action must be explicit in the deploy script or runbook, with verification before replay or traffic-dependent operations.

Emergency workflow:

1. If production is down, a direct server hotfix is allowed.
2. Record exactly which files changed and why.
3. Run production smoke checks.
4. Immediately backfill the same changes into git.
5. Commit and push so the next normal deploy does not overwrite or conflict with the hotfix.
6. Remove any temporary files or deployment artifacts from the server.

## Recovery Runbook

1. Confirm backend health:
   `curl -H 'Connection: close' http://127.0.0.1/healthz/ready`

2. Verify the DB duplicate guard before any manual replay:
   `unique_active_booking_per_user_date`

3. Prefer the durable Roo queue for new incidents:
   inspect `data/coworking_booking_intents.db` for `pending_retry` or `blocked` rows.

4. If reconstructing older requests, use Slack history rather than container logs.

5. Replay only after a dry run and DB uniqueness verification.

6. Verify recovery in the backend DB by Slack user/date.

7. Post per-thread user confirmations and a channel summary.

## Prevention

Roo should no longer need manual Slack-log replay for backend timeout windows after this fix. A booking request that times out is now retained as a durable intent and retried automatically, while the backend unique active booking constraint prevents duplicate bookings for the same user/date.
