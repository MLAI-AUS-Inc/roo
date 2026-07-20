# Slack Cross-Org Channel Bridge — Implementation Plan

> **Status (updated 2026-07-20):** Running as a **dedicated "Bridge" Slack app installed in both workspaces** — the symmetric two-bot design (a.k.a. Path A). Both channels are polled and `chat:write.customize` preserves author name/avatar. Destination-aware native mentions are supported through explicit ID overrides, exact email matching, and Slack-autocomplete-safe qualified handles such as `hex:alice` (with legacy `@hex:alice` support). Identity directories include configured channel members so Slack Connect participants omitted by `users.list` remain addressable; unresolved identities remain inert. Roo is untouched. Code lives in `roo-standalone/bridge/`.

**Goal:** Bidirectional sync between one channel in the MLAI Slack workspace (where Roo lives) and one channel in the Stone & Chalk (S&C) Slack workspace, relayed by the existing DigitalOcean droplet. Members of each org read and write entirely from their own workspace.

**Asymmetry that shapes everything:** Sam is an admin/owner in MLAI but an ordinary member in S&C. MLAI-side capabilities are unlimited (bots, custom apps, eventually puppet accounts). S&C-side capabilities depend on that workspace's app-installation policy and otherwise fall back to Sam's personal user token.

---

## Step 0 — Preflight (do before writing any code)

1. **Discover S&C's app policy.** Create the bridge app at api.slack.com, then attempt to install/authorize it against the S&C workspace. Three outcomes:
   - **Path A — app installs cleanly (bot allowed):** symmetric bridge, best fidelity. Build this.
   - **Path B — bot install blocked, but user OAuth allowed:** asymmetric bridge through Sam's user token. Workable.
   - **Path C — all app authorization requires admin approval:** either ask the S&C community manager to approve the app (show them this doc), **or** use **Path S (session-token client, below)** which needs no app at all. Path S is the recommended default for S&C precisely because it doesn't depend on this policy — but it's a Slack ToS grey area on an org Sam doesn't own, so the consent conversation (0.2) is required, not optional.
2. **Get S&C's blessing regardless of path.** Mirroring members' messages into another org is a consent issue before it's a technical one. Tell the community manager what the bridge does; put a permanent note in both channel topics: "⛓ This channel is bridged with <other org> #channel — messages are mirrored both ways."
3. **Pick channels.** MLAI: create `#stone-and-chalk` (or reuse). S&C: create `#mlai` or whichever channel Sam can socially own. Record both channel IDs.
4. **Record identities** needed for loop prevention and self-puppeting: Sam's MLAI user ID, Sam's S&C user ID, both app IDs, both bot user IDs (Path A).
5. **Confirm plan tiers** (free vs paid) for both workspaces — only matters for the later puppet upgrade (free = puppets cost nothing in MLAI).

---

## Architecture

```
MLAI  #stone-and-chalk                          S&C  #mlai
  │        ▲                                     │        ▲
events     │ post via bot                     events      │ Path A: post via bot
(bridge    │ (chat:write.customize:           (bot events │   (spoofed name/avatar)
 bot)      │  S&C author's name+avatar)        or Sam's   │ Path B: post via Sam's
  │        │                                   user-scope │   user token, prefixed
  ▼        │                                   events)    │   "*Alice:* …"
 ┌─────────┴─────────────────────────────────────┴────────┐
 │              bridge service on DO droplet               │
 │  tokens: MLAI bot, S&C bot (Path A) or Sam xoxp (B),   │
 │          Sam's MLAI xoxp (self-puppet, optional)        │
 │  SQLite: message ts-map, posted-message registry        │
 │  logic: loop filter → attribution → translate → post    │
 └──────────────────────────────────────────────────────────┘
```

