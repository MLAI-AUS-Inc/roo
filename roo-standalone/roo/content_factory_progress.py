from __future__ import annotations

CONTENT_FACTORY_ARTICLE_COST_POINTS = 6
CONTENT_FACTORY_REQUEST_SOURCE = "roo_slackbot"


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
                    "text": (
                        f"💳 This paid run costs {CONTENT_FACTORY_ARTICLE_COST_POINTS} Roo points. "
                        "They were deducted when the run started."
                    ),
                }
            ],
        },
    ]
