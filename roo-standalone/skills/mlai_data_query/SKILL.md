---
name: mlai-data-query
description: Query curated read-only MLAI backend data resources through the permissioned data access API
trigger_keywords:
  - data catalog
  - database catalog
  - query data
  - data resource
  - data resources
  - vibe raising companies
  - startup update drafts
  - monthly update drafts
  - content factory jobs
  - content factory runs
  - linear issues
  - gmail messages
  - slack messages
  - github integrations
  - financial records
requires_auth: true
routing:
  use_when: >
    The user wants to READ curated MLAI backend data: counts, lists, or
    aggregates over Vibe Raising companies, startup/monthly update drafts,
    Content Factory jobs/runs, synced Linear issues/projects, Gmail/Slack sync
    metadata, GitHub integration status, financial records, organizations, or
    coworking booking records — or asks what data/tables/resources Roo can query.
  avoid_when: >
    CREATING Linear issues (linear-meeting-actions). Luma event attendance
    (luma-events). Personal points/coworking bookings (mlai-points). Questions
    about files the user uploaded (chat). Roo never writes data or runs SQL.
  examples:
    - {text: "How many Vibe Raising companies do we have?", action: query}
    - {text: "Which Content Factory jobs failed this week?", action: query}
    - {text: "Show Linear issues synced for this startup", action: query}
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
      operation: {type: string, enum: [list, count, aggregate], description: "Query type."}
      limit: {type: integer, description: "Max rows if the user asked for a specific number."}
---

# MLAI Data Query Skill

Use this skill when a Slack user asks Roo to read curated backend data across the MLAI app, especially Vibe Raising, startup update artifacts, Content Factory state, Linear/Gmail/Slack sync metadata, GitHub integration status, financial metadata, organizations, and coworking booking records.

Roo must not write data and must not send SQL. It calls the backend's read-only data access API:

- `GET /api/v1/data/catalog/`
- `POST /api/v1/data/query/`

The backend registry is the security contract. Each resource has explicit allow-listed fields, supported operations, filters, ordering, pagination limits, and role-scoped policies. There is no "admin bypass all" mode.

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
