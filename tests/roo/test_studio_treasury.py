"""Offline signed-ingress, replay and private-delivery contracts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import json

import pytest

from roo.studio_treasury_commands import enqueue, enqueue_event, parse
from roo.studio_treasury_interactions import handle
from roo.studio_treasury_worker import blocks, format_review


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(STUDIO_TREASURY_COMMANDS_ENABLED=True, STUDIO_TREASURY_SLACK_IDS="USAM",
        STUDIO_TREASURY_QUEUE_DIR=str(tmp_path), TIMESHEET_SLACK_TEAM_ID="T123", TIMESHEET_SLACK_BOT_USER_ID="UBOT")


def test_exact_commands_preserve_member_invoice_and_other_payments():
    assert parse("<@UBOT> pay builder invoices", "UBOT", False) == {"operation": "preview", "selector": "recent"}
    assert parse("check builders invoices 48350", "UBOT", True)["selector"] == "48350"
    assert parse("check meetings 2026-10-11 to 2026-10-25", "UBOT", True) == {
        "operation": "meeting_scan", "start": "2026-10-11", "end": "2026-10-25"}
    for text in ("invoice latest", "payroll", "pay Stripe", "pay builder invoices", "add actions to Linear"):
        assert parse(text, "UBOT", False) is None


def test_generic_events_do_not_require_studio_configuration():
    assert enqueue_event(SimpleNamespace(), {"event": {"type": "message", "channel": "D123",
        "channel_type": "im", "text": "hello", "user": "UMEMBER", "ts": "123.456"}}) == {"handled": False}


def test_workspace_treasurer_and_replay_are_verified(settings):
    command = {"operation": "preview", "selector": "recent"}
    kwargs = dict(team="T123", actor="USAM", channel="C123", source="123.456", command=command)
    assert enqueue(settings, **kwargs)["new"]
    assert not enqueue(settings, **kwargs)["new"]
    assert not enqueue(settings, **{**kwargs, "actor": "UOTHER"})["text"].startswith("I’m")
    assert len(list(Path(settings.STUDIO_TREASURY_QUEUE_DIR).glob("*.json"))) == 1
    with pytest.raises(ValueError):
        enqueue(settings, **{**kwargs, "command": {"operation": "authorize"}})


def test_members_can_confirm_only_through_backend_verified_actor(settings):
    event = {"team_id": "T123", "event": {"type": "message", "channel": "D123", "channel_type": "im",
        "user": "UOTHER", "text": "confirm meeting " + "a"*64 + " 20 min", "ts": "123.456"}}
    assert enqueue_event(settings, event)["handled"]
    job = json.loads(next(Path(settings.STUDIO_TREASURY_QUEUE_DIR).glob("*.json")).read_text())
    assert job["actor"] == "UOTHER" and job["command"]["minutes"] == 20
    assert "STUDIO_TREASURY_KEY" not in str(job)


def test_modal_validates_minutes_and_uses_signed_user(settings):
    payload = {"type": "view_submission", "team": {"id": "T123"}, "user": {"id": "UOTHER"}, "view": {
        "id": "V1", "hash": "h1", "callback_id": "studio_meeting_time",
        "private_metadata": json.dumps({"channel": "D123", "value": {"meeting": "a"*64}}),
        "state": {"values": {"time": {"minutes": {"value": "20"}}}}}}
    assert handle(settings, payload, Mock())["response_action"] == "clear"
    job = json.loads(next(Path(settings.STUDIO_TREASURY_QUEUE_DIR).glob("*.json")).read_text())
    assert job["actor"] == "UOTHER" and job["command"]["minutes"] == 20
    payload["view"]["state"]["values"]["time"]["minutes"]["value"] = "-1"
    assert handle(settings, payload, Mock())["response_action"] == "errors"


def test_payment_buttons_only_authorize_reviewed_handoff():
    result = {"id": "123", "status": "preview", "ready_cents": 6500,
              "invoices": [{"status": "ready", "number": "A1", "lines": []}], "exceptions": []}
    assert blocks(result)[1]["elements"][0]["action_id"] == "studio_treasury_authorize"
    result.update(status="reserved", handoffs=[{"number": "A1", "currency": "AUD", "cents": 6500, "reference": "ROO-123", "status": "reserved"}])
    assert len(blocks(result)) == 1
    assert "confirmed" not in format_review(result)


def test_invoice_period_scan_proposes_addition_before_a_write():
    result = {"calendar_scan": [{"title": "Acquire check-in", "start": "2026-10-12", "action": "planned"}],
              "scan": {"mode": "review", "start": "2026-10-11", "end": "2026-10-25"}, "meetings": [], "links": []}
    assert blocks(result)[1]["elements"][0]["action_id"] == "studio_meeting_add_scan"
    result["calendar_scan"][0]["action"] = "needs_review"
    assert blocks(result)[1]["elements"][0]["action_id"] == "studio_meeting_add_scan"
    result["scan"]["mode"] = "create"
    assert len(blocks(result)) == 1


@pytest.mark.asyncio
async def test_calendar_failures_preserve_action_result_and_preview_does_not_queue(settings):
    from roo.studio_meeting_calendar import enrich
    settings.STUDIO_MEETINGS_CALENDAR_ENABLED = True
    client = SimpleNamespace(studio_calendar=AsyncMock(side_effect=TimeoutError()))
    candidates = [{"project_match": {"project": {"id": "P1"}, "confidence": .99}}]
    message, rows = await enrich(client, settings, actor="UOTHER", candidates=candidates, mode="create")
    assert "unavailable" in message and rows == []
    client.studio_calendar = AsyncMock(return_value={"meetings": [{"title": "Check-in", "start": "2026-11-15", "action": "planned", "confirmation_needed": True}]})
    await enrich(client, settings, actor="UOTHER", candidates=candidates, mode="review", channel="C123")
    assert not list(Path(settings.STUDIO_TREASURY_QUEUE_DIR).glob("*.json"))


def test_signed_payment_request_bypasses_inference_and_rejects_forgery(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from roo import main as runtime
    from roo.config import Settings, get_settings
    from tests.roo.test_timesheet_ingress import headers, payload
    config = Settings(_env_file=None, OPENAI_API_KEY="synthetic", SLACK_BOT_TOKEN="synthetic",
        SLACK_SIGNING_SECRET="test-secret", SLACK_RECEIPTS_DB_PATH=str(tmp_path/"receipts.db"),
        TIMESHEET_SLACK_TEAM_ID="T123", TIMESHEET_SLACK_BOT_USER_ID="UBOT",
        STUDIO_TREASURY_COMMANDS_ENABLED=True, STUDIO_TREASURY_SLACK_IDS="USAM",
        STUDIO_TREASURY_QUEUE_DIR=str(tmp_path/"queue"))
    runtime.app.dependency_overrides[get_settings] = lambda: config
    monkeypatch.setattr(runtime, "_invoice_ack", AsyncMock())
    monkeypatch.setattr(runtime, "_handle_app_mention_with_room_choice", AsyncMock(side_effect=AssertionError("inference")))
    try:
        client = TestClient(runtime.app)
        body = json.dumps(payload(text="<@UBOT> pay builder invoices")).encode()
        valid = headers(body)
        assert client.post("/slack/events", content=body, headers=valid).status_code == 200
        assert client.post("/slack/events", content=body, headers=valid).status_code == 200
        assert len(list((tmp_path/"queue").glob("*.json"))) == 1
        forged = {**valid, "X-Slack-Signature": "v0="+"0"*64}
        assert client.post("/slack/events", content=body, headers=forged).status_code == 403
        runtime._handle_app_mention_with_room_choice.assert_not_called()
    finally:
        runtime.app.dependency_overrides.clear()
