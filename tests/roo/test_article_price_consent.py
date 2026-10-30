import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, MagicMock

import httpx
import pytest

from roo.clients import mlai_backend
from roo import content_factory_pricing as pricing
from roo.content_factory_progress import build_content_factory_live_cost_text


ORIGIN = {
    "workspace_id": "TMLAI", "channel_id": "C123",
    "thread_ts": "111.222", "message_ts": "111.222",
}


def bind(card, **overrides):
    pricing.bind_confirmation_card(card["blocks"], text=card["message"], **{**ORIGIN, **overrides})


@pytest.fixture(autouse=True)
def clear_confirmations():
    pricing._confirmations.clear()
    yield
    pricing._confirmations.clear()


def setup_client(monkeypatch, replies):
    client = mlai_backend.MLAIBackendClient(base_url="https://backend.test", api_key="test")
    calls = []

    async def request(method, endpoint, **kwargs):
        calls.append((endpoint, dict(kwargs["json"])))
        status, data = replies.pop(0)
        return httpx.Response(status, json=data, request=httpx.Request(method, f"https://backend.test{endpoint}"))

    monkeypatch.setattr(client, "_request", request)
    return client, calls


def token(card):
    return card["blocks"][1]["elements"][0]["value"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [0, 6, 12])
async def test_new_article_quotes_requesting_founder_and_only_dispatches_exact_confirmation(monkeypatch, cost):
    client, calls = setup_client(monkeypatch, [(200, {"costPoints": cost}), (200, {"job_id": "new", "status": "queued"})])
    card = await client.trigger_article_generation(
        "UEFFECTIVE", "mlai.au", topic="AI", requested_by_slack_user_id="UFOUNDER",
        client_request_id="stable-123", slack_channel_id="C123", context="Keep this context",
    )
    assert calls == [("/api/v1/content/quote", {
        "domain": "mlai.au", "slack_user_id": "UEFFECTIVE",
        "requested_by_slack_user_id": "UFOUNDER", "client_request_id": "stable-123",
    })]
    assert f"{cost} Roo points" in card["message"]
    bind(card)
    record = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    assert record["payload"]["expected_cost_points"] == cost
    result = await client.submit_article_request(record["endpoint"], record["payload"])
    assert result["job_id"] == "new"
    assert calls[-1][1]["expected_cost_points"] == cost
    assert type(calls[-1][1]["expected_cost_points"]) is int
    assert calls[-1][1]["client_request_id"] == "stable-123"
    assert calls[-1][1]["context"] == "Keep this context"


@pytest.mark.asyncio
async def test_legacy_source_only_confirmation_keeps_source_option_and_backend_key_resolution(monkeypatch):
    client, calls = setup_client(monkeypatch, [(200, {"costPoints": 6})])
    card = await client.confirm_article_topic("research-7", "UFOUNDER", option_index=2)
    assert calls[0] == ("/api/v1/content/quote", {
        "slack_user_id": "UFOUNDER", "source_run_id": "research-7",
    })
    bind(card)
    record = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    assert record["endpoint"] == "/api/v1/content/jobs/research-7/confirm"
    assert record["payload"]["option_index"] == 2
    assert record["payload"]["source_run_id"] == "research-7"
    assert "client_request_id" not in record["payload"]
    assert record["payload"]["expected_cost_points"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", [{}, {"costPoints": None}, {"costPoints": True}, {"costPoints": "12"}, {"costPoints": -1}, {"costPoints": 6.0}, []])
async def test_malformed_quotes_fail_closed(monkeypatch, quote):
    client, calls = setup_client(monkeypatch, [(200, quote)])
    with pytest.raises(pricing.ArticleQuoteUnavailableError):
        await client.trigger_article_generation("UFOUNDER", "mlai.au", topic="AI")
    assert len(calls) == 1
    assert calls[0][0] == "/api/v1/content/quote"
    assert not pricing._confirmations


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 409, 500, 503])
async def test_quote_errors_never_dispatch_or_fallback(monkeypatch, status):
    client, calls = setup_client(monkeypatch, [(status, {"costPoints": 12})])
    with pytest.raises(pricing.ArticleQuoteUnavailableError):
        await client.confirm_article_topic("job-7", "UFOUNDER", domain="mlai.au")
    assert len(calls) == 1
    assert not pricing._confirmations


