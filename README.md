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

Copy `.env.example` to `.env` and configure:
- `DATABASE_URL` - PostgreSQL connection
- `SLACK_BOT_TOKEN` - Slack bot token
- `OPENAI_API_KEY` - For LLM and embeddings
