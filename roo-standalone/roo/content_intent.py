"""Shared helpers for routing domain-driven content requests."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


DOMAIN_PATTERN = re.compile(r"\b(?:https?://)?([a-z0-9][a-z0-9.-]+\.[a-z]{2,})\b")
SLACK_FORMATTED_ENTITY_PATTERN = re.compile(r"<([^>|]+)\|([^>]+)>")
SLACK_PLAIN_URL_PATTERN = re.compile(r"<((?:https?://|mailto:)[^>]+)>")
SLACK_USER_MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")
TRAILING_DELEGATION_PATTERN = re.compile(
    r"^(?P<body>.+?)\s+(?P<keyword>as|for)\s+(?P<mention><@[A-Z0-9]+>)\s*$",
    re.IGNORECASE,
)

# Scan/analyse requests must name an explicit target (repo, codebase, domain,
# site, or a literal domain name). Bare verbs like "analyse this proposal" or
# "inspect the CSV" are NOT content scans — they fall through to the LLM router.
SCAN_PATTERNS = (
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:the\s+|my\s+|our\s+)?(?:repo(?:sitory)?|codebase|domain|site|website)\b",
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:the\s+)?repo\s+for\s+(?:the\s+)?domain\b",
    r"\b(?:re-?scan|scan|analy[sz]e|inspect)\s+(?:https?://)?[a-z0-9][a-z0-9.-]+\.[a-z]{2,}\b",
)
SCAFFOLD_PATTERNS = (
    r"\bscaffold\b",
    r"\bcreate\s+(?:the\s+|a\s+|an\s+|my\s+|our\s+)?articles?\b",
    r"\barticles?\s+directory\b",
    r"\barticles?\s+page\b",
    r"\bset\s+up\s+(?:the\s+|a\s+|an\s+|my\s+|our\s+)?(?:articles?|blog)\b",
    r"\bcreate\s+(?:the\s+|a\s+|an\s+|my\s+|our\s+)?blog\s+page\b",
    r"\badd\s+(?:the\s+|a\s+|an\s+|my\s+|our\s+)?blog\b",
)
RESEARCH_PATTERNS = (
    # "(?!\s+papers?)" keeps noun usages like "research paper on X" out.
    r"\bresearch\b(?!\s+papers?\b).*\b(article|topic|keyword|blog(?:\s+post)?)\b",
    r"\bdiscover\b.*\b(topic|topics|article|keyword|keywords|blog(?:\s+post)?)\b",
    r"\b(?:best|next)\s+article\s+to\s+write\b",
    r"\bwhat\s+should\s+i\s+write\b",
    r"\b(?:recommend|suggest)\s+(?:a\s+)?(?:topic|article|keyword)\b",
)
PUBLISH_PR_PATTERNS = (
    r"\bpublish\b.*\b(?:article|bundle|draft|post)\b.*\bas\s+a\s+p\.?r\.?\b",
    r"\bpublish\b.*\bas\s+a\s+pull\s+request\b",
    r"\bpush\b.*\b(?:article|bundle|draft|post)\b.*\bto\s+(?:a\s+)?p\.?r\.?\b",
    r"\bpush\b.*\b(?:article|bundle|draft|post)\b.*\bto\s+(?:a\s+)?pull\s+request\b",
    r"\bturn\b.*\b(?:article|bundle|draft|post)\b.*\binto\s+a\s+p\.?r\.?\b",
    r"\bopen\b.*\b(?:a\s+)?(?:draft\s+)?p\.?r\.?\b",
)
# Writing requires verb + object ("write/draft/generate ... article/blog/content").
# A bare mention of "article" or "blog" is vocabulary, not intent — "summarise
# this article" must NOT route here.
WRITE_PATTERNS = (
    r"\bwrite\b.*\b(article|blog(?:\s+post)?|content)\b",
    r"\bgenerate\b.*\b(article|blog(?:\s+post)?|content)\b",
    r"\bdraft\b.*\b(article|blog(?:\s+post)?)\b",
)
# NOTE (Phase 3, routing redesign): the regex routing tables that used to live
# here (GITHUB_AUTH_PATTERNS, CONTENT_TARGET_PATTERN, thread follow-up
# patterns, parse_routing_intent) were deleted — routing is done by the LLM
# tool-calling router over the SKILL.md catalog (roo/router.py). The patterns
# below survive only as helpers for delegation parsing and executor flows.


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


def extract_slack_user_mention(raw_value: str | None) -> Optional[str]:
    """Extract a Slack user ID from a real mention like <@U123ABC>."""
    value = str(raw_value or "").strip()
    match = SLACK_USER_MENTION_PATTERN.fullmatch(value)
    return match.group(1) if match else None


def extract_content_factory_delegation(text: str) -> tuple[str, Optional[Dict[str, Any]]]:
    """Strip a trailing content-factory delegation clause like `as <@U123>`.

    Returns the cleaned text plus delegation metadata when the clause applies.
    The backward-compatible `for <@U123>` alias only applies to write/article
    requests so normal phrasing like `write an article for domain.com` is not
    misclassified.
    """
    normalized = normalize_slack_text(text).strip()
    if not normalized:
        return normalized, None

    match = TRAILING_DELEGATION_PATTERN.match(normalized)
    if not match:
        return normalized, None

    body = str(match.group("body") or "").strip()
    keyword = str(match.group("keyword") or "").strip().lower()
    target_slack_user_id = extract_slack_user_mention(match.group("mention"))
    if not body or not target_slack_user_id:
        return normalized, None

    action = detect_content_action(body)
    if keyword == "for" and action != "write":
        return normalized, None
    if action not in {"scan", "scaffold", "research", "publish_pr", "write"}:
        return normalized, None

    return body, {
        "effective_slack_user_id": target_slack_user_id,
        "delegation_keyword": keyword,
        "content_action": action,
    }


def is_explicit_scan_request(text: str, action: Optional[str] = None) -> bool:
    """Return True when the user is clearly asking Roo to scan something."""
    if str(action or "").strip().lower() == "scan":
        return True

    text_lower = normalize_slack_text(text).lower().strip()
    return any(re.search(pattern, text_lower) for pattern in SCAN_PATTERNS)