@pytest.mark.asyncio
async def test_research_remains_unchanged(monkeypatch):
    client, calls = setup_client(monkeypatch, [(200, {"status": "queued", "job_id": "research"})])
    await client.trigger_article_generation("UFOUNDER", "mlai.au", client_request_id="research-1")
    assert len(calls) == 1
    assert calls[0][0] == "/api/v1/content/generate"
    assert "expected_cost_points" not in calls[0][1]


@pytest.mark.asyncio
async def test_non_price_conflict_is_not_requoted(monkeypatch):
    client, calls = setup_client(monkeypatch, [(409, {"code": "wrong_payer"})])
    with pytest.raises(httpx.HTTPStatusError):
        await client.trigger_article_generation("UFOUNDER", "example.com", topic="AI", expected_cost_points=6)
    assert len(calls) == 1


def test_foreign_expired_and_mutated_confirmations_fail_closed(monkeypatch):
    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 6)
    with pytest.raises(ValueError, match="requesting user"):
        pricing.get_confirmation(token(card), "UOTHER", **ORIGIN)
    bind(card)
    record = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    record["payload"]["expected_cost_points"] = 0
    assert pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)["payload"]["expected_cost_points"] == 6
    monkeypatch.setattr(pricing.time, "monotonic", lambda: record["expires_at"] + 1)
    with pytest.raises(ValueError, match="expired"):
        pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)


@pytest.mark.asyncio
async def test_slack_price_change_requires_another_click_and_keeps_request_key(monkeypatch):
    from roo import main, slack_client

    client, calls = setup_client(monkeypatch, [
        (200, {"costPoints": 6}),
        (409, {"code": "roo_points_quote_changed", "costPoints": 12}),
        (200, {"costPoints": 12}),
        (200, {"status": "queued", "job_id": "article-1"}),
    ])
    card = await client.trigger_article_generation("UFOUNDER", "example.com", topic="AI", client_request_id="fixed-id")
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    monkeypatch.setattr(slack_client, "get_slack_app_identity", lambda: {"team_id": "TMLAI", "user_id": "UROO"})
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="test"))
    message = {"user": "UROO", "ts": "111.222", "text": card["message"], "blocks": deepcopy(card["blocks"])}

    def update_message(**kwargs):
        text = kwargs["text"]
        if kwargs.get("parse") != "none":
            text = text.replace("example.com", "<http://example.com|example.com>")
        message.update(text=text, blocks=kwargs["blocks"])
        return {"ok": True, "channel": "C123", "ts": "111.222"}

    update = Mock(side_effect=update_message)
    history = Mock(side_effect=lambda **kwargs: {"ok": True, "messages": [deepcopy(message)]})
    monkeypatch.setattr(slack_client, "get_slack_client", lambda: SimpleNamespace(chat_update=update, conversations_history=history))
    monkeypatch.setattr(main, "_remember_content_thread_context", Mock())
    monkeypatch.setattr(main, "_maybe_schedule_content_factory_watchdog", Mock())
    bind(card)
    action_payload = {
        "team": {"id": "TMLAI"}, "channel": {"id": "C123"}, "message": deepcopy(message),
        "actions": [{"action_id": "confirm_article_price", "value": token(card)}],
    }
    result = await main._handle_article_price_confirmation(action_payload, "UOTHER")
    assert json.loads(result.body)["response_type"] == "ephemeral"
    assert len(calls) == 1
    result = await main._handle_article_price_confirmation(action_payload, "UFOUNDER")
    assert result.status_code == 200
    assert [call[0] for call in calls] == ["/api/v1/content/quote", "/api/v1/content/generate", "/api/v1/content/quote"]
    assert calls[1][1]["expected_cost_points"] == 6
    assert "12 Roo points" in update.call_args.kwargs["text"]
    assert update.call_args.kwargs["attachments"] == []
    with pytest.raises(ValueError, match="expired"):
        pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    action_payload["actions"][0]["value"] = update.call_args.kwargs["blocks"][1]["elements"][0]["value"]
    action_payload["message"] = deepcopy(message)
    await main._handle_article_price_confirmation(action_payload, "UFOUNDER")
    assert calls[-1][1]["expected_cost_points"] == 12
    assert {call[1]["client_request_id"] for call in calls} == {"fixed-id"}


@pytest.mark.parametrize("domain", ["mlai.au", "example.com"])
def test_progress_never_invents_price_or_debit(domain):
    text = build_content_factory_live_cost_text(domain)
    assert "free" not in text
    assert "deducted" not in text
    assert "4 Roo" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("action_id", ["confirm_topic", "select_alternative", "confirm_topic_btn_0"])
