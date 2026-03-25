"""Shared helpers for routing domain-driven content requests."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


DOMAIN_PATTERN = re.compile(r"\b(?:https?://)?([a-z0-9][a-z0-9.-]+\.[a-z]{2,})\b")
SLACK_FORMATTED_ENTITY_PATTERN = re.compile(r"<([^>|]+)\|([^>]+)>")
SLACK_PLAIN_URL_PATTERN = re.compile(r"<((?:https?://|mailto:)[^>]+)>")

SCAN_PATTERNS = (
    r"^(?:please\s+)?(?:re-?scan|scan|analy[sz]e|inspect)\b",
    r"\b(?:can|could|would)\s+you\s+(?:re-?scan|scan|analy[sz]e|inspect)\b",
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:the\s+)?(?:repo(?:sitory)?|codebase|domain|site|website|project)\b",
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:the\s+)?repo\s+for\s+(?:the\s+)?domain\b",
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:https?://)?[a-z0-9][a-z0-9.-]+\.[a-z]{2,}\b",
)
SCAFFOLD_PATTERNS = (
    r"\bscaffold\b",
    r"\bcreate\s+(?:the\s+)?articles?\b",
    r"\barticles?\s+directory\b",
    r"\barticles?\s+page\b",
    r"\bset\s+up\s+(?:the\s+)?(?:articles?|blog)\b",
    r"\bcreate\s+(?:the\s+)?blog\s+page\b",
    r"\badd\s+(?:the\s+)?blog\b",
)
RESEARCH_PATTERNS = (
    r"\bresearch\b.*\b(article|topic|keyword|blog(?:\s+post)?)\b",
    r"\b(?:best|next)\s+article\s+to\s+write\b",
    r"\bwhat\s+should\s+i\s+write\b",
    r"\b(?:recommend|suggest)\s+(?:a\s+)?(?:topic|article|keyword)\b",
)
PUBLISH_PR_PATTERNS = (
    r"\bpublish\b.*\b(?:article|bundle|draft|post)\b.*\bas\s+a\s+p\.?r\.?\b",
    r"\bpublish\b.*\bas\s+a\s+pull\s+request\b",
    r"\bturn\b.*\b(?:article|bundle|draft|post)\b.*\binto\s+a\s+p\.?r\.?\b",
    r"\bopen\b.*\b(?:a\s+)?(?:draft\s+)?p\.?r\.?\b",
)
WRITE_PATTERNS = (
    r"\bwrite\b.*\b(article|blog(?:\s+post)?|content)\b",
    r"\bgenerate\b.*\b(article|blog(?:\s+post)?|content)\b",
    r"\barticle\b",
    r"\bblog(?:\s+post)?\b",
)
CONTENT_TARGET_PATTERN = re.compile(
    r"\b(?:repo(?:sitory)?|codebase|domain|site|website|project|app)\b"
)
GITHUB_AUTH_PATTERNS = (
    r"\bgithub\s+integration\b",
    r"\b(?:re-?connect|connect|authori[sz]e|link|install|fix)\b.*\bgithub\b",
    r"\bgithub\b.*\b(?:re-?connect|connect|authori[sz]e|link|install|fix)\b",
    r"\bgithub\s+(?:auth|authentication|access|permission)\b",
)
THREAD_DOMAIN_REFERENCE_PATTERN = re.compile(
    r"\b(?:it|this|that|my\s+(?:domain|site|website|repo|repository|codebase)|"
    r"the\s+(?:domain|site|website|repo|repository|codebase))\b"
)
THREAD_FOLLOW_UP_PATTERN = re.compile(
    r"\b(?:do\s+it|go\s+ahead|continue|proceed|write\s+it|scan\s+it|scan\s+this|rescan)\b"
)


def normalize_slack_text(text: str) -> str:
    """Convert Slack link markup into plain text for routing."""
    if not text:
        return ""

    def replace_formatted_entity(match: re.Match[str]) -> str:
        target, label = match.groups()
        if target.startswith(("http://", "https://", "mailto:")):
            return label or target
        return label or target

    normalized = SLACK_FORMATTED_ENTITY_PATTERN.sub(replace_formatted_entity, text)
    normalized = SLACK_PLAIN_URL_PATTERN.sub(lambda match: match.group(1), normalized)
    return " ".join(normalized.split()).strip()


def extract_domain(text: str) -> Optional[str]:
    """Extract the first domain from plain text or Slack-formatted text."""
    normalized = normalize_slack_text(text).lower()
    match = DOMAIN_PATTERN.search(normalized)
    return match.group(1) if match else None


def detect_content_action(text: str) -> Optional[str]:
    """Infer the main content action requested by the user."""
    text_lower = normalize_slack_text(text).lower().strip()

    if any(re.search(pattern, text_lower) for pattern in SCAN_PATTERNS):
        return "scan"
    if any(re.search(pattern, text_lower) for pattern in SCAFFOLD_PATTERNS):
        return "scaffold"
    if any(re.search(pattern, text_lower) for pattern in RESEARCH_PATTERNS):
        return "research"
    if any(re.search(pattern, text_lower) for pattern in PUBLISH_PR_PATTERNS):
        return "publish_pr"
    if any(re.search(pattern, text_lower) for pattern in WRITE_PATTERNS):
        return "write"
    return None


def is_explicit_scan_request(text: str, action: Optional[str] = None) -> bool:
    """Return True when the user is clearly asking Roo to scan something."""
    if str(action or "").strip().lower() == "scan":
        return True

    text_lower = normalize_slack_text(text).lower().strip()
    return any(re.search(pattern, text_lower) for pattern in SCAN_PATTERNS)


def parse_routing_intent(
    text: str,
    *,
    thread_skill_name: Optional[str] = None,
    thread_domain: Optional[str] = None,
    thread_job_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a deterministic routing decision for common content flows."""
    normalized = normalize_slack_text(text)
    text_lower = normalized.lower().strip()

    explicit_domain = extract_domain(normalized)
    references_thread_domain = bool(
        thread_domain
        and (
            THREAD_DOMAIN_REFERENCE_PATTERN.search(text_lower)
            or THREAD_FOLLOW_UP_PATTERN.search(text_lower)
        )
    )
    domain = explicit_domain or (thread_domain if references_thread_domain else None)
    action = detect_content_action(normalized)

    if any(re.search(pattern, text_lower) for pattern in GITHUB_AUTH_PATTERNS):
        params: Dict[str, Any] = {}
        if domain:
            params["domain"] = domain
        if action == "scan":
            params["action"] = "scan"
        return {"skill_name": "github-integration", "params": params}

    if action == "scan" and (domain or CONTENT_TARGET_PATTERN.search(text_lower)):
        params = {"action": "scan"}
        if domain:
            params["domain"] = domain
        return {"skill_name": "content-factory", "params": params}

    if action == "scaffold":
        params = {"action": "scaffold"}
        if domain:
            params["domain"] = domain
        return {"skill_name": "content-factory", "params": params}

    if action == "research":
        params = {}
        if domain:
            params["domain"] = domain
        return {"skill_name": "content-factory", "params": params}

    if (
        action == "publish_pr"
        and thread_skill_name == "content-factory"
        and (thread_job_id or thread_domain)
    ):
        params = {"action": "publish_pr"}
        if thread_job_id:
            params["job_id"] = thread_job_id
        if domain:
            params["domain"] = domain
        return {"skill_name": "content-factory", "params": params}

    if action == "write" and (
        domain or re.search(r"\b(?:article|blog(?:\s+post)?|content)\b", text_lower)
    ):
        params = {"action": "write"}
        if domain:
            params["domain"] = domain
        return {"skill_name": "content-factory", "params": params}

    if (
        thread_skill_name == "content-factory"
        and thread_domain
        and THREAD_FOLLOW_UP_PATTERN.search(text_lower)
    ):
        params = {"domain": thread_domain}
        if "scan" in text_lower or "rescan" in text_lower:
            params["action"] = "scan"
        return {"skill_name": "content-factory", "params": params}

    return None
