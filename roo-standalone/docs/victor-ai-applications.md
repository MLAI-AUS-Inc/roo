# Victor AI applications in Roo

This integration lets anyone in the configured Slack channel ask Roo for a
summary, a compact list, one application's full approved fields, help examples,
or a filtered CSV export. It is read-only and disabled by default on both
services.

## Channel behavior

- Roo exposes the skill whenever the resolved channel name is
  `exp-victor-ai`. There are no workspace, channel, or user ID allowlists.
- The executor repeats the channel-name check before retrieving data.
- Each backend request has a fresh, short-lived HMAC signature and single-use
  nonce. Ordinary Roo/backend API keys cannot call these endpoints.
- mlai-backend consumes the nonce, serves only `GET`/`HEAD`, and records a
  PII-free access audit. Channel selection remains Roo's responsibility.
- Applicant fields are escaped before Slack rendering and never sent to an LLM.
  CSV cells are protected against spreadsheet-formula execution.

## Deployment configuration

Generate one dedicated secret of at least 32 characters and store the same
value in the Roo and mlai-backend secret stores. Do not reuse `ROO_API_KEY` or
`MLAI_API_KEY`.

Configure mlai-backend first:

```dotenv
VICTOR_AI_ROO_ENABLED=false
VICTOR_AI_ROO_SIGNING_SECRET=<dedicated-secret>
VICTOR_AI_ROO_ASSERTION_MAX_AGE_SECONDS=60
VICTOR_AI_ROO_EXPORT_MAX_ROWS=5000
```

Apply the `victor_ai` migration, deploy the backend, and then change
`VICTOR_AI_ROO_ENABLED` to `true`.

Configure Roo with the channel name and the same secret:

```dotenv
VICTOR_AI_SKILL_ENABLED=false
VICTOR_AI_ROO_SIGNING_SECRET=<same-dedicated-secret>
VICTOR_AI_SLACK_CHANNEL_NAME=exp-victor-ai
VICTOR_AI_BACKEND_TIMEOUT_SECONDS=20
```

Deploy Roo while disabled, verify startup, then set
`VICTOR_AI_SKILL_ENABLED=true` and restart.

## Acceptance checks

From any account in `#exp-victor-ai`, try:

- `@Roo what can I ask about Victor applications?`
- `@Roo how many Victor AI applications do we have?`
- `@Roo list the latest Victor AI applications`
- `@Roo show Victor application <ID>`
- `@Roo export complete Victor applications to CSV`

Repeat a data request from a different channel and a DM; neither must expose the
skill or return data. Confirm the CSV is uploaded in the requesting thread, the
backend audit records the Slack actor and action without applicant PII, and the
summary headline counts only complete applications while reporting partial
leads separately.
