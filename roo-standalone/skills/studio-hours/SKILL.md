---
name: studio-hours
description: Private Studio client hours, monthly allowance, project and builder breakdowns
requires_auth: true
routing:
  use_when: Studio hours used/remaining, project/builder hours, monthly charts, or a detailed follow-up.
  avoid_when: Builder payroll timesheets, coworking attendance, ticket creation, or effort sizing.
  examples:
    - {text: "How many of my 40 Studio hours have we used this month?", action: summary}
    - {text: "Studio hours report and chart for the last 3 months", action: summary}
    - {text: "Give me the in-depth report on what those hours were spent on", action: detailed}
  negative_examples:
    - {text: "How many people used coworking?", instead: mlai-points}
actions:
  - name: summary
    description: High-level hours, monthly chart and project/builder totals. Default current month.
    params:
      month: {type: string, description: "Start YYYY-MM/current/last. Last N months: recent (includes current); last N full months: last_complete. previous reuses prior period."}
      months: {type: integer, description: "Number of consecutive months from month, 1–12; default 1."}
  - name: detailed
    description: Full dated work breakdown and CSV. Inherits the client's last report period unless specified.
    params:
      month: {type: string, description: "Start YYYY-MM/current/last/recent/last_complete/previous. Omit to reuse prior period."}
      months: {type: integer, description: "Number of consecutive months, 1–12."}
---

# Studio client hours

Queue a fresh report for the authenticated Slack requester. The worker alone
resolves client ownership from verified Slack IDs and explicit project IDs.
Names, mentions, model parameters, and messages never grant access to another
client. Reports and attachments are delivered privately to the requester.

Default to a clear summary: hours used out of the shared monthly allowance,
remaining or overage, each project's total and builders within each project.
Use detailed only when the client asks for the work-level breakdown. Follow-ups
inherit the last requested period for this requester; explicit periods override it.

For “last three months”, use summary with month=recent and months=3. This includes
the current month to date and the previous two calendar months, resolved by the
backend in Melbourne time. For “last three full/completed months”, use
month=last_complete and months=3. Never calculate relative YYYY-MM dates yourself.
Multi-month summaries show the period total, a short monthly comparison, one
combined project/builder breakdown, and a monthly chart. No ticket-level detail
is needed unless requested. For explicit date ranges, select a starting month
and consecutive month count.
Do not silently narrow an ambiguous date request; ask which period is wanted.
The current implementation counts completed-ticket size hours, not clocked time.
Do not invent hourly activities, billable hours, source access or project ownership.
