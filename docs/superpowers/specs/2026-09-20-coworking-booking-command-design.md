# Coworking booking command: approved conversational design

This records the short design approved in the conversation on 20 September 2026. It is a reference for the requested implementation plan, not a newly approved architectural proposal.

Admins need a quick list of people booked into the coworking space, separate from the existing coworking reports. “Checked in” means an active booking, not verified physical attendance.

- `/coworking-today` returns today's active bookings.
- `/coworking-today YYYY-MM-DD` returns active bookings for that date.
- Dates use `Australia/Melbourne`.
- Every response is private to the requester (`ephemeral`).
- Only active authorised admins can obtain the list.
- Show the selected date, the number of people booked, and their names.
- Exclude cancelled bookings; show an explicit empty result when nobody is booked.
- Use deterministic parsing, database lookup, and formatting, with no AI routing or generation.
- Keep this separate from existing coworking reports.
- No booking writes, arrival tracking, exports, schedules, or new dependencies are required.

Example:

> Coworking bookings · 21 September 2026  
> 3 people booked  
> Alice Smith  
> Ben Jones  
> Casey Lee

Implementation details proposed by the plan: a small dedicated read endpoint, strict ISO date validation, a bounded synchronous Slack response, and a narrow exception for this read-only command on the admin surface. These details are for review with the plan.