async def test_all_topic_buttons_display_quote_before_submission(monkeypatch, action_id):
    from roo import main, slack_client

    client, calls = setup_client(monkeypatch, [(200, {"costPoints": 12})])
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    monkeypatch.setattr(slack_client, "get_slack_app_identity", lambda: {"team_id": "TMLAI", "user_id": "UROO"})
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="test"))
    monkeypatch.setattr(main, "get_agent", lambda: SimpleNamespace(get_thread_context=lambda *args: {
        "requested_by_slack_user_id": "UFOUNDER", "effective_slack_user_id": "UFOUNDER",
    }))
    update = Mock(return_value={"ok": True, "channel": "C123", "ts": "111.222"})
    monkeypatch.setattr(slack_client, "get_slack_client", lambda: SimpleNamespace(chat_update=update))
    watchdog = Mock()
    monkeypatch.setattr(main, "_maybe_schedule_content_factory_watchdog", watchdog)
    action = {"action_id": action_id, "value": "confirm:AI:job-7"}
    if action_id == "select_alternative":
        action["selected_option"] = {"value": action.pop("value")}
    if action_id == "confirm_topic_btn_0":
        action["value"] = "confirm_topic:job-7:0"
    payload = {
        "type": "block_actions", "user": {"id": "UFOUNDER"},
        "channel": {"id": "C123"}, "message": {"ts": "111.222"},
        "actions": [action],
    }
    result = await main._handle_slack_action_payload(SimpleNamespace(headers={}), payload)
    assert result.status_code == 200
    assert len(calls) == 1 and calls[0][0] == "/api/v1/content/quote"
    assert "12 Roo points" in update.call_args.kwargs["text"]
    assert update.call_args.kwargs["blocks"][1]["elements"][0]["action_id"] == "confirm_article_price"
    displayed_token = update.call_args.kwargs["blocks"][1]["elements"][0]["value"]
    assert pricing.get_confirmation(displayed_token, "UFOUNDER", **ORIGIN)["origin"] == ORIGIN
    watchdog.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["workspace", "channel", "thread", "message", "missing_thread", "container", "unbound"])
async def test_wrong_card_origin_cannot_dispatch_or_rewrite_destination(monkeypatch, change):
    from roo import main

    card = pricing.create_confirmation("/api/v1/content/jobs/job-7/confirm", {
        "slack_user_id": "UFOUNDER", "source_run_id": "job-7", "client_request_id": "stable-id",
    }, 6)
    if change != "unbound":
        bind(card, thread_ts="111.000")
    saved = deepcopy(pricing._confirmations[token(card)])
    payload = {
        "team": {"id": "TMLAI"}, "channel": {"id": "C123"},
        "message": {"ts": "111.222", "thread_ts": "111.000"},
        "container": {"channel_id": "C123", "message_ts": "111.222"},
        "actions": [{"action_id": "confirm_article_price", "value": token(card)}],
    }
    if change == "workspace":
        payload["team"]["id"] = "TOTHER"
    elif change == "channel":
        payload["channel"]["id"] = payload["container"]["channel_id"] = "COTHER"
    elif change == "thread":
        payload["message"]["thread_ts"] = "111.999"
    elif change == "message":
        payload["message"]["ts"] = payload["container"]["message_ts"] = "111.999"
    elif change == "missing_thread":
        del payload["message"]["thread_ts"]
    elif change == "container":
        payload["container"]["message_ts"] = "111.999"
    constructor = Mock()
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", constructor)
    result = await main._handle_article_price_confirmation(payload, "UFOUNDER")
    assert json.loads(result.body)["response_type"] == "ephemeral"
    constructor.assert_not_called()
    assert pricing._confirmations[token(card)] == saved


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "TOTHER"), ("channel_id", "COTHER"),
    ("thread_ts", "111.000"), ("message_ts", "111.333"),
])
def test_card_cannot_be_rebound_to_a_copied_origin(field, value):
    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 6)
    bind(card)
    saved = deepcopy(pricing._confirmations[token(card)])
    with pytest.raises(ValueError, match="already bound"):
        bind(card, **{field: value})
    assert pricing._confirmations[token(card)] == saved


