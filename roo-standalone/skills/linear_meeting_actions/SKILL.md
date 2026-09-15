---
name: linear-meeting-actions
description: Create Linear tasks/updates from Slack or size tasks across a named Linear project
requires_auth: true
parameters:
  - action: One of extract, create, approve, reject, or size_project_issues. Defaults to create for transcript-to-Linear requests.
  - transcript: Pasted meeting transcript, notes, or summary text. If omitted, use the current Slack thread or bounded recent channel context and supported attached files.
  - project_hint: Optional Linear project name or slug mentioned by the user.
  - owner_hint: Optional Linear assignee name, email, or Slack mention.
  - team_hint: Optional Linear team name or key mentioned by the user.
  - confirm_create: Boolean flag used by Slack approval actions for uncertain items.
  - mode: For size_project_issues, missing_only (default) or replace_existing.
routing:
  use_when: >
    Create Linear tasks/updates from Slack context or files, or size/rescore a
    named project's tasks.
  avoid_when: >
    MLAI points tasks (mlai-points), Linear how-to questions, or tasks without a
    Linear destination.
  examples:
    - {text: "add a task to linear to fix the login bug", action: create}
    - {text: "send this attached PDF to Linear as tasks", action: create}
    - {text: "size all unsized tasks in Linear project Aaron AI", action: size_project_issues}
  negative_examples:
    - {text: "create a task called fix docs worth 5 points", instead: mlai-points}
actions:
  - name: create
    description: Create issues from the message, thread, or files.
    params:
      project_hint: {type: string, description: "Linear project name/slug if mentioned."}
      team_hint: {type: string, description: "Linear team name/key if mentioned."}
  - name: extract
    description: Preview action items without creating them.
    params:
      project_hint: {type: string}
      team_hint: {type: string}
  - name: approve
    description: Approve previewed uncertain items.
  - name: reject
    description: Reject previewed uncertain items.
  - name: size_project_issues
    description: Preview and confirm effort labels across one project.
    params:
      project_hint: {type: string, description: "Exact project name or slug."}
      mode: {type: string, enum: [missing_only, replace_existing], description: "Skip or rescore existing sizes."}
---

# Linear Meeting Actions Skill

This skill turns pasted Slack meeting transcripts, summaries, supported Slack files, PDFs, DOCX documents, and images into Linear issues. It reads Linear context first, extracts action items, maps each item to the right owner and project, creates high-confidence issues, and asks for Slack review when assignment or project confidence is uncertain.

## Capabilities

- Parse Slack message/thread text and supported attached files for concrete to-do items.
- For top-level commands such as "add this as a task", read a bounded slice of the preceding channel conversation on demand.
- Create a Linear issue directly from an explicit Slack command such as "create a to do item in Linear project X and assign to Y".
- Create explicit, high-confidence direct or bulk assignments in one step; keep preview-only, ambiguous, and discussion-derived follow-ups review-only.
- Create a Linear project update only when the request affirmatively asks for one and Roo can confidently match the project; honor explicit instructions to skip, omit, or not write an update.
- Use the latest Linear project update and recent project issues as context for concise PDF/transcript-derived project updates.
- Download and parse `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.png`, `.jpg`, `.jpeg`, `.webp`, and non-animated `.gif` files from the current Slack thread.
- Inspect Linear teams, users, active projects, project members, labels, and recent open issues.
- Treat the complete accessible team catalogue as an authorization boundary; the production Linear API key must use **All teams you have access to** so newly created teams are included automatically.
- Resolve an explicitly named destination against the full Linear project catalogue when it is absent from the active-project snapshot.
- Assign new issues to the best matching Linear user.
- Attach new issues to the best matching Linear project and team.
- Consolidate paraphrased action items that produce the same deliverable, preserve genuinely separate work, avoid likely duplicate open issues, and make retries idempotent from Slack source evidence.
- Use the exact `gpt-5.6-sol` model through the OpenAI Responses API for every Linear inference stage.
- Select model reasoning effort from source length, source count, ambiguity, partial work, dependencies, artifacts, and conflicting context; never derive model effort from the XS/S/M/L/XL task label.
- Recover a timed-out meeting-note chunk at progressively smaller scope while keeping the overall extraction bounded and creating no partial Linear issues.
- For every resolved project, estimate remaining effort in bounded batches and apply exactly one compatible XS/S/M/L/XL label to each new non-terminal issue.
- Preview effort labels for every eligible active issue in one named project and apply them only after the requester confirms.
- Persist uncertain proposals as a requester-bound backend batch before showing compact Slack review controls, including Approve all and Reject all.
- Accept an untagged `approve all`/`reject all`-style reply only from the original requester inside Roo's still-active pending-review thread.

