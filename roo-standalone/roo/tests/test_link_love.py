import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

link_love = importlib.import_module("roo.link_love")
main_module = importlib.import_module("roo.main")
slack_client_module = importlib.import_module("roo.slack_client")


class FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class FakeAwardClient:
    def __init__(self, balances=None):
        self.calls = []
        self.balances = balances or {}

    async def system_award_points(self, admin_slack_id, target_slack_id, points, reason):
        self.calls.append(
            {
                "admin_slack_id": admin_slack_id,
                "target_slack_id": target_slack_id,
                "points": points,
                "reason": reason,
            }
        )
        return {
            "points_awarded": points,
            "new_balance": self.balances.get(target_slack_id, 10),
        }


class RetryableFailureAwardClient:
    def __init__(self):
        self.calls = []

    async def system_award_points(self, admin_slack_id, target_slack_id, points, reason):
        self.calls.append((admin_slack_id, target_slack_id, points, reason))
        raise httpx.TransportError("backend temporarily unavailable")


def make_store(tmp_path):
    return link_love.LinkLoveAwardStore(tmp_path / "link-love.db")


def root_message(user="UROOT", text="Launch post"):
    return {"user": user, "text": text}


def reply_event(user="UHELPER", channel="CBOOST", root_ts="111.000", reply_ts="222.000", text="Done"):
    return {
        "type": "message",
        "user": user,
        "channel": channel,
        "thread_ts": root_ts,
        "ts": reply_ts,
        "text": text,
    }


async def llm_engaged(*args, **kwargs):
    return FakeLLMResponse('{"engaged": true, "confidence": 0.98, "reason": "explicit engagement"}')


async def llm_not_engaged(*args, **kwargs):
    return FakeLLMResponse('{"engaged": false, "confidence": 0.93, "reason": "vague support only"}')


def test_parse_link_love_classification_handles_fenced_json():
    result = link_love.parse_link_love_classification(
        '```json\n{"engaged": true, "confidence": 0.87, "reason": "liked"}\n```'
    )

    assert result.engaged is True
    assert result.confidence == 0.87
    assert result.reason == "liked"


def test_link_love_prompt_excludes_vague_support():
    messages = link_love.build_link_love_classification_messages(
        root_text="Please support my launch",
        reply_text="Love it!",
    )
    prompt_text = "\n".join(message["content"] for message in messages)

    assert "Do not count questions" in prompt_text
    assert "love it" in prompt_text.lower()
    assert "done" in prompt_text.lower()
    assert "liked" in prompt_text.lower()


