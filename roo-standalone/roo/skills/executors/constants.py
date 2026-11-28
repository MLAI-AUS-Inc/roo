"""Stable constants shared with existing callers."""
from __future__ import annotations
from typing import Any
import re


def can_manage_points_admins(settings: Any, user_id: str, team_id: str) -> bool:
    """Match one configured Slack identity; missing or malformed settings deny."""
    expected_user = getattr(settings, "POINTS_SUPER_ADMIN_SLACK_ID", "")
    expected_team = getattr(settings, "POINTS_SUPER_ADMIN_SLACK_TEAM_ID", "")
    return bool(
        isinstance(expected_user, str)
        and isinstance(expected_team, str)
        and re.fullmatch(r"[UW][A-Z0-9]+", expected_user)
        and re.fullmatch(r"T[A-Z0-9]+", expected_team)
        and user_id == expected_user
        and team_id == expected_team
    )


FULL_POINTS_ADMIN_ROLES = {"admin", "committee", "portfolio_lead"}


COWORKING_REPORT_ROLES = {*FULL_POINTS_ADMIN_ROLES, "partner"}


LINEAR_MEETING_TIMEOUT_RECOVERY_OVERLAP_CHARS = 300


FOUNDER_ACCOUNT_LINK_MAX_FUTURE_SECONDS = 35 * 60


class LinearMeetingExtractionDeadlineError(RuntimeError):
    """The complete meeting-action extraction exceeded its wall-clock budget."""


ROO_TOPUP_PACKS = {
    "topup_5": {
        "points": 10,
        "label": "10 Top-up Roo Points",
        "price": "A$19.99",
    },
    "topup_10": {
        "points": 20,
        "label": "20 Top-up Roo Points",
        "price": "A$36.99",
    },
    "topup_25": {
        "points": 50,
        "label": "50 Top-up Roo Points",
        "price": "A$63.99",
    },
}


ROO_TOPUP_PACK_BY_POINTS = {
    pack["points"]: pack_id for pack_id, pack in ROO_TOPUP_PACKS.items()
}


def _linear_task_sizing_setting(
    settings: Any,
    canonical_name: str,
    legacy_name: str,
    default: Any,
) -> Any:
    configured = getattr(settings, canonical_name, None)
    if configured in (None, ""):
        configured = getattr(settings, legacy_name, default)
    return default if configured in (None, "") else configured


