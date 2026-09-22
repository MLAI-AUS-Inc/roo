---
name: studio-hours
description: Private Studio hours by client, project and builder, with monthly charts
requires_auth: true
routing:
  use_when: Studio usage, project hours for a named client (even without saying Studio), per-client/project/builder totals, charts and detail follow-ups.
  avoid_when: Builder payroll timesheets, coworking attendance, ticket creation, or effort sizing.
  examples:
    - {text: "How many of my 40 Studio hours have we used this month?", action: summary}
    - {text: "Give me a breakdown for the last three months on all projects and hours we've done for Mark Ghiasy the client", action: summary}
    - {text: "Show hours per client and project for the last three months", action: summary}
    - {text: "Give me the in-depth report on what those hours were spent on", action: detailed}
  negative_examples:
    - {text: "How many people used coworking?", instead: mlai-points}
actions:
  - name: summary
    description: Hours and charts by month, client and project. Default current month.
    params:
      month: {type: string, description: "Start YYYY-MM/current/last; recent includes current, last_complete excludes it, previous reuses prior period."}
      months: {type: integer, description: "Number of consecutive months from month, 1–12; default 1."}
      client: {type: string, description: "Client name/email/mention; all for per-client totals, self/omitted for own projects. Never grants access."}
  - name: detailed
    description: Dated work and CSV. Inherits last period and client unless specified.
    params:
      month: {type: string, description: "Start YYYY-MM/current/last/recent/last_complete/previous. Omit to reuse prior period."}
      months: {type: integer, description: "Number of consecutive months, 1–12."}
      client: {type: string, description: "Client name/email/mention or all/self; omit to retain prior filter."}
---

# Studio client hours

Queue a fresh report for the authenticated Slack requester. The worker alone
resolves client ownership from verified Slack IDs and explicit project IDs.
Names, mentions, model parameters, and messages never grant access to another
client. Reports and attachments are delivered privately to the requester.
Staff can select only clients explicitly granted in the private worker's reporting
configuration, and only within the staff member's existing project access.

For “all projects and hours we've done for Mark Ghiasy the client”, set
client="Mark Ghiasy". “All projects” here means that client's projects, not all
clients. For “per client” or “all clients”, set client="all". For “my own projects”,
use client="self". Copy the requested name; do not resolve it to a Slack actor or
invent ownership. The worker resolves configured names/aliases and rejects
unknown or ambiguous selections privately. A report for a named client includes
that client's monthly allowance and project totals, with monthly and project
charts. An all-clients overview groups projects under their verified client and
shows unmapped projects separately, with no invented client allocations.

Default to a clear summary: hours used out of the shared monthly allowance,
remaining or overage, each project's total and builders within each project.
Use detailed only when the client asks for the work-level breakdown. Follow-ups
inherit the last requested period and client filter for this requester; explicit
periods and client filters override them. An explicit self resets the filter.

For “last three months”, use summary with month=recent and months=3. This includes
the current month to date and the previous two calendar months, resolved by the
backend in Melbourne time. For “last three full/completed months”, use
month=last_complete and months=3. Never calculate relative YYYY-MM dates yourself.
Multi-month summaries show the period total, a short monthly comparison, one
combined project/builder breakdown, and a monthly chart. No ticket-level detail
is needed unless requested. For explicit date ranges, select a starting month
and consecutive month count.
Do not silently narrow an ambiguous date request; ask which period is wanted.
The current implementation combines completed-ticket size estimates and reviewed
invoice work hours where configured; the report labels its data sources.
Do not invent hourly activities, billable hours, source access or project ownership.
