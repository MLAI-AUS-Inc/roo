---
name: notion-agenda
description: Add meeting agenda items
requires_auth: true
# Rollout gate, and keeps the general catalog within its token budget.
exclusive_channels: [roo-testing]
routing:
  use_when: Adding an item to a Notion meeting agenda.
  avoid_when: Room bookings or Linear tasks.
  examples:
    - {text: "add budget review to tomorrow's agenda", action: add_agenda_item}
    - {text: "put merch on the agenda for Monday's meeting", action: add_agenda_item}
    - {text: "agenda item for 27 Oct: partner events", action: add_agenda_item}
  negative_examples:
    - {text: "book a room for tomorrow's meeting", instead: meeting-room-booking}
actions:
  - name: add_agenda_item
    description: Add one item.
    params:
      item: {type: string}
      date: {type: string}
---

# Notion Agenda

Add one bullet to the agenda of the dated MLAI Meeting Notes page in Notion,
creating the page from the template if it doesn't exist. The backend owns the
Notion credential, the committee-member check, the write and its verification;
Roo never holds a Notion token.

`item` is the agenda text only, without the date or "add to the agenda".
`date` is the meeting date phrase as written (for example "tomorrow" or
"27 Oct"). Roo resolves it from the Slack message time in Australia/Melbourne,
and asks when the date is missing or unclear.

Reply "Added" only when the backend confirms the item was verified on the page.
Never edit or remove existing agenda items.
