"""Project update intent and presentation for Roo's existing Linear workflow."""

from __future__ import annotations

import re
from html import escape
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .linear_project_reconciliation import ProjectFact, ProjectReconciliation


_UPDATE = r"(?:(?:a|an|the|any|our|this|that)\s+)?(?:(?:short|brief|concise|linear)\s+)*project\s+(?:status\s+)?updates?\b"
_WRITE = r"(?:post|publish|send|create|add|write|make|do|include|generate)"
_UPDATE_NOUN = re.compile(rf"\b{_UPDATE}")
_NO_UPDATE = re.compile(
    rf"\b(?:(?:don't|dont|do not|never)\s+{_WRITE}|no|not|without|skip|omit|avoid)\s+"
    rf"(?:(?:writing|creating|adding|posting|publishing|including|generating|sending)\s+)?{_UPDATE}"
)
# Only a request clause can authorize a write. A ticket to "post a project update"
# and somebody else's quoted words are evidence, not commands from the requester.
_REQUEST_PREFIX = (
    r"(?:^|[.!?;,\n]|\b(?:and(?: then)?|then|also|but)\b)\s*"
    r"(?:<@[a-z0-9]+>\s*|roo\s+)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:(?:don't|dont|do not)\s+forget\s+to\s+)?"
)
_PUBLISH = re.compile(
    rf"{_REQUEST_PREFIX}(?P<verb>{_WRITE})\s+(?P<object>{_UPDATE}|it\b|the update\b)"
    r"(?P<tail>(?:(?!\b(?:and|then|but)\b)[^.!?;,\n])*)"
)
_DRAFT = re.compile(rf"\b(?:draft|preview|prepare|suggest|show(?: me)?)\s+{_UPDATE}|\b{_UPDATE}\s+draft\b")
_HOLD = re.compile(
    r"\b(?:(?:don't|dont|do not|never)\s+(?:(?:actually|yet)\s+)?(?:post|publish|send|submit)|"
    r"(?:without|before)\s+(?:posting|publishing|sending)|"
    r"(?:draft|preview)\s+only|(?:only|just)\s+(?:a\s+)?(?:draft|preview)|"
    r"(?:if|unless|once|after|when)\s+(?:i|we|you|approved|confirmed|reviewed)\b)"
)


def meeting_request_text(text: str, params: dict[str, Any]) -> str:
    request = str(text or "")
    pasted = params.get("transcript")
    if isinstance(pasted, str) and pasted.strip():
        request = request.replace(pasted.strip(), "")
    # People regularly paste speaker turns, quotes and notes after the instruction.
    request = re.split(r"(?im)(?:^|\s)(?:meeting notes|transcript|notes)\s*:\s*", request, maxsplit=1)[0]
    request = re.sub(r"```.*?(?:```|$)", "", request, flags=re.S)
    request = re.sub(r'(?m)^\s*>.*$|“[^”\n]*”|"[^"\n]*"', "", request)
    request = re.sub(r"(?m)^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?"
                     r"(?:[A-Z][a-z]+(?: [A-Z][a-z]+){0,2}):[^\n]*$", "", request)
    return request.casefold().replace("’", "'")


_TASK_NOUN = (
    r"(?:(?:a|an|the|any|all|these|those|new|agreed|linear|remaining|concrete|actionable)\s+)*"
    r"(?:tasks?|issues?|tickets?|to[- ]?dos?|action items?)\b"
)
_TASK_REQUEST = re.compile(
    rf"{_REQUEST_PREFIX}(?P<verb>extract(?:\s+and\s+assign)?|draft|preview|review|suggest|list|show|identify|find|"
    rf"create|add|file|open|log)\s+{_TASK_NOUN}"
)
_NO_TASKS = re.compile(
    rf"\b(?:no|without|skip|omit|not|(?:don't|dont|do not|never)\s+"
    rf"(?:create|add|file|open|log|extract|list|suggest))\s+{_TASK_NOUN}"
)


_DEFERRED = re.compile(
    r"\b(?:if|unless|later|tomorrow|wait\s+(?:for|until)|"
    r"next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:after|once|until)\s+[^.!?\n]{0,70}\b(?:approv\w*|confirm\w*|review\w*))\b"
)
_OTHER_DESTINATION = re.compile(
    r"\bhere\b|\b(?:to|in|on|via)\s+(?:(?:the|this|that|our|your)\s+)?"
    r"(?:slack|email|notion|channel|chat|thread|#\S+|<#[^>]+>)"
)


