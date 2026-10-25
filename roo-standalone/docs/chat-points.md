# Roo awards for members without Slack

A linked Points Admin can select a member in MLAI Chat's @ mention picker and
send `@Roo give @Member 4 points for helping at the event`. The recipient uses
their existing MLAI profile and wallet; they do not need a Slack account.

The command needs one positive whole-number amount followed by `points` or
`pts`, and a reason after `for`. Roo asks for a new, explicit selection when a
historical command contains only a display name. It never searches for a
recipient by name or creates a placeholder Slack account.

Beneficiaries must be selected before the first prose `for` after the amount.
Both `give @Member 4 points for helping` and `give 4 points to @Member for
helping` work. People mentioned in the reason describe the work and receive no
award. A `for` inside a bound member name or code example does not end the
beneficiary segment. Ambiguous beneficiary selections ask for clarification.
Overlapping names use the longest selected label once; selecting `Sam Donegan`
before the reason does not also select someone called `Sam` mentioned in the
reason. If a signed beneficiary selection is unavailable, Roo names that
unresolved selection alongside confirmed awards and asks the admin to check any
earlier outcome before another award. Unavailable people mentioned only in the
reason do not appear as unresolved beneficiaries.

## Trusted command context

The authenticated backend bridge actor endpoint returns version 2:

```json
{
  "version": 2,
  "posting_mode": "member",
  "actor_profile_id": "00000000-0000-4000-8000-000000000001",
  "user_id": "UADMIN",
  "workspace_id": "TWORKSPACE",
  "channel_id": "CCHANNEL",
  "message_id": "111.222",
  "thread_ts": "111.222",
  "bridge_user_id": "UADMIN",
  "source_event_id": "64 lowercase hex characters",
  "text": "<@UROO> give @Member 4 points for helping",
  "source_labels": ["Member"],
  "recipient_targets": [
    {
      "kind": "mlai_profile",
      "profile_id": "00000000-0000-4000-8000-000000000002",
      "display_name": "Member",
      "source_labels": ["Member"],
      "slack_user_id": null
    }
  ]
}
```

The backend validates the exact acknowledged bridge delivery, account, current
device and posting authority. A transport sender can equal the resolved human
only in a scoped version 2 `member` response. Existing `bot` responses still
require a different human actor. Unsupported versions, altered scope, malformed
UUIDs and malformed recipient lists fail closed. Legacy bot responses without
the version field remain compatible during rollout.

API-posted member mentions with only an `app_id` marker use the same verified
resolver; ordinary human Slack mentions without app markers keep their direct
path. Native clients supply signed `mlai-mention` tags for explicitly selected people.
Broadcast-expanded `p` tags and reply participants cannot authorize awards.
Roo converts the response into an immutable context that travels separately
from model parameters through agent and skill dispatch. The source text fixes
the amount and reason; the model cannot supply recipient IDs or substitute an
award payload. Original signed mention labels are preserved separately from
the member's current display name. The top-level label list includes unavailable
selected members, so revocation or target omission cannot turn their names into
an amount on retry. Labels and code examples are masked when
parsing amounts, so renaming a member or mentioning someone called `4 points`
cannot create an unintended award amount. Actors still need a linked Slack identity and full Points Admin
permission. This change supports Chat-only recipients.

## Awards and delivery

Roo calls the existing `/api/v1/points/admin/award/` endpoint with exactly one of
`target_profile_id` and `target_slack_id`. Canonical profile calls require an
`idempotency_key`. For Chat awards Roo derives one operation from the verified
source event and canonical recipient profile UUID. Each recipient keeps the same
operation if another selected account becomes unavailable or targets reorder
between retries. The backend checks canonical identity,
permission, self-awards, allowance and changed-payload replays atomically.

Roo checks the returned canonical recipient and ledger confirmation before
claiming success. Public outcomes contain the award and escaped member name,
without a wallet balance. Slack DMs are sent only to a real Slack identity in a
newly created award response. Chat-only recipients never trigger Slack linking,
email matching or Slack DMs.

Commands mixing canonical Chat targets with distinct Slack mentions ask for
separate award commands before crediting anyone. A canonical member's native
selection and matching verified Slack alias are recognized as the same person.
Only mentions in the beneficiary segment participate in this check. If an
original native selection becomes unavailable, its rendered Slack alias cannot
switch the same source command to a second kind of award operation; Roo asks
for a fresh explicit selection instead.
Code masking covers top-level backtick/tilde fences, space/tab indented code
lines and matching-length inline spans, including multiline spans. Unclosed
fences consume the remaining source; unmatched backtick runs hide the rest of
their line. Code examples cannot supply a missing prose award amount.

Public replies use a deterministic Slack `client_msg_id`. Failed or unconfirmed
posting releases the durable Slack event receipt. A retry submits the same
award operation, which the backend returns as a replay, then retries the public
outcome. A Slack `duplicate_message` response confirms prior delivery. No Roo
database schema change is required.

## Release and verification

Deploy the compatible backend actor and award contracts first, followed by Roo
and the client explicit mention metadata. Verify both verified member posting
and bridge bot fallback. A member who only has MLAI Chat should receive the
award exactly once in their existing home balance. Re-deliver the same command,
then simulate a public notification failure; both retries must retain the
original ledger entry.

Timeouts, retryable backend responses and malformed success confirmations also
leave the original command retryable. Roo sends a separate, deduplicated pending
acknowledgement until it can confirm the original operation. It does not ask the
admin to issue a new command for an uncertain award outcome.

Native introduction awards use the backend receipt pipeline and its separate
introduction gate. Roo's Slack introduction normalizer continues to ignore bot
posts so bridge echoes cannot earn a second introduction award. Activate the
native introduction channel and reward gate only after the backend collector
is configured. Historical recovery requires a bounded, reviewed canonical
introduction operation rather than blindly replaying an ambiguous old command.

Database-free tests:

```sh
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
python -m pytest roo/tests/test_chat_points_awards.py \
  roo/tests/test_bridge_mentions.py roo/tests/test_rate_card.py -q
```

The points request and private delivery suites include unrelated coworking
schema migration helpers. Run their individual award cases unless those
specific migrations have separate approval.
