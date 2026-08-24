import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo.backend_identity import get_backend_actor_context
from roo.config import Settings
from roo.meeting_room_clarifications import (
    MeetingRoomClarificationStore,
    get_meeting_room_clarification_store,
    room_choice_from_reply,
)


MELBOURNE = ZoneInfo("Australia/Melbourne")


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "SLACK_RECEIPTS_DB_PATH": str(tmp_path / "roo-state.db"),
        "OPENAI_API_KEY": "synthetic-openai-key",
        "MLAI_BACKEND_URL": "https://backend.test",
        "ROO_API_KEY": "roo-test-key",
        "MEETING_ROOM_BOOKING_ENABLED": True,
        "ROO_CONTEXTUAL_RESPONSES_ENABLED": False,
    }
    values.update(overrides)
    return Settings(**values)


def _record_prompt(store, *, now=None, ttl_seconds=600):
    starts_at = datetime(2026, 8, 25, 14, tzinfo=MELBOURNE)
    return store.record_prompt(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        request_message_ts="111.000",
        owner_user_id="UOWNER",
        starts_at=starts_at.isoformat(),
        ends_at=(starts_at + timedelta(hours=1)).isoformat(),
        available_room_slugs=["small-meeting-room", "big-meeting-room"],
        ttl_seconds=ttl_seconds,
        now=now,
    )


@pytest.fixture(autouse=True)
def reset_store_cache():
    get_meeting_room_clarification_store.cache_clear()
    yield
    get_meeting_room_clarification_store.cache_clear()


@pytest.mark.parametrize(
    ("reply", "expected"),
    (
        ("big room", "big-meeting-room"),
        ("the large meeting room please", "big-meeting-room"),
        ("<@UROO> small room", "small-meeting-room"),
        ("the small one", "small-meeting-room"),
        ("big room or small room", None),
        ("not the big room", None),
        ("sounds good", None),
    ),
)
def test_room_choice_reply_parser_requires_one_unambiguous_room(reply, expected):
    assert room_choice_from_reply(reply) == expected


def test_clarification_survives_restart_and_first_room_choice_wins(tmp_path):
    database_path = tmp_path / "state.db"
    first_store = MeetingRoomClarificationStore(database_path)
    _record_prompt(first_store, now=1000)

    restarted_store = MeetingRoomClarificationStore(database_path)
    claimed = restarted_store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOWNER",
        room_slug="big-meeting-room",
        choice_message_ts="112.000",
        now=1001,
    )
    competing = first_store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOWNER",
        room_slug="small-meeting-room",
        choice_message_ts="113.000",
        now=1002,
    )

    assert claimed["disposition"] == "claimed"
    assert claimed["record"]["selected_room_slug"] == "big-meeting-room"
    assert claimed["record"]["booking_client_request_id"]
    assert competing["disposition"] == "already_selected"
    assert competing["record"]["selected_room_slug"] == "big-meeting-room"


def test_clarification_rejects_another_actor_and_expires(tmp_path):
    store = MeetingRoomClarificationStore(tmp_path / "state.db")
    _record_prompt(store, now=1000, ttl_seconds=10)

    wrong_actor = store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOTHER",
        room_slug="big-meeting-room",
        choice_message_ts="112.000",
        now=1001,
    )
    expired = store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOWNER",
        room_slug="big-meeting-room",
        choice_message_ts="113.000",
        now=1011,
    )

    assert wrong_actor["disposition"] == "wrong_owner"
    assert expired["disposition"] == "expired"
    assert expired["record"]["status"] == "expired"


