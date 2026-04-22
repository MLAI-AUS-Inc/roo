import importlib

import httpx
import pytest


coworking = importlib.import_module("roo.coworking_booking_intents")
backend_module = importlib.import_module("roo.clients.mlai_backend")
slack_client_module = importlib.import_module("roo.slack_client")


def test_intent_store_persists_and_claims_due_work(tmp_path):
    store = coworking.CoworkingBookingIntentStore(tmp_path / "intents.db")

    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
        request_text="book me in today",
    )

    assert intent["status"] == "pending"
    assert intent["idempotency_key"] == "coworking:U123:2026-04-22"

    leased = store.reserve_for_processing(intent["id"], owner="test-worker")
    assert leased["status"] == "processing"
    assert leased["attempt_count"] == 1

    retry = store.mark_retryable_failure(
        intent["id"],
        error="MLAIBackendUnavailableError: timeout",
        delay_seconds=0,
    )
    assert retry["status"] == "pending_retry"

    claimed = store.claim_due(owner="retry-worker")
    assert len(claimed) == 1
    assert claimed[0]["status"] == "processing"
    assert claimed[0]["attempt_count"] == 2

    confirmed = store.mark_confirmed(
        intent["id"],
        backend_result={"id": "booking-1", "date": "2026-04-22"},
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["backend_booking_id"] == "booking-1"
    assert store.counts_by_status() == {"confirmed": 1}


@pytest.mark.asyncio
async def test_process_intent_confirms_existing_booking_and_notifies(tmp_path, monkeypatch):
    store = coworking.CoworkingBookingIntentStore(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
        request_text="book me in today",
    )
    leased = store.reserve_for_processing(intent["id"], owner="test-worker")
    posted = []

    class FakeClient:
        async def get_my_bookings(self, slack_user_id):
            assert slack_user_id == "U123"
            return [{"date": "2026-04-22", "status": "booked"}]

        async def book_coworking(self, *args, **kwargs):
            raise AssertionError("should not create a duplicate booking")

    monkeypatch.setattr(
        slack_client_module,
        "post_message",
        lambda **kwargs: posted.append(kwargs) or {"ts": "333.444"},
    )

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=FakeClient(),
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert result["already_booked"] is True
    assert store.get(intent["id"])["status"] == "confirmed"
    assert posted == [
        {
            "channel": "C123",
            "thread_ts": "111.222",
            "text": "I retried your queued coworking booking and confirmed 2026-04-22. You're booked.",
        }
    ]


@pytest.mark.asyncio
async def test_process_intent_keeps_retryable_backend_timeout_queued(tmp_path):
    store = coworking.CoworkingBookingIntentStore(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
        request_text="book me in today",
    )
    leased = store.reserve_for_processing(intent["id"], owner="test-worker")

    class FakeClient:
        async def get_my_bookings(self, slack_user_id):
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=FakeClient(),
        notify=True,
    )

    updated = store.get(intent["id"])
    assert result["status"] == "pending_retry"
    assert updated["status"] == "pending_retry"
    assert "backend unavailable" in updated["last_error"]


def test_retryable_exception_classifier():
    request = httpx.Request("POST", "https://backend.test/api")
    retryable_response = httpx.Response(503, request=request)
    blocked_response = httpx.Response(400, request=request)

    assert coworking.is_retryable_coworking_exception(
        httpx.HTTPStatusError("unavailable", request=request, response=retryable_response)
    )
    assert not coworking.is_retryable_coworking_exception(
        httpx.HTTPStatusError("bad request", request=request, response=blocked_response)
    )