@pytest.mark.parametrize("response", [
    {"ok": False},
    {"ok": True, "channel": "COTHER", "ts": "111.222"},
    {"ok": True, "channel": "C123"},
    {"ok": True, "channel": "C123", "ts": "111.999"},
    {"ok": True, "channel": "C123", "ts": "111.222", "message": {"thread_ts": "111.999"}},
])
def test_unverified_card_delivery_never_activates_token(monkeypatch, response):
    from roo import slack_client

    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 6)
    monkeypatch.setattr(slack_client, "get_slack_app_identity", lambda: {"team_id": "TMLAI", "user_id": "UROO"})
    with pytest.raises(ValueError):
        slack_client.bind_article_price_message(
            response, text=card["message"], blocks=card["blocks"], channel="C123", thread_ts=None, message_ts="111.222",
        )
    with pytest.raises(ValueError, match="original message"):
        pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "chat"])
@pytest.mark.parametrize("thread_ts", [None, "111.000"])
@pytest.mark.parametrize("change", [None, "price", "topic", "label", "fallback", "attachments", "slack_defaults"])
async def test_delivered_card_content_is_verified_for_slack_and_chat(monkeypatch, transport, thread_ts, change):
    from roo import main, slack_client
    from roo.chat_actions import action_digest, handle_chat_action
    from fastapi import HTTPException

    client, calls = setup_client(monkeypatch, [
        (200, {"costPoints": 12}), (200, {"status": "confirmed", "job_id": "article-1"}),
    ])
    card = await client.confirm_article_topic("job-7", "UFOUNDER", domain="example.com", option_index=1)
    with pytest.raises(ValueError, match="original message"):
        pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    message = {"user": "UROO", "ts": "111.222", "text": card["message"], "blocks": card["blocks"]}
    if thread_ts:
        message["thread_ts"] = thread_ts
    slack = MagicMock()
    slack.auth_test.return_value = {"ok": True, "team_id": "TMLAI", "user_id": "UROO", "bot_id": "BROO"}
    def post_message(**kwargs):
        # Slack's default URL parsing would rewrite the retained fallback text.
        text = kwargs["text"]
        if kwargs.get("parse") != "none":
            text = text.replace("example.com", "<http://example.com|example.com>")
        message.update(text=text, blocks=kwargs["blocks"])
        return {"ok": True, "channel": "C123", "ts": "111.222", "message": message}

    slack.chat_postMessage.side_effect = post_message
    slack.conversations_history.side_effect = lambda **kwargs: {"ok": True, "messages": [deepcopy(message)]}
    slack.conversations_replies.side_effect = lambda **kwargs: {"ok": True, "messages": [deepcopy(message)]}

    def update_message(**kwargs):
        message.update(text=kwargs["text"], blocks=kwargs["blocks"])
        return {"ok": True, "channel": "C123", "ts": "111.222"}

    slack.chat_update.side_effect = update_message
    monkeypatch.setattr(slack_client, "get_slack_client", lambda: slack)
    monkeypatch.setattr(slack_client, "get_slack_app_identity", lambda: {"team_id": "TMLAI", "user_id": "UROO"})
    # This is the real shared posting boundary used by normal agent replies
    # and internal Chat mentions, with only Slack's transport mocked.
    slack_client.post_message("C123", card["message"], thread_ts=thread_ts, blocks=card["blocks"])
    origin = {**ORIGIN, "thread_ts": thread_ts or "111.222"}
    saved = pricing.get_confirmation(token(card), "UFOUNDER", **origin)
    assert saved["origin"] == origin
    if change == "price":
        message["text"] = message["text"].replace("12 Roo points", "6 Roo points")
        message["blocks"][0]["text"]["text"] = message["blocks"][0]["text"]["text"].replace("12 Roo points", "6 Roo points")
        message["blocks"][1]["elements"][0]["text"]["text"] = "Confirm 6 Roo points"
    elif change == "topic":
        message["blocks"][0]["text"]["text"] = "Generate a different article for another website?"
    elif change == "label":
        message["blocks"][1]["elements"][0]["text"]["text"] = "Confirm 6 Roo points"
    elif change == "fallback":
        message["text"] = message["text"].replace("12 Roo points", "6 Roo points")
    elif change == "attachments":
        message["attachments"] = [{"text": "This article now costs 6 Roo points."}]
    elif change == "slack_defaults":
        message["blocks"][0]["block_id"] = "slack-generated-id"
        message["blocks"][0]["text"]["emoji"] = True
    changed = change not in {None, "slack_defaults"}
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="test"))
    monkeypatch.setattr(main, "_remember_content_thread_context", Mock())
    monkeypatch.setattr(main, "_maybe_schedule_content_factory_watchdog", Mock())
    action = card["blocks"][1]["elements"][0]

    async def dispatch(payload):
        return await main._handle_slack_action_payload(SimpleNamespace(headers={}), payload)

    if transport == "slack":
        result = await dispatch({
            "type": "block_actions", "team": {"id": "TMLAI"}, "user": {"id": "UFOUNDER"},
            "channel": {"id": "C123"}, "message": message, "actions": [action],
            "container": {"channel_id": "C123", "message_ts": "111.222"},
        })
        if changed:
            assert json.loads(result.body)["response_type"] == "ephemeral"
        else:
            assert json.loads(result.body) == {}
    else:
        chat_payload = {
            **origin, "user_id": "UFOUNDER", "action_id": "confirm_article_price",
            "action_hash": action_digest(message, action), "perform": True, "value": "forged-token",
        }
        if changed:
            # A fresh digest of the edited bot-owned source does not establish
            # consent to the original stored price or request.
            with pytest.raises(HTTPException) as error:
                await handle_chat_action(chat_payload, client=slack, dispatch=dispatch)
            assert error.value.status_code == 403
        else:
            result = await handle_chat_action(chat_payload, client=slack, dispatch=dispatch)
            assert "confirmed price of 12 Roo points" in result["message"]["text"]
    if changed:
        assert len(calls) == 1
        slack.chat_update.assert_not_called()
        with pytest.raises(ValueError, match="expired"):
            pricing.get_confirmation(token(card), "UFOUNDER", **origin)
        return
    assert len(calls) == 2
    assert calls[-1][0] == "/api/v1/content/jobs/job-7/confirm"
    assert calls[-1][1] == saved["payload"]
    assert calls[-1][1]["expected_cost_points"] == 12
    assert "client_request_id" not in calls[-1][1]
    assert calls[-1][1]["source_run_id"] == "job-7"
    assert pricing.get_confirmation(token(card), "UFOUNDER", **origin) == saved