@pytest.mark.asyncio
async def test_qualifying_reply_awards_points_without_immediate_slack_post(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    client = FakeAwardClient(balances={"UHELPER": 42})
    posted_messages = []
    monkeypatch.setattr(slack_client_module, "post_message", lambda **kwargs: posted_messages.append(kwargs))

    result = await link_love.handle_link_love_reply(
        reply_event(text="Liked and commented"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=60,
    )

    assert result["status"] == "awarded"
    assert client.calls == [
        {
            "admin_slack_id": "UROO",
            "target_slack_id": "UHELPER",
            "points": 2,
            "reason": "link-love",
        }
    ]
    assert posted_messages == []
    assert store.get_due_notification_groups() == []


@pytest.mark.asyncio
async def test_same_user_gets_one_award_per_root_post(tmp_path):
    store = make_store(tmp_path)
    client = FakeAwardClient()

    first = await link_love.handle_link_love_reply(
        reply_event(reply_ts="222.000", text="Done"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )
    second = await link_love.handle_link_love_reply(
        reply_event(reply_ts="333.000", text="Liked"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert first["status"] == "awarded"
    assert second["status"] == "already_awarded"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_same_user_can_earn_for_different_root_posts(tmp_path):
    store = make_store(tmp_path)
    client = FakeAwardClient()

    await link_love.handle_link_love_reply(
        reply_event(root_ts="111.000", reply_ts="222.000"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )
    await link_love.handle_link_love_reply(
        reply_event(root_ts="444.000", reply_ts="555.000"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_root_author_cannot_earn_on_own_post(tmp_path):
    store = make_store(tmp_path)
    client = FakeAwardClient()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM should not be called for root author replies")

    result = await link_love.handle_link_love_reply(
        reply_event(user="UROOT", text="Done"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(user="UROOT"),
        llm_chat=fail_if_called,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert result == {"status": "ignored", "reason": "root_author_reply"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_non_proof_reply_can_be_followed_by_later_proof_reply(tmp_path):
    store = make_store(tmp_path)
    client = FakeAwardClient()

    first = await link_love.handle_link_love_reply(
        reply_event(reply_ts="222.000", text="Love it!"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_not_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )
    second = await link_love.handle_link_love_reply(
        reply_event(reply_ts="333.000", text="Liked"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert first["status"] == "ineligible"
    assert second["status"] == "awarded"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_retryable_backend_failure_keeps_award_pending(tmp_path):
    store = make_store(tmp_path)
    client = RetryableFailureAwardClient()

    result = await link_love.handle_link_love_reply(
        reply_event(text="Done"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert result["status"] == "pending_retry"
    assert client.calls == [("UROO", "UHELPER", 2, "link-love")]
    assert result["award"]["status"] == "pending_award"
    assert result["award"]["attempt_count"] == 1
    assert store.get_due_notification_groups() == []


@pytest.mark.asyncio
async def test_batched_notification_groups_awarded_users_in_root_thread(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    client = FakeAwardClient(balances={"UONE": 12, "UTWO": 18})
    posted_messages = []
    monkeypatch.setattr(
        slack_client_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "999.000"},
    )

    await link_love.handle_link_love_reply(
        reply_event(user="UONE", reply_ts="222.000", text="Done"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )
    await link_love.handle_link_love_reply(
        reply_event(user="UTWO", reply_ts="333.000", text="Liked and reposted"),
        store=store,
        client=client,
        get_root_message=lambda channel, ts: root_message(),
        llm_chat=llm_engaged,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )

    assert link_love.post_due_link_love_notifications(store=store) == 1
    assert posted_messages == [
        {
            "channel": "CBOOST",
            "thread_ts": "111.000",
            "text": (
                ":tada: Awarded 2 points each for link-love.\n"
                ":white_check_mark: <@UONE>: now has 12 pts\n"
                ":white_check_mark: <@UTWO>: now has 18 pts"
            ),
        }
    ]
    assert link_love.post_due_link_love_notifications(store=store) == 0


@pytest.mark.asyncio
async def test_slack_events_boost_thread_reply_triggers_link_love_handler(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_handle_link_love_reply(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            BOOST_LINK_LOVE_ENABLED=True,
            BOOST_LINK_LOVE_CHANNEL_NAME="boost-my-startup",
        ),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_channel_id",
        lambda name: "CSTART" if name == "_start-here" else "CBOOST",
    )
    monkeypatch.setattr(main_module, "handle_link_love_reply", fake_handle_link_love_reply)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": reply_event(channel="CBOOST", root_ts="111.000", reply_ts="222.000")
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {"type": "message", "user": "UHELPER", "channel": "CBOOST", "ts": "111.000", "text": "Top level"},
        {"type": "message", "user": "UHELPER", "channel": "COTHER", "thread_ts": "111.000", "ts": "222.000"},
        {"type": "message", "user": "UBOT", "channel": "CBOOST", "thread_ts": "111.000", "ts": "222.000", "bot_id": "B123"},
        {"type": "message", "user": "UHELPER", "channel": "CBOOST", "thread_ts": "111.000", "ts": "222.000", "subtype": "message_changed"},
    ],
)
async def test_slack_events_non_qualifying_messages_do_not_trigger_link_love(event, monkeypatch):
    handled_events = []

    def fake_create_task(coro):
        coro.close()
        return None

    async def fake_handle_link_love_reply(event):
        handled_events.append(event)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            BOOST_LINK_LOVE_ENABLED=True,
            BOOST_LINK_LOVE_CHANNEL_NAME="boost-my-startup",
        ),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_channel_id",
        lambda name: "CSTART" if name == "_start-here" else "CBOOST",
    )
    monkeypatch.setattr(main_module, "handle_link_love_reply", fake_handle_link_love_reply)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    class FakeRequest:
        async def json(self):
            return {"event": event}

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert handled_events == []


@pytest.mark.asyncio
async def test_slack_events_dm_does_not_trigger_link_love(monkeypatch):
    link_love_events = []
    mention_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_handle_link_love_reply(event):
        link_love_events.append(event)

    async def fake_handle_mention(event):
        mention_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            BOOST_LINK_LOVE_ENABLED=True,
            BOOST_LINK_LOVE_CHANNEL_NAME="boost-my-startup",
        ),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_channel_id",
        lambda name: "CSTART" if name == "_start-here" else "CBOOST",
    )
    monkeypatch.setattr(main_module, "handle_link_love_reply", fake_handle_link_love_reply)
    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    event = {
        "type": "message",
        "user": "UHELPER",
        "channel": "D123",
        "channel_type": "im",
        "ts": "222.000",
        "text": "hello",
    }

    class FakeRequest:
        async def json(self):
            return {"event": event}

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert link_love_events == []
    assert mention_events == [event]
