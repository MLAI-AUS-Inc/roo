# Coworking intents v2 rollout

Roo no longer changes the coworking intent SQLite schema during application
startup. Startup performs read-only validation and remains unready when the v2
schema is absent.

## Local or staging migration

After taking a volume snapshot, run the one-shot migration from
`roo-standalone`:

```bash
python scripts/migrate_coworking_booking_intents_v2.py
```

Use `--database PATH` only for an explicitly selected database. The migration
is transactional and idempotent. It adds the notification-delivery columns and
indexes, then permanently clears historical `request_text` values because raw
Slack messages are not needed for replay.

## Production gate

The deploy workflow defaults `COWORKING_INTENTS_V2_MIGRATION_APPROVED` to
`false`. In that state it only validates the mounted database and rolls back if
v2 is absent. Set the repository variable to `true` only for the separately
approved migration deployment, after confirming a current volume snapshot.
Reset it to `false` after the successful rollout.

The account-link action has a separate `FOUNDER_ACCOUNT_LINK_ENABLED` flag,
also disabled by default. When enabled, deployment requires Roo's dependency
health response to include the exact backend contract
`slack-founder-link-v1`; a generic backend readiness response is insufficient.

## Recovery

On deployment failure, the workflow restores the prior commit and `.env`, then
rebuilds the previous Roo release. The v2 changes are additive, so the previous
release can run against the upgraded database. Cleared raw Slack text is an
intentional privacy deletion and is not restored; canonical booking fields
remain available for retries.
