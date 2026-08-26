import importlib

import httpx
import pytest


coworking = importlib.import_module("roo.coworking_booking_intents")
backend_module = importlib.import_module("roo.clients.mlai_backend")


def booking_result(
    *,
    cost=8,
    discount_applied=False,
    explicitly_linked=False,
    already_booked=False,
):
    return {
        "id": "booking-1",
        "date": "2026-04-22",
        "status": "booked",
        "points_cost": cost,
        "standard_points_cost": 8,
        "monthly_update_discount_applied": discount_applied,
        "founder_tools_account_linked": explicitly_linked,
        "founder_tools_explicitly_linked": explicitly_linked,
        "already_booked": already_booked,
        "idempotent": already_booked,
    }


class FakeClient:
    def __init__(self, *, result=None, error=None, balance=16):
        self.result = result or booking_result()
        self.error = error
        self.balance = balance
        self.book_calls = []
        self.balance_calls = []

    async def book_coworking(self, slack_user_id, booking_date, channel_id):
        self.book_calls.append((slack_user_id, booking_date, channel_id))
        if self.error is not None:
            raise self.error
        return dict(self.result)

    async def get_balance(self, slack_user_id):
        self.balance_calls.append(slack_user_id)
        return {"balance": self.balance}


def leased_intent(
    tmp_path,
    *,
    slack_user_id="U123",
    requested_by_slack_id=None,
    channel_id="C123",
    thread_ts="111.222",
):
    store = coworking.CoworkingBookingIntentStore(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id=slack_user_id,
        requested_by_slack_id=requested_by_slack_id,
        booking_date="2026-04-22",
        channel_id=channel_id,
        thread_ts=thread_ts,
        request_text="book me in today",
    )
    return store, intent, store.reserve_for_processing(intent["id"], owner="test-worker")


def capture_delivery(monkeypatch, *, dm_response=None, post_error=None):
    direct_messages = []
    channel_messages = []
    ephemeral_messages = []

    def send_dm(user_id, text, **kwargs):
        direct_messages.append({"user_id": user_id, "text": text, **kwargs})
        if dm_response is None:
            return {"ok": True, "channel": "D123", "ts": "333.444"}
        return dm_response

    def post_message(**kwargs):
        if post_error is not None:
            raise post_error
        channel_messages.append(kwargs)
        return {"ok": True, "ts": "444.555"}

    def post_ephemeral(**kwargs):
        ephemeral_messages.append(kwargs)
        return {"ok": True, "message_ts": "555.666"}

    # Legacy suites reload executor while installing import-time stubs. Always
    # patch the live modules used by the retry worker, not collection-time refs.
    executor_module = importlib.import_module("roo.skills.executor")
    slack_client_module = importlib.import_module("roo.slack_client")
    monkeypatch.setattr(executor_module, "send_dm", send_dm)
    monkeypatch.setattr(executor_module, "post_ephemeral", post_ephemeral)
    monkeypatch.setattr(slack_client_module, "post_message", post_message)
    return direct_messages, channel_messages, ephemeral_messages


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
async def test_existing_booking_replay_uses_complete_result_and_private_link_guidance(
    tmp_path,
    monkeypatch,
):
    store, intent, leased = leased_intent(tmp_path)
    client = FakeClient(
        result=booking_result(
            cost=8,
            discount_applied=False,
            explicitly_linked=False,
            already_booked=True,
        ),
        balance=12,
    )
    direct_messages, channel_messages, _ephemeral = capture_delivery(monkeypatch)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert result["backend_result"]["already_booked"] is True
    assert client.book_calls == [("U123", "2026-04-22", "C123")]
    assert client.balance_calls == ["U123"]
    stored = store.get(intent["id"])
    assert stored["status"] == "confirmed"
    assert stored["backend_result_json"]
    assert len(direct_messages) == 1
    assert "Cost: 8 points" in direct_messages[0]["text"]
    assert "Balance remaining: 12 points" in direct_messages[0]["text"]
    assert "`@Roo link`" in direct_messages[0]["text"]
    assert len(channel_messages) == 1
    assert channel_messages[0]["thread_ts"] == "111.222"
    assert "`@Roo link`" not in channel_messages[0]["text"]


@pytest.mark.asyncio
async def test_discounted_retry_omits_monthly_update_and_link_guidance(
    tmp_path,
    monkeypatch,
):
    store, _intent, leased = leased_intent(tmp_path)
    client = FakeClient(
        result=booking_result(
            cost=4,
            discount_applied=True,
            explicitly_linked=False,
        )
    )
    direct_messages, _channel_messages, _ephemeral = capture_delivery(monkeypatch)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert "Cost: 4 points" in direct_messages[0]["text"]
    assert "monthly update" not in direct_messages[0]["text"]
    assert "`@Roo link`" not in direct_messages[0]["text"]


