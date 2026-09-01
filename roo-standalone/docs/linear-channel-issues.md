# Local Slack test for Linear channel issues

Roo uses its existing signed Slack HTTP endpoints for this feature. The local
test therefore runs the backend and Roo together, then exposes Roo through an
HTTPS development tunnel. Do not create a separate Bolt application or use
`slack run`; that would exercise a different runtime from production.

## 1. Configure the backend worktree

Set these values in the backend worktree's untracked `.env`. Use a new shared
local service key for both `ROO_API_KEY` values and enter the Linear key
directly; do not put either secret in source control or chat.

```dotenv
RUN_MIGRATIONS_ON_START=0
ROO_API_KEY=replace_with_shared_local_service_key
LINEAR_API_KEY=replace_locally
LINEAR_WRITE_API_KEY=use_a_distinct_team_scoped_write_key
LINEAR_CHANNEL_ISSUE_BINDINGS_JSON={"T05N9C1QSJC:C0BRM181EDV":{"display_name":"MLAI_TECH · Todo","team_name":"MLAI_TECH","state_name":"Todo","linear_team_id":"def24f5e-2990-4e28-9e06-e89db4a09f9f","linear_state_id":"f3591a1e-f7a2-4514-9280-000d43ea60e5"}}
LINEAR_CHANNEL_ISSUE_MAX_COMMENTS=250
LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=true
```

Start Django directly on port 8001 using the repository's normal development
environment:

```bash
python manage.py runserver 127.0.0.1:8001
```

Do not use `docker-compose.local.yml` for this check: its web service is
configured to apply migrations on startup. This feature itself has no database
migration.

## 2. Configure and start Roo

Set these values in this worktree's untracked `.env`. Use replacement Slack
credentials entered directly from the Slack app settings; previously disclosed
credentials must be revoked.

```dotenv
SLACK_BOT_TOKEN=xoxb-replace-locally
SLACK_SIGNING_SECRET=replace-locally
MLAI_BACKEND_URL=http://127.0.0.1:8001
ROO_API_KEY=replace_with_shared_local_service_key
ROO_SURFACE=public
LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=true
```

Also configure one supported LLM provider because Roo's router selects the
`mlai-data-query` action before executing it. Then start Roo:

```bash
uvicorn roo.main:app --reload --host 127.0.0.1 --port 8000
```

## 3. Connect the development Slack app

Create an HTTPS tunnel to `http://127.0.0.1:8000`. Set the development Slack
app's URLs to the resulting hostname:

- Event subscriptions: `https://TUNNEL_HOST/slack/events`
- Interactivity: `https://TUNNEL_HOST/slack/actions`
- `/roo-dev`: `https://TUNNEL_HOST/slack/commands`

Leave Socket Mode disabled. Reinstall the app after scope changes, invite
`@Roo Dev` to `#roo-testing`, and test with:

```text
@Roo Dev what Linear issues are in the MLAI_TECH Todo list at the moment?
```

Roo should return numbered identifiers and titles. In the response thread,
test each supported follow-up:

```text
@Roo Dev tell me more about TECH-16
@Roo Dev show me number 2
@Roo Dev details on deployment alerts
@Roo Dev what Linear statuses are available?
@Roo Dev move TECH-29 to In Progress
@Roo Dev add a comment to TECH-29 saying local testing passed
```

The backend should return `403` if the same app invokes these actions from any
channel other than `#roo-testing` (`C0BRM181EDV`). Production uses a separate
binding for `#tech_volunteers` (`C0BS0J2Q3M1`).

Write commands execute immediately and therefore must be explicit. Roo first
reads the current issue, and the backend rejects the edit if `updatedAt`
changes before the mutation. Neither Roo nor the backend retries an uncertain
write response; inspect Linear before manually retrying. Set both write flags
back to `false` to exercise the kill switch.
