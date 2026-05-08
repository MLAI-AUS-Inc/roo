---
name: linear-meeting-actions
description: Extract action items from Slack meeting transcripts, summaries, files, PDFs, DOCX documents, or images, create Linear project updates, and create correctly assigned Linear issues
trigger_keywords:
  - meeting actions
  - meeting action items
  - meeting notes to linear
  - meeting summary to linear
  - transcript to linear
  - pdf to linear
  - docx to linear
  - image to linear
  - file to linear
  - linear tasks from meeting
  - create linear tickets from transcript
  - extract action items
  - sync meeting notes to linear
requires_auth: true
parameters:
  - action: One of extract, create, approve, or reject. Defaults to create for transcript-to-Linear requests.
  - transcript: Pasted meeting transcript, notes, or summary text. If omitted, use the current Slack thread history and supported attached files.
  - project_hint: Optional Linear project name or slug mentioned by the user.
  - team_hint: Optional Linear team name or key mentioned by the user.
  - confirm_create: Boolean flag used by Slack approval actions for uncertain items.
---

# Linear Meeting Actions Skill

This skill turns pasted Slack meeting transcripts, summaries, supported Slack files, PDFs, DOCX documents, and images into Linear issues. It reads Linear context first, extracts action items, maps each item to the right owner and project, creates high-confidence issues, and asks for Slack review when assignment or project confidence is uncertain.

## Capabilities

- Parse Slack message/thread text and supported attached files for concrete to-do items.
- Create a Linear project update when the request explicitly asks for a project update and Roo can confidently match the project.
- Download and parse `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.png`, `.jpg`, `.jpeg`, `.webp`, and non-animated `.gif` files from the current Slack thread.
- Inspect Linear teams, users, active projects, project members, labels, and recent open issues.
- Assign new issues to the best matching Linear user.
- Attach new issues to the best matching Linear project and team.
- Avoid likely duplicate open issues.
- Ask for Slack approval when an action item is useful but not confident enough to create automatically.

## Parameters

- **action**: One of `extract`, `create`, `approve`, or `reject`. Defaults to `create`.
- **transcript**: Pasted meeting transcript, notes, or summary text. If omitted, inspect the current Slack thread and attached files.
- **project_hint**: Optional Linear project name or slug.
- **team_hint**: Optional Linear team name or key.
- **confirm_create**: Internal boolean used for Slack approval actions.

## Workflow

1. Build the source text from the current Slack message, recent thread history, and supported attached files. V1 does not proactively cache files or read Canvases.
2. Read Linear before writing:
   - Teams
   - Users
   - Active projects with teams and members
   - Issue labels
   - Recent open issues for duplicate detection
3. Extract concrete action items into structured candidates with title, description, owner hint, project hint, due date, priority, evidence, source label, and confidence.
4. If a project update was requested, create one for the matched Linear project.
5. Match owners by Slack mention/email first, then Linear user email, then display/name similarity.
6. Match projects by explicit project hint, name/slug similarity, and project membership context.
7. Create only high-confidence, non-duplicate candidates.
8. Present uncertain candidates in Slack with Approve and Reject buttons.
9. Report created project updates, created issues, skipped duplicates, and unresolved items clearly.

## Creation Rules

- Do not create an issue without a matched Linear team.
- Do not auto-create an issue without a high-confidence assignee and project.
- Apply the `meeting-action` label if it already exists. If it does not exist, create the issue without labels.
- Issue descriptions must include the meeting context, evidence snippet, Slack source identifiers when available, and a note that Roo generated the issue from meeting notes.

## Response Style

Keep Slack responses concise. Lead with created Linear links, then list review-needed or skipped items.
