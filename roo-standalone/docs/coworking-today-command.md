# Private coworking booking list

Send a message using Slack's actual Roo mention:

```text
@Roo coworking-today
@Roo coworking-today 2026-09-21
```

Today uses Australia/Melbourne, including daylight saving. The reply lists the
names and number of people with active bookings. It reports bookings, not who
physically arrived or remains in the office. Cancelled bookings are excluded;
empty dates explicitly say there are no active bookings.

The request is an ordinary message visible to the conversation's members.
**The result is private to the requester.** In a channel, Roo sends a private
reply at channel level. In an existing thread, it sends that private reply in
the same thread. Roo must have access to the conversation; this is not a command
that can be run from arbitrary DMs with other people. In a DM to Roo, include
the explicit mention too. Select the real Roo user from Slack's mention picker;
typing plain text that merely looks like `@Roo` does not count as a mention.

## Deterministic behaviour

The event handler intercepts a leading Roo mention followed by the exact
`coworking-today` token before AI, contextual conversation or meeting-room
routing. It validates the optional `YYYY-MM-DD`, calls the separate booking-list
endpoint and formats the result directly. Extra arguments or invalid dates get
fixed usage instructions. Other messages retain normal Roo behaviour.

The backend checks the current full Points Admin policy: active admin, committee
or portfolio-lead roles, plus existing configured bootstrap admins. Partner-only
report access does not grant this command. Being a Slack workspace admin alone
is insufficient. Failed lookups return private errors, never an empty list.

Names are literal text, so stored names cannot trigger Slack mentions. Long
lists are split into blocks; over-limit lists give an explicit error rather than
dropping names silently. The existing two-second lookup budget is retained.

## Service configuration and rollout

Deploy the backend endpoint before Roo:
`GET /api/v1/points/coworking/bookings-for-date/` with `slack_user_id` from the
verified event and a strict ISO `date`. Set Roo's `MLAI_BACKEND_URL` and dedicated
`ROO_API_KEY` to match the backend. Generic/internal credential fallback is
not used. Keep Public Roo and Admin Roo credentials separate.

Use Roo's existing Events API subscription to `app_mention` (and `message.im`
for DMs), with `chat:write` for private replies. **No new slash command needs
registration.** `/coworking-today` is retired; if a stale registration still
exists, it returns private guidance only. Remove that registration during an
authorised rollout if it was previously added.

Signed requests, event receipts and deployment context allowlists remain in
force. Admin Roo still limits access to allowed channels and DM users. The bot
mention must match the bot ID obtained from that deployment's Slack identity.
The channel's `app_mention` event owns delivery; its duplicate `message` event
is ignored so the same request does not also enter AI routing.

Roo's existing event lease requests Slack retries until background processing
finishes. Successful delivery completes the receipt; a failed private delivery
releases it for retry. No failure falls back to a public reply or AI. Private
Slack messages are ephemeral and their delivery is not guaranteed by Slack.

The tracked manifest contains legacy HTTP addresses; verify the selected app's
HTTPS Events API URL during rollout, and do not copy a legacy address or guess
a TLS hostname. This change has not deployed or updated any live Slack app.

## Validation

From `roo-standalone`, with synthetic credentials only:

```sh
SLACK_BOT_TOKEN=xoxb-test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
  .venv/bin/python -m pytest roo/tests/test_coworking_snapshot.py \
  roo/tests/test_coworking_snapshot_mentions.py roo/tests/test_slack_security.py \
  roo/tests/test_surface_security.py -q
```

After the backend repository's required approval for disposable migrations,
run its `tests.test_coworking_snapshot` plus coworking service, API and report
regressions. No new migration or model change is required.

Before enabling in a real workspace, verify with authorised staging fixtures:

- Admin channel and thread requests get private active-booking lists.
- Non-admins and deactivated admins get only private denial.
- Wrong-bot mentions, duplicate message events and retries do not leak or
  duplicate results; no AI processing occurs for matched commands.
- Invalid arguments show usage; empty dates say no active bookings.
- Allowlisted Admin Roo channels/DMs work; other contexts are ignored.
- Backend and delivery failures never produce a public result.
- Ordinary coworking reports still work independently.

Slack documents private replies and the requirement for an existing active
thread in [chat.postEphemeral](https://docs.slack.dev/reference/methods/chat.postEphemeral/).
Staging delivery checks remain necessary. To roll back this entry point, revert
the mention-routing change; the read-only backend endpoint can remain deployed.
