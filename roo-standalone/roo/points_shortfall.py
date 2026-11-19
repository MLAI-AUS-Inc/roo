"""Explain a Roo Points shortfall using the backend's own numbers.

MLAI backend rejects a debit with ``Insufficient balance: 4 < 8 required`` (whole
points) or ``Insufficient microroo balance: 4000000 < 8000000 required``. Members
need those numbers to know what to do, so Roo shows them instead of a vague
"not enough points" line.
"""

import re
from decimal import Decimal
from typing import Optional

MICROROO_PER_POINT = Decimal(1_000_000)
FAILURE_CODE_PREFIX = "insufficient_points:"

_SHORTFALL = re.compile(
    r"insufficient\s+(?P<micro>microroo\s+)?balance:\s*"
    r"(?P<available>\d+(?:\.\d+)?)\s*<\s*(?P<needed>\d+(?:\.\d+)?)\s*required",
    re.IGNORECASE,
)


def _points(value: Decimal) -> str:
    return format(value.normalize(), "f")


def parse_points_shortfall(detail: str) -> Optional[tuple[str, str]]:
    """Return (available, needed) in whole Roo Points, or None."""
    match = _SHORTFALL.search(str(detail or ""))
    if not match:
        return None
    available = Decimal(match["available"])
    needed = Decimal(match["needed"])
    if match["micro"]:
        available /= MICROROO_PER_POINT
        needed /= MICROROO_PER_POINT
    return _points(available), _points(needed)


def format_points_shortfall(available: str, needed: str) -> str:
    return (
        f"Not enough Roo Points: **{available}** available, **{needed}** needed. "
        "DM Roo `topup` to add more."
    )


def public_points_error(detail: str) -> str:
    """Rewrite a backend shortfall with its numbers; leave other details as is."""
    shortfall = parse_points_shortfall(detail)
    if shortfall is None:
        return str(detail or "").strip()
    return format_points_shortfall(*shortfall)


def shortfall_failure_code(detail: str) -> Optional[str]:
    """A bounded, storable code for a shortfall (numbers only, no free text)."""
    shortfall = parse_points_shortfall(detail)
    if shortfall is None:
        return None
    return f"{FAILURE_CODE_PREFIX}{shortfall[0]}:{shortfall[1]}"


def shortfall_from_failure_code(code: str) -> Optional[str]:
    """Render the reason stored by ``shortfall_failure_code``."""
    code = str(code or "")
    if not code.startswith(FAILURE_CODE_PREFIX):
        return None
    parts = code[len(FAILURE_CODE_PREFIX):].split(":")
    if len(parts) != 2 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        return None
    return format_points_shortfall(*parts)
