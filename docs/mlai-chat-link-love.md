# MLAI Chat link-love replies

Public Roo verifies bot-marked thread replies only in the explicitly configured
`BOOST_LINK_LOVE_CHANNEL_ID` when `BOOST_LINK_LOVE_ENABLED` is enabled. The signed
Slack envelope, deployment allowlists and durable event leases remain required.
Other bot traffic retains its existing routing restrictions.

The backend's existing Roo-key-only actor endpoint accepts `purpose=link_love`
for the matching `ROO_LINK_LOVE_SLACK_CHANNEL_ID` (MLAI's boost channel by default).
It checks the exact completed Chat-to-Slack delivery, public channel mapping,
thread, current verified device, account, consent, and original posting authority.
It returns the canonical profile and original reply without requiring a Roo
mention or a Slack account. That result enters only the reward handler.

The award store uses `profile:<uuid>` in its existing recipient field for these
members; retry records need no schema change. The backend system award API
accepts `target_profile_id` only with Roo's credential, exactly two points,
`link-love`, a stable operation key and a currently verified consenting account.
Chat and Slack awards normalize to one canonical ledger key per member/post,
while retaining existing historical Slack keys. A member's own root is ineligible.
Notifications display an escaped account name with `(MLAI Chat)`.

Deploy the backend before Roo. No new migrations or Slack scopes are required.
Deployment does not replay ignored historical replies. A bounded replay needs
explicit operator authorization and must use the original message identity and
normal verified reward path; never award the bridge bot or infer identity from
the visible author header.

From `roo-standalone`, run offline tests with synthetic credentials:

```sh
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au .venv/bin/python -m pytest \
  ../tests/roo/test_chat_link_love.py ../tests/roo/test_link_love.py -q
```