@pytest.mark.asyncio
@pytest.mark.parametrize("effective_user", ["UFOUNDER", "UEFFECTIVE"])
async def test_paid_source_replay_preserves_original_six_point_quote_without_replacement_key(monkeypatch, effective_user):
    # The trusted backend has verified this source's original six-point debit
    # and persisted original-paid-key, despite the current new-run price of 12.
    # Its source-only contract deliberately does not expose or replace the key.
    client, calls = setup_client(monkeypatch, [
        (200, {"costPoints": 6}),
        (200, {"status": "confirmed", "job_id": "original-paid-article"}),
        (200, {"status": "confirmed", "job_id": "original-paid-article"}),
    ])
    card = await client.confirm_article_topic(
        "legacy-paid-source", effective_user, option_index=0,
        requested_by_slack_user_id="UFOUNDER",
    )
    bind(card)
    if effective_user != "UFOUNDER":
        with pytest.raises(ValueError, match="requesting user"):
            pricing.get_confirmation(token(card), effective_user, **ORIGIN)
    confirmed = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    for _ in range(2):
        result = await client.submit_article_request(confirmed["endpoint"], confirmed["payload"])
        assert result["job_id"] == "original-paid-article"
    assert calls[0] == ("/api/v1/content/quote", {
        "slack_user_id": effective_user, "requested_by_slack_user_id": "UFOUNDER",
        "source_run_id": "legacy-paid-source",
    })
    assert [call[0] for call in calls[1:]] == ["/api/v1/content/jobs/legacy-paid-source/confirm"] * 2
    assert all("client_request_id" not in payload for _, payload in calls)
    assert all(payload["source_run_id"] == "legacy-paid-source" for _, payload in calls)
    assert all(payload["requested_by_slack_user_id"] == "UFOUNDER" for _, payload in calls)
    assert calls[1][1] == calls[2][1] == confirmed["payload"]
    assert calls[1][1]["expected_cost_points"] == 6


