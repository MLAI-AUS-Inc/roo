from typing import Iterable


MAX_SECTION_CHARS = 2800
MAX_EMAIL_SECTIONS_PER_MESSAGE = 45
MAX_FALLBACK_EMAIL_CHARS = 3500
MAX_FALLBACK_CHARS = 4000


def _chunk_email_lines(emails: Iterable[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for email in emails:
        line = str(email or "").strip().lower()
        if not line:
            continue
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > MAX_SECTION_CHARS:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if current_length else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_candidate_email_payloads(data: dict) -> list[dict]:
    """Build one or more Slack-safe messages without dropping email addresses."""
    emails = [
        value.strip().lower()
        for value in data.get("emails") or []
        if isinstance(value, str) and value.strip()
    ]
    count = int(data.get("eligible_count") or len(emails))
    if not emails:
        message = "No active members currently have 100 or more lifetime-earned Roo Points and a usable email."
        return [{
            "message": message,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
        }]

    email_sections = _chunk_email_lines(emails)
    section_groups: list[list[str]] = []
    current_group: list[str] = []
    current_group_length = 0
    for section in email_sections:
        added_length = len(section) + (1 if current_group else 0)
        if current_group and (
            len(current_group) >= MAX_EMAIL_SECTIONS_PER_MESSAGE
            or current_group_length + added_length > MAX_FALLBACK_EMAIL_CHARS
        ):
            section_groups.append(current_group)
            current_group = []
            current_group_length = 0
        current_group.append(section)
        current_group_length += len(section) + (1 if current_group_length else 0)
    if current_group:
        section_groups.append(current_group)

    payloads = []
    for sections in section_groups:
        part = len(payloads) + 1
        heading = (
            f"Eligible member emails ({count})"
            if part == 1
            else f"Eligible member emails ({count}) - continued"
        )
        plain_email_list = "\n".join(sections)
        message = f"{heading}\n{plain_email_list}"
        if len(message) > MAX_FALLBACK_CHARS:
            raise ValueError("Candidate email fallback exceeds Slack's text limit")
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{heading}*\n"
                        "Active MLAI users with at least 100 lifetime-earned contribution points. "
                        "Copy these addresses into Luma:"
                    ),
                },
            },
            *[
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": section},
                }
                for section in sections
            ],
        ]
        payloads.append({"message": message, "blocks": blocks})
    return payloads
