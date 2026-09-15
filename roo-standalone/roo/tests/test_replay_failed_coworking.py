import importlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    manifest = replay.build_manifest(
        replay_run_id="INC-1",
        valid=valid,
        quarantined=quarantined,
        execute=False,
    )
    assert "book me in today again" not in json.dumps(manifest)


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
        "🌐 MLAI request method=POST endpoint=/api/v1/points/coworking/book/",
        "✅ Message posted to C1 (thread: 111.222)",
    ]

    valid, quarantined = replay.parse_failed_coworking_log(
        lines,
        incident_local_date=date(2026, 4, 22),
    )

    assert valid == []
    assert quarantined == []


@pytest.mark.asyncio
async def test_execute_never_rebooks_when_active_booking_is_missing(monkeypatch):
    class FakeBackendClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def get_my_bookings(self, slack_user_id):
            return []

        async def book_coworking(self, *args, **kwargs):
            raise AssertionError("legacy replay must never create a booking")

    posted = []
    backend_module = importlib.import_module("roo.clients.mlai_backend")
    config_module = importlib.import_module("roo.config")
    slack_module = importlib.import_module("roo.slack_client")
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeBackendClient)
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            ROO_API_KEY="roo-key",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(
        slack_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )
    candidate = replay.ReplayCandidate(
        slack_user_id="U1",
        channel_id="C1",
        thread_ts="111.222",
        booking_date="2026-04-22",
        source_line=1,
        idempotency_key="replay_coworking:U1:2026-04-22",
    )

    results = await replay.execute_replay(
        [candidate], replay_run_id="INC-1", summary_channel_id="CSUMMARY"
    )

    assert results[0]["status"] == "manual_reconciliation_required"
    assert "backend_result" not in results[0]
    assert "did not create a new booking" in posted[0]["text"]
    assert "manual_reconciliation_required=1" in posted[-1]["text"]
