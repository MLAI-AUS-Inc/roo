# Context-aware Slack responses

Public Roo can classify selected channel messages and respond when a user is
clearly continuing a Roo conversation without tagging the bot. The feature is
off by default. Admin Roo cannot enable it.

## Slack app setup

1. Replace the placeholder hostname in `slack-app-manifests/roo-public.yaml`.
2. Apply the manifest to the existing Public Roo Slack app.
3. Reinstall/reauthorise the app after the new history scopes are approved.
4. Update `SLACK_BOT_TOKEN` if Slack rotates it.
5. Invite Roo only to the public/private pilot channels it should observe.

The relevant subscriptions are `message.channels` and `message.groups`. Keep
`app_mention` and `message.im`; the application deduplicates overlapping logical
message deliveries by workspace, channel, and Slack message timestamp.

## Enforced pilot

The first production pilot uses:

```dotenv
ROO_CONTEXTUAL_RESPONSES_ENABLED=true
ROO_CONTEXTUAL_SHADOW_MODE=false
ROO_CONTEXTUAL_CHANNEL_IDS=C09L4DMMU5N
```

Roo can send an untagged reply only after the allowlist, candidate prefilter,
same-user session checks, and high-confidence addressedness gate pass. Review
structured `ADDRESSING_DECISION` log lines; they contain message metadata and
decision reasons, but not Slack message text.

Run the labelled live evaluation before enabling replies:

```bash
python scripts/run_addressing_eval.py
```

Roll back immediately by setting `ROO_CONTEXTUAL_RESPONSES_ENABLED=false`;
removing Slack event subscriptions is not required for the kill switch to take
effect.

## Safety policy

- Implicit messages fail closed on classifier errors or timeouts.
- Direct mentions retain the old execution path if the optional context layer
  fails.
- Only the same requester inherits an active thread/adjacency session.
- Bot messages, edits, deletes, unsupported message subtypes, and non-candidates
  are ignored before classification.
- Implicit skill execution is restricted by `ROO_IMPLICIT_ACTION_ALLOWLIST`.
  The default permits chat, balance checks, and top-up checkout creation. Admin
  points actions and actions affecting another user still require a direct Roo
  mention.
- The session database stores identifiers, timestamps, state, and expiry only;
  it does not store Slack message text.
