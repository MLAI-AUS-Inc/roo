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