@pytest.mark.asyncio
async def test_source_reconfirmation_keeps_authoritative_source_key_resolution(monkeypatch):
    client, calls = setup_client(monkeypatch, [
        (200, {"costPoints": 6}),
        (409, {"code": "roo_points_quote_changed", "costPoints": 12}),
        (200, {"costPoints": 12}),
        (200, {"status": "confirmed", "job_id": "new-article"}),
    ])
    card = await client.confirm_article_topic("research-source", "UFOUNDER")
    bind(card)
    first = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    refreshed = await client.submit_article_request(first["endpoint"], first["payload"])
    assert len(calls) == 3  # Quote refresh does not dispatch the newly priced article.
    assert refreshed["status"] == pricing.PRICE_CONFIRMATION_STATUS
    bind(refreshed)
    second = pricing.get_confirmation(token(refreshed), "UFOUNDER", **ORIGIN)
    await client.submit_article_request(second["endpoint"], second["payload"])
    assert all("client_request_id" not in payload for _, payload in calls)
    assert all(payload["source_run_id"] == "research-source" for _, payload in calls)
    assert calls[1][1]["expected_cost_points"] == 6
    assert calls[3][1]["expected_cost_points"] == 12


@pytest.mark.asyncio
async def test_explicit_original_source_key_is_forwarded_without_rewriting(monkeypatch):
    client, calls = setup_client(monkeypatch, [(200, {"costPoints": 6}), (200, {"status": "confirmed"})])
    card = await client.confirm_article_topic("paid-source", "UFOUNDER", client_request_id="original-paid-key")
    bind(card)
    record = pricing.get_confirmation(token(card), "UFOUNDER", **ORIGIN)
    await client.submit_article_request(record["endpoint"], record["payload"])
    assert all(payload["client_request_id"] == "original-paid-key" for _, payload in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_snapshot", ["clicked", "source"])
async def test_both_click_snapshot_and_fresh_source_must_match_saved_card(monkeypatch, changed_snapshot):
    from roo import main, slack_client

    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 12)
    bind(card)
    clicked = {"user": "UROO", "ts": "111.222", "text": card["message"], "blocks": deepcopy(card["blocks"])}
    source = deepcopy(clicked)
    changed = clicked if changed_snapshot == "clicked" else source
    changed["text"] = changed["text"].replace("12 Roo points", "6 Roo points")
    read = Mock(return_value=source)
    monkeypatch.setattr(slack_client, "read_article_price_message", read)
    constructor = Mock()
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", constructor)
    result = await main._handle_article_price_confirmation({
        "team": {"id": "TMLAI"}, "channel": {"id": "C123"}, "message": clicked,
        "actions": [{"action_id": "confirm_article_price", "value": token(card)}],
    }, "UFOUNDER")
    assert json.loads(result.body)["response_type"] == "ephemeral"
    read.assert_called_once_with(ORIGIN)
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "author", "thread", "workspace", "api_failure"])
async def test_unverified_fresh_slack_source_never_submits(monkeypatch, failure):
    from roo import main, slack_client

    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 12)
    bind(card)
    clicked = {"user": "UROO", "ts": "111.222", "text": card["message"], "blocks": deepcopy(card["blocks"])}
    source = deepcopy(clicked)
    if failure == "author":
        source["user"] = "UOTHER"
    elif failure == "thread":
        source["thread_ts"] = "111.000"
    slack = MagicMock()
    slack.conversations_history.return_value = {
        "ok": failure != "api_failure", "messages": [] if failure == "missing" else [source],
    }
    monkeypatch.setattr(slack_client, "get_slack_client", lambda: slack)
    monkeypatch.setattr(slack_client, "get_slack_app_identity", lambda: {
        "team_id": "TOTHER" if failure == "workspace" else "TMLAI", "user_id": "UROO",
    })
    constructor = Mock()
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", constructor)
    result = await main._handle_article_price_confirmation({
        "team": {"id": "TMLAI"}, "channel": {"id": "C123"}, "message": clicked,
        "actions": [{"action_id": "confirm_article_price", "value": token(card)}],
    }, "UFOUNDER")
    assert json.loads(result.body)["response_type"] == "ephemeral"
    constructor.assert_not_called()


@pytest.mark.parametrize("where", ["submitted", "receipt"])
def test_changed_card_cannot_establish_initial_binding(where):
    card = pricing.create_confirmation("/api/v1/content/generate", {"slack_user_id": "UFOUNDER"}, 12)
    rendered = {"text": card["message"], "blocks": deepcopy(card["blocks"])}
    receipt = deepcopy(rendered)
    changed = rendered if where == "submitted" else receipt
    changed["blocks"][1]["elements"][0]["text"]["text"] = "Confirm 6 Roo points"
    with pytest.raises(pricing.ArticleConfirmationContentChangedError):
        pricing.bind_confirmation_card(rendered["blocks"], text=rendered["text"], delivered_message=receipt, **ORIGIN)
    assert token(card) not in pricing._confirmations