## Parameters

- **action**: One of `extract`, `create`, `approve`, `reject`, or `size_project_issues`. Defaults to `create`.
- **transcript**: Pasted meeting transcript, notes, or summary text. If omitted, inspect the current Slack thread or bounded recent channel context and attached files.
- **project_hint**: Optional Linear project name or slug.
- **owner_hint**: Optional Linear assignee name, email, or Slack mention.
- **team_hint**: Optional Linear team name or key.
- **confirm_create**: Internal boolean used for Slack approval actions.
- **mode**: For project backfills, use `missing_only` by default or `replace_existing` only when the requester explicitly asks to rescore existing labels.

## Workflow

1. Build the source text from the current Slack thread or, for a top-level contextual reference, at most the configured recent channel window. Resolve speaker profiles, source-local timestamps, and the evidence permalink. Roo does not read other channels or proactively cache channel traffic.
2. Read Linear before writing:
   - Teams
   - Users
   - Active projects with descriptions/content, linked Slack channel, teams, and true members when the Linear schema supports them
   - Latest project update for each active project when available
   - Issue labels
   - Recent open issues for duplicate detection
   - The backend's required-team access check. If it reports incomplete access,
     stop before extraction or writes and explain that an admin must re-authorize
     Roo's read/write credential for all required teams.
3. For explicit direct issue commands, parse the command itself as the issue source, including project and assignee hints.
4. Otherwise, extract concrete action items into structured candidates with title, description, owner hint, project terms, due expression/date, explicit-commitment flag, evidence message timestamp, source label, and confidence.
5. Resolve the request-level project once and reuse that canonical match for both the project update and every extracted item. Compare namespace-independent names so a source such as `Project Acquire` can match `[Studio] Project Acquire`; never let the update and its tasks silently diverge to different projects.
6. If a project update was affirmatively requested and not explicitly negated, summarize all parsed source chunks, compare against the latest project update context, and create a concise Linear project update for the canonical matched project.
7. If no concrete action item is found but the user asked to add the thread context to Linear, draft one contextual issue and require Slack approval before creation.
8. Match owners by explicit requester/self-reference and Slack mention/email first, then unique Linear name/email-prefix match, then display/name similarity.
9. Match projects by explicit project hint, exact full or namespace-independent name/slug, linked Slack channel, project description/content/update/recent issues, and true project membership as a tie-breaker. If an explicit name is not an exact active-snapshot match, resolve it against the full live project catalogue before extraction; use a unique exact/strong match and fail closed on ambiguity or absence.
10. Select only a team that is both attached to the resolved project and present in Roo's complete accessible-team catalogue. If the project team is absent, stop that item and report that Roo's Linear API key cannot access the team; never substitute the configured default team.
11. Resolve relative dates from the evidence message in the configured workspace timezone.
12. Suppress candidates that are already completed or cancelled. Consolidate candidates that share the same action family, object, owner, project, and outcome before effort sizing; do not merge distinct setup, evaluation, implementation, documentation, outreach, or scheduling deliverables merely because their wording overlaps.
13. For each resolved project, read its bounded project updates, active work, terminal references, related issues, label registry, and labelled precedents. Estimate candidates in bounded groups of three with structured `gpt-5.6-sol` Responses API calls.
14. Estimate remaining work rather than original scope: XS is up to 15 minutes, S is up to 1 hour, M is up to 2 hours, L is up to 3 hours, and XL is up to 5 hours or has substantial uncertainty. Work over 5 hours is XL and should be split.
15. Start direct cleanup at `low`, ordinary extraction and source summaries at `medium`, and contextual inference, project synthesis, and effort sizing at `high`. Increase to `xhigh` when deterministic complexity signals warrant it.
16. Treat each extracted source chunk as one inference source; track total batch chunks separately so a long PDF does not artificially increase every chunk's reasoning effort.
17. Retry a structured parsing or workflow-contract failure at most once. For a missing effort-sizing structured response, lower reasoning effort and increase the output allowance so the model can emit the full schema; for other validation failures, use the next reasoning level.
18. If meeting-action inference times out, keep that worker slot and retry only the failed chunk at progressively smaller paragraph/page-aligned scope. Cancel queued work immediately when bounded recovery is exhausted. Finish extraction and deduplication before any issue write; if recovery or the total extraction deadline fails, create nothing and say so explicitly.
19. Discard duplicate or unrecognized optional evidence references without rejecting an otherwise valid effort assessment. Normalize its rationale to one sentence and, when duration-based, add the rubric's canonical time anchor if the model omitted one.
20. If an effort-sizing batch still fails its required candidate or rubric fields, retry its candidates individually and preserve successful assessments. Add the exact effort label and normalized one-sentence rationale to each new issue description and Slack preview. Never create an eligible project issue without exactly one valid effort label.
21. Send an idempotency fingerprint and preserve a Slack permalink in the Linear issue.
22. For an explicit create request over files or meeting notes, create each high-confidence, explicitly committed, fully resolved item after all extraction, deduplication, and sizing succeeds. An `extract`, `preview`, or otherwise non-committal request never writes issues.
23. Persist every remaining review proposal in the backend before rendering controls. Show compact per-item Approve/Reject controls plus batch Approve all/Reject all controls; the batch remains usable across Roo restarts until its bounded expiry.
24. An untagged decision may continue only a same-requester, same-thread Roo session whose durable batch is awaiting approval, and only for an unambiguous whole-batch approval or rejection phrase. All new Linear requests, cross-user replies, and general channel chatter still require normal direct addressing and authorization gates.
25. Report created project updates, created issues, skipped duplicates, and unresolved items clearly. Keep the first Slack response compact; leave detailed evidence and effort rationale in Linear rather than repeating it for every item in Slack.
26. For `size_project_issues`, resolve exactly one active project, verify the requester maps to an authorized Linear user, page every issue and project update, and stop without writes if pagination, context, model output, or the five-label preflight is incomplete.
27. Exclude completed, cancelled, and duplicate work. In `missing_only`, skip issues carrying exactly one valid size label but correct issues carrying multiple size labels. In `replace_existing`, rescore all active issues.
28. Persist the complete preview in the backend. Apply only through requester-bound Slack confirmation; re-read each live issue, skip terminal races, reject stale project/team/content changes, preserve all unrelated labels, and make retries idempotent.