def meeting_task_write_is_held(text: str, params: dict[str, Any]) -> bool:
    request = meeting_request_text(text, params)
    return bool(_NO_TASKS.search(request) or _DEFERRED.search(request))


def meeting_update_task_intent(
    text: str, params: dict[str, Any] | None = None
) -> Literal["none", "review", "create"]:
    """An update request doesn't grant permission to stage or create issues."""
    request = meeting_request_text(text, params or {})
    matches = list(_TASK_REQUEST.finditer(request))
    if not matches or _NO_TASKS.search(request):
        return "none"
    if _HOLD.search(request) or _DEFERRED.search(request):
        return "review"
    if any(m["verb"] in {"create", "add", "file", "open", "log"} for m in matches):
        return "create"
    return "review"


def project_update_intent(text: str, params: dict[str, Any]) -> Literal["none", "draft", "publish"]:
    """Distinguish a request for text from permission to write to Linear."""
    request = meeting_request_text(text, params)
    action = re.sub(r"[^a-z]", "", str(params.get("action") or "").lower())
    routed_to_update = action in {"projectupdate", "updateproject", "projectstatus"}
    if not _UPDATE_NOUN.search(request):
        return "draft" if routed_to_update else "none"

    draft_requested = bool(_DRAFT.search(request))
    if _NO_UPDATE.search(request):
        return "draft" if draft_requested and _HOLD.search(request) else "none"
    matches = list(_PUBLISH.finditer(request))
    brief_request = re.match(rf"^\s*(?:please\s+)?{_UPDATE}", request)
    source_request = re.search(rf"\b(?:for|into|as)\s+{_UPDATE}", request)
    if not (routed_to_update or draft_requested or matches or brief_request or source_request):
        return "none"
    if _HOLD.search(request) or _DEFERRED.search(request):
        return "draft"
    for match in matches:
        target = match["object"] + match["tail"]
        if re.search(r"\b(?:if|unless|once|later|tomorrow|after|when)\b", target):
            continue
        if _OTHER_DESTINATION.search(target):
            continue
        linear = bool(re.search(r"\blinear\s+project\b|\b(?:to|in|on|into)\s+linear\b", target))
        # A pronoun needs a single clear antecedent. Users can always specify
        # "publish the project update" in mixed task/update requests.
        if match["object"] in {"it", "the update"} and not (draft_requested and linear):
            continue
        if match["verb"] in {"post", "publish"} or linear:
            return "publish"
    return "draft"


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _has_evidence(ids: list[str], evidence: dict[str, str]) -> bool:
    return bool(ids) and all(evidence.get(ref, "").strip() for ref in ids)


def render_project_update(result: ProjectReconciliation, previous_body: str = "") -> str | None:
    """Render only supported changes and decisions in their reconciled priority order."""
    # A question or condition in the previous update can contain today's new
    # assertion verbatim. Compare whole statements rather than substrings.
    prior = {
        (_words(sentence), sentence.rstrip().endswith("?"))
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", previous_body)
    }
    seen: set[str] = set()

    def select(facts: list[ProjectFact], limit: int) -> list[str]:
        selected: list[str] = []
        for fact in facts:
            sentence = " ".join(fact.text.split())
            key = _words(sentence)
            if not key or key in seen or (key, sentence.endswith("?")) in prior:
                continue
            if not _has_evidence(fact.evidence_ids, result.evidence):
                continue
            seen.add(key)
            selected.append(sentence if sentence.endswith((".", "!", "?")) else sentence + ".")
            if len(selected) == limit:
                break
        return selected

    changes = select(result.changes, 3)
    decisions = select(result.decisions, 1)
    # Preserve the highest-priority change and decision intact, including qualifiers.
    # Drop lower-priority details before turning a short update into meeting minutes.
    while len(changes) > 1 and sum(len(line.split()) for line in changes + decisions) > 60:
        changes.pop()
    return "\n\n".join(filter(None, [" ".join(changes), " ".join(decisions)])) or None


def project_update_health(result: ProjectReconciliation) -> str | None:
    """Linear requires health. A project with no supported assessment stays a draft."""
    if result.health in {"onTrack", "atRisk", "offTrack"} and _has_evidence(
        result.health_evidence_ids, result.evidence
    ):
        return result.health
    return None


