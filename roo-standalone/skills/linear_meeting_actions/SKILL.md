---
name: linear-meeting-actions
description: Extract action items from Slack meeting transcripts, summaries, files, PDFs, DOCX documents, or images and create correctly assigned Linear issues
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
routing:
  use_when: >
    The user wants Linear issues/tickets/tasks created or extracted — from meeting
    notes, transcripts, summaries, action items, attached files (PDF/DOCX/images),
    the current thread, or a direct request ("add a task to linear to fix X").
  avoid_when: >
    MLAI community tasks worth points (mlai-points). How-to questions about Linear
    itself. The word "task"/"action items" without any Linear destination.
  examples:
    - {text: "turn this meeting summary into Linear tasks", action: create}
    - {text: "add a task to linear to fix the login bug", action: create}
    - {text: "extract action items from this transcript and add them to Linear", action: create}
    - {text: "send this attached PDF to Linear as tasks", action: create}
  negative_examples:
    - {text: "create a task called fix docs worth 5 points", instead: mlai-points}
    - {text: "how do I write a good linear ticket?", instead: respond_in_chat}
actions:
  - name: create
    description: Extract candidate action items (from the message, thread, or files) and create Linear issues.
    params:
      project_hint: {type: string, description: "Linear project name/slug if mentioned."}
      team_hint: {type: string, description: "Linear team name/key if mentioned."}
  - name: extract
    description: Only extract/preview action items without creating issues yet.
    params:
      project_hint: {type: string}
      team_hint: {type: string}
  - name: approve
    description: Approve previously previewed uncertain items (usually a button follow-up).
  - name: reject
    description: Reject previously previewed uncertain items.
---

# Linear Meeting Actions Skill

This skill turns pasted Slack meeting transcripts, summaries, supported Slack files, PDFs, DOCX documents, and images into Linear issues. It reads Linear context first, extracts action items, maps each item to the right owner and project, creates high-confidence issues, and asks for Slack review when assignment or project confidence is uncertain.

## Capabilities

- Parse Slack message/thread text and supported attached files for concrete to-do items.
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
4. Match owners by Slack mention/email first, then Linear user email, then display/name similarity.
5. Match projects by explicit project hint, name/slug similarity, and project membership context.
6. Create only high-confidence, non-duplicate candidates.
7. Present uncertain candidates in Slack with Approve and Reject buttons.
8. Report created issues, skipped duplicates, and unresolved items clearly.

## Creation Rules

- Do not create an issue without a matched Linear team.
- Do not auto-create an issue without a high-confidence assignee and project.
- Apply the `meeting-action` label if it already exists. If it does not exist, create the issue without labels.
- Issue descriptions must include the meeting context, evidence snippet, Slack source identifiers when available, and a note that Roo generated the issue from meeting notes.

## Response Style

Keep Slack responses concise. Lead with created Linear links, then list review-needed or skipped items.
