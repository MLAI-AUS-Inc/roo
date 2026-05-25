import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

executor_module = importlib.import_module("roo.skills.executor")
SkillExecutor = executor_module.SkillExecutor


def test_resolve_points_action_detects_leaderboard_phrases():
    executor = SkillExecutor()

    for text in [
        "MLAI championships this week",
        "mlai championship last week",
        "coworking leaderboard",
        "leaderboard for coworking",
        "who came in the most last week",
        "who's been in the most this month",
        "who's coming in the most",
        "top 10 members this month",
        "top members last week",
        "most active coworking this week",
    ]:
        assert (
            executor._resolve_points_action({}, text) == "coworking_leaderboard"
        ), f"expected leaderboard routing for: {text!r}"


def test_resolve_points_action_leaderboard_action_aliases():
    executor = SkillExecutor()

    for action in [
        "coworking_leaderboard",
        "leaderboard",
        "championship",
        "championships",
        "coworking_championship",
        "coworking_championships",
        "ranking",
        "coworking_ranking",
    ]:
        assert (
            executor._resolve_points_action({"action": action}, "")
            == "coworking_leaderboard"
        ), f"expected leaderboard routing for action: {action!r}"


def test_leaderboard_check_runs_before_report():
    executor = SkillExecutor()

    # "leaderboard coworking" should NOT get swallowed by the coworking_report path.
    assert (
        executor._resolve_points_action({}, "leaderboard coworking last week")
        == "coworking_leaderboard"
    )


def test_parse_leaderboard_limit_defaults_and_clamps():
    executor = SkillExecutor()

    assert executor._parse_leaderboard_limit("MLAI championships this week", {}) == 5
    assert executor._parse_leaderboard_limit("top 10 members this month", {}) == 10
    assert executor._parse_leaderboard_limit("top 3 coworking", {}) == 3
    assert executor._parse_leaderboard_limit("top 100 coworking", {}) == 25
    assert executor._parse_leaderboard_limit("top 0 coworking", {}) == 1
    assert executor._parse_leaderboard_limit("anything", {"limit": "8"}) == 8
    assert executor._parse_leaderboard_limit("anything", {"limit": 50}) == 25


def _sample_report_users(users):
    return {
        "range": {"start_date": "2026-05-18", "end_date": "2026-05-25"},
        "totals": {"unique_users": 34, "booked_user_days": 67},
        "users": users,
    }


def test_format_coworking_leaderboard_renders_medals_ties_and_footer():
    """
    Standard competition ranking ties: two on 4 days both show 🥈,
    the next row is rank 4 (not 3).
    """
    executor = SkillExecutor()

    report = _sample_report_users(
        [
            {"slack_user_id": "U123", "booking_count": 5},
            {"slack_user_id": "U456", "booking_count": 4},
            {"slack_user_id": "U789", "booking_count": 4},
            {"slack_user_id": "U234", "booking_count": 3},
            {"slack_user_id": "U567", "booking_count": 3},
        ]
    )

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "🏆 MLAI Championships — last week" in output
    assert "Range: 2026-05-18 to 2026-05-25" in output
    assert "🥇 <@U123>  5 days" in output
    assert "🥈 <@U456>  4 days" in output
    assert "🥈 <@U789>  4 days" in output
    # Tied 🥈 means next rank skips 🥉 and becomes 4.
    assert "🥉" not in output
    # Two tied at 3 days also share rank 4 under competition ranking.
    rank_four_lines = [line for line in output.splitlines() if line.startswith(" 4.")]
    assert len(rank_four_lines) == 2
    assert " 4. <@U234>  3 days" in output
    assert " 4. <@U567>  3 days" in output
    assert "34 members showed up across 67 user-days" in output


def test_format_coworking_leaderboard_three_distinct_counts_uses_all_medals():
    executor = SkillExecutor()

    report = _sample_report_users(
        [
            {"slack_user_id": "U1", "booking_count": 5},
            {"slack_user_id": "U2", "booking_count": 4},
            {"slack_user_id": "U3", "booking_count": 3},
            {"slack_user_id": "U4", "booking_count": 2},
        ]
    )

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "🥇 <@U1>  5 days" in output
    assert "🥈 <@U2>  4 days" in output
    assert "🥉 <@U3>  3 days" in output
    assert " 4. <@U4>  2 days" in output


