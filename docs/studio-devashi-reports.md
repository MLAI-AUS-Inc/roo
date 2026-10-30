# Devarshi Desai · Study Nash reporting

Devarshi (also requested as Devashi Desai) uses **completed Linear tasks** from
the single `[Studio] Studynash` project for usage reports. The user explicitly
selected this source on 30 October 2026. Invoice hours and payment records are
separate from this task-usage view.

The existing [Studio hours](studio-client-hours.md) path reads fresh Linear
evidence, reconstructs the first completion, project, assignee and effort label,
and counts each task once. It includes archived completed work. Hours use the
existing rubric: XS 0.25h, S 1h, M 2h, L 3h and XL 5h. These are size estimates,
not clocked time. Missing/conflicting labels and unmapped builders appear in the
exceptions export; incomplete history blocks the report.

## Private configuration

Merge the verified owner into the private worker's existing
`STUDIO_REPORTS_CLIENTS_JSON`. This example uses synthetic IDs:

```json
{
  "UEXAMPLEDEVASHI": {
    "name": "Devarshi Desai",
    "aliases": ["Devashi Desai", "Study Nash", "Studynash", "[Studio] Studynash", "Project Study Nash"],
    "project_ids": ["00000000-0000-0000-0000-000000000003"],
    "hours_source": "linear",
    "history_start_month": "2026-08",
    "monthly_hours": 40,
    "monthly_allowances": {"2026-08": null, "2026-09": null}
  }
}
```

`hours_source: linear` excludes invoice backfills, replacements, pending invoice
markers and reviewed finance overlays for that owner's projects. A task-only
request does not open the invoice/finance files or invoke the finance refresh
service. Other owners keep their current source policy. Mixed staff reports
apply each owner's policy within the existing access grants.

Keep the verified project spellings as explicit owner aliases. The router may
send `client: "Study Nash"` for “my Study Nash hours”, even though it selects the
correct hours tool. These aliases select the existing one-project owner within
the requester's grants; they do not infer ownership or grant another client access.

`history_start_month` is a private, verified project-history boundary for
project-to-date requests. Study Nash's creation month was verified as August
2026. It is independent of contract commencement, invoices and payment coverage.
An omitted boundary still permits explicit months, but project-to-date fails
closed until configured. Public Roo cannot supply or change either setting.

Use the reviewed base monthly allowance and explicit overrides; a null month
shows usage without inventing an allowance before the contract. A task burndown
compares counted estimates with that month's **base** allowance, resetting at
each calendar month. It excludes contractual rollover and payment credits, and
does not calculate contractual remaining entitlement. Partial months prominently
mark unresolved tasks and an unconfirmed balance.

Read-only inspection confirmed that Study Nash is already in the worker's
project map and Sam's project scope. Add the verified owner ID to Sam's
`report_client_ids` to permit named-client reporting. Preserve existing owners,
project access and schedule IDs. Do not change payroll's excluded projects or
`.env.timesheets`; client reporting does not authorize payroll changes.

Verified IDs and credentials remain in private deployment configuration.
This document is an activation handoff, not proof of production activation.
Changing an owner's source/history or revoking a grant invalidates saved delivery.
No new recurring schedule is required for on-demand reporting.

## Requests and output

```text
@Roo show Devarshi Desai's Study Nash hours this month
@Roo show all completed-task hours for Devashi Desai's Study Nash project
@Roo show project-to-date Studio hours and a burndown for Devarshi Desai
@Roo give me the detailed tasks and hours behind that report
```

Use `studio-hours` for these requests. The report includes monthly/project
usage charts, a base-allowance task burndown when an allowance is configured,
builder totals, and detailed task CSVs when requested. Both spellings select the
same configured owner. The authenticated requester receives the private DM;
naming Devarshi never changes its recipient.

Requests for **used hours minus purchased-hour credits** remain the separate
[finance report](studio-client-balance.md), with its own reviewed cash evidence
and access checks. Those sources are not a prerequisite for task usage.

Regression coverage:
[`test_studio_report_linear_policy.py`](../tests/roo/test_studio_report_linear_policy.py),
[`test_studio_report_policy.py`](../tests/roo/test_studio_report_policy.py),
[`test_studio_task_burndown.py`](../tests/roo/test_studio_task_burndown.py).
