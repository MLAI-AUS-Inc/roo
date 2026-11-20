"""Project-scoped Linear issue retrieval and matching for meeting actions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProjectIssueInventory:
    project_id: str
    issues: list[dict] = field(default_factory=list)
    complete: bool = False
    snapshot_at: str = ""


@dataclass
class ProjectIssueMatch:
    disposition: str
    reason: str
    issue: dict | None = None


async def load_project_issue_inventory(client: Any, project_id: str) -> ProjectIssueInventory:
    """Read every page, including terminal issues. Incomplete reads cannot clear a write."""
    inventory = ProjectIssueInventory(project_id)
    if not project_id:
        return inventory
    try:
        page = await client.list_project_issues(project_id)
    except Exception:
        return inventory
    if not isinstance(page, dict) or page.get("truncated") is not False:
        return inventory
    project = page.get("project")
    nodes = page.get("nodes")
    if not isinstance(project, dict) or project.get("id") != project_id or not isinstance(nodes, list):
        return inventory
    issues = []
    for issue in nodes:
        if not isinstance(issue, dict) or not issue.get("id") or not issue.get("title"):
            return inventory
        if any(issue.get(key) is not None and not isinstance(issue[key], dict) for key in ("state", "assignee")):
            return inventory
        scope = issue.get("project")
        if scope is not None and (not isinstance(scope, dict) or scope.get("id") != project_id):
            return inventory
        issues.append({**issue, "project": {"id": project_id}})
    return ProjectIssueInventory(project_id, issues, True, str(page.get("snapshotAt") or ""))


_ALIASES = {"docs": "documentation", "doc": "documentation", "csvs": "csv"}
_COMMON = set(
    "a an and are as at be build by can create do for from implement in into is it "
    "of on or our should so support task that the this to with we will work add update".split()
)
_TERMINAL = {"completed", "canceled", "cancelled", "duplicate"}


def _words(text: Any) -> list[str]:
    return [_ALIASES.get(word, word) for word in re.findall(r"[^\W_]+[+#]*", str(text or "").casefold())]


def _candidate_text(candidate: dict) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "remaining_work", "description", "evidence", "acceptance_criteria")
    )


def _ranked_issues(candidate: dict, inventory: ProjectIssueInventory) -> tuple[list[dict], list[dict]]:
    """Retrieve possibilities. Overlap and referenced IDs never establish equivalence."""
    if not inventory.complete or not candidate.get("title"):
        return [], []
    title_words = _words(candidate["title"])
    title_terms = set(title_words) - _COMMON
    text = _candidate_text(candidate)
    context_terms = set(_words(text)) - _COMMON
    references = set(re.findall(r"(?<![\w-])[A-Z][A-Z0-9_]*-\d+(?![\w-])", text.upper()))
    scored = []
    rest = []
    for issue in inventory.issues:
        if issue.get("project") is not None and (issue.get("project") or {}).get("id") != inventory.project_id:
            continue
        issue_words = _words(issue["title"])
        issue_terms = set(issue_words) - _COMMON
        exact = title_words == issue_words
        referenced = str(issue.get("identifier") or "").upper() in references
        shared = title_terms & issue_terms
        context_hits = context_terms & issue_terms
        description_hits = title_terms & (set(_words(str(issue.get("description") or "")[:1600])) - _COMMON)
        if not (exact or referenced or shared or len(context_hits) >= 2 or len(description_hits) >= 2):
            rest.append(issue)
            continue
        title_overlap = len(shared) / max(1, len(title_terms | issue_terms))
        score = (exact, referenced, title_overlap, len(context_hits), len(description_hits))
        scored.append((score, str(issue["id"]), issue))
    scored.sort(key=lambda row: row[1])
    scored.sort(key=lambda row: row[0], reverse=True)
    # Give the semantic pass project context for paraphrases with no shared terms.
    # These broader options alone are never considered deterministic matches.
    rest.sort(key=lambda row: (str(row.get("updatedAt") or row.get("createdAt") or ""), str(row["id"])), reverse=True)
    return [row[2] for row in scored], rest


def _issue_context(issue: dict) -> dict:
    return {
        "id": issue["id"],
        "identifier": issue.get("identifier"),
        "title": issue["title"],
        "description": str(issue.get("description") or "")[:1200],
        "state": issue.get("state") or {},
        "assignee": (issue.get("assignee") or {}).get("name"),
        "dueDate": issue.get("dueDate"),
    }


def shortlist_project_issues(candidate: dict, inventory: ProjectIssueInventory, *, limit: int = 8) -> list[dict]:
    ranked, rest = _ranked_issues(candidate, inventory)
    return [_issue_context(issue) for issue in (ranked + rest)[:max(0, limit)]]


def _tracked(issue: dict) -> ProjectIssueMatch:
    state = (issue.get("state") or {}).get("type")
    if state in _TERMINAL or issue.get("completedAt") or issue.get("canceledAt"):
        return ProjectIssueMatch("already_closed", "Linear has already closed this work.", issue)
    if not state:
        return ProjectIssueMatch("needs_review", "The matching issue has no verified state.", issue)
    return ProjectIssueMatch("already_tracked", "Linear already tracks this outcome.", issue)


def match_project_issue(
    candidate: dict,
    inventory: ProjectIssueInventory,
    *,
    semantic_issue_id: str = "",
    semantic_relation: str = "",
) -> ProjectIssueMatch:
    if not inventory.complete:
        return ProjectIssueMatch("unavailable", "The full project issue inventory is unavailable.")
    if not _words(candidate.get("title")):
        return ProjectIssueMatch("needs_review", "The proposed action has no title.")
    ranked, rest = _ranked_issues(candidate, inventory)
    shortlist = (ranked + rest)[:8]
    if semantic_issue_id or semantic_relation:
        issue = next((
            row for row in shortlist
            if semantic_issue_id and semantic_issue_id in (row["id"], row.get("identifier"))
        ), None)
        # For a genuinely new outcome the model can return distinct without inventing an ID.
        if not issue and not (semantic_relation == "distinct" and not semantic_issue_id):
            return ProjectIssueMatch("needs_review", "The proposed issue link was not verified in this project.")
        if semantic_relation == "same_outcome":
            return _tracked(issue)
        if semantic_relation == "changed_scope":
            return ProjectIssueMatch("changed_scope", "The meeting changes the scope of an existing issue.", issue)
        if semantic_relation != "distinct":
            return ProjectIssueMatch("needs_review", "The relationship to the existing issue is unclear.", issue)
        # A distinct call for one issue must not mask a second exact match.
        if issue:
            ranked = [row for row in ranked if row["id"] != issue["id"]]
    exact = [issue for issue in ranked if _words(issue["title"]) == _words(candidate["title"])]
    if len(exact) == 1:
        issue = exact[0]
        details = [candidate.get("remaining_work"), *(candidate.get("acceptance_criteria") or [])]
        body = " ".join(_words(issue.get("description")))
        for detail in details:
            words = _words(detail)
            if words and words != _words(candidate["title"]) and " ".join(words) not in body:
                return ProjectIssueMatch("needs_review", "This title matches, but the proposed scope needs verification.", issue)
        return _tracked(issue)
    if len(exact) > 1:
        return ProjectIssueMatch("needs_review", "Multiple project issues have this title.")
    if ranked and semantic_relation != "distinct":
        return ProjectIssueMatch("needs_review", "Compare the proposed outcome with the existing issue.", ranked[0])
    return ProjectIssueMatch("new", "No matching outcome was identified in this project.")
