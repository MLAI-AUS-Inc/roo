# Finding meeting-room times

Members can ask Roo in Slack:

- `what times are the meeting rooms available tomorrow?`
- `find me a two-hour meeting room slot tomorrow`
- `when is Lovelace free for 2 hours tomorrow?`

Roo replies privately with available **start times** grouped by Turing and
Lovelace, or just the explicitly named room. Ranges are inclusive and use
30-minute steps. With no duration, Roo shows one-hour meetings; members can
request exactly 1 or 2 hours. A range of `9:00 AM to 11:00 AM` for a one-hour meeting
includes starts at 9:00, 9:30, 10:00, 10:30 and 11:00, finishing by noon.

Dates and times use Melbourne time. The day search uses the existing backend
availability endpoint once per room. It subtracts bookings and administrative
blocks, excludes started times and meetings that would extend beyond the queried
day, and skips ambiguous daylight-saving hours. It does not impose business
hours that the backend has not configured; closures must be room blocks.

These are room openings, not a reservation or a guarantee of a member's balance
or daily allowance. To choose a slot, ask Roo to book the named room on the date
at the chosen time **with the same duration**, for example:
`book Lovelace on 2026-09-15 at 9am for 2 hours`.
Roo checks that specific interval before its normal private preview. The backend
rechecks availability and eligibility when the member clicks Confirm booking.

The availability search itself needs no database change. The room rename uses
backend migration `0042_rename_meeting_rooms`. Default discovery uses
only Turing and Lovelace; the Conference Room access feature is a separate change.

Regression coverage is in `roo/tests/test_meeting_room_availability.py` and
`roo/tests/test_meeting_room_booking.py`, including duration filtering, blocks,
midnight boundaries, DST transitions, private delivery and rechecking a chosen
slot. Routing examples are in `roo/routing_eval/cases/meeting_room_booking.yaml`;
the deterministic routing gate does not substitute for a live model evaluation.

New bookings and duration-filtered searches reject 90 minutes. Existing confirmed
90-minute bookings remain visible and can be cancelled normally; they are not
resized or charged again. Deploy the matching backend duration guard to enforce
this rule for old confirmation buttons and direct API requests as well.

## Room names

Lovelace was formerly the Small Meeting Room; Turing was formerly the Big
Meeting Room.
Roo accepts both the new names and the old size-based aliases. The stable API
slugs remain `small-meeting-room` (Lovelace) and `big-meeting-room` (Turing).

Examples: `book Lovelace tomorrow at 2pm` and `is Turing free tomorrow?`.
