---
name: studio-hours
description: Private Studio hours by project and person.
requires_auth: true
routing:
  use_when: Studio usage by client/project/builder, allowance burndowns, hours burnt by person, worker breakdowns, contributor charts.
  avoid_when: Payroll, purchased-hour credits, coworking or effort sizing.
  examples:
    - {text: "How many of my 40 Studio hours have we used this month?", action: summary}
    - {text: "Who did what hours on Master App over the last three months?", action: summary}
    - {text: "Chart Mark Ghiasy's Master App hours by person", action: summary}
    - {text: "Give me the work behind those hours", action: detailed}
  negative_examples:
    - {text: "How many people used coworking?", instead: mlai-points}
actions:
  - name: summary
    description: Hours and charts. Default current month.
    params:
      month: {type: string, description: "YYYY-MM/current/last/recent/last_complete; all for project-to-date; previous for follow-ups like 'plot that'."}
      months: {type: integer, description: "1–12 consecutive months."}
      client: {type: string, description: "Client name/email/mention, all, or self (default)."}
      project: {type: string, description: "Requested project name or ID; all resets a prior filter."}
      chart: {type: string, enum: [stacked, cumulative, both, monthly], description: "Worker breakdown / who did what hours: monthly (one bar per month). One bar per project: stacked. Running totals: cumulative. Both original views: both."}
  - name: detailed
    description: Dated work and CSV. Inherits prior filters and charts.
    params:
      month: {type: string, description: "Start YYYY-MM/current/last/recent/last_complete/previous/all. Omit to reuse prior period."}
      months: {type: integer, description: "Number of consecutive months, 1–12."}
      client: {type: string, description: "Client name/email/mention or all/self; omit to retain prior filter."}
      project: {type: string}
      chart: {type: string, enum: [stacked, cumulative, both, monthly]}
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

For “Devashi Desai's Study Nash hours”, set client="Devashi Desai", project="Study Nash", and use the
same summary/detail actions. The worker must verify the configured single
Study Nash project grant. Project names in a message never add access. A Study
Nash task-usage burndown uses this skill and the configured base monthly allowance.
For hours used against purchased-hour credits, use studio-client-balance.
The client's name may also be spelled Devarshi Desai; copy the requested spelling
and let the private worker resolve configured aliases.
For “all completed tasks” or “from the beginning”, use month=all. The private
worker resolves configured Linear history; never substitute the contract or invoice
start, change effort labels, or add invoice quantities to completed-ticket hours.

Default to a clear summary: hours used out of the shared monthly allowance,
remaining or overage, each project's total and builders within each project.
Use detailed when the client asks for the work-level breakdown. Copy a requested
project name or ID into project. For all projects, omit project on a new summary
or use project="all" to clear a previous project filter.

For “worker breakdown” or “who did what hours” on a project, use chart="monthly".
It shows each contributor within each requested calendar month, even when no work was attributed.
“Last three months” uses month="recent", months=3.
For a stacked breakdown of hours per person on each project, use chart="stacked".
For lines showing each person's cumulative hours over time, use chart="cumulative".
For both views or an unspecific request to chart hours by person, use chart="both".
For example, “chart who burnt hours on Mark Ghiasy's Master App over the last couple
of months” uses client="Mark Ghiasy", project="Master App", chart="both",
month="recent", months=2. For project-only requests, omit client.
People named as contributors belong in the breakdown; their names do not select
the client. Hours reflect the project's configured source. Future sprint estimates
and dollar costs require separate data.

For “plot that by person” after an hours report, use summary with month="previous"
and chart="both". Detail and previous-period follow-ups retain the prior period,
client, project and chart unless overridden. Changing client clears the previous
project restriction. An explicit self selects the requester's own projects.

For “last three months”, use summary with month=recent and months=3. This includes
the current month to date and the previous two calendar months, resolved by the
backend in Melbourne time. For “last three full/completed months”, use
month=last_complete and months=3. Never calculate relative YYYY-MM dates yourself.
Multi-month summaries show the period total, a short monthly comparison, one
combined project/builder breakdown, and a monthly chart. No ticket-level detail
is needed unless requested. For explicit date ranges, select a starting month
and consecutive month count.
For “project to date”, “all time” or “from the beginning”, use month=all. The
private worker resolves the beginning of reviewed history. Unallocated invoice
hour-units count toward the full reviewed period, separately from dated monthly
hours. The private worker may apply reviewed month estimates when explicitly
requested; these are labelled in charts and detail. Never invent allocations
from the conversation or change source dates yourself.
Do not silently narrow an ambiguous date request; ask which period is wanted.
The report labels its sources. Invoice-first projects use invoice quantities
and recorded work. Projects configured for completed-ticket estimates use
the task size at first completion.
Do not invent hourly activities, billable hours, source access or project ownership.
