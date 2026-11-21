from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from roo import points_confirmation_delivery as delivery
from roo.slack_client import ThreadMessages


@pytest.fixture
def job():
    return {
        "request_id": 42,
        "channel_id": "CLOCAL",
        "thread_ts": "100.1",
        "text": "✅ Points request approved by <@UADMIN>.",
        "attempt": 1,
        "author_id": "UROO",
        "existing_message_id": None,
    }


@pytest.mark.asyncio
async def test_worker_retries_an_approval_after_restart_without_reawarding(monkeypatch, job):
    post = Mock(return_value={"ok": True, "ts": "101.2"})
    monkeypatch.setattr(delivery, "post_message", post)
    job["attempt"] = 2
    monkeypatch.setattr(delivery, "get_thread_messages", Mock(return_value=ThreadMessages()))
    client = SimpleNamespace(
        claim_points_confirmation=AsyncMock(return_value=job),
        record_points_confirmation=AsyncMock(),
    )

    assert await delivery.deliver_next_confirmation(client)
    assert post.call_args.kwargs["thread_ts"] == "100.1"
    assert post.call_args.kwargs["metadata"]["event_payload"] == {"request_id": "42"}
    client.record_points_confirmation.assert_awaited_once_with(42, "CLOCAL", "101.2")


@pytest.mark.asyncio
async def test_lost_slack_receipt_reuses_message_by_matching_roo_metadata(monkeypatch, job):
    post = Mock()
    monkeypatch.setattr(delivery, "post_message", post)
    job["attempt"] = 2
    marker = {"event_type": delivery.EVENT_TYPE, "event_payload": {"request_id": "42"}}
    history = ThreadMessages([
        {"user": "UOTHER", "thread_ts": "100.1", "ts": "100.9", "metadata": marker},
        {"user": "UROO", "thread_ts": "100.1", "ts": "101.2", "metadata": marker},
    ])
    monkeypatch.setattr(delivery, "get_thread_messages", Mock(return_value=history))
    client = SimpleNamespace(
        claim_points_confirmation=AsyncMock(return_value=job),
        record_points_confirmation=AsyncMock(),
    )

    assert await delivery.deliver_next_confirmation(client)
    post.assert_not_called()
    client.record_points_confirmation.assert_awaited_once_with(42, "CLOCAL", "101.2")


@pytest.mark.asyncio
async def test_incomplete_history_leaves_the_approval_queued(monkeypatch, job):
    post = Mock()
    monkeypatch.setattr(delivery, "post_message", post)
    job["attempt"] = 2
    monkeypatch.setattr(delivery, "get_thread_messages", Mock(return_value=ThreadMessages(complete=False)))
    client = SimpleNamespace(
        claim_points_confirmation=AsyncMock(return_value=job),
        record_points_confirmation=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="history is incomplete"):
        await delivery.deliver_next_confirmation(client)
    post.assert_not_called()
    client.record_points_confirmation.assert_not_called()
