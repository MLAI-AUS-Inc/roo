import importlib.util
import sys
from datetime import date
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "ops" / "replay_failed_coworking.py"
spec = importlib.util.spec_from_file_location("replay_failed_coworking", SCRIPT_PATH)
replay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = replay
spec.loader.exec_module(replay)


def test_parser_keeps_latest_valid_failed_booking_per_user_date():
    lines = [
        "🦘 ROO MENTION: from U1 in C1",
        "   Text: <@Roo> book me in today...",
        "   Extracted params: {'action': 'book_coworking', 'date': 'today'}",
        "🌐 MLAI circuit_breaker_open endpoint=/api/v1/points/coworking/book/",
        "✅ Message posted to C1 (thread: 111.222)",
        "🦘 ROO MENTION: from U1 in C1",
        "   Text: <@Roo> book me in today again...",
        "   Extracted params: {'action': 'book_coworking', 'date': 'today'}",
        "🌐 MLAI request_failed method=POST endpoint=/api/v1/points/coworking/book/",
        "✅ Message posted to C1 (thread: 111.333)",
    ]

    valid, quarantined = replay.parse_failed_coworking_log(
        lines,
        incident_local_date=date(2026, 4, 22),
    )

    assert quarantined == []
    assert len(valid) == 1
    assert valid[0].slack_user_id == "U1"
    assert valid[0].booking_date == "2026-04-22"
    assert valid[0].thread_ts == "111.333"
    assert valid[0].idempotency_key == "replay_coworking:U1:2026-04-22"


def test_parser_quarantines_failed_booking_without_thread_ts():
    lines = [
        "🦘 ROO MENTION: from U1 in C1",
        "   Text: <@Roo> book me in 2026-04-22...",
        "   Extracted params: {'action': 'book_coworking', 'date': '2026-04-22'}",
        "🌐 MLAI request_failed method=POST endpoint=/api/v1/points/coworking/book/",
    ]

    valid, quarantined = replay.parse_failed_coworking_log(
        lines,
        incident_local_date=date(2026, 4, 22),
    )

    assert valid == []
    assert len(quarantined) == 1
    assert quarantined[0].reason == "missing_thread_ts_for_slack_confirmation"


def test_parser_ignores_non_failed_booking_by_default():
    lines = [
        "🦘 ROO MENTION: from U1 in C1",
        "   Text: <@Roo> book me in today...",
        "   Extracted params: {'action': 'book_coworking', 'date': 'today'}",
        "✅ Message posted to C1 (thread: 111.222)",
    ]

    valid, quarantined = replay.parse_failed_coworking_log(
        lines,
        incident_local_date=date(2026, 4, 22),
    )

    assert valid == []
    assert quarantined == []
