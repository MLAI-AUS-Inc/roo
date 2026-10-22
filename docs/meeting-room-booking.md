# Meeting-room booking contract

Default room selection and general availability show only **Big Meeting Room**
and **Small Meeting Room**, in public threads and in DMs. This remains true even
if a backend room-list response contains additional rooms. Model-generated room
parameters do not count as an explicit request.

A member can explicitly ask `book the conference room tomorrow at 2pm for an
hour`. Roo checks that member's access through backend availability before
asking for missing times, then uses the existing private preview and confirmed
booking flow. For admin bookings, both availability calls and the confirmation
carry the target member's identity. The backend owns eligibility and charging.

The Conference Room is never offered as a default alternative or a room-choice
button. Its confirmation button is supported only through an explicit request.
A backend `room_unavailable` error renders exactly:

> The Conference Room is unavailable.

This response is private and contains no eligibility reason, threshold, balance,
or instructions to earn more points. Cancellation and existing-booking recovery
continue through the normal owner-bound flows. Existing prices, durations,
Melbourne timezone handling, expiry and replay protections apply.

## Release dependency

Deploy the companion `mlai-backend` Conference Room access checks before this
Roo change. Configure the `conference-room` record using the backend's existing
Meeting Room admin after the protected backend is deployed; see its
`docs/meeting-room-booking.md`. Roo can safely deploy before the room record is
created: the protected backend returns the same unavailable message.

## Verification

Run the meeting-room booking, action and clarification suites, router catalog
and deterministic routing gates. All use synthetic data and mocked external
calls. The routing evaluation dataset includes explicit Conference Room booking,
availability and cancellation requests; live model routing evaluations require
separately authorized development credentials.
