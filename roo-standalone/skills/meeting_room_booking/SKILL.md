---
name: meeting-room-booking
description: Book rooms
routing:
  use_when: >
    Meeting Room requests.
  avoid_when: >
    Coworking, events, Google/Outlook sync, attendees, or unauthorized bookings.
  examples:
    - {text: "room calendar tomorrow?", action: check_room_availability}
    - {text: "book room tomorrow 2pm-4pm", action: book_meeting_room}
    - {text: "book <@U123> room tomorrow 1.5h", action: book_meeting_room}
    - {text: "my room bookings", action: list_my_room_bookings}
    - {text: "cancel room tomorrow", action: cancel_meeting_room}
  negative_examples:
    - {text: "book coworking tomorrow", instead: mlai-points}
actions:
  - name: check_room_availability
    description: Check times.
    params:
      room: {type: string}
      date: {type: string}
      start_time: {type: string}
      end_time: {type: string}
  - name: book_meeting_room
    description: Book.
    params:
      room: {type: string}
      date: {type: string}
      start_time: {type: string}
      end_time: {type: string}
      duration_hours: {type: number}
  - name: list_my_room_bookings
    description: List mine.
  - name: cancel_meeting_room
    description: Cancel.
    params:
      room: {type: string}
      date: {type: string}
---

# Meeting Room Booking

Use this skill only for the hourly MLAI meeting rooms. Coworking is a separate,
full-day booking flow in `mlai-points`.

## Rules

- Treat every date and time as Australia/Melbourne.
- Treat `tomorow`, `tommorow`, and `tommorrow` as `tomorrow`.
- If an availability check or booking gives no date, use the next Melbourne
  calendar day. If it gives a vague or invalid date, ask for an explicit date.
- The active choices are `Small Meeting Room` and `Big Meeting Room`. Treat
  `large room` as the Big Meeting Room.
- Derive an explicit room only from the member's message, never from a model-only
  parameter. If availability does not name a room, show both. If a booking does
  not name a room in a public channel, ask publicly in the same thread whether
  they want the Big or Small Meeting Room. Accept only that requester's room
  reply in that thread, then continue privately. In a DM, use room-choice buttons.
- Ask for a missing booking start time. Do not invent one.
- If the member gives a start but no duration or end, use one hour.
- Bookings last 1 to 2 hours and use 30-minute increments. Accept phrases such
  as `an hour and a half`, `1.5 hours`, and `90 minutes`.
- Starts and ends must be on the hour or half-hour. Each started hour costs one
  Roo Point, so a 90-minute booking costs 2 points.
- Full Points Admins may book one tagged member. Derive the target only from the
  Slack message, charge the tagged member, and never charge the administrator.
- Non-admins cannot book for tagged users. Never use a model-provided Slack identity.
- Never claim a slot is reserved before the member clicks Confirm booking.
- Keep availability, bookings, balances, and cancellation details private.
- Public room-choice prompts may name the Big and Small Meeting Rooms. Keep the
  resolved time, availability, points, preview, confirmation, and cancellation
  details private; after a room reply, post only a short public acknowledgement
  once the private DM succeeds.
- Do not support titles, attendees, recurrence, calendar invitations, or reminders.

## Examples

- `book the meeting room tomorrow at 2pm for an hour and a half`
- `is either meeting room free tomorrow at 2pm?`
- `book the small meeting room tomorrow at 2pm for an hour`
- `book the large room tomorrow at 2:30pm for 90 minutes`
- `book the meeting room tomorrow from 2:30pm to 4pm`
- `book <@U123> into the meeting room tomorrow at 2pm for 90 minutes`
- `cancel my meeting room booking tomorrow`