def test_format_coworking_leaderboard_with_two_users_drops_bronze():
    executor = SkillExecutor()

    report = {
        "range": {"start_date": "2026-05-18", "end_date": "2026-05-25"},
        "totals": {"unique_users": 2, "booked_user_days": 5},
        "users": [
            {"slack_user_id": "U1", "booking_count": 3},
            {"slack_user_id": "U2", "booking_count": 2},
        ],
    }

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "🥇 <@U1>  3 days" in output
    assert "🥈 <@U2>  2 days" in output
    assert "🥉" not in output
    # Only two member rows, no padding entries.
    lines = [line for line in output.splitlines() if "<@" in line]
    assert len(lines) == 2


def test_format_coworking_leaderboard_empty_users_friendly_message():
    executor = SkillExecutor()

    report = {
        "range": {"start_date": "2026-05-18", "end_date": "2026-05-25"},
        "totals": {"unique_users": 0, "booked_user_days": 0},
        "users": [],
    }

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "🏆 MLAI Championships — last week" in output
    assert "Range: 2026-05-18 to 2026-05-25" in output
    assert "be the first to claim the title" in output
    assert "<@" not in output


def test_format_coworking_leaderboard_missing_users_key_treated_as_empty():
    executor = SkillExecutor()

    report = {
        "range": {"start_date": "2026-05-18", "end_date": "2026-05-25"},
        "totals": {"unique_users": 0, "booked_user_days": 0},
    }

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "be the first to claim the title" in output


def test_format_coworking_leaderboard_caps_at_25_and_singular_day():
    executor = SkillExecutor()

    users = [
        {"slack_user_id": f"U{i:03d}", "booking_count": (30 - i)}
        for i in range(30)
    ]
    report = {
        "range": {"start_date": "2026-05-01", "end_date": "2026-05-25"},
        "totals": {"unique_users": 30, "booked_user_days": sum(u["booking_count"] for u in users)},
        "users": users,
    }

    output = executor._format_coworking_leaderboard(report, "last month", 100)
    # Cap at 25 rows.
    member_rows = [line for line in output.splitlines() if "<@" in line]
    assert len(member_rows) == 25


def test_format_coworking_leaderboard_singular_day_word():
    executor = SkillExecutor()

    report = {
        "range": {"start_date": "2026-05-18", "end_date": "2026-05-25"},
        "totals": {"unique_users": 1, "booked_user_days": 1},
        "users": [{"slack_user_id": "U1", "booking_count": 1}],
    }

    output = executor._format_coworking_leaderboard(report, "last week", 5)

    assert "🥇 <@U1>  1 day" in output
    assert "1 member showed up across 1 user-day" in output


def test_leaderboard_label_picks_friendly_keyword_or_iso_range():
    executor = SkillExecutor()

    assert executor._coworking_leaderboard_label("last week vibes", "2026-05-18", "2026-05-25") == "last week"
    assert executor._coworking_leaderboard_label("this week", "2026-05-18", "2026-05-25") == "this week"
    assert executor._coworking_leaderboard_label("last month please", "2026-04-01", "2026-04-30") == "last month"
    assert (
        executor._coworking_leaderboard_label("from 2026-05-01 to 2026-05-25", "2026-05-01", "2026-05-25")
        == "2026-05-01 → 2026-05-25"
    )


def test_is_coworking_leaderboard_request_matches_brief_phrases():
    executor = SkillExecutor()

    for text in [
        "MLAI championships",
        "mlai championship this week",
        "coworking leaderboard",
        "leaderboard for coworking last week",
        "who came in the most last week",
        "who's been in the most",
        "who's coming in the most",
        "top members this week",
        "top 10 members this month",
        "most active coworking this week",
    ]:
        assert executor._is_coworking_leaderboard_request(text, {}), f"missed: {text!r}"


def test_is_coworking_leaderboard_request_skips_unrelated_phrases():
    executor = SkillExecutor()

    for text in [
        "coworking report last week",
        "coworking summary last 6 months",
        "how many points do I have",
        "book me in tomorrow",
        "balance",
    ]:
        assert not executor._is_coworking_leaderboard_request(text, {}), f"matched but shouldn't: {text!r}"
