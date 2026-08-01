# Slack Cross-Org Channel Bridge

Mirrors channels between the **MLAI** workspace (the hub) and one or more partner
Slack workspaces, relayed by this service on the droplet. Members of each org
read and write entirely in their own workspace. Full design + rationale:
[`../../SLACK_BRIDGE_PLAN.md`](../../SLACK_BRIDGE_PLAN.md).

## Design

A dedicated "Bridge" Slack app is installed in MLAI and in each partner
workspace (Roo is not involved). MLAI is the hub: this service holds the MLAI
token plus, for each mirrored channel, the partner workspace's token. Every side
is **polled** — no inbound webhooks, so it runs as its own process and nothing is
wired into Roo.

It is **store-and-forward**: the pollers *capture* each new message into a
durable inbound queue (SQLite), and a delivery worker drains the queue, posting
into the other workspace with `chat:write.customize` (real name + avatar) and
retries/backoff. A failed post is retried, not lost.

```
poll MLAI #x ─┐                      ┌─> post into partner #x
              ├─> inbound queue ─> delivery worker
poll partner #x ┘                    └─> post into MLAI #x
```

Each mirrored channel is a **pair** with two directions. Loop prevention is via a
posted-message registry, keyed by `(team, channel, ts)` so it holds across all
pairs; a circuit breaker pauses posting if it ever floods.

## Setup

### 1. Bridge app + scopes
Create one app (or one per workspace — see SLACK_BRIDGE_PLAN.md) with bot scopes
`channels:history`, `channels:read`, `chat:write`, `chat:write.customize`,
`users:read`, `users:read.email`, `files:read`, `files:write` (add
`groups:history`, `groups:read` for private channels). Install or reinstall it
in MLAI and each partner workspace after adding the email scope, and
**invite the bot to every bridged channel** (`/invite @MLAI Bridge`).

### 2. Configure `.env`
```
MLAI_BOT_TOKEN=xoxb-…               # MLAI install
BRIDGE_PAIRS=[{"label":"hex","mlai_channel":"exp-victor-ai","remote_token":"xoxb-…","remote_channel":"exp-victor-ai","mention_alias":"hex","user_map":{}}]
BRIDGE_MENTION_MODE=observe         # plain | observe | native
```
`mlai_channel` / `remote_channel` accept a channel **name** (resolved at startup)
or an ID. Add a partner workspace by appending another object to the JSON list.

### 3. Cross-workspace mentions

Slack user IDs are workspace-specific, so the bridge maintains an in-memory
directory for every connected workspace. It combines the workspace user list
with members of every configured bridged channel; this includes Slack Connect
participants who may be absent from `users.list`. It maps a native source
mention to a destination user using:

1. `user_map` (MLAI user ID -> partner user ID), when configured;
2. an exact case-insensitive email match;
3. inert `@Name (MLAI)` / `@Name (HEX)` text when no safe match exists.

To notify someone who exists **only** in the other workspace, write a qualified
plain-text handle:

- In MLAI, `hex:alice` becomes Alice's native HEX mention.
- In HEX, `mlai:sam` becomes Sam's native MLAI mention.
- In HEX, `mlai:roo` becomes Roo's native MLAI bot mention when Roo's exact
  Slack handle is `roo`.
- Spaces in a unique display name become hyphens, so `Alice Smith` can be
  addressed as `hex:alice-smith`. An exact destination member ID also works.
- A unique first name also works, so `mlai:shan` resolves a sole `Shan Yang`.
  Common honorifics are ignored (`Dr Sam Donegan` contributes `sam`). If two
  destination identities share that first name, neither is selected.
- Small typos are tolerated when exactly one person is the clear nearby match
  (one edit for short handles, up to two for longer handles). Ambiguous or
  low-confidence guesses stay plain text and do not notify anyone.

The qualified form intentionally does not start with `@`: Slack's composer
otherwise tries to replace the workspace alias with a local user before the
bridge receives the message. The legacy `@hex:alice` / `@mlai:sam` syntax is
still accepted when it reaches the bridge as plain text.

Ambiguous, unknown, and deleted identities are never guessed. Active bots can
only be addressed by an exact qualified handle or exact destination user ID;
they are never mapped by email or fuzzy spelling. Mentions inside code or links
stay literal, and `@here`, `@channel`, `@everyone`, and user groups never fan
out across organizations.

Roll out with `BRIDGE_MENTION_MODE=observe`: candidates are counted and logged
by workspace/user ID, but messages remain inert. Once verified, switch to
`native`. `plain` retains the legacy no-notification behavior. Directory health
and aggregate mention counters are included in `GET /healthz`; profile emails
are neither persisted nor logged.

If the same person uses different emails, add the explicit mapping inside that
pair:

```json
"user_map":{"U_MLAI_ID":"U_HEX_ID"}
```

### 4. Run / deploy
```bash
cd roo-standalone
docker compose -f docker-compose.bridge.yml up -d --build
```
…or install `ops/slack-bridge.service.example` as a systemd unit. Health:
`GET /healthz`. A pair whose channel doesn't exist yet (or the bot isn't invited
to) is skipped with a warning — create/invite, then restart.

## v1 scope and limitations
- **Both sides poll** (a few seconds of latency). Mirrors messages from when it
  starts **forward** — it does not backfill existing history (a separate one-off).
- **Threaded replies are mirrored** (and re-threaded under the right parent),
  via a per-poll sweep of recent threads — but only for threads whose parent is
  within `THREAD_SWEEP_SECONDS` (default 3 days).
- **Edits/deletes are not mirrored** in poll mode; **reactions** are skipped;
  **files** are re-uploaded best-effort.
- Pick channels where Roo doesn't post automated content, or set
  `BRIDGE_RELAY_BOT_MESSAGES=false`, so Roo's posts don't cross over.

## Consent
Mirroring members' messages into another org is a consent matter — get each
partner's community-manager sign-off and note the bridge in both channel topics.

## Tests
```bash
cd roo-standalone && python -m pytest bridge/tests -q
```
