from unittest.mock import AsyncMock

import pytest

from roo.linear_project_context import (
    load_project_issue_inventory,
    match_project_issue,
    shortlist_project_issues,
)


def issue(key, title, state="started", **fields):
    return {"id": f"id-{key}", "identifier": key, "title": title, "state": {"type": state}, **fields}


def page(issues):
    return {"project": {"id": "p"}, "truncated": False, "nodes": issues, "snapshotAt": "2026-11-20T01:02:03Z"}


@pytest.mark.asyncio
async def test_closed_and_old_issues_from_full_inventory_are_searchable():
    old = issue("P-42", "Update onboarding docs", "completed")
    rows = [old] + [issue(f"P-{100+i}", f"Work item {i}") for i in range(120)]
    client = AsyncMock()
    client.list_project_issues.return_value = page(rows)
    inventory = await load_project_issue_inventory(client, "p")
    match = match_project_issue({"title": "Update onboarding documentation"}, inventory)
    assert inventory.complete and len(inventory.issues) == 121
    assert match.disposition == "already_closed" and match.issue["identifier"] == "P-42"
    assert match.issue["project"]["id"] == "p"
    client.list_project_issues.assert_awaited_once_with("p")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"project": {"id": "p"}, "nodes": [], "truncated": True},
    {"project": {"id": "other"}, "nodes": [], "truncated": False},
    page([issue("Q-1", "Private issue", project={"id": "other"})]),
    RuntimeError("page did not advance"),
])
async def test_failed_or_mismatched_inventory_never_clears_a_write(payload):
    client = AsyncMock()
    if isinstance(payload, Exception):
        client.list_project_issues.side_effect = payload
    else:
        client.list_project_issues.return_value = payload
    inventory = await load_project_issue_inventory(client, "p")
    assert not inventory.complete and not inventory.issues
    assert match_project_issue({"title": "Add export"}, inventory).disposition == "unavailable"


@pytest.mark.asyncio
async def test_reworded_same_outcome_can_link_and_related_work_stays_separate():
    client = AsyncMock()
    client.list_project_issues.return_value = page([
        issue("P-12", "Password reset", description="Send a recovery link to regain access."),
        issue("P-13", "Add invoice export for iOS"),
    ])
    inventory = await load_project_issue_inventory(client, "p")
    candidate = {"title": "Let members recover their accounts"}
    # No title terms overlap; the model can still see this issue in the scoped shortlist.
    assert "P-12" in [i["identifier"] for i in shortlist_project_issues(candidate, inventory)]
    assert match_project_issue(candidate, inventory, semantic_issue_id="P-12", semantic_relation="same_outcome").disposition == "already_tracked"
    android = {"title": "Add invoice export for Android", "evidence": "Depends on P-13"}
    assert match_project_issue(android, inventory).disposition == "needs_review"
    assert match_project_issue(android, inventory, semantic_issue_id="id-P-13", semantic_relation="distinct").disposition == "new"
    assert match_project_issue(android, inventory, semantic_issue_id="id-P-13", semantic_relation="changed_scope").disposition == "changed_scope"
    assert match_project_issue(android, inventory, semantic_issue_id="OUT-10", semantic_relation="same_outcome").disposition == "needs_review"


@pytest.mark.asyncio
async def test_a_different_title_match_cannot_mask_a_second_exact_match():
    client = AsyncMock()
    client.list_project_issues.return_value = page([
        issue("P-11", "Build data export"),
        issue("P-12", "Build data export"),
    ])
    inventory = await load_project_issue_inventory(client, "p")
    candidate = {"title": "Build data export"}
    assert match_project_issue(candidate, inventory).disposition == "needs_review"
    matched = match_project_issue(candidate, inventory, semantic_issue_id="P-11", semantic_relation="distinct")
    assert matched.issue["identifier"] == "P-12" and matched.disposition == "already_tracked"


@pytest.mark.asyncio
async def test_same_title_and_different_acceptance_criteria_require_an_outcome_check():
    client = AsyncMock()
    client.list_project_issues.return_value = page([
        issue("P-11", "Export invoices", description="Include signed invoices."),
    ])
    inventory = await load_project_issue_inventory(client, "p")
    candidate = {"title": "Export invoices", "acceptance_criteria": ["Include unsigned invoices."]}
    assert match_project_issue(candidate, inventory).disposition == "needs_review"
    assert match_project_issue(candidate, inventory, semantic_issue_id="P-11", semantic_relation="distinct").disposition == "new"
