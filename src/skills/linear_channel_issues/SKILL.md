---
name: linear-channel-issues
description: Read, create, or explicitly edit MLAI_TECH Linear issues in an approved Slack channel
requires_auth: true
exclusive_channels: [tech_volunteers, roo-testing]
routing:
  use_when: MLAI_TECH issue reads, status lists/filters, explicit creation, or explicit edits.
  avoid_when: Meeting extraction, destructive changes, or unrelated data.
  examples:
    - {text: "What Linear statuses are available?", action: list_statuses}
    - {text: "Show MLAI_TECH issues in review", action: list_issues}
    - {text: "Move TECH-29 to In Progress", action: update_issue}
    - {text: "Add a comment to TECH-29 saying the Slack rollout is verified", action: update_issue}
    - {text: "Create a Linear issue titled Fix deployment alerts", action: create_issue}
  negative_examples:
    - {text: "Extract every action item from this meeting into Linear", instead: linear-meeting-actions}
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
  - name: create_issue
    description: Immediately create one issue in the channel-bound MLAI_TECH team.
    params:
      title: {type: string}
      description: {type: string}
      status: {type: string}
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

Creates and edits require an explicit Roo request and `LINEAR_CHANNEL_ISSUE_WRITES_ENABLED`.
Never infer a mutation from an untagged contextual message. If the issue, field,
mode or value is ambiguous, ask a clarification question and change nothing.

Creation requires an explicit title and always targets the backend-bound
MLAI_TECH team. An explicit description and status may also be supplied.

Allowed edits are comments, title, description append/replace, priority,
estimate, due date, assignee, labels, project, cycle, status, and a duplicate
relation. Never move teams, archive, trash, delete, restore, edit/delete an
existing comment, or send arbitrary GraphQL.

Before writing, fetch the current issue and send its exact `updatedAt` value to
the backend. The backend rechecks it immediately before mutation. Do not retry a
write transport failure: tell the user to inspect Linear before trying again.
