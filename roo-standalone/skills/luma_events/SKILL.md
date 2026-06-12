---
name: luma-events
description: Report on Luma event registrations and export attendee CSV files for recent MLAI events
# Keep keywords high-precision. Bare generic nouns (csv, attendees, guests,
# registered) used to capture any message containing them ("inspect the CSV I
# uploaded…"); anything less specific now goes through the LLM router.
trigger_keywords:
  - luma
  - guest list
  - attendee list
  - attendee csv
  - attendee report
  - export guests
  - export attendees
  - past csv documents
  - mlai events
  - recent events
routing:
  use_when: >
    The user wants DATA about who signed up for / attended MLAI events on Luma:
    registration counts, attendee lists or reports, check-in numbers, or CSV exports.
  avoid_when: >
    Questions about an event's content, time, or topic (chat). Coworking attendance
    (mlai-points coworking_report). Files the user uploaded themselves.
  examples:
    - {text: "how many people registered for the april 29 event", action: attendee_report}
    - {text: "who's coming to the patient-data workshop?", action: attendee_report}
    - {text: "export the attendee list as a csv", action: export_attendee_csv}
    - {text: "how many signed up for thursday's event?", action: attendee_report}
  negative_examples:
    - {text: "what's the topic for this week's meetup?", instead: respond_in_chat}
    - {text: "can you inspect the CSV I uploaded and tell me the columns?", instead: respond_in_chat}
    - {text: "how many people used the coworking space this week", instead: mlai-points}
actions:
  - name: attendee_report
    description: Report registration/check-in counts for recent or specific events.
    params:
      event_count: {type: integer, description: "How many recent events (default 3)."}
      event_date: {type: string, description: "Specific event date if given."}
  - name: export_attendee_csv
    description: Same report but explicitly uploading attendee CSV files.
    params:
      event_count: {type: integer}
      event_date: {type: string}
      include_csv: {type: boolean, description: "Set true."}
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
