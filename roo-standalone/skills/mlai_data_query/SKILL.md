---
name: mlai-data-query
description: Query curated read-only MLAI backend data resources through the permissioned data access API
requires_auth: true
routing:
  use_when: >
    Read curated MLAI backend counts, lists, or aggregates, including the live
    Linear queue bound to the current Slack channel, or list queryable resources.
  avoid_when: >
    Creating Linear issues, Luma attendance, personal points/bookings, uploaded
    files, or any write/SQL request.
  examples:
    - {text: "How many Vibe Raising companies do we have?", action: query}
    - {text: "Which Content Factory jobs failed this week?", action: query}
    - {text: "What issues are in the MLAI_TECH Todo list?", action: list_linear_channel_issues}
    - {text: "Tell me more about TECH-16", action: get_linear_channel_issue}
    - {text: "What data resources can Roo query?", action: catalog}
  negative_examples:
    - {text: "create a linear ticket from this thread", instead: linear-meeting-actions}
    - {text: "how many people registered for the AI safety event?", instead: luma-events}
    - {text: "how busy was the coworking space in may?", instead: mlai-points}
actions:
  - name: catalog
    description: List the data resources/tables Roo is allowed to query.
  - name: query
    description: Run a read-only list/count/aggregate query against one resource.
    params:
      resource: {type: string, description: "Exact resource key if known (e.g. vibe_raising_companies, content_factory_jobs, linear_issues)."}
      operation: {type: string, enum: [list, count, aggregate]}
      limit: {type: integer, description: "Maximum rows."}
  - name: list_linear_channel_issues
    description: List the channel's approved live Linear issue titles.
    params:
      limit: {type: integer, description: "Maximum titles."}
  - name: get_linear_channel_issue
    description: Get one approved issue with its comments.
    params:
      issue_reference: {type: string, description: "Key, list number, URL, or title words."}
---

# MLAI Data Query Skill

Use this skill when a Slack user asks Roo to read curated backend data across the MLAI app, especially Vibe Raising, startup update artifacts, Content Factory state, Linear/Gmail/Slack sync metadata, GitHub integration status, financial metadata, organizations, and coworking booking records.

Roo must not write data and must not send SQL. It calls the backend's read-only data access API:

- `GET /api/v1/data/catalog/?requester_slack_id=<actual Slack user ID>`
- `POST /api/v1/data/query/`
- `POST /api/v1/integrations/linear/channel-issues/list`
- `POST /api/v1/integrations/linear/channel-issues/detail`

For a channel-bound Linear queue, return identifiers and titles first. Resolve
follow-ups by exact issue identifier, Linear URL, numbered item in the current
Slack thread, or a unique title match. The backend remains authoritative for
the Slack-channel-to-Linear-team binding and must reject issues from other
teams before descriptions or comments are returned.

The backend registry is the security contract. Each resource has explicit allow-listed fields, supported operations, filters, ordering, pagination limits, and role-scoped policies. There is no "admin bypass all" mode.
The catalog uses the same requester identity and policies, so it only includes
resources, operations, and fields the requesting Slack user can query.

## Parameters

- **action**: Optional action. Use `catalog` when the user asks what data/tables/resources are available.
- **resource**: Optional exact resource key, such as `vibe_raising_companies`, `monthly_update_drafts`, `content_factory_jobs`, or `linear_issues`.
- **operation**: Optional operation. One of `list`, `count`, or `aggregate`.
- **fields**: Optional list of allow-listed field names to return.
- **filters**: Optional list of filters. Each filter must use `{ "field": "...", "operator": "...", "value": ... }`.
- **group_by**: Optional list of fields for aggregate queries.
- **order_by**: Optional list of `{ "field": "...", "direction": "asc|desc" }`.
- **limit**: Optional positive integer. Roo caps default requests before the backend resource max.
- **offset**: Optional non-negative integer for pagination.

## Query Rules

- The only supported filter operators are `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, and `icontains`.
- `icontains` means case-insensitive substring search and only works on backend fields marked searchable.
- Paginated list responses use `returned_count`, `limit`, `offset`, and `has_more`.
- `count` is only the count returned by an explicit count operation. List responses do not include total matching rows.
- Tokens, secrets, credentials, raw payloads, raw attachment data, storage paths, and sync cursors are never exposed.

## Examples

- "How many Vibe Raising companies do we have?"
- "Show startup update drafts for my company."
- "Which Content Factory jobs failed?"
- "Show Linear issues synced for this startup."
- "What data resources can Roo query?"