def test_two_requesters_can_clarify_independently_in_one_thread(tmp_path):
    store = MeetingRoomClarificationStore(tmp_path / "state.db")
    first = _record_prompt(store, now=1000)
    starts_at = datetime(2026, 8, 25, 16, tzinfo=MELBOURNE)
    second = store.record_prompt(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        request_message_ts="112.000",
        owner_user_id="UOTHER",
        starts_at=starts_at.isoformat(),
        ends_at=(starts_at + timedelta(hours=1)).isoformat(),
        available_room_slugs=["small-meeting-room", "big-meeting-room"],
        now=1001,
    )

    first_claim = store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOWNER",
        room_slug="big-meeting-room",
        choice_message_ts="113.000",
        now=1002,
    )
    second_claim = store.claim_choice(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        actor_user_id="UOTHER",
        room_slug="small-meeting-room",
        choice_message_ts="114.000",
        now=1002,
    )

    assert first["id"] != second["id"]
    assert first_claim["record"]["starts_at"] != second_claim["record"]["starts_at"]
    assert first_claim["disposition"] == "claimed"
    assert second_claim["disposition"] == "claimed"


@pytest.mark.asyncio
async def test_public_thread_reply_resumes_privately_with_persisted_request(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    original = _record_prompt(store)
    completed = []
    posted = []

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            context = get_backend_actor_context()
            completed.append((kwargs, context))
            return {
                "message": "I've sent you a private reply about the Meeting Room.",
                "data": {
                    "action": "book_meeting_room",
                    "delivery": "direct_message",
                },
            }

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    handled = await main_module._handle_meeting_room_text_choice(
        {
            "type": "message",
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "big room",
            "client_msg_id": "choice-client-id",
        },
        slack_team_id="TMLAI",
    )

    assert handled is True
    assert len(completed) == 1
    call, context = completed[0]
    assert call["room_slug"] == "big-meeting-room"
    assert call["starts_at"] == original["starts_at"]
    assert call["ends_at"] == original["ends_at"]
    assert call["booking_client_request_id"]
    assert context.slack_team_id == "TMLAI"
    assert context.acting_slack_user_id == "UOWNER"
    assert posted == [
        {
            "channel": "CROOMS",
            "text": "I've sent you a private reply about the Meeting Room.",
            "thread_ts": "111.000",
        }
    ]
    stored = store.find(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
    )
    assert stored["status"] == "completed"

    later_reply = await main_module._handle_meeting_room_text_choice(
        {
            "type": "message",
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "120.000",
            "text": "big room",
        },
        slack_team_id="TMLAI",
    )
    assert later_reply is False
    assert len(completed) == 1
    assert len(posted) == 1


@pytest.mark.asyncio
async def test_public_room_choice_cannot_be_hijacked_or_moved_to_another_thread(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    _record_prompt(store)
    executions = []
    posted = []

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            executions.append(kwargs)
            raise AssertionError("unauthorised replies must not execute")

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    wrong_owner = await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOTHER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "small room",
        },
        slack_team_id="TMLAI",
    )
    wrong_thread = await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "999.000",
            "ts": "113.000",
            "text": "small room",
        },
        slack_team_id="TMLAI",
    )

    assert wrong_owner is True
    assert wrong_thread is False
    assert executions == []
    assert posted == []
    assert store.find(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
    )["status"] == "awaiting_choice"


@pytest.mark.asyncio
async def test_competing_public_replies_create_only_one_private_preview(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    _record_prompt(store)
    started = asyncio.Event()
    release = asyncio.Event()
    executions = []
    posted = []

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            executions.append(kwargs)
            started.set()
            await release.wait()
            return {
                "message": "I've sent you a private reply about the Meeting Room.",
                "data": {"delivery": "direct_message"},
            }

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    first = asyncio.create_task(
        main_module._handle_meeting_room_text_choice(
            {
                "user": "UOWNER",
                "channel": "CROOMS",
                "thread_ts": "111.000",
                "ts": "112.000",
                "text": "big room",
            },
            slack_team_id="TMLAI",
        )
    )
    await started.wait()
    second = await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "113.000",
            "text": "small room",
        },
        slack_team_id="TMLAI",
    )
    release.set()
    await first

    assert second is True
    assert len(executions) == 1
    assert executions[0]["room_slug"] == "big-meeting-room"
    assert any("already handling" in row["text"] for row in posted)
    assert any("private reply" in row["text"] for row in posted)


