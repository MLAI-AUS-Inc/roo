---
name: luma-events
description: Export Luma event attendee and guest lists as CSV files for recent MLAI events
trigger_keywords:
  - luma
  - attendees
  - attendee
  - guest list
  - guests
  - export guests
  - csv
  - csv documents
  - past csv documents
  - mlai events
  - recent events
---

# Luma Events Skill

This skill lets Roo export approved Luma guests for recent MLAI calendar events as CSV files in Slack.

## Capabilities

- Find the latest ended events from the MLAI Luma calendar.
- Export approved guests for one or more events.
- Upload one CSV per event to the Slack thread where the request was made.

## Parameters

- **action**: The action to perform. Use `export_attendees` for attendee, guest list, or CSV export requests.
- **event_count**: Number of recent ended events to export. Defaults to 3. Use 1 for "latest event".
- **approval_status**: Luma guest approval status to export. Defaults to `approved`.

## Workflow

1. Verify the requester is allowed to export attendee data.
2. Use the configured Luma calendar API key.
3. Fetch recent ended events from Luma.
4. Fetch approved guests for each selected event.
5. Build one CSV per event with guest identity, ticket, check-in, source, and registration-answer columns.
6. Upload the CSV files to Slack.

## Security

- Never print or reveal the Luma API key.
- Never store attendee CSVs permanently.
- Only allow Points Admin users with the `admin`, `committee`, or `partner` role.
