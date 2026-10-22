"""Hermetic member redemption contract and consent tests; no DB or services."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from roo.backend_identity import (
    BackendActorContext, BackendIdentityError, use_backend_actor_context,
)
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from roo.event_discounts import confirmation_quote_id, handle_event_discount

QUOTE_ID = "00000000-0000-4000-8000-000000000001"
REDEMPTION_ID = "00000000-0000-4000-8000-000000000002"
ACTOR = BackendActorContext("TMLAI", "UMEMBER", "DPRIVATE", "1792454400.000100", "EvTEST")
SETTINGS = SimpleNamespace(ROO_EVENT_DISCOUNTS_ENABLED=True, ROO_SURFACE="public", ROO_API_KEY="test-service-key", ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID="TMLAI")


def quote_result():
    return {
        "quote_id": QUOTE_ID,
        "event": {"id": "evt-test", "name": "AI Meetup", "url": "https://luma.com/ai-meetup", "starts_at": "2027-01-15T18:00:00+11:00"},
        "points_cost": "10.00", "discount_description": "20% off a standard ticket",
        "eligible_balance": "25.00",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }


def receipt(status="fulfilled"):
    return {
        "id": REDEMPTION_ID, "status": status,
        "event": {"name": "AI Meetup"}, "points_cost": "10.00",
        "coupon_code": "ROO-UNIQUE", "checkout_url": "https://luma.com/ai-meetup?coupon=ROO-UNIQUE",
        "expires_at": "2027-01-15T00:00:00Z",
    }


def fake_client():
    return SimpleNamespace(
        quote_event_discount=AsyncMock(return_value=quote_result()),
        redeem_event_discount=AsyncMock(return_value=receipt()),
        get_event_redemption=AsyncMock(return_value=receipt()),
        list_event_redemptions=AsyncMock(return_value={"redemptions": [receipt()]}),
    )


async def handle(client, *, action="quote_event_discount", text="I want to spend my points on AI Meetup", params=None, settings=SETTINGS, actor=ACTOR, **kwargs):
    args = {"user_id": "UMEMBER", "channel_id": "DPRIVATE", "thread_ts": "1792454400.000100", **kwargs}
    with use_backend_actor_context(actor):
        return await handle_event_discount(client=client, action=action, text=text,
                                           params=params or {"event_query": "AI Meetup"}, settings=settings, **args)


@pytest.mark.asyncio
async def test_quote_shows_authoritative_cost_and_exact_confirmation_without_spending():
    client = fake_client()
    result = await handle(client, params={"event_query": "AI Meetup", "slack_user_id": "UFORGED"})
    client.quote_event_discount.assert_awaited_once_with("AI Meetup")
    client.redeem_event_discount.assert_not_awaited()
    assert f"confirm event discount {QUOTE_ID}" in result["message"]
    assert "10.00 Roo Points" in result["message"]
    assert "No points have been spent" in result["message"]
    assert result["data"]["private_points"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["yes", "confirmed", f"don't confirm event discount {QUOTE_ID}", f"please confirm event discount {QUOTE_ID}", f"quoted: confirm event discount {QUOTE_ID}"])
async def test_model_supplied_consent_or_quote_never_spends(text):
    client = fake_client()
    result = await handle(client, action="redeem_event_discount", text=text, params={"quote_id": QUOTE_ID, "confirmed": True})
    client.redeem_event_discount.assert_not_awaited()
    assert "copy the" in result


@pytest.mark.asyncio
async def test_exact_confirmation_uses_original_text_not_forged_tool_quote():
    client = fake_client()
    result = await handle(client, action="redeem_event_discount", text=f"confirm event discount {QUOTE_ID}", params={"quote_id": REDEMPTION_ID, "target_user": "UFORGED"})
    client.redeem_event_discount.assert_awaited_once_with(QUOTE_ID)
    assert "ROO-UNIQUE" in result["message"]
    assert "https://luma.com/ai-meetup?coupon=ROO-UNIQUE" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"channel_id": "CPUBLIC"}, {"user_id": "UFORGED"}, {"thread_ts": "1792454401.000100"},
    {"actor": None}, {"settings": SimpleNamespace(ROO_EVENT_DISCOUNTS_ENABLED=False)},
    {"settings": SimpleNamespace(ROO_EVENT_DISCOUNTS_ENABLED=True, ROO_SURFACE="admin")},
])
async def test_missing_private_verified_actor_and_disabled_surface_fail_before_api(kwargs):
    client = fake_client()
    result = await handle(client, **kwargs)
    assert isinstance(result, str)
    for method in vars(client).values():
        method.assert_not_awaited()
    assert "ROO-UNIQUE" not in result


@pytest.mark.asyncio
async def test_existing_coupon_reads_do_not_spend():
    client = fake_client()
    result = await handle(client, action="get_event_redemption", params={"redemption_id": REDEMPTION_ID})
    assert "ROO-UNIQUE" in result["message"]
    result = await handle(client, action="list_event_redemptions")
    assert "ROO-UNIQUE" in result["message"]
    client.redeem_event_discount.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_is_not_reported_as_success_and_timeout_instructs_safe_recovery():
    client = fake_client()
    client.redeem_event_discount.return_value = receipt("processing")
    result = await handle(client, action="redeem_event_discount", text=f"confirm event discount {QUOTE_ID}")
    assert "still being created" in result["message"]
    assert "ROO-UNIQUE" not in result["message"]
    client.redeem_event_discount.side_effect = MLAIBackendUnavailableError("timeout")
    result = await handle(client, action="redeem_event_discount", text=f"confirm event discount {QUOTE_ID}")
    assert "repeating your confirmation safely" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://attacker.test/ai?coupon=ROO-UNIQUE", "https://luma.com.attacker.test/ai?coupon=ROO-UNIQUE", "https://luma.com/ai?coupon=OTHER", "https://user:password@luma.com/ai?coupon=ROO-UNIQUE", "http://luma.com/ai?coupon=ROO-UNIQUE", "https://luma.com/ai?coupon=ROO-UNIQUE&coupon=OTHER", "https://luma.com/ai?coupon=ROO-UNIQUE&note=<@UFORGED>"])
async def test_untrusted_or_mismatched_coupon_links_are_never_returned(url):
    client = fake_client()
    client.get_event_redemption.return_value = {**receipt(), "checkout_url": url}
    result = await handle(client, action="get_event_redemption", params={"redemption_id": REDEMPTION_ID})
    assert isinstance(result, str)
    assert url not in result


@pytest.mark.asyncio
async def test_executor_wires_event_action_without_passing_model_identity(monkeypatch):
    from roo.skills import executor
    monkeypatch.setattr(executor, "get_settings", lambda: SETTINGS)
    client = fake_client()
    with use_backend_actor_context(ACTOR):
        result = await executor.SkillExecutor()._handle_points_action(
            client=client, action="quote_event_discount", params={"event_query": "AI Meetup", "user_id": "UFORGED"},
            text="spend points for AI Meetup", user_id="UMEMBER", channel_id="DPRIVATE", thread_ts="1792454400.000100", skill=None,
        )
    assert "AI Meetup" in result["message"]
    client.quote_event_discount.assert_awaited_once_with("AI Meetup")


@pytest.mark.asyncio
async def test_http_contract_uses_private_actor_and_stable_key_for_retries(monkeypatch):
    captures = []
    async def request(_self, method, url, **kwargs):
        captures.append((method, url, kwargs))
        payload = {"redemptions": []} if url.endswith("redemptions/") else receipt()
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    client = MLAIBackendClient(base_url="https://backend.test/api/v1", api_key="test-roo-key", internal_api_key="never-use-admin", actor_context=ACTOR)
    await client.quote_event_discount("AI Meetup")
    await client.redeem_event_discount(QUOTE_ID)
    await client.redeem_event_discount(QUOTE_ID)
    await client.get_event_redemption(REDEMPTION_ID)
    await client.list_event_redemptions()
    assert captures[0][1] == "https://backend.test/api/v1/points/event-discounts/quote/"
    for method, url, kwargs in captures:
        values = kwargs["json"] if method == "POST" else kwargs["params"]
        assert values["slack_user_id"] == "UMEMBER"
        assert values["slack_team_id"] == "TMLAI"
        assert values["slack_channel_id"] == "DPRIVATE"
        assert values["slack_thread_ts"] == "1792454400.000100"
        assert kwargs["headers"]["X-API-Key"] == "test-roo-key"
    first = captures[1][2]
    second = captures[2][2]
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
    assert first["json"]["request_id"] == first["headers"]["Idempotency-Key"]
    assert first["json"]["confirmed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,surface", [(None, "public"), (ACTOR, "admin"), (BackendActorContext("TMLAI", "UMEMBER", "CPUBLIC", "1792454400.000100", "EvTEST"), "public")])
async def test_http_client_denies_unverified_or_admin_context_before_transport(monkeypatch, actor, surface):
    transport = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "request", transport)
    client = MLAIBackendClient(base_url="https://backend.test", api_key="test-key", actor_context=actor, surface=surface)
    with pytest.raises(BackendIdentityError):
        await client.quote_event_discount("AI Meetup")
    transport.assert_not_awaited()


def test_confirmation_fast_path_and_existing_coupon_commands():
    from roo.agent import RooAgent
    agent = RooAgent.__new__(RooAgent)
    assert agent._match_fast_path(f"confirm event discount {QUOTE_ID}") == "redeem_event_discount"
    assert agent._match_fast_path("my event discounts") == "list_event_redemptions"
    assert confirmation_quote_id(f"do not confirm event discount {QUOTE_ID}") is None


@pytest.mark.asyncio
async def test_ambiguous_events_show_backend_choices_without_spending():
    client = fake_client()
    response = httpx.Response(409, json={"code": "ambiguous_event", "candidates": [
        {"event_id": "evt-one", "event_name": "AI Meetup Melbourne"},
        {"event_id": "evt-two", "event_name": "AI Meetup Sydney"},
    ]}, request=httpx.Request("POST", "https://backend.test/quote/"))
    client.quote_event_discount.side_effect = httpx.HTTPStatusError("ambiguous", request=response.request, response=response)
    result = await handle(client)
    assert "AI Meetup Melbourne" in result and "evt-two" in result
    client.redeem_event_discount.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_allowlist_denies_before_calling_api():
    client = fake_client()
    actor = BackendActorContext("TOTHER", "UMEMBER", "DPRIVATE", "1792454400.000100", "EvTEST")
    result = await handle(client, actor=actor)
    assert "not enabled for this workspace" in result
    client.quote_event_discount.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, [], {"event": "malformed", "quote_id": QUOTE_ID, "expires_at": "2027-01-01T00:00:00Z"}, {**quote_result(), "points_cost": "NaN"}, {**quote_result(), "points_cost": "-1"}])
async def test_invalid_backend_quote_does_not_claim_success_or_spend(result):
    client = fake_client()
    client.quote_event_discount.return_value = result
    response = await handle(client)
    assert isinstance(response, str)
    assert "couldn't verify" in response
    client.redeem_event_discount.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_new_redemptions_preserve_private_history():
    client = fake_client()
    settings = SimpleNamespace(**{**vars(SETTINGS), "ROO_EVENT_DISCOUNTS_ENABLED": False})
    result = await handle(client, action="list_event_redemptions", settings=settings)
    assert "ROO-UNIQUE" in result["message"]
    client.list_event_redemptions.assert_awaited_once()
    client.redeem_event_discount.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"event_query": "Completely different reward"}, {"event_query": "evt-tampered", "slack_user_id": "UFORGED"}])
async def test_original_chat_event_marker_overrides_model_query_without_changing_actor(monkeypatch, params):
    from roo.skills import executor
    monkeypatch.setattr(executor, "get_settings", lambda: SETTINGS)
    client = fake_client()
    client.quote_event_discount.return_value["event"]["id"] = "evt-explicit-choice"
    text = (
        "I want to spend my Roo Points to get a discount to Public Event Title "
        "(Luma event ID: evt-explicit-choice). Please check eligibility and quote "
        "the points cost and discount before I confirm."
    )
    with use_backend_actor_context(ACTOR):
        result = await executor.SkillExecutor()._handle_points_action(
            client=client, action="quote_event_discount", params=params,
            text=text, user_id="UMEMBER", channel_id="DPRIVATE",
            thread_ts="1792454400.000100", skill=None,
        )
    client.quote_event_discount.assert_awaited_once_with("evt-explicit-choice")
    client.redeem_event_discount.assert_not_awaited()
    assert "confirm event discount" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["(Luma event ID: not-canonical)", "(Luma event ID: evt-one) (Luma event ID: evt-two)"])
async def test_malformed_or_conflicting_original_markers_fail_before_api(marker):
    client = fake_client()
    result = await handle(client, text=marker, params={"event_query": "evt-model-choice"})
    assert isinstance(result, str)
    client.quote_event_discount.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_chat_card_request_uses_quote_fast_path_without_model_parameters():
    from roo.agent import RooAgent
    agent = RooAgent.__new__(RooAgent)
    agent._execute_fast_points = AsyncMock(return_value={"message": "Quoted"})
    text = (
        "I want to spend my Roo Points to get a discount to Public Event Title "
        "(Luma event ID: evt-explicit-choice). Please check eligibility and quote "
        "the points cost and discount before I confirm."
    )
    assert agent._match_fast_path(text) == "quote_event_discount"
    result = await agent._try_fast_path(text, "UMEMBER", "DPRIVATE", "1792454400.000100")
    assert result == {"message": "Quoted"}
    agent._execute_fast_points.assert_awaited_once_with(
        "UMEMBER", "quote_event_discount", text=text,
        channel_id="DPRIVATE", thread_ts="1792454400.000100",
    )


@pytest.mark.asyncio
async def test_backend_cannot_substitute_a_different_event_for_original_id():
    client = fake_client()
    result = await handle(client, text="Quote (Luma event ID: evt-explicit-choice)")
    assert isinstance(result, str)
    assert "couldn't verify" in result
    client.redeem_event_discount.assert_not_awaited()


@pytest.mark.asyncio
async def test_www_coupon_link_is_normalized_to_exact_canonical_luma_link():
    client = fake_client()
    client.get_event_redemption.return_value = {
        **receipt(), "checkout_url": "https://www.luma.com/ai-meetup?coupon=ROO-UNIQUE",
    }
    result = await handle(client, action="get_event_redemption", params={"redemption_id": REDEMPTION_ID})
    assert "https://luma.com/ai-meetup?coupon=ROO-UNIQUE" in result["message"]
    assert "www.luma.com" not in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_changes,extra", [
    ({"url": "https://luma.com.evil.test/ai"}, {}),
    ({"url": "https://luma.com/two/slugs"}, {}),
    ({"starts_at": "2027-01-01T00:00:00"}, {}),
    ({}, {"eligible_balance": "NaN"}),
    ({}, {"eligible_balance": "1"}),
])
async def test_incomplete_or_untrusted_quote_terms_cannot_offer_spending(event_changes, extra):
    client = fake_client()
    result = quote_result()
    result.update(extra)
    result["event"].update(event_changes)
    client.quote_event_discount.return_value = result
    response = await handle(client)
    assert isinstance(response, str)
    assert "couldn't verify" in response
    client.redeem_event_discount.assert_not_awaited()
