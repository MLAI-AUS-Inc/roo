# MLAI Chat coworking replies

Public Roo's existing bearer-authenticated `POST /api/mention` supports an
optional `post_reply: true` for the MLAI Chat coworking bridge. Existing callers
retain the response-only behavior when this option is absent.

The option requires a UUID `request_id`, a Slack member `user_id`, public Slack
`channel_id` and `thread_ts`. The backend obtains the member from the signed
MLAI Chat sender's verified account link, posts/checkpoints the Slack root, and
passes a frozen-date command such as `Please book me in on 2026-09-08.`. This
uses Roo's existing coworking shortcut and booking checks.

Roo posts its result using its own Slack identity, the provided root thread and
a stable `client_msg_id`. `suppress_post` responses retain their normal delivery
behavior. Empty responses and Slack failures return 503 instead of a delivered
acknowledgement. Successful handling returns `reply_delivered: true`; this
acknowledges the reply, not a successful booking. The reply may explain that a
booking is unavailable or still pending.

The endpoint stays disabled on Admin Roo, and on Public Roo when
`INTERNAL_MENTION_API_KEY` is unset. The backend's
`ROO_INTERNAL_MENTION_API_KEY` must match this Public Roo service credential.
Deploy this endpoint before enabling the backend handoff. No administrative key
belongs in this flow. No live service or booking is used by the new tests.

Run `roo/tests/test_internal_mentions.py` and `roo/tests/test_slack_security.py`
with synthetic test settings to cover the reply contract, preserved bearer and
surface gates, and the existing coworking shortcut.