## Creation Rules

- Do not create an issue without a matched Linear team.
- Do not use a project team that is missing from Roo's accessible-team catalogue, and do not fall back to an unrelated default team when a project already declares its team.
- Do not auto-create an issue without a high-confidence assignee, project, accessible project team, complete extraction pass, and valid effort label when sizing is required.
- A contextual command can auto-create only when its source contains an explicit assignment/commitment and the assignee, project, team, and due date are unambiguous.
- Do not auto-create contextual discussion-thread issues; always request Slack approval first. Explicit bulk create commands over attached notes may auto-create only candidates whose source records an explicit commitment.
- Honor an explicit fallback such as "if you can't find the right person or are unsure, assign them to Dr Sam" for otherwise unresolved action-item owners.
- Project updates are created immediately only when affirmatively requested, not when the request says not to write one, and the project match is confident.
- Never substitute a merely similar active project for an unresolved explicit destination. A full-catalogue lookup may use an inactive project only when the user's explicit title uniquely matches it.
- Never turn a Linear catalogue, permission, or required-team access failure into `not found`; distinguish an unavailable/incomplete connection from a genuine completed full-catalogue search.
- Apply the compatible `meeting-action` label if it already exists.
- In project sizing `review` or `required` mode, apply exactly one of `Extra Small (XS)`, `Small (S)`, `Medium (M)`, `Large (L)`, or `Extra Large (XL)`. Fail closed if sizing context, structured output, or the compatible label is unavailable.
- Project sizing `shadow` mode captures estimates without mutating issue labels or descriptions; `review` sends every candidate for approval; `required` may auto-create only a high-confidence, context-sufficient assessment.
- Never mutate existing issues during project sizing until the requester presses Apply. Preserve descriptions and every non-effort label.
- Issue descriptions must include the context, evidence snippet, source message permalink/identifiers when available, and a note that Roo generated the issue from Slack context.

## Response Style

Keep Slack responses concise. Lead with created Linear links, then list review-needed or skipped items.
