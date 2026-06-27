# Slack Cross-Org Channel Bridge

Bridges one channel in **MLAI** with one channel in **Stone & Chalk**, relayed by
this service on the droplet. Members of each org read and write entirely in their
own workspace. Full design + rationale: [`../../SLACK_BRIDGE_PLAN.md`](../../SLACK_BRIDGE_PLAN.md).

## Design

A **dedicated "Bridge" Slack app** is installed in both workspaces (Roo is not
involved). This service holds both bot tokens and polls both channels — no
inbound webhooks, so it runs as its own process and nothing is wired into Roo.

It is **store-and-forward**: the pollers *capture* each new message into a
durable inbound queue (SQLite), and a separate delivery worker drains the queue,
posting into the other workspace with retries + backoff. A failed post is
retried, not lost.

| | MLAI | Stone & Chalk |
|---|---|---|
| Auth | Bridge app's MLAI install (bot token) | Bridge app's S&C install (bot token) |
| Ingest | poll `conversations.history` | poll `conversations.history` |
| Egress | bot post w/ `chat:write.customize` | bot post w/ `chat:write.customize` |

Both directions are symmetric: each author appears in the other workspace with
their real name + avatar (small APP badge).

Flow: `poll → capture to inbound queue → delivery worker → post`. Capture applies
the cheap filters (loop guard, subtype, dedup) so only real content is queued;
delivery does attribution, translation, posting, and retry/backoff.

## Setup

### 1. Create the Bridge app + install in both orgs
Create a new app at api.slack.com (NOT Roo). Add these **bot token scopes**:
`channels:history`, `channels:read`, `chat:write`, `chat:write.customize`,
`users:read`, `files:read`, `files:write` (add `groups:history`, `groups:read`
if either channel is private).

Then install it into **both** workspaces — activate *Manage Distribution →
Public Distribution* on the Bridge app and use its install URL to add it to MLAI
and to Stone & Chalk. Each install yields a separate bot token (`xoxb-…`):
`MLAI_BOT_TOKEN` and `SNC_BOT_TOKEN`. Invite the Bridge bot to the bridged
channel in each workspace.

No Request URL / Event Subscriptions needed — the bridge polls.

### 2. Run
```bash
cd roo-standalone
cp bridge/.env.example .env   # then fill it in (or merge into your existing .env)
uvicorn bridge.main:app --host 0.0.0.0 --port 8100
```
Health: `GET /healthz`. No public port is needed — the bridge only makes
outbound calls (both sides poll), so there are no inbound webhooks to expose.

### 3. Deploy on the droplet (Docker)
Roo runs via Docker Compose, so the bridge ships the same way — its own
container in its own Compose project, independent of Roo's deploy:
```bash
cd roo-standalone
docker compose -f docker-compose.bridge.yml up -d --build
```
To have systemd supervise it (mirroring `ops/docker-health-watchdog.service.example`):
```bash
sudo cp ops/slack-bridge.service.example /etc/systemd/system/slack-bridge.service
# edit WorkingDirectory if your deploy dir differs from /root/roo/roo-standalone
sudo systemctl daemon-reload && sudo systemctl enable --now slack-bridge
```
Because it's a separate Compose project (`name: slack-bridge`), Roo's regular
`docker compose up -d --remove-orphans` deploy never starts or removes it.

## v1 scope and limitations
- **Both sides poll**, not real-time. Expect a few seconds of latency
  (`MLAI_POLL_SECONDS` / `SNC_POLL_SECONDS`). Upgrade path: subscribe each install
  to the Events API and feed events into the same relay handlers.
- **Edits/deletes are not mirrored** in poll mode (polling sees current state,
  not change/delete events).
- **Reactions** are intentionally skipped in v1.
- **Files** are re-uploaded best-effort across workspaces.
- Pick a channel where Roo doesn't post automated content, or set
  `BRIDGE_RELAY_BOT_MESSAGES=false`, so Roo's posts don't cross over.

## Consent
Mirroring members' messages into another org is a consent matter — get S&C's
community-manager sign-off and note the bridge in both channel topics.

## Loop prevention
Every message the bridge posts is written to `posted_registry`; any polled
message found there is dropped. A circuit breaker pauses posting if it ever
exceeds `BRIDGE_MAX_POSTS_PER_MIN` and DMs `BRIDGE_ALERT_DM_USER_ID`.

## Tests
```bash
cd roo-standalone && python -m pytest bridge/tests -q
```