@pytest.mark.asyncio
async def test_private_delivery_failure_closes_choice_without_public_details(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    _record_prompt(store)
    posted = []

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            return {
                "message": "I could not open a private Slack DM. DM Roo and try again there.",
                "data": {
                    "delivery": "direct_message",
                    "delivery_failed": True,
                },
            }

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "small room",
        },
        slack_team_id="TMLAI",
    )

    assert store.find(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
    )["status"] == "failed"
    assert len(posted) == 1
    assert "private Slack DM" in posted[0]["text"]
    assert "2:00" not in posted[0]["text"]
    assert "points" not in posted[0]["text"].lower()


@pytest.mark.asyncio
async def test_public_ack_failure_does_not_reopen_a_delivered_private_preview(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    _record_prompt(store)

    class Executor:
        async def complete_meeting_room_room_choice(self, **kwargs):
            return {
                "message": "I've sent you a private reply about the Meeting Room.",
                "data": {"delivery": "direct_message"},
            }

    def fail_public_post(**kwargs):
        raise RuntimeError("Slack post failed")

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "get_agent",
        lambda: SimpleNamespace(skill_executor=Executor()),
    )
    monkeypatch.setattr(main_module, "post_message", fail_public_post)

    handled = await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "big room",
        },
        slack_team_id="TMLAI",
    )

    assert handled is True
    assert store.find(
        team_id="TMLAI",
        channel_id="CROOMS",
        thread_ts="111.000",
        owner_user_id="UOWNER",
    )["status"] == "completed"


@pytest.mark.asyncio
async def test_expired_public_reply_asks_requester_to_restart(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    store = get_meeting_room_clarification_store(
        configured.SLACK_RECEIPTS_DB_PATH
    )
    _record_prompt(store, now=time.time() - 20, ttl_seconds=10)
    posted = []

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ok": True},
    )

    handled = await main_module._handle_meeting_room_text_choice(
        {
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "big room",
        },
        slack_team_id="TMLAI",
    )

    assert handled is True
    assert posted[0]["thread_ts"] == "111.000"
    assert "expired" in posted[0]["text"]


@pytest.mark.asyncio
async def test_slack_dispatches_room_reply_outside_contextual_pilot(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    handled = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_room_choice(event, **kwargs):
        handled.append((event, kwargs))

    def capture_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "_handle_public_message_with_room_choice",
        fake_room_choice,
    )
    monkeypatch.setattr(main_module.asyncio, "create_task", capture_task)
    monkeypatch.setattr("roo.slack_client.get_channel_id", lambda name: None)

    payload = {
        "team_id": "TMLAI",
        "event_id": "EV-CHOICE",
        "event": {
            "type": "message",
            "channel_type": "channel",
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "big room",
        },
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest(), _verified=True)
    await asyncio.gather(*scheduled_tasks)

    assert response.status_code == 200
    assert len(handled) == 1
    assert handled[0][0]["thread_ts"] == "111.000"
    assert handled[0][1]["slack_team_id"] == "TMLAI"


@pytest.mark.asyncio
async def test_room_choice_gate_failure_preserves_explicit_mention_path(
    tmp_path,
    monkeypatch,
):
    configured = _settings(tmp_path)
    routed = []

    async def fail_choice(*args, **kwargs):
        raise RuntimeError("state unavailable")

    async def fake_mention(event):
        routed.append(event)
        return {"result": {}}

    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module,
        "_handle_meeting_room_text_choice",
        fail_choice,
    )
    monkeypatch.setattr(main_module, "_handle_mention", fake_mention)

    result = await main_module._handle_app_mention_with_room_choice(
        {
            "type": "app_mention",
            "user": "UOWNER",
            "channel": "CROOMS",
            "thread_ts": "111.000",
            "ts": "112.000",
            "text": "<@UROO> big room",
        },
        slack_team_id="TMLAI",
    )

    assert result == {"result": {}}
    assert len(routed) == 1
