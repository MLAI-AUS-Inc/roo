"""Convert coworking date phrases before they reach the booking API.

The caller supplies today's local date. No model, network, or system-clock
fallback is used here. Unsupported or ambiguous dates require clarification.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta


class CoworkingDateError(ValueError):
    """A date needs clarification before a coworking action can run."""


DATE_QUESTION = (
    "Which date do you mean? Try a date like `18 September`, `tomorrow`, "
    "or `next Friday`."
)
MONTHS = {
    alias: month
    for month in range(1, 13)
    for alias in (calendar.month_name[month].lower(), calendar.month_abbr[month].lower())
}
MONTHS["sept"] = 9
WEEKDAYS = {
    alias: day
    for day in range(7)
    for alias in (calendar.day_name[day].lower(), calendar.day_abbr[day].lower())
}
WEEKDAYS.update(tues=1, weds=2, thurs=3, thur=3)
MONTH = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
WEEKDAY = "(?:" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\.?"
DAY = r"\d{1,2}(?:st|nd|rd|th)?"
YEAR = r"(?:,?\s+\d{4})?"
MONTH_FIRST = re.compile(rf"({MONTH})\s*({DAY})({YEAR})")
DAY_FIRST = re.compile(rf"({DAY})\s+(?:of\s+)?({MONTH})({YEAR})")
RELATIVE = (
    r"day after tomorrow|today|tomorrow|tomorow|tommorow|tommorrow|yesterday"
    r"|in\s+(?:\d+|one|two|three|four|five|six|seven)\s+(?:days?|weeks?)"
)
DATE_REFERENCE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|{MONTH_FIRST.pattern}|{DAY_FIRST.pattern}"
    rf"|{RELATIVE}|(?:(?:this|next)\s+)?{WEEKDAY})(?!\w)",
    re.IGNORECASE,
)
NUMERIC_DATE = re.compile(r"(?<![\d-])\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b(?![\d-])")
UNRESOLVED_REFERENCE = re.compile(
    rf"\b(?:{MONTH}|{WEEKDAY}|next|this|last|week|weekend|month|year|"
    r"christmas|easter|sometime|later|soon|after|before|between|until|through|"
    r"every|each|except|instead|not)\b|\d",
    re.IGNORECASE,
)


def _without_times_and_mentions(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+>", " ", text, flags=re.IGNORECASE)
    # Coworking is a whole-day booking; supplied times do not select its date.
    return re.sub(
        r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})\b",
        " ", text, flags=re.IGNORECASE,
    )


def _parse_phrase(phrase: str, today: date) -> date:
    phrase = phrase.lower().strip().rstrip(".")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", phrase):
            return date.fromisoformat(phrase)

        match = MONTH_FIRST.fullmatch(phrase)
        if match:
            month_name, day_text, year_text = match.groups()
        else:
            match = DAY_FIRST.fullmatch(phrase)
            if match:
                day_text, month_name, year_text = match.groups()
        if match:
            month = MONTHS[month_name.rstrip(".")]
            day = int(re.sub(r"(?:st|nd|rd|th)$", "", day_text))
            year = int(year_text.strip(" ,")) if year_text else today.year
            resolved = date(year, month, day)
            # Without a year, choose the next occurrence. An explicit year
            # stays explicit; the backend still enforces its booking window.
            if not year_text and resolved < today:
                resolved = date(year + 1, month, day)
            return resolved

        aliases = {
            "today": 0, "tomorrow": 1, "tomorow": 1, "tommorow": 1,
            "tommorrow": 1, "day after tomorrow": 2, "yesterday": -1,
        }
        if phrase in aliases:
            return today + timedelta(days=aliases[phrase])
        relative = re.fullmatch(r"in\s+(\d+|one|two|three|four|five|six|seven)\s+(days?|weeks?)", phrase)
        if relative:
            amount, unit = relative.groups()
            words = {word: i for i, word in enumerate("one two three four five six seven".split(), 1)}
            count = int(amount) if amount.isdigit() else words[amount]
            return today + timedelta(days=count * (7 if unit.startswith("week") else 1))
        weekday = re.fullmatch(rf"(?:(this|next)\s+)?({WEEKDAY})", phrase)
        if weekday:
            qualifier, name = weekday.groups()
            offset = WEEKDAYS[name.rstrip(".")] - today.weekday()
            if qualifier != "this":
                offset %= 7
                if qualifier == "next" and offset == 0:
                    offset = 7
            return today + timedelta(days=offset)
    except (ValueError, OverflowError):
        raise CoworkingDateError(
            "That date isn't valid. Which day, month and year did you mean?"
        ) from None
    raise CoworkingDateError(DATE_QUESTION)


def _from_text(text: str, today: date, *, strict: bool) -> date | None:
    cleaned = _without_times_and_mentions(text.lower())
    if NUMERIC_DATE.search(cleaned):
        raise CoworkingDateError(
            "Which day and month do you mean? Please spell out the month, like `18 September`."
        )
    matches = list(DATE_REFERENCE.finditer(cleaned))
    if len(matches) > 1:
        raise CoworkingDateError("Which single date should I use? Please send one date at a time.")
    remainder = DATE_REFERENCE.sub(" ", cleaned)
    if UNRESOLVED_REFERENCE.search(remainder):
        raise CoworkingDateError(DATE_QUESTION)
    if matches:
        if strict and re.sub(r"[\s,.:!?]+", "", remainder):
            raise CoworkingDateError(DATE_QUESTION)
        return _parse_phrase(matches[0].group(), today)
    if strict and cleaned.strip():
        raise CoworkingDateError(DATE_QUESTION)
    return None


def resolve_coworking_date(
    raw_date: object,
    text: str,
    *,
    today: date,
    default_to_today: bool,
) -> str | None:
    """Resolve router parameters or recover a date omitted by the router.

    Check the original message for ambiguity even when the router has selected
    just one date from a range. Never default an unrecognised date to today.
    """
    from_text = _from_text(str(text or ""), today, strict=False)
    raw = str(raw_date or "").strip().strip(".,")
    resolved = _from_text(raw, today, strict=True) if raw else from_text
    if resolved is None and default_to_today:
        resolved = today
    return resolved.isoformat() if resolved else None
