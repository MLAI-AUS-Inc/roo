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

## Scheduled jobs

The recommended production arrangement keeps `JOBS_SCHEDULER_ENABLED=false` in
Roo. `mlai-backend` owns the 7 a.m. Melbourne schedule and Slack posting. Roo
only needs job trigger configuration when explicitly used as a manual caller.
Active Points Admins, including committee members, can DM Roo
`run the daily jobs scrape now`; Roo replies directly in the DM, while channel
requests receive a threaded reply.
