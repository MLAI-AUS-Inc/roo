from __future__ import annotations

CONTENT_FACTORY_ARTICLE_COST_POINTS = 6
CONTENT_FACTORY_REQUEST_SOURCE = "roo_slackbot"
FREE_CONTENT_FACTORY_DOMAINS = {"mlai.au"}


def normalize_content_factory_domain(domain: str | None) -> str:
    if not domain:
        return ""

    normalized = domain.strip().lower()
    if normalized.startswith("https://"):
        normalized = normalized[8:]
    elif normalized.startswith("http://"):
        normalized = normalized[7:]

    if normalized.startswith("www."):
        normalized = normalized[4:]

    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]

    return normalized


def get_content_factory_article_cost_points(domain: str | None) -> int:
    normalized_domain = normalize_content_factory_domain(domain)
    if normalized_domain in FREE_CONTENT_FACTORY_DOMAINS:
        return 0
    return CONTENT_FACTORY_ARTICLE_COST_POINTS


def is_free_content_factory_domain(domain: str | None) -> bool:
    return get_content_factory_article_cost_points(domain) == 0


def build_content_factory_live_cost_text(domain: str | None) -> str:
    if is_free_content_factory_domain(domain):
        normalized_domain = normalize_content_factory_domain(domain) or "this domain"
        return f"💳 Articles for {normalized_domain} are free. No Roo points will be deducted for this run."

    return (
        f"💳 This paid run costs {CONTENT_FACTORY_ARTICLE_COST_POINTS} Roo points. "
        "They were deducted when the run started."
    )


def build_live_status_blocks(
    domain: str,
    *,
    summary_text: str,
    include_decision_stage: bool,
    current_stage: str = "preparing",
) -> list[dict]:
    stages = [
        ("preparing", "Preparing run"),
        ("researching", "Researching"),
    ]
    if include_decision_stage:
        stages.append(("awaiting_confirmation", "Awaiting your decision"))
    stages.extend(
        [
            ("writing", "Writing draft"),
            ("final_checks", "Final checks"),
            ("complete", "Complete"),
        ]
    )

    stage_lines = []
    current_seen = False
    for stage_key, label in stages:
        if stage_key == current_stage:
            icon = "⏳"
            current_seen = True
        elif current_seen:
            icon = "⬜"
        else:
            icon = "✅"
        stage_lines.append(f"{icon} {label}")

    text = (
        f"*Content Factory for {domain}*\n\n"
        f"*Now:* {summary_text}\n\n"
        f"{chr(10).join(stage_lines)}"
    )

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": build_content_factory_live_cost_text(domain),
                }
            ],
        },
    ]