def _text(value: Any) -> str:
    return escape(str(value or ""), quote=False)


def _link(url: Any, label: Any) -> str:
    label = _text(label).replace("|", " ")
    if isinstance(url, str) and re.fullmatch(r"https://[^\s<>|]+", url):
        return f"<{_text(url)}|{label}>"
    return label


def format_review_item(item: dict[str, Any]) -> str:
    """One description next to the existing requester-bound review buttons."""
    details = [_text(item.get(key)) for key in ("assignee", "project") if item.get(key)]
    if item.get("due_date"):
        details.append(f"due {_text(item['due_date'])}")
    if item.get("effort_label"):
        details.append(_text(item["effort_label"]))
    return "\n".join(filter(None, [f"*{_text(item.get('title') or 'Untitled action')}*", " · ".join(details)]))


def format_meeting_result(
    created: list[dict[str, Any]],
    review_needed: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    *,
    project_update: dict[str, Any] | None = None,
    project_update_error: str | None = None,
    project_update_draft: str | None = None,
    contextual_review_mode: bool = False,
) -> str:
    lines: list[str] = []
    if project_update:
        label = (project_update.get("project") or {}).get("name") or "View in Linear"
        status = "Already published" if project_update.get("idempotentReplay") else "Created Linear project update"
        lines.append(f"{status} · {_link(project_update.get('url'), label)}")
    if project_update_draft:
        lines.append(f"*Draft project update*\n{_text(project_update_draft)}")
    if project_update_error:
        lines.append(_text(project_update_error))

    # Replays are successful operations but did not create a second ticket.
    tracked = [item["issue"] for item in created if (item.get("issue") or {}).get("idempotentReplay")]
    new = [item for item in created if not (item.get("issue") or {}).get("idempotentReplay")]
    if new:
        lines.append(f"Created {len(new)} Linear issue{'s' if len(new) != 1 else ''}.")
        for item in new[:5]:
            issue = item.get("issue") or {}
            label = " · ".join(filter(None, [issue.get("identifier"), item.get("title") or issue.get("title")]))
            details = [
                _text(item[key]) for key in ("assignee", "project", "effort_label") if item.get(key)
            ]
            if item.get("due_date"):
                details.append(f"due {_text(item['due_date'])}")
            lines.append(" · ".join([_link(issue.get("url"), label), *details]))
        if len(new) > 5:
            lines.append(f"Plus {len(new) - 5} more in Linear.")
    if review_needed:
        if contextual_review_mode and len(review_needed) == 1:
            lines.append("I found a possible issue in this thread. Please review before I create it.")
        else:
            count = len(review_needed)
            lines.append(f"{count} suggested action{'s need' if count != 1 else ' needs'} your approval.")

    terminal_count = 0
    unresolved: list[dict[str, Any]] = []
    for item in skipped:
        if item.get("duplicate"):
            tracked.append(item["duplicate"])
        elif str(item.get("reason") or "").startswith("Work is already "):
            terminal_count += 1
        else:
            unresolved.append(item)
    if tracked:
        links = list(dict.fromkeys(
            _link(issue.get("url"), issue.get("identifier") or issue.get("title"))
            for issue in tracked if issue.get("identifier") or issue.get("title")
        ))
        detail = " · " + ", ".join(links[:3]) if links else ""
        lines.append(f"Already tracked in Linear: {len(tracked)}{detail}.")
    if terminal_count:
        lines.append(f"Left {terminal_count} closed item{'s' if terminal_count != 1 else ''} out.")
    # Keep actionable failures visible, including for plain-text clients.
    for item in unresolved[:8]:
        title = _text(item.get("title") or "Action")
        reason = _text(item.get("reason") or "Needs clarification")
        details = "; ".join(f"{label}: {_text(item[key])}" for key, label in (
            ("project", "project"), ("assignee", "assignee"),
        ) if item.get(key))
        related = item.get("related_issue") or {}
        if related:
            reference = _link(related.get("url"), related.get("identifier") or related.get("title") or "existing issue")
            lines.append(f"{title} — {reason} · {reference}")
        else:
            lines.append(f"{title} — {reason}" + (f" ({details})" if details else ""))
    if len(unresolved) > 8:
        lines.append(f"{len(unresolved) - 8} more items need clarification.")
    return "\n".join(lines) or "No Linear issues were created from those meeting notes."
