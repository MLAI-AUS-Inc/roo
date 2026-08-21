---
name: meeting-room-booking
description: Manage Meeting Room bookings
routing:
  use_when: >
    Meeting Room requests.
  avoid_when: >
    Coworking, events, calendars, or booking others.
  examples:
    - {text: "room free tomorrow 2pm?", action: check_room_availability}
    - {text: "book room tomorrow 2pm-4pm", action: book_meeting_room}
    - {text: "my room bookings", action: list_my_room_bookings}
  negative_examples:
    - {text: "book coworking tomorrow", instead: mlai-points}
actions:
  - name: check_room_availability
    description: Check times.
    params:
      date: {type: string}
      start_time: {type: string}
      end_time: {type: string}
  - name: book_meeting_room
    description: Confirm booking.
    params:
      date: {type: string}
      start_time: {type: string}
      end_time: {type: string}
      duration_hours: {type: integer}
  - name: list_my_room_bookings
    description: List mine.
  - name: cancel_meeting_room
    description: Choose cancellation.
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
