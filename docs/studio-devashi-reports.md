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
    "monthly_allowances": {},
    "contract_terms": {
      "version": 1,
      "project_ids": ["00000000-0000-0000-0000-000000000003"],
      "commencement_date": "2026-10-16",
      "renewal": "monthly",
      "included_hours": 40,
      "rollover_percent": 50,
      "rollover_cap_hours": 20,
      "rollover_cycles": 1,
      "rollover_first": true,
      "source": {
        "drive_file_id": "REPLACE_WITH_SIGNED_FILE_ID",
        "sha256": "REPLACE_WITH_SIGNED_FILE_SHA256",
        "signed_at": "2026-10-16T04:42:39Z",
        "reviewed_at": "REPLACE_WITH_REVIEW_TIMESTAMP",
        "reviewed_by": "REPLACE_WITH_VERIFIED_REVIEWER_SLACK_ID"
      }
    }
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

The signed agreement's cover gives commencement as **16 October 2026**;
clauses 4, 5 and 25 provide 40 hours monthly, 50% rollover capped at 20 hours,
and monthly renewal. Sam approved these terms on 2 November. The first cycle
is 16 October–15 November; the next starts 16 November, not 1 November or a
fixed 30 days later. Incoming rollover is used first and expires after one
cycle. Only unused base hours can roll onward. See [contract cycles](studio-contract-cycles.md).

The chart, summary and allowance CSV use the same cycle calculation. Calendar
usage stays separate: a November report with no new work can still show 8h used
in the current contract cycle and 32h estimated remaining. Pre-commencement
tasks remain history and never consume the contract allowance. The worker
reads from commencement for opening balances, even for a single month or week.
Unresolved task history withholds remaining hours and affected rollover.

Read-only inspection confirmed that Study Nash is already in the worker's
project map and Sam's project scope. Add the verified owner ID to Sam's
`report_client_ids` to permit named-client reporting. Preserve existing owners,
project access and schedule IDs. Do not change payroll's excluded projects or
`.env.timesheets`; client reporting does not authorize payroll changes.

Verified IDs and credentials remain in private deployment configuration.
This document is an activation handoff, not proof of production activation.
Changing an owner's source/history or revoking a grant invalidates saved delivery.
No new recurring schedule is required for on-demand reporting.

## Reviewed completion sizes

An owner may have a private `effort_resolutions` mapping when a reviewer confirms
which effort label present at first completion supersedes a conflicting label:

```json
"effort_resolutions": {
  "00000000-0000-0000-0000-000000000091": {
    "project_id": "00000000-0000-0000-0000-000000000003",
    "completion_event_id": "00000000-0000-0000-0000-000000000092",
    "size": "S"
  }
}
```

These synthetic IDs must be replaced with verified evidence. Only Linear owners
can configure reviews. The worker checks the historical project, first completion
event, and label added in that event before counting the selected size. When a
reviewed label was added earlier (for example, during review), add its event UUID
as `label_event_id` alongside `completion_event_id`. The worker verifies both
events, requires the label change at or before first completion, and confirms
the selected label was still present then. Omission preserves the original
same-event rule. Reviews never count tasks still in review or use later sizing.
Reviews follow existing owner/staff grants and are included in the scope hash,
so changing one invalidates saved delivery. Summary, detailed output and CSV
identify the reviewed size basis. Payroll and Linear records are not modified.

On 30 October, Sam confirmed the completion-time S and XS sizes for the two
conflicting October tasks. The five eligible October tasks therefore total
2.75 size-estimated hours under the existing rubric, rather than the previous
partial 1.5 hours at that read. Alan subsequently completed four October tasks.
On 2 November, Sam confirmed M (2h) for the remaining conflicting phone-system
task, whose M label was added during review before completion. With that review,
October totals 8 estimated hours: Shan 2.75h and Alan 5.25h across nine tasks.

## Requests and output

```text
@Roo show Devarshi Desai's Study Nash hours this month
@Roo show all completed-task hours for Devashi Desai's Study Nash project
@Roo show project-to-date Studio hours and a burndown for Devarshi Desai
@Roo give me the detailed tasks and hours behind that report
```

Use `studio-hours` for these requests. The report includes monthly/project
usage charts, a contract-cycle task burndown including agreed rollover,
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
Contract boundary, rollover and exemption coverage is in
[`test_studio_contract_cycles.py`](../tests/roo/test_studio_contract_cycles.py).
