# Coworking intents v3 rollout

V3 is the append-only successor to the shared v2 migration. An early v2 body
could leave terminal `confirmed` or `blocked` rows marked `not_required` even
though their Slack delivery outcome was unknowable. A database that recorded
v2 will not rerun a corrected v2 body, so startup now requires schema version
3.

## Migration

Take a volume snapshot and stop every Roo process that can write the shared
SQLite volume. From `roo-standalone`, run:

```bash
python scripts/migrate_coworking_booking_intents_v3.py
```

The migration first completes v2 when necessary, permanently removes legacy
raw Slack request text, adds reconciliation audit fields, and sets
`PRAGMA user_version=3`. Every terminal row whose current state cannot prove
whether notification was required or delivered is moved to
`reconciliation_required`. This deliberately quarantines both unsafe history
and legitimate no-notification history because those histories have identical
stored values.

Production deployment remains gated by
`COWORKING_INTENTS_V3_MIGRATION_APPROVED=false`. Set it to `true` only for a
separately approved rollout after the snapshot is confirmed, then reset it to
`false`. Once migration begins, failed deployment keeps Roo stopped for
forward recovery instead of starting a v2 writer.

## Reconciliation

For each quarantined row, compare the backend booking record and Slack history
under an incident or ticket. Resolve exactly one intent with a non-secret audit
reference:

```bash
python scripts/reconcile_coworking_notification.py \
  --intent-id 123 \
  --outcome delivered \
  --operator-reference INC-1234 \
  --apply
```

Use `delivered` only when Slack delivery is proven, `not-required` only when no
notification was required, and `retry` only when non-delivery is proven and a
new notification should be sent. The command accepts only quarantined terminal
rows, records the outcome, timestamp, and reference transactionally, and
cannot be replayed after the state fence changes.
