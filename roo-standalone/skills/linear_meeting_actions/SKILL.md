---
name: linear-meeting-actions
description: Create Linear issues or project updates from Slack context, meeting notes, and files with project and assignee resolution
requires_auth: true
parameters:
  - action: One of extract, create, approve, or reject. Defaults to create for transcript-to-Linear requests.
  - transcript: Pasted meeting transcript, notes, or summary text. If omitted, use the current Slack thread or bounded recent channel context and supported attached files.
  - project_hint: Optional Linear project name or slug mentioned by the user.
  - owner_hint: Optional Linear assignee name, email, or Slack mention.
  - team_hint: Optional Linear team name or key mentioned by the user.
  - confirm_create: Boolean flag used by Slack approval actions for uncertain items.
routing:
  use_when: >
    The user wants Linear issues/tasks or project updates created from a direct
    request, nearby Slack context, meeting notes, a thread, or attached files.
  avoid_when: >
    MLAI points tasks (mlai-points), Linear how-to questions, or tasks without a
    Linear destination.
  examples:
    - {text: "add a task to linear to fix the login bug", action: create}
    - {text: "send this attached PDF to Linear as tasks", action: create}
    - {text: "add this as a task for me in Linear", action: create}
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
- For top-level commands such as "add this as a task", read a bounded slice of the preceding channel conversation on demand.
- Create a Linear issue directly from an explicit Slack command such as "create a to do item in Linear project X and assign to Y".
- Create an explicit, high-confidence contextual assignment in one step; keep discussion-derived follow-ups review-only.
- Create a Linear project update when the request explicitly asks for a project update and Roo can confidently match the project.
- Use the latest Linear project update and recent project issues as context for concise PDF/transcript-derived project updates.
- Download and parse `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.png`, `.jpg`, `.jpeg`, `.webp`, and non-animated `.gif` files from the current Slack thread.
- Inspect Linear teams, users, active projects, project members, labels, and recent open issues.
- Assign new issues to the best matching Linear user.
- Attach new issues to the best matching Linear project and team.
- Avoid likely duplicate open issues and make retries idempotent from Slack source evidence.
- Use the exact `gpt-5.6-sol` model through the OpenAI Responses API for every Linear inference stage.
- Select model reasoning effort from source length, source count, ambiguity, partial work, dependencies, artifacts, and conflicting context; never derive model effort from the XS/S/M/L/XL task label.
- Recover a timed-out meeting-note chunk once at smaller scope while keeping the overall extraction bounded and creating no partial Linear issues.
- For projects whose current Linear name starts exactly with `[Studio]`, estimate the remaining effort with the dedicated Studio rubric and apply exactly one compatible XS/S/M/L/XL label.
- Ask for Slack approval when an action item is useful but not confident enough to create automatically.

## Parameters

- **action**: One of `extract`, `create`, `approve`, or `reject`. Defaults to `create`.
- **transcript**: Pasted meeting transcript, notes, or summary text. If omitted, inspect the current Slack thread or bounded recent channel context and attached files.
- **project_hint**: Optional Linear project name or slug.
- **owner_hint**: Optional Linear assignee name, email, or Slack mention.
- **team_hint**: Optional Linear team name or key.
- **confirm_create**: Internal boolean used for Slack approval actions.

## Workflow

1. Build the source text from the current Slack thread or, for a top-level contextual reference, at most the configured recent channel window. Resolve speaker profiles, source-local timestamps, and the evidence permalink. Roo does not read other channels or proactively cache channel traffic.
2. Read Linear before writing:
   - Teams
   - Users
   - Active projects with descriptions/content, linked Slack channel, teams, and true members when the Linear schema supports them
   - Latest project update for each active project when available
   - Issue labels
   - Recent open issues for duplicate detection
3. For explicit direct issue commands, parse the command itself as the issue source, including project and assignee hints.
4. Otherwise, extract concrete action items into structured candidates with title, description, owner hint, project terms, due expression/date, explicit-commitment flag, evidence message timestamp, source label, and confidence.
5. If a project update was requested, summarize all parsed source chunks, compare against the latest project update context, and create a concise Linear project update for the matched project.
6. If no concrete action item is found but the user asked to add the thread context to Linear, draft one contextual issue and require Slack approval before creation.
7. Match owners by explicit requester/self-reference and Slack mention/email first, then unique Linear name/email-prefix match, then display/name similarity.
8. Match projects by explicit project hint, exact name/slug, linked Slack channel, project description/content/update/recent issues, and true project membership as a tie-breaker.
9. Resolve relative dates from the evidence message in the configured workspace timezone.
10. Suppress candidates that are already completed, cancelled, or duplicates.
11. For each resolved project whose current title starts exactly with `[Studio]`, read its bounded project updates, active work, terminal references, related issues, label registry, and labelled precedents. Estimate all candidates for the same project in one structured `gpt-5.6-sol` Responses API call.
12. Estimate remaining work rather than original scope: XS is up to 15 minutes, S is up to 1 hour, M is up to 2 hours, L is up to 3 hours, and XL is up to 5 hours or has substantial uncertainty. Work over 5 hours is XL and should be split.
13. Start direct cleanup at `low`, ordinary extraction and source summaries at `medium`, and contextual inference, project synthesis, and Studio sizing at `high`. Increase to `xhigh` when deterministic complexity signals warrant it.
14. Treat each extracted source chunk as one inference source; track total batch chunks separately so a long PDF does not artificially increase every chunk's reasoning effort.
15. Retry a structured parsing or workflow-contract failure at most once with the next reasoning level. Use `max` only when that single escalation follows an `xhigh` attempt.
16. If meeting-action inference times out, retry only the failed chunk once at smaller paragraph/page-aligned scope with bounded concurrency. Finish extraction and deduplication before any issue write; if recovery or the total extraction deadline fails, create nothing and say so explicitly.
17. Add the exact effort label and one-sentence rationale to the issue description and Slack preview. Never create an eligible Studio issue without exactly one valid effort label.
18. Send an idempotency fingerprint and preserve a Slack permalink in the Linear issue.
19. Present uncertain candidates in Slack with Approve and Reject buttons.
20. Report created project updates, created issues, skipped duplicates, and unresolved items clearly.

## Creation Rules

- Do not create an issue without a matched Linear team.
- Do not auto-create an issue without a high-confidence assignee and project.
- A contextual command can auto-create only when its source contains an explicit assignment/commitment and the assignee, project, team, and due date are unambiguous.
- Do not auto-create contextual discussion-thread issues; always request Slack approval first.
- Project updates are created immediately when explicitly requested and the project match is confident.
- Apply the compatible `meeting-action` label if it already exists.
- In Studio sizing `review` or `required` mode, apply exactly one of `Extra Small (XS)`, `Small (S)`, `Medium (M)`, `Large (L)`, or `Extra Large (XL)`. Fail closed if sizing context, structured output, or the compatible label is unavailable.
- Studio sizing `shadow` mode captures estimates without mutating issue labels or descriptions; `review` sends every Studio candidate for approval; `required` may auto-create only a high-confidence, context-sufficient assessment.
- Issue descriptions must include the context, evidence snippet, source message permalink/identifiers when available, and a note that Roo generated the issue from Slack context.

## Response Style

Keep Slack responses concise. Lead with created Linear links, then list review-needed or skipped items.
