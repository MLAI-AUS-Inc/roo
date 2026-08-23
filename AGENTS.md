# AI agent contributor guide

Read [`README.md`](README.md) before working in this repository. The active
application root is `roo-standalone`; do not assume commands run from the Git
repository root.

## Safety and trust boundaries

- Keep Public Roo and Admin Roo credentials, configuration, and permissions
  separate.
- Never place `ORG_BRAIN_API_KEY` or equivalent administrative authority in the
  public runtime.
- Never send Slack messages, create Linear issues, moderate content, trigger
  jobs, or call live external services unless the user explicitly requests the
  external action.
- Never use production tokens in tests or local development.
- Never create, run, or apply a database migration without explicit approval
  for that specific migration, including migrations invoked indirectly.
- Treat runtime skills as executable capabilities, not passive prompt text.

## Working directory and commands

Run Python application commands from `roo-standalone`:

```bash
cd roo-standalone
source .venv/bin/activate
pytest
uvicorn roo.main:app --reload
```

Do not start a server for a documentation-only task. Prefer targeted tests for
the module or skill in scope.

## Code and documentation map

- `roo-standalone/roo/main.py`: FastAPI composition root
- `roo-standalone/roo/router.py`: request and intent routing
- `roo-standalone/roo/config.py`: runtime configuration
- `roo-standalone/roo/tests`: application tests
- `roo-standalone/skills`: runtime skill packages
- `roo-standalone/bridge`: bridge service
- `roo-standalone/slack-app-manifests`: requested Slack scopes and events
- `roo-standalone/docs`: service operations and incident notes

Root-level plans and audits may be historical. Verify their claims against the
current application and configuration before using them as instructions.

## Change expectations

- Preserve signature validation, replay protection, identity matching, channel
  allowlists, and explicit action gates.
- Keep external side effects behind reviewed, testable boundaries.
- Update the relevant environment example and documentation when configuration
  changes.
- Update skill routing tests when triggers, permissions, or side effects change.
- For documentation-only changes, validate paths and links without contacting
  external services.
