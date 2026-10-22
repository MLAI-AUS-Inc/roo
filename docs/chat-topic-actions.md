# Topic actions from MLAI Chat

`POST /api/chat-actions` is a Public Roo-only internal endpoint authenticated with
`INTERNAL_MENTION_API_KEY`, the existing `/api/mention` service credential. It
accepts workspace/channel/message/thread IDs and the verified member's Slack ID.
For `perform: true`, it additionally accepts an allowlisted action ID and SHA-256
digest of the source blocks/action value. The backend derives the member identity
and checks active Slack consent and conversation visibility before calling.

Roo fetches the exact current source message with its own Slack client. Workspace
and bot authorship must match `auth.test`; DM counterpart must match the actor.
Only `confirm_topic_btn_<index>`, `cancel_topic_btn` and
`select_article_delivery_mode` are accepted, with a matching fresh digest. URL
buttons and buttons requiring Slack confirmation dialogs are not dispatched.
The same handler as `/slack/actions` performs requester/delegation checks and
updates Slack, preserving the established content flow. The Slack-signed route
still requires its signature dependency and replay check.

`perform: false` returns only the current display fields and never dispatches an
action. Tokens, response URLs and caller-provided values are not forwarded. Test
with `roo/tests/test_chat_actions.py` and the existing article-flow tests using
synthetic credentials and mocked clients. Release this endpoint before enabling
the companion backend and Chat clients; no migration is needed.
