---
name: luma-events
description: Report on Luma event registrations and export attendee CSV files for recent MLAI events
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
  - registered
  - registrations
---

# Luma Events Skill

This skill lets Roo report on approved Luma guests for recent MLAI calendar events. CSV files are uploaded only when the user explicitly asks for CSVs, exports, guest lists, or attendee lists.

## Capabilities

- Ask mlai-backend for recent or date-specific Luma attendee reports.
- Summarize registration and check-in counts.
- Upload one CSV per event when the request explicitly asks for CSV/export/list output.

## Parameters

- **action**: The action to perform. Use `attendee_report` for count/report requests and CSV/export/list requests.
- **event_count**: Number of recent ended events to export. Defaults to 3. Use 1 for "latest event".
- **event_date**: Optional event date in `YYYY-MM-DD` for a date-specific report.
- **approval_status**: Luma guest approval status to export. Defaults to `approved`.
- **include_csv**: Only true when the user explicitly asks for CSVs, export, guest list, or attendee list output.

## Workflow

1. Send the request to mlai-backend with the requester Slack ID.
2. Let mlai-backend verify the requester role and call Luma.
3. Return a concise Slack summary for report/count prompts.
4. Decode and upload CSV payloads returned by mlai-backend only for explicit CSV/export/list prompts.

## Security

- Roo must not be configured with, print, or reveal the Luma API key.
- mlai-backend owns Luma API access and role enforcement.
- Never store attendee CSVs permanently.
