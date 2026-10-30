# Article price confirmation

Roo reads article prices from `POST /api/v1/content/quote` using its existing
Content Factory service authentication. It sends the requesting founder's
`requested_by_slack_user_id` (or `slack_user_id`), the domain, and the same
`client_request_id` used for submission for new generation. Topic confirmation
sends `source_run_id` and omits `client_request_id` unless an existing key is
explicitly provided. The backend resolves the source's persisted key after
verifying source ownership, company, payer, payment lineage and refund state.
This preserves an original paid price (including historical six-point debits)
and the stable key for research/deferred sources. Roo never substitutes a new
key for a source. Legacy source-only buttons do not require a domain lookup.
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
change it. Roo also retains the original text and blocks, including the price,
topic, button label, and opaque token. Before either a Slack or Chat confirmation
can submit, Roo reads the exact bot-owned message from Slack and compares both
that source and the click's message snapshot to the saved content. A digest of
an edited message is insufficient. Changed content invalidates the token and
requires a fresh quote and confirmation. Unavailable or unverified Slack source
messages stop submission. Only Slack-generated block IDs and default plain-text
emoji flags are normalized. The confirmed request submits the unchanged
integer as `expected_cost_points`. Topic-less research keeps its existing
request contract; its later article confirmation goes through the article gate.
Price-card posts and updates disable Slack's automatic URL parsing, so fallback
text remains comparable; updates also explicitly clear prior attachments.

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

The release requires the backend quote endpoint, quote-aware generate/confirm
routes, and the Chat action contract. On 30 October 2026, these backend contracts
were verified in successful production deployment `faaeac1`; the desktop/mobile
Chat action allowlists were verified on Chat main. Roo’s consent, identity, and
executor regressions pass against the current repository structure. Sam
authorised the admin merge and release. No service credentials, Slack scopes,
or production schemas are changed.

Offline regression command (from `roo-standalone`, using test placeholders):

```bash
python -m pytest ../tests/roo/test_article_price_consent.py \
  ../tests/roo/test_content_factory_article_flow.py \
  ../tests/roo/test_mlai_backend_client.py ../tests/roo/test_chat_actions.py -q
```
