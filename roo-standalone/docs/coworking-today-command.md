# Private coworking booking list

Admins can use `/coworking-today` for today's active bookings, or
`/coworking-today 2026-09-21` for a specific date. Today uses Australia/Melbourne,
including daylight saving. The reply lists names and the number of people
booked, and is visible only to the requester. This reports bookings, not who
physically arrived or remains in the office.

This is a deterministic command: no Roo AI routing, prompts, report generation,
or booking writes. Cancelled bookings are excluded. Empty dates explicitly say
there are no active bookings. A failed lookup is an error, never an empty list.
The backend supplies active Points Admin authorisation for admin, committee and
portfolio-lead roles, plus existing configured bootstrap admins. A partner's
permission to request ordinary coworking reports does not grant this command.

## Service configuration

Deploy the backend endpoint before Roo:
`GET /api/v1/points/coworking/bookings-for-date/` with `slack_user_id` from the
verified Slack payload and a strict ISO `date`. Set Roo's `MLAI_BACKEND_URL` and
its dedicated `ROO_API_KEY` to match the backend. Generic/internal credential
fallback is deliberately disabled for this command. Never add Admin Roo's
organisational-memory key to the public runtime.

Signed requests, replay protection and deployment context allowlists remain in
force. Admin Roo permits this one command in its allowed channels, or in direct
messages from `ROO_ALLOWED_DM_USER_IDS`. A signed `D…` channel ID identifies the
DM for this command. Other admin slash commands retain their existing gates.

The lookup has a two-second total budget and no transport retries. Slow or
unavailable backends produce a private retry message, with no delayed public
message. Stored names are rendered as literal text, so names cannot trigger
Slack mentions. Long lists are split into blocks; an over-limit list produces an
explicit error instead of dropping people silently.

## Slack registration (rollout step)

Register the command in the **one existing Slack app** whose signed requests
reach the chosen Roo deployment. Do not register competing copies on Public
and Admin Roo apps in the same workspace.

| Setting | Value |
| --- | --- |
| Command | `/coworking-today` |
| Request URL | Chosen deployment's verified **HTTPS** `/slack/commands` URL |
| Description | Privately list coworking bookings for a date |
| Usage hint | `[YYYY-MM-DD]` |
| Escape channels, users, and links | Off |
| OAuth scope | `commands` (already requested by Roo) |

The tracked `slack-app-manifests/roo-public.yaml` currently contains legacy HTTP
addresses. Do not copy that address for this private command or guess a TLS
hostname. During an authorised rollout, export the selected app's actual
manifest, verify its HTTPS command endpoint, and add the command entry to its
`features.slash_commands`, preserving other entries. Save that verified manifest
back to the tracked file if Public Roo is the selected app. For an Admin Roo
app, update its own manifest; never replace the public app's configuration with
admin configuration. Reinstall only if Slack requires it. No additional
message scopes or `response_url` delivery are needed.

Registration and deployment have not been performed by this change.

## Validation

From `roo-standalone`, with synthetic credentials only:

```sh
SLACK_BOT_TOKEN=xoxb-test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
  .venv/bin/python -m pytest roo/tests/test_coworking_snapshot.py \
  roo/tests/test_slack_security.py roo/tests/test_surface_security.py -q
```

After obtaining the backend repository's required approval for disposable test
migrations, run its `tests.test_coworking_snapshot` and existing
`roo.tests.CoworkingServiceTests` with the Django test runner and isolated SQLite
settings. No model change or new migration is required.

Before enabling in a real workspace, use authorised staging fixtures to verify:

- An admin sees only active bookings for the selected date, privately, even
  when the command is invoked in a public channel.
- A non-admin or deactivated admin gets denial, with no names.
- Invalid arguments show usage; empty dates say no active bookings.
- Allowlisted Admin Roo DMs work; non-allowlisted contexts are denied.
- A delayed backend returns a private retry response before Slack's deadline.
- The existing coworking reports continue to work independently.

Slack's [slash-command documentation](https://docs.slack.dev/interactivity/implementing-slash-commands/)
specifies the three-second acknowledgement deadline and ephemeral replies.
The local handler tests cover a delayed backend; deployment latency still
needs staging verification.

Rollback: remove this slash-command registration from the selected Slack app.
The additive read-only backend endpoint may remain deployed.
