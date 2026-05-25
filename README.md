# Roo Standalone - AI Agent Service

A standalone FastAPI microservice for the Roo AI agent with PostgreSQL + pgvector for vector embeddings and a skills-based architecture.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn roo.main:app --reload

# Run with Docker
docker-compose up -d
```

## Project Structure

```
roo-standalone/
├── roo/                    # Main application
│   ├── main.py             # FastAPI entrypoint
│   ├── config.py           # Settings
│   ├── agent.py            # Core agent
│   ├── llm.py              # LLM client
│   ├── database.py         # PostgreSQL + pgvector
│   ├── embeddings.py       # Vector embeddings
│   ├── slack_client.py     # Slack SDK
│   ├── skills/             # Skill system
│   └── clients/            # External API clients
├── skills/                 # Skill definitions (*.md)
├── tests/                  # Test suite
└── docker-compose.yml
```

## Skills System

Skills are defined as markdown files in `skills/`. Each skill specifies:
- Trigger keywords
- Parameters to extract
- Actions to perform (LLM, database, API calls)

See `skills/connect_users.md` for an example.

## Environment Variables

Copy `roo-standalone/.env.example` to `roo-standalone/.env` and configure:
- `DATABASE_URL` - PostgreSQL connection
- `SLACK_BOT_TOKEN` - Slack bot token
- `OPENAI_API_KEY` - For LLM and embeddings
- `MLAI_BACKEND_URL` - Backend API URL for points, admin lookup, and Luma attendee reports
- `ROO_API_KEY`/`MLAI_API_KEY`/`INTERNAL_API_KEY` - Backend auth key for Roo-to-mlai-backend calls

For Slack file parsing, the Roo Slack app must include the `files:read` bot scope and be reinstalled after the scope is added. If Slack rotates the bot token during reinstall, update `SLACK_BOT_TOKEN` in production and restart Roo.

For daily jobs, the recommended production setup is:
- `mlai-backend` owns the 7am Melbourne scheduler and Slack posting
- Roo keeps `JOBS_SCHEDULER_ENABLED=false`
- Roo only needs `JOBS_API_URL` and `JOBS_TRIGGER_TOKEN` if you want it to trigger backend jobs manually in the future

## MLAI Championships (coworking leaderboard) — admin setup

The `coworking_leaderboard` action lets any member ask Roo for a ranked list of who's booked the coworking space the most ("MLAI championships", "leaderboard", "who came in the most this week", etc.). The Roo-side intent, formatter, and tests are in place — but the feature needs two things that have to happen outside this repo before it works end-to-end.

**Slack admin (manual, one-time):**
1. Create a Slack channel named `#mlai-championships` for testing the response in isolation before it goes general-availability.
2. Invite the Roo bot user to that channel.
3. Post in-channel with something like `@Roo MLAI championships this week` to smoke-test once steps 4–5 below are done.

**mlai-backend owner:**
4. Extend `GET /api/v1/points/coworking/report/` to accept `?include_users=true` and return a top-level `users` array sorted desc by booking count. Each entry should include at least `slack_user_id` and `booking_count`. Roo defensively re-sorts and handles a missing/empty `users` array with a friendly empty-state, so the feature degrades safely if the field isn't shipped yet.
5. Confirm `slack_user_id` (not `user_id`) is the field name on the response.

No new env vars are required for Roo. The leaderboard is public by design — every member can run it — so it does not gate on a points-admin role like the existing `coworking_report` command does.
