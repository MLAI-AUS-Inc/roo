---
name: linear-channel-issues
description: Read or explicitly edit existing MLAI_TECH Linear issues in an approved Slack channel
requires_auth: true
exclusive_channels: [tech_volunteers, roo-testing]
routing:
  use_when: Existing MLAI_TECH issue reads, status lists/filters, or explicit edits.
  avoid_when: New issues, meeting extraction, destructive changes, or unrelated data.
  examples:
    - {text: "What Linear statuses are available?", action: list_statuses}
    - {text: "Show MLAI_TECH issues in review", action: list_issues}
    - {text: "Move TECH-29 to In Progress", action: update_issue}
    - {text: "Add a comment to TECH-29 saying the Slack rollout is verified", action: update_issue}
  negative_examples:
    - {text: "Create a Linear task from this thread", instead: linear-meeting-actions}
actions:
  - name: list_statuses
    description: List live MLAI_TECH statuses.
  - name: list_issues
    description: List issues, optionally by status.
    params:
      status: {type: string}
      limit: {type: integer}
  - name: get_issue
    description: Read one issue.
    params:
      issue_reference: {type: string}
  - name: update_issue
    description: Apply one explicit allow-listed edit immediately.
    params:
      issue_reference: {type: string}
      field: {type: string, enum: [comment, title, description, priority, estimate, due_date, assignee, label, project, cycle, status, duplicate]}
      mode: {type: string, enum: [set, append, add, remove, replace]}
      value: {type: string}
---

# Linear Channel Issues

Only operate in a Slack channel that the backend has bound to MLAI_TECH. Treat
the backend as authoritative for workspace, channel, team, issue, status and
field validation.

Writes require an explicit Roo request and `LINEAR_CHANNEL_ISSUE_WRITES_ENABLED`.
Never infer a mutation from an untagged contextual message. If the issue, field,
mode or value is ambiguous, ask a clarification question and change nothing.

Allowed edits are comments, title, description append/replace, priority,
estimate, due date, assignee, labels, project, cycle, status, and a duplicate
relation. Never move teams, archive, trash, delete, restore, edit/delete an
existing comment, or send arbitrary GraphQL.

Before writing, fetch the current issue and send its exact `updatedAt` value to
the backend. The backend rechecks it immediately before mutation. Do not retry a
write transport failure: tell the user to inspect Linear before trying again.
