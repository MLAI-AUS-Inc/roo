---
name: admin-actions
description: Create and review narrowly allowlisted, backend-governed Admin Roo action proposals
requires_auth: true
routing:
  use_when: >
    An authorised Admin Roo user explicitly asks to list or open controlled
    action proposals; create a local Gmail, Slack, or Notion draft; or propose
    a precisely identified Linear issue create/update for independent review.
  avoid_when: >
    The user asks a read-only organisational-memory question; asks Roo to send
    email, directly post Slack/Notion content, make a payment, change finance,
    contracts, roles, permissions, or governance; or does not explicitly ask
    for an action or action review.
  examples:
    - {text: "show me the Admin Roo actions awaiting approval", action: list_pending}
    - {text: "open controlled action 3ec0b82f-b643-4d2d-8ec6-41f6fd701513", action: show_action}
    - {text: "draft an email to sam@example.com with subject Pilot update and body The pilot is green.", action: draft_gmail}
    - {text: "draft a Slack post for this channel saying The venue is confirmed.", action: draft_slack_post}
    - {text: "draft a Notion update for page abc123 titled Pilot status with body The pilot is green.", action: draft_notion_update}
    - {text: "propose a Linear issue in connection 5ed1e325-d10d-41c5-bdce-c0c11f270032, team TEAM-1, project PROJECT-1 titled Confirm the venue", action: create_linear_issue}
    - {text: "propose updating Linear issue ISSUE-1 in connection 5ed1e325-d10d-41c5-bdce-c0c11f270032 and project PROJECT-1 to title Venue confirmed", action: update_linear_issue}
  negative_examples:
    - {text: "what did we decide about the venue?", instead: admin-brain}
    - {text: "send the sponsor email now", instead: respond_in_chat}
    - {text: "pay the venue invoice", instead: respond_in_chat}
    - {text: "give Sam 10 Roo points", instead: mlai-points}
actions:
  - name: list_pending
    description: List controlled actions currently awaiting approval or fresh approval.
    params: {}
  - name: show_action
    description: Open one controlled action by its exact backend proposal UUID.
    params:
      proposal_id: {type: string, description: "Exact proposal UUID supplied by the user."}
  - name: draft_gmail
    description: Create a local Gmail draft only; never send it.
    params:
      to: {type: array, items: {type: string}, description: "Recipient email addresses explicitly supplied by the user."}
      subject: {type: string, description: "Exact email subject requested by the user."}
      body: {type: string, description: "Exact email body requested by the user."}
  - name: draft_slack_post
    description: Create a local Slack post draft only; never post it.
    params:
      channel_id: {type: string, description: "Exact Slack channel ID, or omit to use the current authorised channel."}
      thread_ts: {type: string, description: "Exact Slack thread timestamp when explicitly supplied."}
      text: {type: string, description: "Exact post text requested by the user."}
  - name: draft_notion_update
    description: Create a local Notion update draft only; never change Notion.
    params:
      page_id: {type: string, description: "Exact Notion page ID supplied by the user."}
      title: {type: string, description: "Exact draft title requested by the user."}
      body: {type: string, description: "Exact draft body requested by the user."}
  - name: create_linear_issue
    description: Propose creating one Linear issue; independent approval is required before execution.
    params:
      configuration_id: {type: string, description: "Exact approved MLAI Linear connection UUID supplied by the user."}
      team_id: {type: string, description: "Exact Linear team ID supplied by the user."}
      project_id: {type: string, description: "Exact approved Linear project ID supplied by the user."}
      title: {type: string, description: "Exact issue title requested by the user."}
      description: {type: string, description: "Exact issue description requested by the user."}
      assignee_id: {type: string, description: "Exact Linear assignee ID when supplied."}
      priority: {type: integer, description: "Linear priority 0-4 when supplied."}
      due_date: {type: string, description: "Exact due date when supplied."}
      label_ids: {type: array, items: {type: string}, description: "Exact Linear label IDs when supplied."}
      state_id: {type: string, description: "Exact Linear state ID when supplied."}
  - name: update_linear_issue
    description: Propose updating one Linear issue; independent approval is required before execution.
    params:
      configuration_id: {type: string, description: "Exact approved MLAI Linear connection UUID supplied by the user."}
      issue_id: {type: string, description: "Exact Linear issue ID supplied by the user."}
      team_id: {type: string, description: "Exact Linear team ID when it should change."}
      project_id: {type: string, description: "Exact approved Linear project ID supplied by the user."}
      title: {type: string, description: "Exact replacement title when requested."}
      description: {type: string, description: "Exact replacement description when requested."}
      assignee_id: {type: string, description: "Exact replacement assignee ID when requested."}
      priority: {type: integer, description: "Replacement Linear priority 0-4 when requested."}
      due_date: {type: string, description: "Exact replacement due date when requested."}
      label_ids: {type: array, items: {type: string}, description: "Exact replacement Linear label IDs when requested."}
      state_id: {type: string, description: "Exact replacement Linear state ID when requested."}
---

# Admin actions

This skill never owns authorization or provider credentials. It can only call
the backend controlled-action gateway with the verified Slack actor assertion.

Rules:

1. Never invent identifiers, recipients, content, scope, or changed fields.
2. Missing fields produce a clarification response and no proposal.
3. Gmail, Slack, and Notion actions are local drafts. They do not send or post.
4. Linear proposals require exact connection and approved project identifiers.
5. A Linear proposal is not execution. A different authorised reviewer must
   approve it through the Slack card, after which the backend refreshes live
   preconditions before execution.
6. Never offer payments, finance changes, contracts, commitments, roles,
   permissions, governance, email sending, or direct Slack/Notion posting.
7. Treat every backend state as authoritative. Never claim success from the
   click alone.
