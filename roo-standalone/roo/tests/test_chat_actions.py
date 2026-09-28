from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

from roo.chat_actions import action_digest, handle_chat_action


def fixture():
    action = {
        "type": "button",
        "action_id": "confirm_topic_btn_0",
        "value": "confirm_topic:job-one:0",
        "text": {"type": "plain_text", "text": "Choose AI"},
    }
    message = {
        "user": "UROO",
        "ts": "1790000000.000001",
        "text": "Topics",
        "blocks": [{"type": "actions", "elements": [action]}],
    }
    payload = {
        "workspace_id": "TMLAI",
        "channel_id": "DROO",
        "message_ts": message["ts"],
        "thread_ts": message["ts"],
        "user_id": "UOWNER",
        "action_id": action["action_id"],
        "action_hash": action_digest(message, action),
        "perform": True,
    }
    client = MagicMock()
    client.auth_test.return_value = {"team_id": "TMLAI", "user_id": "UROO"}
    client.conversations_history.return_value = {"messages": [message]}
    client.conversations_info.return_value = {"channel": {"user": "UOWNER"}}
    dispatch = AsyncMock(return_value=JSONResponse({}))
    return payload, message, client, dispatch


@pytest.mark.asyncio
async def test_dispatch_uses_current_slack_value_and_verified_actor():
    payload, message, client, dispatch = fixture()
    payload["value"] = "confirm_topic:attacker-job:9"
    updated = {
        **message,
        "blocks": [
            {
                "type": "context",
                "elements": [{"type": "plain_text", "text": "Accepted"}],
            }
        ],
    }
    client.conversations_history.side_effect = [
        {"messages": [message]},
        {"messages": [updated]},
    ]
    result = await handle_chat_action(payload, client=client, dispatch=dispatch)
    dispatched = dispatch.call_args.args[0]
    assert dispatched["actions"][0]["value"] == "confirm_topic:job-one:0"
    assert dispatched["user"]["id"] == "UOWNER"
    assert result["message"] == updated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,status",
    [
        ("workspace", 403),
        ("author", 404),
        ("owner", 403),
        ("value", 409),
        ("removed", 409),
        ("content", 409),
        ("confirm", 409),
    ],
)
async def test_stale_or_foreign_actions_never_dispatch(change, status):
    payload, message, client, dispatch = fixture()
    if change == "workspace":
        client.auth_test.return_value["team_id"] = "TOTHER"
    if change == "author":
        message["user"] = "UOTHER"
    if change == "owner":
        client.conversations_info.return_value["channel"]["user"] = "UOTHER"
    if change == "value":
        message["blocks"][0]["elements"][0]["value"] = "changed"
    if change == "removed":
        message["blocks"] = []
    if change == "content":
        message["blocks"].append(
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "Different topic"},
            }
        )
    if change == "confirm":
        message["blocks"][0]["elements"][0]["confirm"] = {"title": {"text": "Confirm"}}
    with pytest.raises(HTTPException) as error:
        await handle_chat_action(payload, client=client, dispatch=dispatch)
    assert error.value.status_code == status
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_get_never_dispatches_and_reply_fetch_is_exact():
    payload, message, client, dispatch = fixture()
    payload.update(perform=False, thread_ts="1789999999.000001")
    client.conversations_replies.return_value = {"messages": [message]}
    await handle_chat_action(payload, client=client, dispatch=dispatch)
    assert client.conversations_replies.call_args.kwargs["ts"] == payload["thread_ts"]
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_existing_owner_denial_is_not_reported_as_success():
    payload, _, client, dispatch = fixture()
    dispatch.return_value = JSONResponse(
        {"response_type": "ephemeral", "text": "Only the owner can choose"}
    )
    with pytest.raises(HTTPException) as error:
        await handle_chat_action(payload, client=client, dispatch=dispatch)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_unchanged_card_after_handler_failure_is_not_reported_as_success():
    payload, _, client, dispatch = fixture()
    with pytest.raises(HTTPException) as error:
        await handle_chat_action(payload, client=client, dispatch=dispatch)
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface,token,status",
    [("admin", "key", 404), ("public", "wrong", 401), ("public", "", 401)],
)
async def test_endpoint_requires_public_internal_service_authority(
    surface, token, status
):
    from types import SimpleNamespace
    from starlette.requests import Request
    from roo.main import api_chat_actions

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/chat-actions",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    with pytest.raises(HTTPException) as error:
        await api_chat_actions(
            request,
            SimpleNamespace(ROO_SURFACE=surface, INTERNAL_MENTION_API_KEY="key"),
        )
    assert error.value.status_code == status
