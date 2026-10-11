from types import SimpleNamespace
import json

import httpx
import pytest

from roo.proactive.ingress import admit_slack_event, normalize_slack_event, EventAdmissionError


def settings(**extra):
    return SimpleNamespace(**{"PROACTIVE_EVENTS_ENABLED": True, "ROO_SURFACE": "public",
                              "PROACTIVE_EVENT_TEAM_ID": "T123", "PROACTIVE_EVENT_CHANNEL_IDS": "C123",
                              "PROACTIVE_EVENT_API_KEY": "test-" + "s" * 40,
                              "PROACTIVE_EVENT_BACKEND_ORIGIN": "https://backend.invalid", **extra})


def event(**extra):
    return {"type": "event_callback", "team_id": "T123", "event_id": "Ev123", "event_time": 1770000000,
            "event": {"type": "message", "channel": "C123", "ts": "1770000000.123", "user": "U123",
                      "text": "Private text must never become a wake-up payload", **extra}}


def test_event_hint_has_no_text_or_identity_and_stable_source_id():
    normalized = normalize_slack_event(settings(), event())
    assert normalized["source_event_id"] == "slack:T123:Ev123"
    assert normalized["resource"] == "slack:T123:C123"
    assert "Private" not in json.dumps(normalized) and "U123" not in json.dumps(normalized)


@pytest.mark.parametrize("override", [{"channel": "C999"}, {"channel_type": "im"}, {"bot_id": "B123"}, {"type": "app_mention"}, {"subtype": "bot_message"}])
def test_unapproved_sources_and_agent_loops_are_ignored(override):
    assert normalize_slack_event(settings(), event(**override)) is None


def test_admin_and_disabled_never_admit():
    assert normalize_slack_event(settings(ROO_SURFACE="admin"), event()) is None
    assert normalize_slack_event(settings(PROACTIVE_EVENTS_ENABLED=False), event()) is None


def test_updated_bot_message_is_ignored():
    assert normalize_slack_event(settings(), event(subtype="message_changed", message={"bot_id": "B123", "ts": "1770000000.123"})) is None


@pytest.mark.asyncio
async def test_backend_failure_requests_retry_and_never_follows_redirect():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.invalid"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EventAdmissionError):
            await admit_slack_event(settings(), event(), client=client)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_disabled_admission_makes_no_request():
    def handler(request):
        raise AssertionError("No backend call allowed")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await admit_slack_event(settings(PROACTIVE_EVENTS_ENABLED=False), event(), client=client) is False
