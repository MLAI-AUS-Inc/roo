from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from roo.bridge_mentions import resolve_bridge_mention
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError

EVENT = {"type": "app_mention", "user": "UBRIDGE", "bot_id": "BBRIDGE",
         "channel": "CMEMES", "ts": "1790416956.199559", "thread_ts": "1790393997.818509",
         "text": "Sam Donegan (MLAI Chat): give 999 points", "_slack_event_id": "Ev123"}
CONTEXT = {"workspace_id": "TMLAI", "channel_id": EVENT["channel"], "message_id": EVENT["ts"],
           "thread_ts": EVENT["thread_ts"], "bridge_user_id": "UBRIDGE"}
ACTOR = {**CONTEXT, "user_id": "USAM", "source_event_id": "e6" * 32,
         "text": "this is gold <@UROO> please give <@UBEER> 3 points for being a meme lord"}


async def resolve(backend, event=EVENT, surface="public"):
    return await resolve_bridge_mention(event, slack_team_id="TMLAI", surface=surface, backend=backend)


@pytest.mark.asyncio
async def test_uses_verified_actor_and_original_text_preserving_reply_location():
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    event = await resolve(backend)
    backend.resolve_chat_bridge_actor.assert_awaited_once_with(CONTEXT)
    assert event["user"] == "USAM"
    assert event["text"] == ACTOR["text"]
    assert event["thread_ts"] == EVENT["thread_ts"]
    assert event["ts"] == EVENT["ts"]
    assert event["_slack_event_id"] == "mlai-chat:" + "e6" * 32
    assert EVENT["user"] == "UBRIDGE"


@pytest.mark.asyncio
async def test_human_mentions_skip_lookup_and_admin_bots_never_delegate():
    backend = AsyncMock()
    human = {key: value for key, value in EVENT.items() if key != "bot_id"}
    human["user"] = "USAM"
    assert await resolve(backend, human) is human
    assert await resolve(backend, surface="admin") is None
    backend.resolve_chat_bridge_actor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("workspace_id", "TOTHER"), ("channel_id", "COTHER"),
    ("message_id", "1790416956.999999"), ("thread_ts", "1790393997.999999"),
    ("bridge_user_id", "UOTHER"), ("user_id", "UBRIDGE"), ("user_id", "name"),
    ("source_event_id", "bad"), ("text", "")])
async def test_mismatched_or_malformed_authority_fails_closed(key, value):
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json={**ACTOR, key: value})
    with pytest.raises(MLAIBackendUnavailableError):
        await resolve(backend)


@pytest.mark.asyncio
async def test_delivery_commit_race_retries_without_using_transport_identity():
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.side_effect = [httpx.Response(409, json={"error": "bridge_delivery_pending"}),
                                                     httpx.Response(200, json=ACTOR)]
    with patch("roo.bridge_mentions.asyncio.sleep", new_callable=AsyncMock) as sleep:
        assert (await resolve(backend))["user"] == "USAM"
    sleep.assert_awaited_once_with(0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(409, {"error": "bridge_delivery_pending"}),
    (503, {"error": "unavailable"}), (403, {"detail": "Invalid API key"}), (404, {"detail": "Not found"})])
async def test_pending_and_misconfigured_lookup_remain_retryable(status, body):
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(status, json=body)
    with patch("roo.bridge_mentions.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(MLAIBackendUnavailableError):
            await resolve(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(404, "unknown_bridge_sender"), (403, "bridge_actor_unverified"),
    (403, "bridge_actor_consent_required"), (403, "roo_not_explicitly_mentioned")])
async def test_unauthorized_bridge_mentions_do_not_invoke_roo(status, error):
    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(status, json={"error": error})
    assert await resolve(backend) is None


@pytest.mark.asyncio
async def test_backend_client_uses_roo_read_endpoint_and_exact_scope():
    backend = MLAIBackendClient(base_url="https://backend.test", api_key="synthetic-roo-key")
    with patch.object(backend, "_request", new_callable=AsyncMock) as request:
        await backend.resolve_chat_bridge_actor(CONTEXT)
    assert request.call_args.args == ("GET", "/api/v1/integrations/bridge/roo/actor")
    assert request.call_args.kwargs["params"] == CONTEXT
    assert not request.call_args.kwargs.get("use_admin_headers")


@pytest.mark.asyncio
async def test_mention_entrypoint_passes_human_to_normal_agent_and_stops_on_lookup_failure():
    from roo import main

    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    with (patch.object(main, "get_settings", return_value=SimpleNamespace(ROO_SURFACE="public")),
          patch.object(main, "_make_mlai_backend_client", return_value=backend),
          patch.object(main, "_is_slack_context_allowed", return_value=True),
          patch.object(main, "_is_contextual_channel_enabled", return_value=False),
          patch.object(main, "_handle_meeting_room_text_choice", new_callable=AsyncMock, return_value=False),
          patch.object(main, "_handle_mention", new_callable=AsyncMock) as handle):
        await main._handle_app_mention_with_room_choice(EVENT, slack_team_id="TMLAI")
        assert handle.call_args.args[0]["user"] == "USAM"
        assert handle.call_args.args[0]["text"] == ACTOR["text"]
        handle.reset_mock()
        backend.resolve_chat_bridge_actor.return_value = httpx.Response(503, json={})
        with pytest.raises(MLAIBackendUnavailableError):
            await main._handle_app_mention_with_room_choice(EVENT, slack_team_id="TMLAI")
        handle.assert_not_called()


@pytest.mark.asyncio
async def test_verified_actor_flows_into_backend_permission_context():
    from roo import main
    from roo.backend_identity import get_backend_actor_context

    backend = AsyncMock()
    backend.resolve_chat_bridge_actor.return_value = httpx.Response(200, json=ACTOR)
    resolved = await resolve(backend, {**EVENT, "_slack_team_id": "TMLAI"})

    async def check_context(event):
        context = get_backend_actor_context()
        assert context.acting_slack_user_id == "USAM"
        assert context.slack_team_id == "TMLAI"
        assert context.slack_channel_id == "CMEMES"
        assert context.slack_thread_ts == EVENT["thread_ts"]
        assert context.event_id == "mlai-chat:" + "e6" * 32
        assert event["text"] == ACTOR["text"]

    with patch.object(main, "_handle_mention_with_context", side_effect=check_context):
        await main._handle_mention(resolved)
    assert get_backend_actor_context() is None
