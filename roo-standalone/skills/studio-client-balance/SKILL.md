---
name: studio-client-balance
description: Studio hours used minus paid-hour credits.
requires_auth: true
routing:
  use_when: Used vs purchased Studio hours; charts and follow-ups.
  avoid_when: Hours-only, bank reconciliation, Roo Points.
  examples:
    - {text: "Chart Mark Ghiasy's used vs purchased hours", action: summary}
    - {text: "My prepaid Studio hours balance", action: summary}
    - {text: "Detail the hours/payment chart", action: detailed}
  negative_examples:
    - {text: "Show my Studio hours this month", instead: studio-hours}
actions:
  - name: summary
    description: Hours/payment chart; default all.
    params:
      month: {type: string, description: "all/current/last/recent/last_complete/previous/YYYY-MM."}
      months: {type: integer, description: "1–12 consecutive months."}
      client: {type: string, description: "Name/email/mention/self; never grants access."}
  - name: detailed
    description: Evidence/CSV; reuse period/client.
    params:
      month: {type: string}
      months: {type: integer}
      client: {type: string}
---

# Studio hours and purchased-hours

Queue a fresh private hours and purchased-hours report for the authenticated
Slack requester. The separate worker verifies the requester's explicit reporting
permission and resolves the client's permitted projects. Having Studio hours
access alone does not grant financial access. Names and model parameters select
an existing authorized client; they never grant access or change ownership.
The worker privately DMs the report after these checks.

The chart shows reviewed hours used across the client's permitted projects minus
purchased-hour credits. Work hours move the curve up. Actual received client
payments add hour credits at the client's reviewed purchase rate and move the
curve down on their receipt dates. A negative balance means purchased hours exceed
used hours; a positive balance means used hours exceed purchased hours. The y-axis
is hours. Do not interpret it as money owed, profit or a contractor cost balance.
Invoice issue dates and unpaid invoice face values do not add hour credits.
Contractor pay amounts and contractor rates never determine used hours or credits.

Default to summary and month=all for project-to-date. For a named client, copy the
requested name into client, such as client="Mark Ghiasy". Use self or omit client
for the requester's own client. This report selects one client at a time;
client="all" is unsupported.
For “last three months”, use month=recent and months=3, including this month to
date. For “last three full months”, use month=last_complete and months=3.
The backend resolves relative dates in Melbourne. Explicit ranges use a starting
YYYY-MM and consecutive month count. Detailed follow-ups inherit the last
hours/payment report's period and client; they do not inherit hours-only filters.

Only request action, month, months and client. Never supply actor IDs, project
IDs, source paths, invoice IDs, money amounts, rates, access grants, payment dates,
or work-date assumptions. The private worker derives them from reviewed invoice
hours, separately recorded work and Xero receipt records. It labels estimated
work dates, the client purchase rate, source freshness and incomplete coverage.
Never derive contractor hours from pay amounts, invent work hours, assume an
invoice was paid, set a client rate yourself or count unavailable evidence as zero.
Request detailed only when the user wants the hours/payment event list and
evidence export. This skill performs no accounting
writes or reconciliation actions.
