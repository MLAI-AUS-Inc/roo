from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from roo.internal_mentions import handle_internal_mention

PAYLOAD = {
    "text": "Please book me in on 2026-09-08.",
    "user_id": "UMEMBER", "channel_id": "CCOWORK", "thread_ts": "1788838200.000001",
    "post_reply": True, "request_id": "49ef0a0b-bdf4-4aa4-a184-20644cd4e758",
}


@pytest.mark.asyncio
async def test_booking_uses_normal_agent_and_posts_its_reply_with_stable_identity():
    agent = AsyncMock()
    agent.handle_mention.return_value = {"message": "Your desk is confirmed", "skill_used": "coworking"}
    with patch("roo.internal_mentions.post_message", return_value={"ok": True}) as post:
        result = await handle_internal_mention(PAYLOAD, agent)
    assert result["reply_delivered"] is True
    agent.handle_mention.assert_awaited_once_with(
        text=PAYLOAD["text"], user_id="UMEMBER", channel_id="CCOWORK", thread_ts=PAYLOAD["thread_ts"])
    assert post.call_args.kwargs["client_msg_id"] == PAYLOAD["request_id"]
    assert post.call_args.kwargs["thread_ts"] == PAYLOAD["thread_ts"]


@pytest.mark.asyncio
async def test_replies_are_opt_in_and_suppressed_private_responses_stay_private():
    agent = AsyncMock()
    agent.handle_mention.return_value = {"message": "Private reply handled", "suppress_post": True}
    with patch("roo.internal_mentions.post_message") as post:
        await handle_internal_mention(PAYLOAD, agent)
        await handle_internal_mention({**PAYLOAD, "post_reply": False}, agent)
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("request_id", "bad"), ("channel_id", ""), ("thread_ts", ""), ("user_id", "")])
async def test_invalid_destination_never_runs_booking(key, value):
    agent = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await handle_internal_mention({**PAYLOAD, key: value}, agent)
    assert error.value.status_code == 400
    agent.handle_mention.assert_not_called()


@pytest.mark.asyncio
async def test_failed_reply_is_not_reported_as_delivered():
    agent = AsyncMock()
    agent.handle_mention.return_value = {"message": "Done"}
    with patch("roo.internal_mentions.post_message", return_value={"ok": False}):
        with pytest.raises(HTTPException) as error:
            await handle_internal_mention(PAYLOAD, agent)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_empty_reply_is_not_reported_as_delivered():
    agent = AsyncMock()
    agent.handle_mention.return_value = {"message": ""}
    with pytest.raises(HTTPException) as error:
        await handle_internal_mention(PAYLOAD, agent)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_chat_command_uses_existing_coworking_shortcut_with_frozen_date():
    from roo.agent import RooAgent

    agent = object.__new__(RooAgent)
    agent._execute_fast_points = AsyncMock(return_value={"message": "Booking request received"})
    result = await agent._try_fast_path(
        PAYLOAD["text"], PAYLOAD["user_id"], PAYLOAD["channel_id"], PAYLOAD["thread_ts"])
    assert result["message"] == "Booking request received"
    agent._execute_fast_points.assert_awaited_once_with(
        "UMEMBER", "book_coworking", date="2026-09-08",
        channel_id="CCOWORK", thread_ts=PAYLOAD["thread_ts"])


@pytest.mark.asyncio
async def test_private_command_passes_no_conversation_and_blocks_second_execution():
    from unittest.mock import Mock
    from roo.internal_mentions import handle_private_command
    data = dict(
        workspace_id="TTEAM",
        user_id="UADMIN",
        source_event_id="a" * 64,
        text="<@UROO> give <@UANDREW> 4 points for helping",
        version=2,
        actor_profile_id="00000000-0000-4000-8000-000000000001",
        posting_mode="member",
        recipient_targets=[
            dict(
                kind="mlai_profile",
                profile_id="00000000-0000-4000-8000-000000000002",
                display_name="Andrew",
                source_labels=["Andrew"],
                slack_user_id="UANDREW",
            )
        ],
        source_labels=["Andrew"],
    )
    agent = AsyncMock()
    agent.handle_mention.return_value = {
        "message": "Award confirmed",
        "data": {"internal": "private"},
    }
    receipts = Mock()
    receipts.claim.side_effect = [True, False]
    for field in ("channel_id", "thread_ts", "history", "post_reply", "files"):
        with pytest.raises(HTTPException):
            await handle_private_command({**data, field: "unexpected"}, agent, receipts)
    receipts.claim.assert_not_called()
    assert await handle_private_command(data, agent, receipts) == {"message": "Award confirmed"}
    kwargs = agent.handle_mention.await_args.kwargs
    assert not {"channel_id", "thread_ts", "thread_history", "event_files"} & kwargs.keys()
    assert kwargs["verified_bridge_context"].recipient_targets[0].slack_user_id == "UANDREW"
    with pytest.raises(HTTPException) as duplicate:
        await handle_private_command(data, agent, receipts)
    assert duplicate.value.status_code == 409
    agent.handle_mention.assert_awaited_once()


@pytest.mark.parametrize(
    ("surface", "key", "authorization", "status"),
    [
        ("public", "", "", 404),
        ("admin", "test-internal-key", "Bearer test-internal-key", 404),
        ("public", "test-internal-key", "Bearer other-test-key", 401),
    ],
)
def test_private_command_requires_public_surface_and_service_identity(
    monkeypatch, surface, key, authorization, status
):
    from fastapi.testclient import TestClient
    from roo.main import app, get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ROO_SURFACE", surface)
    monkeypatch.setattr(settings, "INTERNAL_MENTION_API_KEY", key)
    with patch("roo.main.get_agent") as agent:
        result = TestClient(app).post(
            "/api/chat-command", headers={"Authorization": authorization}, json={}
        )
        assert result.status_code == status
        agent.assert_not_called()
