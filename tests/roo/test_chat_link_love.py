"""Signed bridge rewards preserve canonical recipients and replay protection."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
import pytest
from fastapi.testclient import TestClient
from roo import main, link_love
from roo.bridge_mentions import resolve_link_love_reply
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from roo.config import Settings, get_settings
from roo.slack_security import get_slack_receipt_store

PROFILE = "00000000-0000-4000-8000-000000000001"
EVENT = dict(type="message", channel_type="channel", user="UBRIDGE", bot_id="BCHAT", subtype="bot_message",
             channel="CBOOST", ts="1793845361.277839", thread_ts="1793842044.896789", text="Untrusted header: done")
CONTEXT = dict(workspace_id="TMLAI", channel_id="CBOOST", message_id=EVENT["ts"], thread_ts=EVENT["thread_ts"],
               bridge_user_id="UBRIDGE", purpose="link_love")
ACTOR = {**CONTEXT, "actor_profile_id": PROFILE, "actor_slack_user_id": "", "source_event_id": "ab" * 32,
         "is_root_author": False, "text": "done"}


@pytest.mark.asyncio
async def test_verified_chat_only_reply_uses_canonical_profile_and_stored_body():
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    event = await resolve_link_love_reply(EVENT, slack_team_id="TMLAI", backend=backend)
    assert event["user"] == f"profile:{PROFILE}"
    assert event["text"] == "done"
    assert event["thread_ts"] == EVENT["thread_ts"]
    backend.resolve_chat_bridge_actor.assert_awaited_once_with(CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"workspace_id": "TOTHER"}, {"thread_ts": "1793842044.999999"},
    {"actor_profile_id": "name"}, {"purpose": "mention"}, {"source_event_id": "bad"},
    {"is_root_author": "false"}, {"actor_slack_user_id": "UBRIDGE> <!everyone"}, {"text": ""}])
async def test_scope_or_authority_mismatch_fails_closed(change):
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json={**ACTOR, **change})
    with pytest.raises(MLAIBackendUnavailableError):
        await resolve_link_love_reply(EVENT, slack_team_id="TMLAI", backend=backend)


@pytest.mark.asyncio
async def test_delivery_race_retries_and_unrelated_bot_stops():
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.side_effect = [httpx.Response(409, json={"error": "bridge_delivery_pending"}),
                                                    httpx.Response(200, json=ACTOR)]
    with patch("roo.bridge_mentions.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert await resolve_link_love_reply(EVENT, slack_team_id="TMLAI", backend=backend)
        sleep.assert_awaited_once_with(0.2)
    backend.resolve_chat_bridge_actor.side_effect = None
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(404, json={"error": "unknown_bridge_sender"})
    assert await resolve_link_love_reply(EVENT, slack_team_id="TMLAI", backend=backend) is None


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, SLACK_BOT_TOKEN="test", SLACK_SIGNING_SECRET="test", OPENAI_API_KEY="test",
        START_HERE_INTRO_ENABLED=False, BOOST_LINK_LOVE_ENABLED=True, BOOST_LINK_LOVE_CHANNEL_ID="CBOOST",
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "receipts.db"))
    main.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    monkeypatch.setattr(main, "_make_mlai_backend_client", lambda: backend)
    queued = []
    monkeypatch.setattr(main, "start_slack_action", queued.append)
    get_slack_receipt_store.cache_clear()
    client = TestClient(main.app)

    def send(event=EVENT, valid=True):
        body = json.dumps(dict(type="event_callback", team_id="TMLAI", event_id="EvReward", event=event)).encode()
        stamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"test", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        return client.post("/slack/events", content=body, headers={"Content-Type": "application/json",
            "X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": signature if valid else "v0=invalid"})
    yield SimpleNamespace(send=send, queued=queued, backend=backend, settings=settings)
    for coro in queued: coro.close()
    main.app.dependency_overrides.clear()
    get_slack_receipt_store.cache_clear()


def test_signed_bridge_reply_enters_reward_handler_once_and_never_general_agent(ingress, monkeypatch):
    handle = AsyncMock()
    monkeypatch.setattr(main, "handle_link_love_reply", handle)
    mention = AsyncMock()
    monkeypatch.setattr(main, "_handle_mention", mention)
    assert ingress.send().status_code == 503
    assert len(ingress.queued) == 1
    asyncio.run(ingress.queued.pop())
    assert handle.call_args.args[0]["user"] == f"profile:{PROFILE}"
    assert handle.call_args.args[0]["text"] == "done"
    assert ingress.send().status_code == 200
    assert not ingress.queued
    mention.assert_not_awaited()


@pytest.mark.parametrize("change", [{"channel": "COTHER"}, {"thread_ts": None}, {"subtype": "message_changed"},
                                  {"subtype": "message_deleted"}, {"ts": EVENT["thread_ts"]}])
def test_unrelated_bot_events_do_not_enter_reward_verification(ingress, change):
    assert ingress.send({**EVENT, **change}).status_code == 200
    assert not ingress.queued
    ingress.backend.resolve_chat_bridge_actor.assert_not_awaited()


def test_bad_signature_and_admin_surface_cannot_delegate(ingress):
    assert ingress.send(valid=False).status_code in (401, 403)
    assert not ingress.queued
    ingress.settings.ROO_SURFACE = "admin"
    assert ingress.send().status_code == 200
    assert not ingress.queued


@pytest.mark.asyncio
async def test_canonical_reward_persists_for_retry_deduplicates_and_renders_safe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(link_love, "_now", lambda: 1793845500.0)
    store = link_love.LinkLoveAwardStore(tmp_path / "awards.db")
    client = SimpleNamespace(system_award_points=AsyncMock(return_value={"points_awarded": 2, "new_balance": 10,
        "display_name": "Chat <@UOTHER> member"}))
    event = {**EVENT, "user": f"profile:{PROFILE}", "text": "done"}
    monkeypatch.setattr("roo.boost_moderation.boost_reward_admission_decision", lambda *_: "legacy")
    monkeypatch.setattr("roo.boost_moderation.boost_reward_root_poster", lambda *_: None)
    monkeypatch.setattr(link_love, "classify_link_love_reply", AsyncMock(return_value=link_love.LinkLoveClassification(True, 1, "done", "{}")))
    first = await link_love.handle_link_love_reply(event, store=store, client=client,
        get_root_message=lambda *_: {"user": "UROOT", "text": "Please support this link"}, bot_user_id="UROO", notification_delay_seconds=0)
    assert first["status"] == "awarded"
    assert client.system_award_points.call_args.kwargs["target_profile_id"] == PROFILE
    assert client.system_award_points.call_args.kwargs["target_slack_id"] == ""
    second = await link_love.handle_link_love_reply({**event, "ts": "1793845362.000001"}, store=store, client=client,
        get_root_message=lambda *_: {"user": "UROOT", "text": "Please support this link"}, bot_user_id="UROO")
    assert second["status"] == "already_awarded"
    client.system_award_points.assert_awaited_once()
    text = link_love._build_notification_text(store.get_due_notification_groups()[0]["awards"])
    assert "(MLAI Chat): awarded 2 pts" in text and "<@UOTHER>" not in text and "<@profile:" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [{"_link_love_root_author": True}, {"_link_love_actor_slack_id": "UROOT"}])
async def test_native_and_linked_root_author_cannot_reward_self(tmp_path, monkeypatch, marker):
    monkeypatch.setattr("roo.boost_moderation.boost_reward_admission_decision", lambda *_: "legacy")
    monkeypatch.setattr("roo.boost_moderation.boost_reward_root_poster", lambda *_: None)
    client = SimpleNamespace(system_award_points=AsyncMock())
    result = await link_love.handle_link_love_reply({**EVENT, "user": f"profile:{PROFILE}", **marker},
        store=link_love.LinkLoveAwardStore(tmp_path / "awards.db"), client=client,
        get_root_message=lambda *_: {"user": "UROOT", "text": "Link"})
    assert result["reason"] == "root_author_reply"
    client.system_award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_sends_only_canonical_recipient():
    backend = MLAIBackendClient(base_url="https://backend.test", api_key="synthetic-roo-key")
    response = httpx.Response(200, json={}, request=httpx.Request("POST", "https://backend.test"))
    with patch.object(backend, "_request", new_callable=AsyncMock, return_value=response) as request:
        await backend.system_award_points("UROO", "", 2, "link-love", f"link_love:CBOOST:1793842044.896789:profile:{PROFILE}", target_profile_id=PROFILE)
    assert request.call_args.kwargs["json"]["target_profile_id"] == PROFILE
    assert "target_slack_id" not in request.call_args.kwargs["json"]
    assert request.call_args.kwargs["use_admin_headers"] is False


def test_allowlist_blocks_lookup_before_and_after_resolution(ingress, monkeypatch):
    monkeypatch.setattr(main, "_is_slack_context_allowed", lambda *_args, **_kwargs: False)
    assert ingress.send().status_code == 200
    assert not ingress.queued
    ingress.backend.resolve_chat_bridge_actor.assert_not_awaited()


def test_transient_identity_failure_keeps_signed_receipt_retryable(ingress, monkeypatch):
    handle = AsyncMock()
    monkeypatch.setattr(main, "handle_link_love_reply", handle)
    ingress.backend.resolve_chat_bridge_actor.return_value = httpx.Response(503, json={})
    ingress.send()
    with pytest.raises(MLAIBackendUnavailableError):
        asyncio.run(ingress.queued.pop())
    handle.assert_not_awaited()
    ingress.backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    ingress.send()
    assert len(ingress.queued) == 1
    asyncio.run(ingress.queued.pop())
    handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_award_retry_after_store_restart_retains_canonical_recipient(tmp_path, monkeypatch):
    monkeypatch.setattr("roo.boost_moderation.boost_reward_admission_decision", lambda *_: "legacy")
    path = tmp_path / "awards.db"
    store = link_love.LinkLoveAwardStore(path)
    _, award = store.create_award(channel_id="CBOOST", root_message_ts=EVENT["thread_ts"],
        slack_user_id=f"profile:{PROFILE}", root_author_slack_id="UROOT", source_reply_message_ts=EVENT["ts"], locked_by="test")
    failing = SimpleNamespace(system_award_points=AsyncMock(side_effect=httpx.TransportError("timeout")))
    result = await link_love.process_link_love_award(award, store=store, client=failing, bot_user_id="UROO")
    assert result["status"] == "pending_retry"
    restarted = link_love.LinkLoveAwardStore(path)
    client = SimpleNamespace(system_award_points=AsyncMock(return_value={"points_awarded": 2, "new_balance": 10}))
    result = await link_love.process_link_love_award(result["award"], store=restarted, client=client, bot_user_id="UROO")
    assert result["status"] == "awarded"
    assert client.system_award_points.call_args.kwargs["target_profile_id"] == PROFILE
    assert client.system_award_points.call_args.kwargs["idempotency_key"] == failing.system_award_points.call_args.kwargs["idempotency_key"]
