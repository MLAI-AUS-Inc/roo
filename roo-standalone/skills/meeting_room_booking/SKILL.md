---
name: meeting-room-booking
description: Manage MLAI Meeting Room bookings
routing:
  use_when: >
    A member wants Meeting Room availability or bookings.
  avoid_when: >
    Coworking (mlai-points), events, calendars, or booking for others.
  examples:
    - {text: "is the meeting room free tomorrow at 2pm?", action: check_room_availability}
    - {text: "book the meeting room tomorrow from 2pm to 4pm", action: book_meeting_room}
    - {text: "show my meeting room bookings", action: list_my_room_bookings}
  negative_examples:
    - {text: "book me in for coworking tomorrow", instead: mlai-points}
actions:
  - name: check_room_availability
    description: Check availability.
    params:
      date: {type: string}
      start_time: {type: string}
  - name: book_meeting_room
    description: Prepare booking confirmation.
    params:
      date: {type: string}
      start_time: {type: string}
      duration_hours: {type: integer}
  - name: list_my_room_bookings
    description: List bookings.
  - name: cancel_meeting_room
    description: Select a booking to cancel.
    params:
      date: {type: string}
---

# Meeting Room Booking

Use this skill only for the hourly MLAI Meeting Room. Coworking is a separate,
full-day booking flow in `mlai-points`.

## Rules

- Treat every date and time as Australia/Melbourne.
- Ask for a missing date or start time. Do not invent one.
- If the member gives a start but no duration or end, use one hour.
- Starts and ends must be on the hour. The backend enforces all final limits.
- Never book for a tagged user or use a model-provided Slack identity.
- Never claim a slot is reserved before the member clicks Confirm booking.
- Keep availability, bookings, balances, and cancellation details private.
- Public requests receive only a short acknowledgement after the private DM succeeds.
- Do not support titles, attendees, recurrence, calendar invitations, or reminders.
