import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from roo.proactive import commands
from roo.proactive.ingress import EventAdmissionError


def settings(**changes):
    return SimpleNamespace(**{
        "PROACTIVE_TASKS_ENABLED": True, "ROO_SURFACE": "public",
        "PROACTIVE_EVENT_TEAM_ID": "T123", "PROACTIVE_EVENT_CHANNEL_IDS": "C123,C456",
        "PROACTIVE_TASK_BOT_USER_ID": "UROO", "PROACTIVE_TASK_API_KEY": "synthetic-" + "s" * 40,
        "PROACTIVE_EVENT_BACKEND_ORIGIN": "https://backend.invalid", **changes,
    })


def envelope(**changes):
    return {"type": "event_callback", "team_id": "T123", "event_id": "Ev123",
            "owner_id": "forged", "grant_id": "forged",
            "event": {"type": "app_mention", "channel": "C123", "user": "U123",
                      "text": "<@UROO> run: Summarize this channel", **changes}}


def test_command_is_explicit_and_identity_comes_only_from_verified_envelope():
    normalized = commands.normalize_task_command(settings(), envelope())
    assert normalized == {"team_id": "T123", "user_id": "U123", "channel_id": "C123",
                          "source_event_id": "slack:T123:Ev123", "instruction": "Summarize this channel"}
    for text in ("run: do it", "Please <@UROO> run: do it", "<@OTHER> run: do it", "<@UROO> help", "<@UROO> run: ", "<@UROO> run: a\x00b"):
        assert commands.normalize_task_command(settings(), envelope(text=text)) is None


@pytest.mark.parametrize("changes", [{"channel": "C999"}, {"user": "not-a-user"}, {"channel_type": "im"},
    {"channel_type": "group"}, {"subtype": "message_changed"}, {"bot_id": "B123"}, {"type": "reaction_added"}])
def test_source_scope_mismatches_are_not_admitted(changes):
    assert commands.normalize_task_command(settings(), envelope(**changes)) is None


def test_wrong_team_admin_disabled_and_malformed_payload_never_admit():
    assert commands.normalize_task_command(settings(), {**envelope(), "team_id": "T999"}) is None
    assert commands.normalize_task_command(settings(ROO_SURFACE="admin"), envelope()) is None
    assert commands.normalize_task_command(settings(PROACTIVE_TASKS_ENABLED=False), envelope()) is None
    for value in (None, [], "message", {"type": "event_callback", "event": []}):
        assert commands.normalize_task_command(settings(), value) is None


@pytest.mark.asyncio
async def test_message_copy_is_consumed_without_second_task_or_network():
    def forbidden(request):
        raise AssertionError("Message copy must never call admission")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        assert await commands.admit_task_command(settings(), envelope(type="message"), client=client) == {"handled": True, "new": False}


@pytest.mark.asyncio
async def test_admission_uses_fixed_path_identity_and_distinct_controller_key():
    seen, task_id = [], str(uuid4())
    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"task_id": task_id, "new": False, "owner_url": "https://untrusted.invalid"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await commands.admit_task_command(settings(), envelope(), client=client)
    assert str(seen[0].url) == "https://backend.invalid/api/v1/agent-runtime/internal/tasks/admit-slack/"
    assert seen[0].headers["Authorization"] == "Bearer " + settings().PROACTIVE_TASK_API_KEY
    assert json.loads(seen[0].content)["user_id"] == "U123"
    assert "grant_id" not in json.loads(seen[0].content)
    assert result["new"] is False and task_id in result["text"]
    assert "untrusted.invalid" not in result["text"]


@pytest.mark.asyncio
async def test_redirect_does_not_forward_credential_and_oversized_body_is_rejected():
    for response in (httpx.Response(307, headers={"location": "https://evil.invalid"}), httpx.Response(202, content=b"x" * 16385)):
        seen = []
        def handler(request):
            seen.append(request)
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(EventAdmissionError):
                await commands.admit_task_command(settings(), envelope(), client=client)
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, [], {"task_id": None, "new": True}, {"task_id": {}, "new": True},
    {"task_id": "../../other", "new": True}, {"task_id": str(uuid4()), "new": "true"}])
async def test_malformed_acknowledgements_become_safe_retry_errors(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202, json=payload))) as client:
        with pytest.raises(EventAdmissionError, match="invalid_response"):
            await commands.admit_task_command(settings(), envelope(), client=client)


@pytest.mark.asyncio
async def test_slow_response_has_total_deadline(monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.002)
                yield b" "
    monkeypatch.setattr(commands, "REQUEST_TIMEOUT", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202, stream=SlowBody()))) as client:
        with pytest.raises(EventAdmissionError, match="unavailable"):
            await commands.admit_task_command(settings(), envelope(), client=client)
