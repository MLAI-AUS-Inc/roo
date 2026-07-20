import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

intro = importlib.import_module("roo.start_here_introductions")


class FakeLLMResponse:
    def __init__(self, content):
        self.content = content


class FakeAwardClient:
    def __init__(self, response=None):
        self.response = response or {
            "awarded": True,
            "points_awarded": 4,
            "new_balance": 12,
        }
        self.calls = []

    async def award_first_channel_post(self, slack_user_id, channel_id):
        self.calls.append((slack_user_id, channel_id))
        return dict(self.response)


class RetryableAwardClient(FakeAwardClient):
    async def award_first_channel_post(self, slack_user_id, channel_id):
        self.calls.append((slack_user_id, channel_id))
        raise httpx.TransportError("temporarily unavailable")


async def valid_intro_llm(*args, **kwargs):
    return FakeLLMResponse(
        '{"introduces_person": true, "describes_startup": true, '
        '"confidence": 0.98, "missing_fields": [], '
        '"reason": "person and startup are both present"}'
    )


async def missing_startup_llm(*args, **kwargs):
    return FakeLLMResponse(
        '{"introduces_person": true, "describes_startup": false, '
        '"confidence": 0.97, "missing_fields": ["startup"], '
        '"reason": "no startup description"}'
    )


def make_store(tmp_path):
    return intro.StartHereIntroductionStore(tmp_path / "start-here.db")


def message_event(*, ts="111.000", text=None):
    return {
        "type": "message",
        "channel": "CSTART",
        "user": "UNEW",
        "ts": ts,
        "text": text
        or "Hi, I'm Jordan, a product manager building software that helps tradies quote jobs.",
    }


def edit_event(*, ts="111.000", text=None):
    return {
        "type": "message",
        "subtype": "message_changed",
        "channel": "CSTART",
        "message": {
            "type": "message",
            "user": "UNEW",
            "ts": ts,
            "text": text
            or "Hi, I'm Jordan, a product manager building software that helps tradies quote jobs.",
        },
    }


def test_normalize_intro_event_accepts_canonical_edits_and_rejects_threads_and_bots():
    normalized = intro.normalize_intro_event(edit_event())

    assert normalized is not None
    assert normalized.is_edit is True
    assert normalized.message_ts == "111.000"

    threaded = message_event()
    threaded["thread_ts"] = "100.000"
    assert intro.normalize_intro_event(threaded) is None

    bot = message_event()
    bot["bot_id"] = "B123"
    assert intro.normalize_intro_event(bot) is None


def test_parse_classification_fails_closed_for_non_boolean_true():
    result = intro.parse_classification(
        '{"introduces_person": "true", "describes_startup": true, '
        '"confidence": 0.99, "missing_fields": []}'
    )

    assert result.introduces_person is False
    assert result.describes_startup is True
    assert "person" in result.missing_fields
    assert result.qualifies(min_confidence=0.8) is False


@pytest.mark.asyncio
async def test_valid_first_intro_awards_four_points_once(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    client = FakeAwardClient()
    notifications = []
    monkeypatch.setattr(
        intro,
        "post_award_notification",
        lambda submission: notifications.append(dict(submission)),
    )

    first = await intro.handle_start_here_intro(
        message_event(),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        min_confidence=0.8,
    )
    duplicate_delivery = await intro.handle_start_here_intro(
        message_event(),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        min_confidence=0.8,
    )

    assert first["status"] == "awarded"
    assert duplicate_delivery["status"] == "duplicate_event"
    assert client.calls == [("UNEW", "CSTART")]
    assert len(notifications) == 1
    assert store.get_for_user("CSTART", "UNEW")["status"] == "awarded"


@pytest.mark.asyncio
async def test_incomplete_intro_requests_edit_without_award(tmp_path):
    store = make_store(tmp_path)
    client = FakeAwardClient()
    feedback = []

    result = await intro.handle_start_here_intro(
        message_event(text="Hi, I'm Jordan and I work in product management."),
        store=store,
        client=client,
        llm_chat=missing_startup_llm,
        min_confidence=0.8,
        post_feedback=lambda **kwargs: feedback.append(kwargs),
    )

    assert result["status"] == "awaiting_edit"
    assert client.calls == []
    assert feedback[0]["thread_ts"] == "111.000"
    assert "what your startup does" in feedback[0]["text"]
    assert "edit this original post" in feedback[0]["text"]


@pytest.mark.asyncio
async def test_editing_canonical_post_can_qualify_but_second_post_cannot(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    client = FakeAwardClient()
    feedback = []
    duplicate_notices = []
    notifications = []
    monkeypatch.setattr(
        intro,
        "post_award_notification",
        lambda submission: notifications.append(dict(submission)),
    )

    incomplete = await intro.handle_start_here_intro(
        message_event(text="Hello!"),
        store=store,
        client=client,
        post_feedback=lambda **kwargs: feedback.append(kwargs),
    )
    second_post = await intro.handle_start_here_intro(
        message_event(ts="222.000", text="This second post has all the details now."),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        notify_duplicate=lambda submission: duplicate_notices.append(dict(submission)),
    )
    repeated_second_post = await intro.handle_start_here_intro(
        message_event(ts="222.000", text="This second post has all the details now."),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        notify_duplicate=lambda submission: duplicate_notices.append(dict(submission)),
    )
    edited = await intro.handle_start_here_intro(
        edit_event(),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        min_confidence=0.8,
    )

    assert incomplete["status"] == "awaiting_edit"
    assert second_post["status"] == "duplicate_post"
    assert repeated_second_post["status"] == "duplicate_post"
    assert len(duplicate_notices) == 1
    assert edited["status"] == "awarded"
    assert client.calls == [("UNEW", "CSTART")]
    assert len(notifications) == 1
    submission = store.get_for_user("CSTART", "UNEW")
    assert submission["canonical_message_ts"] == "111.000"


@pytest.mark.asyncio
async def test_backend_already_awarded_is_terminal_and_silent(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    client = FakeAwardClient({"awarded": False})
    notifications = []
    monkeypatch.setattr(
        intro,
        "post_award_notification",
        lambda submission: notifications.append(dict(submission)),
    )

    result = await intro.handle_start_here_intro(
        message_event(),
        store=store,
        client=client,
        llm_chat=valid_intro_llm,
        min_confidence=0.8,
    )

    assert result["status"] == "already_awarded"
    assert notifications == []
    assert store.get_for_user("CSTART", "UNEW")["status"] == "already_awarded"


@pytest.mark.asyncio
async def test_retryable_award_failure_stays_queued_and_can_succeed(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(intro, "_now", lambda: clock[0])
    notifications = []
    monkeypatch.setattr(
        intro,
        "post_award_notification",
        lambda submission: notifications.append(dict(submission)),
    )
    store = make_store(tmp_path)
    failing_client = RetryableAwardClient()

    first = await intro.handle_start_here_intro(
        message_event(),
        store=store,
        client=failing_client,
        llm_chat=valid_intro_llm,
        min_confidence=0.8,
    )

    assert first["status"] == "pending_retry"
    queued = store.get_for_user("CSTART", "UNEW")
    assert queued["status"] == "pending_award"
    assert queued["next_attempt_at"] == 1030.0

    clock[0] = 1031.0
    claimed = store.claim_due_awards(limit=10, owner="retry-worker")
    assert len(claimed) == 1
    success_client = FakeAwardClient()
    retried = await intro.process_award(claimed[0], store=store, client=success_client)

    assert retried["status"] == "awarded"
    assert success_client.calls == [("UNEW", "CSTART")]
    assert len(notifications) == 1