### Path A (S&C allows bot install) — symmetric spoofing bridge
Both sides run the same design: a bot app per workspace ingests channel messages via Events API and posts into the opposite channel with `chat:write.customize` (author's real name + avatar, small APP badge). This is the Option-3 design from the original plan, instantiated twice.

### Path B (user-token only in S&C) — asymmetric bridge
- **S&C → MLAI:** ingest via *user-scoped* event subscriptions on Sam's S&C token; post into MLAI via the MLAI bridge bot with spoofed name/avatar. Full fidelity on the MLAI side.
- **MLAI → S&C:** post via Sam's S&C user token. Messages appear *from Sam*, with the real author as a bold prefix: `*Alice:* message text` (use a Block Kit context line if nicer). Lower fidelity, zero admin cooperation needed.
- Note: user-scoped `message.channels` events fire for **every** public channel Sam is in at S&C. The relay must filter to the bridged channel ID as the very first check and drop everything else unprocessed and unlogged.

### Path S (RECOMMENDED for S&C) — session-token client, "Beeper-style"
This is how Beeper/mautrix-slack connect to Slack with **no app installed in the workspace at all**, which sidesteps S&C's app policy (Path C dissolves). Instead of an app, the bridge reuses Sam's existing logged-in Slack web session:
- **Credentials:** the `xoxc-…` client token (from browser `localStorage` → `localConfig_v2`) plus the `d` cookie value `xoxd-…`. Together these are exactly what the Slack web client sends; with them the bridge calls Slack's *internal* client API as Sam, with all his normal permissions. No OAuth, no admin approval, no app review.
- **Real-time ingest:** rather than an Events API webhook (which needs an app + public request URL), the bridge opens the **same WebSocket the web client uses** (`client.userBoot` → WS gateway) and receives channel messages live. So the S&C side needs *no* inbound endpoint at all.
- **Egress:** posting with the session client makes messages come from Sam's real S&C account natively — no APP badge, no app at all.

**What it changes vs Path B:** strictly simpler and more capable on the S&C side — removes the app/OAuth dependency entirely and makes S&C ingest a persistent outbound WebSocket instead of an inbound webhook. Everything downstream of ingest (relay logic, MLAI side) is unchanged.

**What it does NOT change:** the *into-S&C* direction is still "as Sam," because Sam's session is the only S&C identity the bridge controls. MLAI authors posting into S&C still appear as `*Alice:* …` under Sam's account. Making MLAI users appear as *themselves in S&C* still requires real puppet accounts in S&C (the double-puppeting upgrade), which Beeper's mechanism does **not** provide — see "Critical distinction" below.

**Critical distinction — Beeper is a single-user aggregator, not a multi-user bridge.** Beeper authenticates *one* account (yours) and renders everyone else as read-only "ghosts" for you to read; when you send, it sends as you. It never makes other people appear back inside Slack. Our goal (every member of each org sees the conversation in their own workspace) is a *bridge*, which is more than Beeper does. The session-token mechanism is the reusable insight; it powers the S&C ingest/egress-as-Sam leg, but the cross-org fan-out is still our own relay.

**Trade-offs / risks of session tokens (be honest about these):**
- **Slack ToS grey area.** Non-official-client automation is against Slack's ToS. Beeper operates in this space openly for personal use, but S&C is an org Sam doesn't own — the Step 0.2 consent conversation matters *more* here, not less. This only ever touches Sam's own account and the one agreed channel.
- **Fragile credentials.** `xoxc`/`xoxd` are tied to the browser session: invalidated by logout, password change, or admin session revocation. The cookie TTL itself is long (>1 year as of late 2025), so the practical failure mode is *session invalidation*, not expiry.
- **Anti-abuse heuristics.** Slack invalidates these tokens when behavior looks non-human — e.g. bulk-caching all users tripped it in the slack-mcp-server case (Aug 2025). Mitigation: behave like a real client — lazy per-id `users.info` with caching, respect rate limits, no bulk scraping.
- **Not production-grade.** Expect occasional manual re-auth (re-extract token+cookie). Build a clean `tokens_revoked`/401 → DM-Sam-to-re-auth path; treat S&C connectivity as best-effort.
- **xoxp alternative:** a user-OAuth token (`xoxp`) avoids the caching-invalidation problem but requires creating an app + OAuth in S&C — the exact dependency Path S exists to avoid. Only worth it if S&C turns out to permit user-OAuth apps (Path B).

### Self-puppeting (both paths, optional layer)
Sam is the one human with real accounts in both orgs. With his user tokens on both sides, his messages bridge as *his real account* — no spoofing, no prefix. This is puppet-zero of the full double-puppeting design and exercises the per-user token routing machinery. Under Path S the S&C "token" is the session client; under Path B it's his `xoxp`.

---

## Slack app configuration

| | App 1: "Bridge" (MLAI) | App 2: "Bridge" (S&C) |
|---|---|---|
| Install type | Bot (Sam is admin) | Path A: bot · Path B: Sam's user OAuth only |
| Bot scopes | `channels:history`, `chat:write`, `chat:write.customize`, `users:read`, `users:read.email`, `files:read`, `files:write` | Path A: same list |
| User scopes (Sam) | `chat:write` (self-puppet, optional) | Path B: `channels:history`, `chat:write`, `users:read`, `files:read`, `files:write` |
| Event subscriptions | Bot events: `message.channels` | Path A: bot events `message.channels` · Path B: same as *user* events |
| Request URL | `https://<droplet>/bridge/events/mlai` | `https://<droplet>/bridge/events/snc` |

Separate request-URL paths per app so each endpoint verifies with its own signing secret. Handle Slack's `url_verification` challenge on both. If the channels are private, swap in `groups:history` / `groups:read`.

---

## Relay service design

### Where it lives
New package `roo-standalone/bridge/` — its own FastAPI app, port, and systemd unit (`slack-bridge.service`), proxied by the existing nginx. Deliberately **not** inside Roo's app: independent deploys, and a Roo crash shouldn't take the bridge down. Copy Roo's signature-verification and `slack_sdk` client patterns ([main.py](roo-standalone/roo/main.py), [slack_client.py](roo-standalone/roo/slack_client.py)).

```
roo-standalone/bridge/
  __init__.py
  main.py          # FastAPI app: /events/mlai, /events/snc, /healthz
  config.py        # env-driven settings
  relay.py         # core pipeline: filter → attribute → translate → post
  identity.py      # author resolution, name/avatar cache, self-puppet routing
  store.py         # SQLite: ts-map, posted-registry, profile cache
  slack.py         # thin clients: per-token WebClient factory
  snc_session.py   # Path S: session-token client + WebSocket ingest worker
  tests/
```

**Ingest differs by path.** MLAI always ingests via the `/events/mlai` HTTP endpoint (proper bot). For S&C: Path A/B use the `/events/snc` HTTP endpoint; **Path S has no inbound endpoint** — `snc_session.py` runs a long-lived background task that authenticates with the `xoxc`/`xoxd` session creds, opens the web-client WebSocket, and feeds received messages into the same `relay.py` pipeline. So in Path S the bridge needs only *one* public request URL (MLAI's), which is one less thing to expose.

### Config (env)
```
MLAI_BOT_TOKEN / MLAI_SIGNING_SECRET / MLAI_CHANNEL_ID
SNC_CHANNEL_ID
SNC_SIGNING_SECRET       # Path A/B only (HTTP ingest)
SNC_BOT_TOKEN            # Path A
SNC_SAM_USER_TOKEN       # Path B (and self-puppet)
SNC_SESSION_XOXC / SNC_SESSION_XOXD   # Path S (session client; encrypt at rest)
MLAI_SAM_USER_TOKEN      # self-puppet, optional
SAM_MLAI_USER_ID / SAM_SNC_USER_ID
MLAI_APP_ID / SNC_APP_ID / bot user IDs
BRIDGE_DB_PATH=/var/lib/slack-bridge/bridge.db
```

### SQLite schema
```sql
-- thread/edit/delete correlation, bidirectional lookups
CREATE TABLE message_map (
  src_team TEXT, src_channel TEXT, src_ts TEXT,
  dst_team TEXT, dst_channel TEXT, dst_ts TEXT,
  PRIMARY KEY (src_team, src_channel, src_ts)
);
CREATE INDEX idx_map_dst ON message_map (dst_team, dst_channel, dst_ts);

-- loop prevention: every message the bridge itself posted
CREATE TABLE posted_registry (
  team TEXT, channel TEXT, ts TEXT, posted_at REAL,
  PRIMARY KEY (team, channel, ts)
);

-- event dedupe (Slack retries up to 3x)
CREATE TABLE seen_events (event_id TEXT PRIMARY KEY, seen_at REAL);
```

### Pipeline (per event)
1. **Verify signature** (per-app secret), ack `200` immediately, process async — same 3-second-ack pattern Roo uses.
2. **Dedupe** on `event_id`.
3. **Channel filter:** drop anything not from the two bridged channel IDs (critical in Path B — Sam's user events cover all his S&C channels).
4. **Loop filter** (order matters):
   a. Author is own bridge bot/app on that side → drop.
   b. `(channel, ts)` in `posted_registry` → drop.
   c. Path B subtlety: Sam's S&C account is both a real participant *and* the relay identity. His organic messages must bridge; bridge-posted ones must not. The registry handles this, but there's a race — the message event can arrive before the `chat.postMessage` response is recorded. Fix: insert a registry *intent* row before calling postMessage and reconcile ts after; belt-and-braces, organic client messages carry `client_msg_id` while API-posted ones don't.
5. **Subtype routing:** plain message → relay; `message_changed` → `chat.update` on mapped ts; `message_deleted` → `chat.delete`; `bot_message` from other bots (e.g. Roo) → relay via spoof path; file shares → file pipeline; everything else (joins, pins) → drop.
6. **Attribution:** resolve author via `users.info` (cached in SQLite with TTL). Choose egress identity:
   - Author is Sam (either side) and self-puppet enabled → post with his opposite-side user token, no prefix.
   - Path A or into-MLAI → bot post with `username` + `icon_url`.
   - Path B into-S&C → Sam's user token with `*Name:* ` prefix.
7. **Translate:** map `<@source-user>` to `<@destination-user>` using manual ID overrides first and exact normalized email second. Resolve qualified destination-only handles such as `hex:alice` without triggering Slack's local autocomplete; retain `@hex:alice` as a legacy input. Use labeled inert text when no safe mapping exists. Keep mass mentions inert; map `thread_ts` through `message_map` for replies.
8. **Post, then record** mapping + registry. On `429`, honor `Retry-After` (volume makes this rare).

### Files
`url_private` is workspace-internal — always re-upload: download with the source-side token, `files.uploadV2` with the egress identity's token to the destination channel (threaded if applicable).

### Reactions
Skip in v1. Bot-mirrored reactions collapse attribution (everything from one identity, one per emoji), and in Path B they'd all appear as Sam. Revisit with puppets.

### Ops
- `tokens_revoked` / auth failures → DM Sam via Roo's existing `send_dm` and disable that direction cleanly (don't crash-loop).
- systemd unit + `/healthz`; logs to journald. Nightly `seen_events`/`posted_registry` pruning (>7 days).
- Tokens in env via systemd `EnvironmentFile` (root-read-only, like Roo's).

---

## Phases

| Phase | Scope | Estimate |
|---|---|---|
| 0 | Preflight: S&C policy discovery, consent, channels, IDs | ½ day (mostly waiting on S&C) |
| 1 | Apps + tokens + endpoint skeleton with verified signatures on both event URLs | ½ day |
| 2 | **MVP:** text both directions, threads, loop prevention, ts-map, deploy on droplet | 1 day |
| 3 | Fidelity: edits, deletes, files, mention translation, self-puppet for Sam | 1 day |
| 4 | Hardening: revocation alerts, retries, pruning, channel-topic notices, README | ½ day |

**Total: ~3.5 days** to a polished v1.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| S&C blocks all app auth (Path C) | Project blocked | Ask S&C admin early (Step 0.1); nothing else is sanctioned |
| S&C revokes Sam's token / membership lapses | S&C direction halts | `tokens_revoked` handler + DM alert; re-auth runbook |
| Path S session token invalidated (logout / password change / anti-abuse heuristic) | S&C leg stops until re-auth | Behave like a real client (no bulk scraping, cache `users.info`, respect rate limits); 401 → DM Sam to re-extract token+cookie; treat S&C as best-effort |
| Loop bug floods both channels | Embarrassing, noisy | Registry + author filters + a circuit breaker: >N bridge posts in 60s → pause + alert |
| Consent/optics in S&C | Trust damage | Step 0.2 blessing + permanent channel-topic disclosure |
| Path B: every S&C message Sam can see hits the endpoint | Privacy/log hygiene | Channel filter first, before any logging |

## Future upgrades (when wanted)

1. **Puppets in MLAI for S&C regulars** — optional higher-fidelity identities that remove the APP badge and make cross-org members directly discoverable in MLAI's native mention picker. Working notifications no longer require puppets: the bridge can emit the destination user's real ID when mapped.
2. **S&C bot via admin approval** — converts Path B to Path A; delete the prefix code path.
3. **More channels** — schema already keys on channel; add a channel-pair table instead of env vars.

## Open questions

1. Which S&C channel — existing one, or a new `#mlai` Sam creates?
2. Is the MLAI workspace on a free or paid plan? (Only matters for the puppet upgrade.)
3. Should other bots' messages (Roo's posts in the MLAI channel) bridge into S&C, or stay internal? Default plan: bridge them.
