# Article price confirmation

Roo reads article prices from `POST /api/v1/content/quote` using its existing
Content Factory service authentication. It sends the requesting founder's
`requested_by_slack_user_id` (or `slack_user_id`), the domain, and the same
`client_request_id` used for submission. Topic confirmation also sends
`source_run_id`; legacy source-only buttons do not require a domain lookup.
The endpoint must verify the actor and ownership and return an integer
`costPoints`. Authentication alone never authorizes a charge.

All article generation and topic-confirmation calls go through the client gate.
Before dispatch, Roo shows the returned price and a `confirm_article_price`
button. A bounded, process-local confirmation record retains the exact request,
requester, and price. The button carries an opaque token. It remains unusable
until successful Slack posting or updating binds it to that card's workspace,
channel, thread, and message timestamp. The acting user and all four origin
fields must match the saved record. A copied token cannot be rebound to a
different message. Submission uses the saved destination; click payloads cannot
change it. The confirmed request submits the unchanged
integer as `expected_cost_points`. Topic-less research keeps its existing
request contract; its later article confirmation goes through the article gate.

An HTTP 409 with `code: roo_points_quote_changed` refreshes the authoritative
quote and asks for another click. It never automatically resubmits at the new
price. The old token is invalidated. Other conflicts remain errors. Missing,
malformed, or unavailable quotes stop article admission without a price fallback.
Generic progress and onboarding messages make no local price or debit claims.

Confirmations expire after 30 minutes and the process retains at most 1,000.
Restarted or different workers cannot use an absent record and fail closed.
After an uncertain submission, retrying the same available confirmation retains
the original request key and expected price; backend payment and provider
idempotency remain authoritative. No database or schema change is introduced.

Chat reuses the existing bot-owned Slack message, action digest, conversation,
and actor checks. Its allowlist includes `confirm_article_price`. Coordinated
Chat backend/frontend allowlists must also support that action before rollout.

The release requires the backend quote endpoint and quote-aware generate/confirm
routes. Keep the coordinated pricing rollout held until backend and Roo checks
and the Chat action contract pass. This change does not enable deployment or
alter service credentials, Slack scopes, or production schemas.

Offline regression command (from `roo-standalone`, using test placeholders):

```bash
python -m pytest roo/tests/test_article_price_consent.py \
  roo/tests/test_content_factory_article_flow.py \
  roo/tests/test_mlai_backend_client.py roo/tests/test_chat_actions.py -q
```
