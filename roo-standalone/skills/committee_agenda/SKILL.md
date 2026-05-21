---
name: committee-agenda
description: Capture user requests to add items to the MLAI committee meeting agenda and post them to the dedicated agenda Slack channel.
trigger_keywords:
  - agenda
  - add to agenda
  - committee agenda
  - meeting agenda
  - next meeting
  - bring up at the meeting
  - raise at the meeting
  - put on the agenda
  - agenda item
  - agenda complete
  - agenda done
  - mark agenda complete
  - close agenda item
  - remove agenda item
  - delete agenda item
  - agenda cleanup
  - clean up completed agenda
  - remove completed agenda items
  - archive completed agenda items
---

# Committee Meeting Agenda Skill

This skill lets any community member ask Roo to add an item to the next committee
meeting agenda. Roo extracts a short title and description from the message
(and surrounding thread if relevant), then posts a structured agenda card to
the `#committee-agenda` channel where committee members triage it.

Seconds are tallied directly from emoji reactions on the posted card; there is
no separate database. Slack itself is the source of truth.

## Capabilities

- **Add** an agenda item from a natural-language request like
  "@Roo add to agenda: discuss venue for next hackathon".
- Pull the description from the thread the user is replying in, when the
  request itself only gives a title.
- Capture the proposer (the requesting Slack user) automatically.
- Post a formatted card to the dedicated agenda channel with proposer mention,
  timestamp, and a permalink back to the originating message when available.
- Reply to the user with a confirmation and a link to the posted card.
- **Complete** an agenda item when a committee member replies to its thread
  with `@Roo agenda complete` (or "mark agenda complete", "agenda done",
  "close agenda item"). Roo edits the card header to "✅ Completed agenda
  item" and posts a completion note in-thread.
- **Remove** an agenda item entirely with `@Roo remove agenda item` /
  `@Roo delete agenda item` in the item's thread. Same gating as complete.
- **Cleanup**: `@Roo agenda cleanup` or `@Roo remove completed agenda items`
  scans the agenda channel and deletes every item already marked complete.
  Admin-only.

## Parameters

- **action**: One of `add`, `complete`, `cleanup` (default: `add`). Set by
  routing; rarely supplied by the user directly.
- **title**: Short summary of the agenda item, used by `add` (max 120 chars).
- **description**: Longer context for the committee, used by `add` (optional).
- **urgency**: One of `low`, `normal`, `high` (default: `normal`), used by `add`.
- **remove**: Boolean. If true with `action=complete`, deletes the agenda
  message instead of marking it ✅ Completed. Set by routing when the user
  says "remove/delete agenda item".
- **reason**: Optional free-text reason the completer can supply, shown in
  the completion note.

## Workflow

### Step 1: Extract the request
From the user's message (and thread history if needed), build:
- A concise `title` (imperative or topic-style, no leading "add to agenda:" filler)
- A `description` capturing useful context — quote thread text if relevant
- An `urgency` inferred only if the user explicitly signalled it

### Step 2: Post to the agenda channel
Call `CommitteeAgendaClient.submit_item(...)` with the extracted fields plus
the requester's Slack user ID and (if available) a permalink back to the
originating message. The client posts a Block Kit card to the configured
agenda channel and returns the posted message's `ts` and a permalink.

### Step 3: Confirm to the user
Reply with a short confirmation including the agenda channel and a link to
the posted item. Encourage other members to react with `:+1:` to second.

## Response Style

- Friendly and brief, Aussie flavour where it fits.
- Always tell the user *where* the item went so they can follow up.
- If the channel isn't configured, explain clearly what an admin needs to do
  (set `COMMITTEE_AGENDA_CHANNEL_ID`) rather than failing silently.

## Permissioning

- `add` is open to any community member.
- `complete` and `remove` (for an individual item) require the requester to
  hold an MLAI Points admin role (`admin`, `committee`, or `portfolio_lead`).
- `cleanup` requires the same admin role.

If a non-admin asks for `complete` or `cleanup`, Roo explains they need
committee permission rather than failing silently.

## Examples

> User: `@Roo add to agenda: budget for new whiteboards in the coworking space`
>
> Roo: `Done! I've added "Budget for new whiteboards in coworking space" to the committee agenda in <#C012ABCDEF|committee-agenda>. React with :+1: to second it. 👍`

> Committee member (in agenda item thread): `@Roo agenda complete`
>
> Roo: `Agenda item marked complete by @alex.`

> Committee member: `@Roo remove completed agenda items`
>
> Roo: `Removed 4 completed agenda items from <#C012ABCDEF>.`
