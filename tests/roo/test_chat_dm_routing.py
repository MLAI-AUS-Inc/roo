"""Signed Chat DM routing, source verification and points execution, fully offline."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
import httpx
import pytest

from roo import main, slack_client
from roo.bridge_mentions import is_chat_dm_candidate, resolve_chat_dm
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from roo.config import Settings, get_settings
from roo.slack_security import get_slack_receipt_store

TEXT = "<@UROO> give <@UMEMBER> 100 points for helping at an event"
EVENT = dict(type="message", channel_type="im", channel="DROO", user="UADMIN",
             bot_id="BCHAT", app_id="ACHAT", ts="1793786540.585259", text=TEXT)
CONTEXT = dict(workspace_id="TMLAI", channel_id="DROO", message_id=EVENT["ts"],
               thread_ts=EVENT["ts"], bridge_user_id="UADMIN")
ACTOR = {**CONTEXT, "version": 2, "posting_mode": "member", "user_id": "UADMIN",
         "actor_profile_id": "00000000-0000-4000-8000-000000000001", "source_event_id": "ab" * 32,
         "message_text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(), "recipient_targets": [], "source_labels": []}


async def resolve(backend, event=EVENT, surface="public"):
    return await resolve_chat_dm(event, slack_team_id="TMLAI", surface=surface, backend=backend)


@pytest.mark.parametrize("overrides,expected", [
    ({}, True), ({"bot_id": None}, True), ({"app_id": None}, True),
    ({"bot_id": None, "app_id": None, "subtype": "bot_message"}, True),
    ({"subtype": "message_changed"}, False), ({"subtype": "message_deleted"}, False),
    ({"channel_type": "channel", "channel": "CPUBLIC"}, False),
    ({"channel_type": "mpim", "channel": "GGROUP"}, False),
    ({"bot_id": None, "app_id": None}, False),
])
def test_only_bot_marked_original_one_to_one_dms_are_verification_candidates(overrides, expected):
    assert is_chat_dm_candidate({**EVENT, **overrides}) is expected


@pytest.mark.asyncio
async def test_private_verification_preserves_signed_text_actor_and_reply_location():
    backend = AsyncMock()
    backend.resolve_chat_dm_actor.return_value = httpx.Response(200, json=ACTOR)
    result = await resolve(backend)
    backend.resolve_chat_dm_actor.assert_awaited_once_with(CONTEXT)
    assert result["user"] == "UADMIN"
    assert result["text"] == TEXT
    assert result["channel"] == "DROO" and result["ts"] == EVENT["ts"]
    assert result["_slack_event_id"] == "mlai-chat:" + ACTOR["source_event_id"]
    assert result["_verified_bridge_context"].actor_profile_id == ACTOR["actor_profile_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"channel_id": "DOTHER"}, {"workspace_id": "TOTHER"}, {"bridge_user_id": "UOTHER"},
    {"user_id": "UOTHER"}, {"thread_ts": "1793786540.999999"}, {"source_event_id": "bad"},
    {"message_text_sha256": "00" * 32}, {"actor_profile_id": "bad"}, {"version": 1},
    {"posting_mode": "bot"}, {"recipient_targets": [{"profile_id": "guessed"}]},
])
async def test_unbound_or_malformed_private_authority_never_dispatches(overrides):
    backend = AsyncMock()
    backend.resolve_chat_dm_actor.return_value = httpx.Response(200, json={**ACTOR, **overrides})
    with pytest.raises(MLAIBackendUnavailableError):
        await resolve(backend)


@pytest.mark.asyncio
async def test_admin_surface_and_ordinary_human_messages_never_delegate():
    backend = AsyncMock()
    assert await resolve(backend, surface="admin") is None
    assert await resolve(backend, {**EVENT, "bot_id": None, "app_id": None}) is None
    backend.resolve_chat_dm_actor.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_race_retries_and_unknown_bots_stop_without_authority():
    backend = AsyncMock()
    backend.resolve_chat_dm_actor.side_effect = [httpx.Response(409, json={"error": "bridge_delivery_pending"}),
                                                httpx.Response(200, json=ACTOR)]
    with patch("roo.bridge_mentions.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert (await resolve(backend))["user"] == "UADMIN"
    sleep.assert_awaited_once_with(0.2)
    backend.resolve_chat_dm_actor.side_effect = None
    backend.resolve_chat_dm_actor.return_value = httpx.Response(404, json={"error": "unknown_bridge_sender"})
    assert await resolve(backend) is None
    backend.resolve_chat_dm_actor.return_value = httpx.Response(503, json={})
    with pytest.raises(MLAIBackendUnavailableError):
        await resolve(backend)


@pytest.mark.asyncio
async def test_client_uses_separate_read_only_private_endpoint_with_redacted_logs():
    backend = MLAIBackendClient(base_url="https://backend.test", api_key="synthetic-roo-key")
    with patch.object(backend, "_request", new_callable=AsyncMock) as request:
        await backend.resolve_chat_dm_actor(CONTEXT)
    assert request.call_args.args == ("GET", "/api/v1/integrations/bridge/roo/dm-actor")
    assert request.call_args.kwargs["params"] == CONTEXT
    assert request.call_args.kwargs["redact_logs"] is True
    assert not request.call_args.kwargs.get("use_admin_headers")


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    configured = Settings(_env_file=None, SLACK_BOT_TOKEN="test", SLACK_SIGNING_SECRET="test",
        OPENAI_API_KEY="test", SLACK_RECEIPTS_DB_PATH=str(tmp_path / "receipts.db"), START_HERE_INTRO_ENABLED=False)
    main.app.dependency_overrides[get_settings] = lambda: configured
    monkeypatch.setattr(main, "get_settings", lambda: configured)
    backend = AsyncMock()
    backend.resolve_chat_dm_actor.return_value = httpx.Response(200, json=ACTOR)
    monkeypatch.setattr(main, "_make_mlai_backend_client", lambda: backend)
    queued = []
    monkeypatch.setattr(main, "start_slack_action", queued.append)
    get_slack_receipt_store.cache_clear()
    slack_client.get_slack_app_identity.cache_clear()
    client = TestClient(main.app)

    def send(event=EVENT, signature_valid=True):
        body = json.dumps({"type": "event_callback", "team_id": "TMLAI", "event_id": "EvChatDM", "event": event}).encode()
        stamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"test", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        return client.post("/slack/events", content=body, headers={
            "Content-Type": "application/json", "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": signature if signature_valid else "v0=invalid",
        })

    yield SimpleNamespace(send=send, queued=queued, backend=backend, settings=configured)
    for coroutine in queued:
        coroutine.close()
    main.app.dependency_overrides.clear()
    get_slack_receipt_store.cache_clear()


def test_signed_dm_runs_points_as_verified_owner_once_and_ignores_model_target(ingress, monkeypatch):
    from roo.backend_identity import get_backend_actor_context
    from roo.skills.executor import SkillExecutor
    from roo.skills.executors import mlai_points

    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
        get_user_by_slack_id=AsyncMock(return_value="known-user"), award_points=AsyncMock(return_value={"replayed": False}))
    monkeypatch.setattr(slack_client, "get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(mlai_points, "send_dm", Mock())

    async def handle(event):
        # The real mention wrapper installs this authority before any skill call.
        async def execute(bound_event):
            assert get_backend_actor_context().acting_slack_user_id == "UADMIN"
            result = await SkillExecutor()._handle_points_action(
                client=client, action="award_points", params={"points": 900, "target_user": "UGUESSED"},
                text=bound_event["text"], user_id=bound_event["user"], channel_id=bound_event["channel"],
                thread_ts=bound_event["ts"], skill=SimpleNamespace(name="mlai-points"),
                verified_bridge_context=bound_event["_verified_bridge_context"],
            )
            assert "Awarded 100 points to <@UMEMBER>" in result
        with patch.object(main, "_handle_mention_with_context", side_effect=execute):
            await original_handle(event)

    original_handle = main._handle_mention
    monkeypatch.setattr(main, "_handle_mention", handle)
    assert ingress.send().status_code == 503
    assert ingress.send().status_code == 503
    assert len(ingress.queued) == 1
    asyncio.run(ingress.queued.pop())
    assert ingress.send().status_code == 200
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.args == ("UADMIN", "UMEMBER", 100, "helping at an event")
    operation = hashlib.sha256(("mlai-chat:" + ACTOR["source_event_id"]).encode()).hexdigest()
    assert client.award_points.call_args.kwargs["idempotency_key"] == f"roo-slack-award:{operation}:UMEMBER"
    assert get_backend_actor_context() is None


def test_transient_lookup_failure_releases_receipt_for_retry(ingress, monkeypatch):
    handle = AsyncMock()
    monkeypatch.setattr(main, "_handle_mention", handle)
    ingress.backend.resolve_chat_dm_actor.return_value = httpx.Response(503, json={})
    assert ingress.send().status_code == 503
    with pytest.raises(MLAIBackendUnavailableError):
        asyncio.run(ingress.queued.pop())
    handle.assert_not_awaited()
    ingress.backend.resolve_chat_dm_actor.return_value = httpx.Response(200, json=ACTOR)
    assert ingress.send().status_code == 503
    asyncio.run(ingress.queued.pop())
    handle.assert_awaited_once()


def test_unsigned_requests_and_unknown_bots_never_execute(ingress, monkeypatch):
    handle = AsyncMock()
    monkeypatch.setattr(main, "_handle_mention", handle)
    assert ingress.send(signature_valid=False).status_code == 403
    assert ingress.queued == []
    ingress.backend.resolve_chat_dm_actor.return_value = httpx.Response(404, json={"error": "unknown_bridge_sender"})
    assert ingress.send().status_code == 503
    asyncio.run(ingress.queued.pop())
    assert ingress.send().status_code == 200
    handle.assert_not_awaited()


def test_admin_surface_never_delegates_private_chat_messages(ingress, monkeypatch):
    ingress.settings.ROO_SURFACE = "admin"
    monkeypatch.setattr(main, "_is_slack_context_allowed", lambda *args, **kwargs: True)
    assert ingress.send().status_code == 200
    assert ingress.queued == []
    ingress.backend.resolve_chat_dm_actor.assert_not_awaited()


def test_deployment_allowlist_is_checked_before_private_lookup(ingress, monkeypatch):
    monkeypatch.setattr(main, "_is_slack_context_allowed", lambda *args, **kwargs: False)
    assert ingress.send().status_code == 200
    assert ingress.queued == []
    ingress.backend.resolve_chat_dm_actor.assert_not_awaited()