@pytest.mark.asyncio
async def test_explicitly_linked_nonqualifying_retry_does_not_offer_relink(
    tmp_path,
    monkeypatch,
):
    store, _intent, leased = leased_intent(tmp_path)
    client = FakeClient(
        result=booking_result(
            cost=8,
            discount_applied=False,
            explicitly_linked=True,
        )
    )
    direct_messages, _channel_messages, _ephemeral = capture_delivery(monkeypatch)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert "monthly update" in direct_messages[0]["text"]
    assert "`@Roo link`" not in direct_messages[0]["text"]


@pytest.mark.asyncio
async def test_retry_dm_failure_never_exposes_link_guidance_publicly(
    tmp_path,
    monkeypatch,
):
    store, _intent, leased = leased_intent(tmp_path)
    client = FakeClient(
        result=booking_result(
            cost=8,
            discount_applied=False,
            explicitly_linked=False,
        )
    )
    direct_messages, channel_messages, ephemeral_messages = capture_delivery(
        monkeypatch,
        dm_response={},
    )

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert result["delivery"]["data"]["dm_delivered"] is False
    assert len(direct_messages) == 1
    assert len(channel_messages) == 1
    assert "`@Roo link`" not in channel_messages[0]["text"]
    assert len(ephemeral_messages) == 1
    assert "private points details" in ephemeral_messages[0]["text"]


@pytest.mark.asyncio
async def test_admin_retry_notifies_target_privately_and_admin_in_original_thread(
    tmp_path,
    monkeypatch,
):
    store, _intent, leased = leased_intent(
        tmp_path,
        slack_user_id="UTARGET",
        requested_by_slack_id="UADMIN",
    )
    client = FakeClient(result=booking_result(cost=8, explicitly_linked=False))
    direct_messages, channel_messages, _ephemeral = capture_delivery(monkeypatch)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert direct_messages[0]["user_id"] == "UTARGET"
    assert "`@Roo link`" in direct_messages[0]["text"]
    assert "Checked <@UTARGET> in" in channel_messages[0]["text"]
    assert "Balance remaining" not in channel_messages[0]["text"]
    assert "`@Roo link`" not in channel_messages[0]["text"]


@pytest.mark.asyncio
async def test_retry_inside_roo_dm_posts_private_result_to_current_dm(
    tmp_path,
    monkeypatch,
):
    store, _intent, leased = leased_intent(
        tmp_path,
        channel_id="D123",
        thread_ts=None,
    )
    client = FakeClient(result=booking_result(cost=8, explicitly_linked=False))
    direct_messages, channel_messages, _ephemeral = capture_delivery(monkeypatch)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert direct_messages == []
    assert len(channel_messages) == 1
    assert channel_messages[0]["channel"] == "D123"
    assert channel_messages[0]["thread_ts"] is None
    assert "`@Roo link`" in channel_messages[0]["text"]


@pytest.mark.asyncio
async def test_notification_failure_does_not_reclassify_confirmed_booking(
    tmp_path,
    monkeypatch,
):
    store, intent, leased = leased_intent(tmp_path)
    client = FakeClient(result=booking_result())
    capture_delivery(
        monkeypatch,
        post_error=RuntimeError("Slack unavailable"),
    )

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert result["delivery_error"] == "RuntimeError"
    assert store.get(intent["id"])["status"] == "confirmed"
    assert client.book_calls == [("U123", "2026-04-22", "C123")]


@pytest.mark.asyncio
async def test_process_intent_keeps_retryable_backend_timeout_queued(tmp_path):
    store, intent, leased = leased_intent(tmp_path)
    client = FakeClient(error=backend_module.MLAIBackendUnavailableError("offline"))

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    updated = store.get(intent["id"])
    assert result["status"] == "pending_retry"
    assert updated["status"] == "pending_retry"
    assert "offline" in updated["last_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_result", [[], {}, {"points_cost": 8}])
async def test_process_intent_treats_malformed_success_as_commit_uncertain(
    tmp_path,
    malformed_result,
):
    store, intent, leased = leased_intent(tmp_path)

    class MalformedClient(FakeClient):
        async def book_coworking(self, slack_user_id, booking_date, channel_id):
            self.book_calls.append((slack_user_id, booking_date, channel_id))
            return malformed_result

    client = MalformedClient()

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=False,
    )

    updated = store.get(intent["id"])
    assert result["status"] == "pending_retry"
    assert updated["status"] == "pending_retry"
    assert "incomplete coworking booking result" in updated["last_error"]


def test_retryable_exception_classifier():
    request = httpx.Request("POST", "https://backend.test/api")
    retryable_response = httpx.Response(503, request=request)
    blocked_response = httpx.Response(400, request=request)

    assert coworking.is_retryable_coworking_exception(
        httpx.HTTPStatusError(
            "unavailable",
            request=request,
            response=retryable_response,
        )
    )
    assert not coworking.is_retryable_coworking_exception(
        httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=blocked_response,
        )
    )
