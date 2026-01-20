from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from roo import quests


class DummySettings:
    MLAI_BACKEND_URL = "http://example"
    MLAI_API_KEY = "test-key"
    INTERNAL_API_KEY = "internal-key"


def _reset_link_love_state():
    quests._link_love_threads.clear()
    quests._link_love_daily_posts.clear()
    quests._link_love_thread_rewards.clear()
    quests._quest_progress.clear()
    quests._completed_quests.clear()


def _patch_link_love(monkeypatch, awards, messages):
    class FakeBackendClient:
        def __init__(self, **_kwargs):
            pass

        async def system_award_points(self, **kwargs):
            awards.append(kwargs)

    async def noop_update_progress(*_args, **_kwargs):
        return None

    monkeypatch.setattr(quests, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(quests, "MLAIBackendClient", FakeBackendClient)
    monkeypatch.setattr(quests, "get_bot_user_id", lambda: "B1")
    monkeypatch.setattr(
        quests,
        "get_channel_id",
        lambda name: "C123" if name == quests.LINK_LOVE_CHANNEL_NAME else None
    )
    monkeypatch.setattr(quests, "post_message", lambda **kwargs: messages.append(kwargs))
    monkeypatch.setattr(quests, "get_thread_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(quests, "_update_progress", noop_update_progress)


@pytest.mark.asyncio
async def test_link_love_awards_once_per_user(monkeypatch):
    _reset_link_love_state()
    awards = []
    messages = []
    _patch_link_love(monkeypatch, awards, messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000000.0001",
        "text": "Founder share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700000001.0002",
        "thread_ts": "1700000000.0001",
        "text": "Liked and shared this!",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700000002.0003",
        "thread_ts": "1700000000.0001",
        "text": "Shared again.",
    })

    assert len(awards) == 1
    assert awards[0]["target_slack_id"] == "U2"
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_link_love_no_self_reward(monkeypatch):
    _reset_link_love_state()
    awards = []
    messages = []
    _patch_link_love(monkeypatch, awards, messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000100.0001",
        "text": "Founder share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000101.0002",
        "thread_ts": "1700000100.0001",
        "text": "Shared it myself.",
    })

    assert awards == []
    assert messages == []


@pytest.mark.asyncio
async def test_link_love_daily_post_limit(monkeypatch):
    _reset_link_love_state()
    awards = []
    messages = []
    _patch_link_love(monkeypatch, awards, messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000200.0001",
        "text": "Founder share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700000201.0002",
        "thread_ts": "1700000200.0001",
        "text": "Liked this.",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000300.0001",
        "text": "Another share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U3",
        "channel": "C123",
        "ts": "1700000301.0002",
        "thread_ts": "1700000300.0001",
        "text": "Shared it.",
    })

    assert len(awards) == 1
    assert awards[0]["target_slack_id"] == "U2"


@pytest.mark.asyncio
async def test_link_love_requires_keyword(monkeypatch):
    _reset_link_love_state()
    awards = []
    messages = []
    _patch_link_love(monkeypatch, awards, messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000400.0001",
        "text": "Founder share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700000401.0002",
        "thread_ts": "1700000400.0001",
        "text": "Nice post!",
    })

    assert awards == []
    assert messages == []


@pytest.mark.asyncio
async def test_link_love_multiple_supporters(monkeypatch):
    _reset_link_love_state()
    awards = []
    messages = []
    _patch_link_love(monkeypatch, awards, messages)

    await quests.handle_quests({
        "type": "message",
        "user": "U1",
        "channel": "C123",
        "ts": "1700000500.0001",
        "text": "Founder share",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U2",
        "channel": "C123",
        "ts": "1700000501.0002",
        "thread_ts": "1700000500.0001",
        "text": "Shared!",
    })

    await quests.handle_quests({
        "type": "message",
        "user": "U3",
        "channel": "C123",
        "ts": "1700000502.0003",
        "thread_ts": "1700000500.0001",
        "text": "Liked and shared.",
    })

    assert len(awards) == 2
    assert {award["target_slack_id"] for award in awards} == {"U2", "U3"}
    assert len(messages) == 2
