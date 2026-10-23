from __future__ import annotations

CONTENT_FACTORY_REQUEST_SOURCE = "roo_slackbot"


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


def build_content_factory_live_cost_text(domain: str | None, cost_points: int | None = None) -> str:
    if type(cost_points) is int and cost_points >= 0:
        return f"💳 Confirmed article price: {cost_points} Roo points."
    return "💳 Article pricing is confirmed with you before generation starts."


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
