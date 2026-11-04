# Verified MLAI Chat DMs

Slack attaches app/bot markers to owner-authored MLAI Chat messages. Public Roo
routes original one-to-one DM events carrying those markers through private
delivery verification before normal skill execution. It keeps signed webhook
validation, deployment allowlists and durable retry/deduplication checks.

The read-only backend `GET /api/v1/integrations/bridge/roo/dm-actor` uses Roo's
existing service credential. It verifies the exact completed Chat delivery,
workspace, DM, owner, thread, current grant/consent/device and connection
generations. It returns the acknowledged text digest without private text.
Roo compares that digest with the signed Slack event, binds the canonical owner
and source event to the existing points path, and keeps award/reply retries
idempotent. Normal human Slack DMs use their existing path; unrelated bots,
edited/deleted messages and Admin Roo cannot use private Chat delegation.

Deploy the backend first. Only newly delivered messages have the required
receipt; ignored historical commands are not replayed. No environment changes
or migrations are required. Private DM points recipients use actual Slack
mentions; an unbound display name cannot select an account.

From `roo-standalone`, run offline checks with synthetic credentials:

```sh
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
  FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
  .venv/bin/python -m pytest ../tests/roo/test_chat_dm_routing.py -q
```
