"""Safe Slack presentation and interaction contracts for Admin Roo memory."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID


ADMIN_BRAIN_UNAVAILABLE_MESSAGE = (
    "Organisational memory is temporarily unavailable. I haven't used another "
    "data source or guessed."
)
ADMIN_BRAIN_ACCESS_DENIED_MESSAGE = (
    "I can't access organisational memory for this request in this Slack context."
)
ADMIN_BRAIN_FEEDBACK_ACTIONS = {
    "admin_brain_feedback_helpful": "relevant",
    "admin_brain_feedback_stale": "stale",
    "admin_brain_feedback_missing": "missing",
}
ADMIN_BRAIN_INCORRECT_ACTION = "admin_brain_feedback_incorrect"
ADMIN_BRAIN_INCORRECT_CALLBACK = "admin_brain_incorrect_feedback"
ADMIN_ACTION_APPROVE = "admin_action_approve"
ADMIN_ACTION_REJECT = "admin_action_reject"
ADMIN_ACTION_REJECT_CALLBACK = "admin_action_reject_submission"

ADMIN_ACTION_LABELS = {
    "draft_gmail": "Gmail draft",
    "draft_slack_post": "Slack post draft",
    "draft_notion_update": "Notion update draft",
    "create_linear_issue": "Create Linear issue",
    "update_linear_issue": "Update Linear issue",
}
ADMIN_ACTION_STATUS_LABELS = {
    "proposed": "Proposed",
    "awaiting_approval": "Awaiting approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "executing": "Executing",
    "completed": "Completed",
    "failed": "Failed",
    "stale": "Needs fresh approval",
    "reversing": "Reversing",
    "reversed": "Reversed",
    "cancelled": "Cancelled",
}

WARNING_LABELS = {
    "limited_evidence": "The available evidence is limited.",
    "stale_memory": "Some supporting memory may be stale.",
    "unresolved_conflict": "The sources contain an unresolved conflict.",
    "semantic_retrieval_unavailable": "Semantic retrieval was unavailable; text retrieval was used.",
    "partial_source_freshness": "One or more connected sources may not be fully up to date.",
    "no_authorized_memory_classes": "No authorised memory class was available for this request.",
}


def slack_safe_text(value: Any, *, limit: Optional[int] = None) -> str:
    """Neutralise Slack mentions and link markup in untrusted answer/source text."""

    text = str(value or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if limit is not None:
        text = text[: max(int(limit), 0)]
    return text


def _safe_source_link(url: Any, label: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not any(character in value for character in "<>|")
    ):
        return f"<{value}|{label}>"
    return label


def _display_datetime(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return slack_safe_text(raw, limit=80)
    return parsed.astimezone().strftime("%d %b %Y, %H:%M %Z")


def _chunk_text(value: str, *, limit: int = 2800) -> list[str]:
    remaining = str(value or "").strip()
    chunks = []
    while remaining and len(chunks) < 8:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks or [""]


def _feedback_value(
    *,
    query_id: str,
    requester_user_id: str,
    primary_claim_id: Optional[str],
) -> str:
    return json.dumps(
        {
            "query_id": query_id,
            "requester_user_id": requester_user_id,
            "claim_id": primary_claim_id or "",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_feedback_value(raw: Any) -> dict[str, str]:
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "query_id": str(payload.get("query_id") or "").strip()[:64],
        "requester_user_id": str(payload.get("requester_user_id") or "").strip()[:32],
        "claim_id": str(payload.get("claim_id") or "").strip()[:64],
    }


def build_admin_brain_response(
    payload: dict,
    *,
    requester_user_id: str,
    primary_claim_id: Optional[str] = None,
) -> dict:
    answer = slack_safe_text(payload.get("answer") or "")
    query_id = str(payload.get("query_id") or "").strip()
    warnings = [
        WARNING_LABELS.get(str(value), "Organisational memory returned a caution for this answer.")
        for value in (payload.get("warnings") or [])
    ]
    warnings = list(dict.fromkeys(warnings))
    freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
    latest = _display_datetime(freshness.get("latest_evidence_at"))
    stale = bool(freshness.get("contains_stale_memory")) or "stale_memory" in (
        payload.get("warnings") or []
    )
    citations = [
        citation
        for citation in list(payload.get("citations") or [])[:5]
        if isinstance(citation, dict)
        and any(
            citation.get(field)
            for field in ("source_id", "source_version_id", "source_url", "label")
        )
    ]
    has_citations = bool(citations)
    sufficiency = str(payload.get("evidence_sufficiency") or "unknown").replace("_", " ")
    confidence = payload.get("confidence")
    try:
        confidence_text = f"{max(0, min(float(confidence), 1)):.0%} confidence"
    except (TypeError, ValueError):
        confidence_text = "confidence unavailable"
    if not has_citations:
        evidence_context = (
            "⚪ No authorised evidence selected · "
            f"{slack_safe_text(sufficiency.title())} evidence · {confidence_text}"
        )
    else:
        freshness_label = "⚠️ Contains stale memory" if stale else "✅ Current authorised evidence"
        time_label = latest or "time unavailable"
        evidence_context = (
            f"{freshness_label} · Latest evidence: {slack_safe_text(time_label)} · "
            f"{slack_safe_text(sufficiency.title())} evidence · {confidence_text}"
        )

    blocks: list[dict] = []
    for index, chunk in enumerate(_chunk_text(answer)):
        prefix = "*🔒 Internal organisational memory*\n" if index == 0 else ""
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{prefix}{chunk}"},
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": evidence_context,
                }
            ],
        }
    )
    if warnings:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Warnings*\n" + "\n".join(f"• {slack_safe_text(item)}" for item in warnings),
                },
            }
        )

    citation_lines = []
    for citation in citations:
        label = slack_safe_text(
            citation.get("label") or citation.get("provider") or "Source",
            limit=180,
        )
        link = _safe_source_link(citation.get("source_url"), label)
        occurred = _display_datetime(citation.get("occurred_at"))
        provider = slack_safe_text(citation.get("provider") or "", limit=60)
        detail = " · ".join(value for value in (provider, occurred) if value)
        citation_lines.append(f"• {link}" + (f" — {detail}" if detail else ""))
    if citation_lines:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Sources*\n" + "\n".join(citation_lines),
                },
            }
        )

    follow_up = slack_safe_text(payload.get("suggested_follow_up") or "", limit=900)
    if follow_up:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Suggested follow-up: {follow_up}"}],
            }
        )

    if query_id:
        value = _feedback_value(
            query_id=query_id,
            requester_user_id=requester_user_id,
            primary_claim_id=primary_claim_id,
        )
        buttons = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Helpful"},
                "style": "primary",
                "action_id": "admin_brain_feedback_helpful",
                "value": value,
            }
        ]
        if primary_claim_id:
            buttons.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Incorrect"},
                    "style": "danger",
                    "action_id": ADMIN_BRAIN_INCORRECT_ACTION,
                    "value": value,
                }
            )
        buttons.extend(
            [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Stale"},
                    "action_id": "admin_brain_feedback_stale",
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Missing context"},
                    "action_id": "admin_brain_feedback_missing",
                    "value": value,
                },
            ]
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"admin_brain_feedback_{query_id[:24]}",
                "elements": buttons,
            }
        )

    fallback_lines = [answer]
    if citation_lines:
        fallback_lines.extend(("", "Sources", *citation_lines))
    return {
        "message": "\n".join(fallback_lines).strip(),
        "blocks": blocks[:50],
        "data": {
            "query_id": query_id,
            "primary_claim_id": primary_claim_id,
            "admin_brain": True,
        },
    }


def build_incorrect_feedback_modal(
    feedback: dict[str, str],
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict:
    metadata = json.dumps(
        {
            **feedback,
            "team_id": str(team_id or ""),
            "channel_id": str(channel_id or ""),
            "thread_ts": str(thread_ts or ""),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "type": "modal",
        "callback_id": ADMIN_BRAIN_INCORRECT_CALLBACK,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Correct memory answer"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "admin_brain_correction",
                "label": {"type": "plain_text", "text": "What should be corrected?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "correction_text",
                    "multiline": True,
                    "min_length": 1,
                    "max_length": 4000,
                },
            }
        ],
    }


def parse_incorrect_feedback_submission(payload: dict) -> dict[str, str]:
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    if view.get("callback_id") != ADMIN_BRAIN_INCORRECT_CALLBACK:
        return {}
    try:
        metadata = json.loads(str(view.get("private_metadata") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    values = view.get("state", {}).get("values", {})
    correction = (
        values.get("admin_brain_correction", {})
        .get("correction_text", {})
        .get("value", "")
    )
    if not isinstance(metadata, dict):
        return {}
    return {
        "query_id": str(metadata.get("query_id") or "").strip()[:64],
        "requester_user_id": str(metadata.get("requester_user_id") or "").strip()[:32],
        "claim_id": str(metadata.get("claim_id") or "").strip()[:64],
        "team_id": str(metadata.get("team_id") or "").strip()[:32],
        "channel_id": str(metadata.get("channel_id") or "").strip()[:32],
        "thread_ts": str(metadata.get("thread_ts") or "").strip()[:64],
        "correction_text": str(correction or "").strip()[:4000],
    }


def _canonical_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (TypeError, ValueError, AttributeError):
        return ""


def _admin_action_value(proposal_id: Any) -> str:
    return json.dumps(
        {"proposal_id": _canonical_uuid(proposal_id)},
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_admin_action_value(raw: Any) -> dict[str, str]:
    try:
        payload = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    proposal_id = _canonical_uuid(payload.get("proposal_id"))
    return {"proposal_id": proposal_id} if proposal_id else {}


def _admin_action_preview(proposal: dict) -> list[str]:
    payload = (
        proposal.get("input_payload")
        if isinstance(proposal.get("input_payload"), dict)
        else {}
    )
    action_type = str(proposal.get("action_type") or "")
    preview: list[str] = []
    if action_type == "draft_gmail":
        recipients = ", ".join(str(value) for value in payload.get("to") or [])
        if recipients:
            preview.append(f"*To:* {slack_safe_text(recipients, limit=500)}")
        if payload.get("subject"):
            preview.append(
                f"*Subject:* {slack_safe_text(payload['subject'], limit=500)}"
            )
        if payload.get("body"):
            preview.append(slack_safe_text(payload["body"], limit=1600))
    elif action_type == "draft_slack_post":
        if payload.get("channel_id"):
            preview.append(
                f"*Destination:* `{slack_safe_text(payload['channel_id'], limit=32)}`"
            )
        if payload.get("text"):
            preview.append(slack_safe_text(payload["text"], limit=1800))
    elif action_type == "draft_notion_update":
        if payload.get("page_id"):
            preview.append(
                f"*Page:* `{slack_safe_text(payload['page_id'], limit=255)}`"
            )
        if payload.get("title"):
            preview.append(
                f"*Title:* {slack_safe_text(payload['title'], limit=500)}"
            )
        if payload.get("body"):
            preview.append(slack_safe_text(payload["body"], limit=1400))
    elif action_type in {"create_linear_issue", "update_linear_issue"}:
        if payload.get("issue_id"):
            preview.append(
                f"*Issue:* `{slack_safe_text(payload['issue_id'], limit=255)}`"
            )
        if payload.get("title"):
            preview.append(
                f"*Title:* {slack_safe_text(payload['title'], limit=700)}"
            )
        if payload.get("description"):
            preview.append(slack_safe_text(payload["description"], limit=1200))
        scope = " · ".join(
            f"{label}: `{slack_safe_text(payload.get(key), limit=255)}`"
            for key, label in (("team_id", "Team"), ("project_id", "Project"))
            if payload.get(key)
        )
        if scope:
            preview.append(scope)
    return preview


def build_admin_action_response(
    proposal: dict,
    *,
    include_controls: bool = True,
) -> dict:
    """Render backend-controlled action state without placing content in button values."""

    proposal_id = _canonical_uuid(proposal.get("id"))
    action_type = str(proposal.get("action_type") or "")
    status = str(proposal.get("status") or "")
    risk = str(proposal.get("risk_level") or "unknown").lower()
    action_label = ADMIN_ACTION_LABELS.get(action_type, "Controlled action")
    status_label = ADMIN_ACTION_STATUS_LABELS.get(status, status.replace("_", " ").title())
    requested_by = slack_safe_text(proposal.get("requested_by_slack_id"), limit=32)
    summary = (
        f"*{slack_safe_text(action_label)}*\n"
        f"Status: *{slack_safe_text(status_label)}* · "
        f"Risk: *{slack_safe_text(risk.title())}*"
    )
    if requested_by:
        summary += f" · Requested by `{requested_by}`"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}}
    ]
    preview = _admin_action_preview(proposal)
    if preview:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(preview)[:2900]},
            }
        )

    error_text = slack_safe_text(proposal.get("error_text"), limit=900)
    if error_text:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"⚠️ {error_text}"}],
            }
        )

    approval = (
        proposal.get("approval")
        if isinstance(proposal.get("approval"), dict)
        else {}
    )
    if (
        include_controls
        and proposal_id
        and bool(proposal.get("requires_approval"))
        and bool(approval.get("pending"))
        and status in {"awaiting_approval", "stale"}
    ):
        value = _admin_action_value(proposal_id)
        blocks.append(
            {
                "type": "actions",
                "block_id": f"admin_action_review_{proposal_id[:24]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve & execute"},
                        "style": "primary",
                        "action_id": ADMIN_ACTION_APPROVE,
                        "value": value,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Approve action?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    "The backend will refresh live preconditions, "
                                    "record your identity, and execute only if still safe."
                                ),
                            },
                            "confirm": {"type": "plain_text", "text": "Approve"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "action_id": ADMIN_ACTION_REJECT,
                        "value": value,
                    },
                ],
            }
        )

    fallback = (
        f"{action_label} — {status_label} ({risk} risk)"
        + (f" — proposal {proposal_id}" if proposal_id else "")
    )
    return {
        "message": fallback,
        "blocks": blocks,
        "data": {
            "admin_action": True,
            "proposal_id": proposal_id,
            "status": status,
        },
    }


def build_admin_action_list_response(payload: dict) -> dict:
    rows = [
        row
        for row in (payload.get("actions") or [])
        if isinstance(row, dict)
        and str(row.get("status") or "") in {"awaiting_approval", "stale"}
    ][:8]
    if not rows:
        return {
            "message": "There are no controlled actions awaiting review.",
            "blocks": None,
            "data": {"admin_action": True, "pending_count": 0},
        }
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Admin Roo actions awaiting review ({len(rows)})",
            },
        }
    ]
    for row in rows:
        card = build_admin_action_response(row)
        blocks.extend(card["blocks"])
        if len(blocks) < 49:
            blocks.append({"type": "divider"})
    return {
        "message": f"{len(rows)} controlled action(s) awaiting review.",
        "blocks": blocks[:50],
        "data": {"admin_action": True, "pending_count": len(rows)},
    }


def build_admin_action_reject_modal(
    action: dict[str, str],
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> dict:
    metadata = json.dumps(
        {
            **action,
            "team_id": str(team_id or ""),
            "channel_id": str(channel_id or ""),
            "thread_ts": str(thread_ts or ""),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "type": "modal",
        "callback_id": ADMIN_ACTION_REJECT_CALLBACK,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "Reject action"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "admin_action_rejection",
                "label": {"type": "plain_text", "text": "Why should this be rejected?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reason",
                    "multiline": True,
                    "min_length": 1,
                    "max_length": 512,
                },
            }
        ],
    }


def parse_admin_action_reject_submission(payload: dict) -> dict[str, str]:
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    if view.get("callback_id") != ADMIN_ACTION_REJECT_CALLBACK:
        return {}
    try:
        metadata = json.loads(str(view.get("private_metadata") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    reason = (
        view.get("state", {})
        .get("values", {})
        .get("admin_action_rejection", {})
        .get("reason", {})
        .get("value", "")
    )
    proposal_id = _canonical_uuid(metadata.get("proposal_id"))
    return {
        "proposal_id": proposal_id,
        "team_id": str(metadata.get("team_id") or "").strip()[:32],
        "channel_id": str(metadata.get("channel_id") or "").strip()[:32],
        "thread_ts": str(metadata.get("thread_ts") or "").strip()[:64],
        "reason": str(reason or "").strip()[:512],
    }
