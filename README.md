# Roo

Roo is MLAI's Slack-facing AI agent service. The active FastAPI application,
tests, skills, runtime configuration, and Docker files live under
[`roo-standalone/`](roo-standalone/).

Roo integrates with Slack, `mlai-backend`, Linear, content services, and
configured AI providers. It supports separate public and administrative
surfaces; their credentials and permissions must remain isolated.

For the wider platform map, start with
[`mlai-engineering`](https://github.com/MLAI-AUS-Inc/mlai-engineering). AI
coding agents must read [`AGENTS.md`](AGENTS.md).

## Repository layout

```text
roo/
├── roo-standalone/
│   ├── roo/                  # FastAPI application and tests
│   ├── skills/               # Runtime skill packages
│   ├── bridge/               # Bridge adapter and tests
│   ├── slack-app-manifests/  # Slack application definitions
│   ├── docs/                 # Service-specific operations and incidents
│   ├── .env.example          # Public Roo configuration template
│   ├── .env.admin.example    # Admin Roo configuration template
│   └── docker-compose*.yml   # Runtime profiles
├── Luma-Stripe-Reconcile/    # Standalone reconciliation utility
└── *.md                      # Historical audits and implementation plans
```

The root planning and audit files are background context. They are not the
authoritative runtime instructions unless this README or a current service
document links to them explicitly.

## Requirements

- Python 3.11
- A virtual environment
- Docker only when working with a Compose runtime profile
- Development credentials for whichever external integration is in scope

## Local setup

Run application commands from `roo-standalone`, not the repository root:

```bash
cd roo-standalone
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
test -f .env || cp .env.example .env
```

At least one supported model provider must be configured for live model calls.
Slack and backend credentials are only required for features that call those
services. Use explicitly provisioned development or staging values; do not use
production credentials for routine local work.

Start the public development application with:

```bash
uvicorn roo.main:app --reload
```

## Roo Dev in Slack

**Roo Dev** is the development Slack app connected to a locally running Roo
server. Use the private **#roo-testing** channel in the MLAI workspace for
manual integration testing. Ask a channel member for access and ensure
`@Roo Dev` is invited. Select the actual bot from Slack's mention picker.

The local Linear reader binds this channel (`C0BRM181EDV`, workspace
`T05N9C1QSJC`) to the **MLAI_TECH · Todo** queue. It supports listing issues
and reading issue details and comments. That binding does not enable issue
editing or authorize the reader in other channels. Production's
`#tech_volunteers` binding is configured separately.

### Configure the local services

Complete the local setup above, then populate the untracked
`roo-standalone/.env` in the checkout or worktree you will actually run:

```dotenv
SLACK_BOT_TOKEN=xoxb-replace-with-roo-dev-bot-token
SLACK_SIGNING_SECRET=replace-with-roo-dev-signing-secret
OPENAI_API_KEY=replace-with-development-provider-key
MLAI_BACKEND_URL=http://127.0.0.1:8001
ROO_API_KEY=replace-with-shared-local-service-key
ROO_SURFACE=public
JOBS_SCHEDULER_ENABLED=false
```

Use Roo Dev's credentials from its Slack app settings and a development AI
key. `GOOGLE_API_KEY` or `ANTHROPIC_API_KEY` can be used instead of
`OPENAI_API_KEY`. Keep credential values out of Git, chat, and logs. Each Git
worktree has its own local `.env`; switching worktrees does not copy it.

For Linear queries, configure the local backend's database, `LINEAR_API_KEY`,
channel binding, and matching `ROO_API_KEY` using the
[Linear channel reader guide](roo-standalone/docs/linear-channel-issues.md).
Start Django from that backend checkout with its development environment active:

```bash
RUN_MIGRATIONS_ON_START=0 python manage.py runserver 127.0.0.1:8001
```

Use an already prepared development database. Never apply migrations without
explicit approval for the specific migration. The backend's local Compose web
service enables migrations on startup, so use the direct command above for
this workflow.

In another terminal, from `roo-standalone` with its virtual environment active:

```bash
uvicorn roo.main:app --reload --host 127.0.0.1 --port 8000
```

### Connect Slack to your local Roo

With `cloudflared` installed, open a third terminal and run:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the HTTPS hostname printed by the tunnel. In the **Roo Dev** Slack app
settings, update and save these URLs, replacing `TUNNEL_HOST` with that hostname:

| Slack setting | URL |
| --- | --- |
| Event Subscriptions → Request URL | `https://TUNNEL_HOST/slack/events` |
| Interactivity & Shortcuts → Request URL | `https://TUNNEL_HOST/slack/actions` |
| Slash Commands → `/roo-dev` → Request URL | `https://TUNNEL_HOST/slack/commands` |

Keep Event Subscriptions enabled and Socket Mode disabled. Wait for Slack to
verify the event URL before saving. Reinstall the app if its scopes change.
Coordinate with other testers before changing callbacks: the shared Roo Dev
app sends events to the configured developer's tunnel.

Keep both servers, the tunnel, and your computer awake while testing. A quick
tunnel gets a new hostname when restarted; update the three callback URLs
each time. Check `/healthz/ready` on ports 8000 and 8001, and on the tunnel
hostname, to confirm the services are reachable.

### Update callbacks through the Slack API

Engineers can update the callback URLs without using the Slack settings UI
once they have an authorized **app configuration access token**. The runtime
bot token (`xoxb-`), signing secret, and app-level token (`xapp-`) do not grant
this configuration access.

To obtain configuration tokens, open [Your Apps](https://api.slack.com/apps),
scroll below the app list to **Your App Configuration Tokens**, click
**Generate Token**, and select the MLAI workspace. Prefer adding engineers as
Roo Dev collaborators so they can generate their own tokens. A configuration
token belongs to a user and workspace and can manage multiple apps; it is not
limited to Roo Dev.

Store the returned values in the running worktree's untracked
`roo-standalone/.env`:

```dotenv
SLACK_CONFIG_TOKEN=replace-with-configuration-access-token
SLACK_CONFIG_REFRESH_TOKEN=replace-with-configuration-refresh-token
```

These variables are for configuration tooling; Roo does not automatically
update Slack callbacks or refresh these tokens on startup. Load them with a
dotenv-aware tool rather than pasting credentials into terminal commands.

For Roo Dev (`A0BT0N95022`), use this API sequence:

1. Call `apps.manifest.export` with `app_id` to read the current manifest.
2. Preserve the full manifest and update only
   `settings.event_subscriptions.request_url`,
   `settings.interactivity.request_url`, and the `url` of the
   `features.slash_commands` entry whose `command` is `/roo-dev`, using the
   three tunnel URLs above.
3. Call `apps.manifest.update` with `app_id` and the complete edited manifest
   encoded as a JSON string. Send the configuration access token in the
   `Authorization: Bearer ...` header. The update replaces the manifest, so
   retain all other settings and scopes.
4. Check the response's `ok` field, then export again and verify all three
   URLs. A successful HTTP response alone does not prove Slack accepted the
   update. URL-only changes should return `permissions_updated: false`.

The access token expires after **12 hours**. Call `tooling.tokens.rotate`
with the saved `refresh_token` to obtain a new `token` and `refresh_token`,
then replace both local environment values. Treat the refresh token as a
secret too. See Slack's [configuration token and rotation guide](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/)
and [manifest update reference](https://docs.slack.dev/reference/methods/apps.manifest.update/).

### Test in #roo-testing

Start with a mention in the channel:

```text
@Roo Dev are you connected?
@Roo Dev what Linear issues are in the MLAI_TECH Todo list at the moment?
```

The second request should return a numbered list of current issue identifiers
and titles. Reply in that same response thread and mention Roo Dev again:

```text
@Roo Dev show me number 2
@Roo Dev tell me more about TECH-29
```

If Roo does not respond, check the Roo terminal output, current event callback
URL, tunnel readiness, and the bot's channel membership. If chat works but
Linear queries fail, check the backend terminal, matching service keys, Linear
key, and channel binding. The reader rejects requests outside its configured
channel; a successful chat reply alone does not verify the backend integration.

## Tests

From `roo-standalone`:

```bash
pytest
```

Prefer targeted tests while developing. Application tests are under
`roo/tests`; bridge tests are under `bridge/tests`.

## Skills

Runtime skills are directories under `roo-standalone/skills`. Inspect the
selected skill package and its tests before changing routing or permissions.
The similarly named Python modules under `roo-standalone/roo/skills` implement
application-side behavior and are not Markdown-only skill definitions.

Skill availability is constrained by the runtime surface and configuration.
Adding a skill directory does not grant the public agent permission to perform
administrative actions.

## Public and Admin Roo

- Public Roo handles approved community-facing interactions.
- Admin Roo is a separately scoped internal surface.
- Public Roo must not receive administrative organisational-memory credentials.
- Service-to-service credentials must be distinct from Slack user or bot
  credentials.
- Keep fail-closed feature flags disabled until the corresponding service and
  review gate are ready.

Use `.env.example` for public configuration and `.env.admin.example` for the
separate administrative profile. Never merge the two files into a shared
credential set.

## Docker profiles

The Compose files represent different runtime surfaces:

- `docker-compose.yml`: public Roo service
- `docker-compose.admin.yml`: administrative service
- `docker-compose.bridge.yml`: bridge adapter

Read the selected file and its referenced environment template before starting
it. Compose startup may contact external services when credentials are present.

## Slack configuration

Slack manifests live in `roo-standalone/slack-app-manifests`. Required scopes
depend on the enabled features. File handling requires `files:read`; contextual
Linear features require the appropriate channel history/read scopes plus
`users:read` and `users:read.email` for identity matching. Reinstalling a Slack
application may rotate credentials, so coordinate manifest changes with the
runtime owner.

For the local, channel-bound Linear issue reader workflow, see
[`roo-standalone/docs/linear-channel-issues.md`](roo-standalone/docs/linear-channel-issues.md).

## Scheduled jobs

Builder payment reminders have a separate, opt-in worker. It DMs each configured
human member of #mlai-studio-builders their full list of open Linear tasks (or an
hours reminder when none are open) on the Thursday before a fortnightly
payment Friday. See the [payment reminder guide](roo-standalone/docs/payment-reminders.md)
for configuration, preview, activation, and delivery recovery.

The recommended production arrangement keeps `JOBS_SCHEDULER_ENABLED=false` in
Roo. `mlai-backend` owns the 7 a.m. Melbourne schedule and Slack posting. Roo
only needs job trigger configuration when explicitly used as a manual caller.
Active Points Admins, including committee members, can DM Roo
`run the daily jobs scrape now`; Roo replies directly in the DM, while channel
requests receive a threaded reply.
