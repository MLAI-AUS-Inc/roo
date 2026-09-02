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

For an approved migration, deployment stops the existing Roo writer before the
one-shot transaction begins. Historical `confirmed` and `blocked` rows cannot
prove whether their old best-effort Slack notification was delivered, so the
migration marks them `reconciliation_required`; it does not retry them blindly
or allow retention cleanup to delete them. Inspect and resolve that quarantine
from the snapshot and Slack history before changing those rows.

The account-link action has a separate `FOUNDER_ACCOUNT_LINK_ENABLED` flag,
also disabled by default. When enabled, deployment requires Roo's dependency
health response to include the exact backend contract
`slack-founder-link-v1`; a generic backend readiness response is insufficient.

## Recovery

Before schema advancement, deployment failure restores the prior commit and
`.env`, then rebuilds the previous Roo release. Once the approved migration has
started, failure is forward-only: Roo remains stopped so the prior writer cannot
reintroduce raw request text or terminal-before-notification states. Repair or
complete the v2 rollout from the volume snapshot, validate the schema with the
new image, and only then restart Roo. Cleared raw Slack text is an intentional
privacy deletion and is never restored.
