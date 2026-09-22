---
name: studio-hours
description: Private Studio client hours, monthly allowance, project and builder breakdowns
requires_auth: true
routing:
  use_when: Client asks for Studio hours used or remaining, monthly project/builder hours, or a detailed follow-up.
  avoid_when: Builder payroll timesheets, coworking attendance, ticket creation, or effort sizing.
  examples:
    - {text: "How many of my 40 Studio hours have we used this month?", action: summary}
    - {text: "Break down my Studio hours by project and builder", action: summary}
    - {text: "Give me the in-depth report on what those hours were spent on", action: detailed}
  negative_examples:
    - {text: "How many people used coworking?", instead: mlai-points}
actions:
  - name: summary
    description: Fresh monthly hours used/remaining, by project and builder. Default current month.
    params:
      month: {type: string, description: "Starting YYYY-MM, current, last, or previous for the client's last requested period. Use current/last for relative months."}
      months: {type: integer, description: "Number of consecutive months from month, 1–12; default 1."}
  - name: detailed
    description: Full dated work breakdown and CSV. Inherits the client's last report period unless specified.
    params:
      month: {type: string, description: "Starting YYYY-MM, current, last, or previous. Omit for a same-period follow-up."}
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

For monthly comparisons, select a starting month and consecutive month count.
Do not silently narrow an ambiguous date request; ask which period is wanted.
The current implementation counts completed-ticket size hours, not clocked time.
Do not invent hourly activities, billable hours, source access or project ownership.
