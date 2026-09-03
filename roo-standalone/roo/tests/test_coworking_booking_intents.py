import asyncio
import hashlib
import importlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest


coworking = importlib.import_module("roo.coworking_booking_intents")
backend_module = importlib.import_module("roo.clients.mlai_backend")
schema_module = importlib.import_module("roo.coworking_booking_schema_v3")
v2_schema_module = importlib.import_module("roo.coworking_booking_schema_v2")
reconciliation_module = importlib.import_module(
    "roo.coworking_notification_reconciliation"
)


def intent_store(db_path):
    schema_module.migrate_coworking_booking_intents_v3(db_path)
    return coworking.CoworkingBookingIntentStore(db_path)


def booking_result(
    *,
    cost=8,
    discount_applied=False,
    explicitly_linked=False,
    already_booked=False,
):
    return {
        "id": "00000000-0000-4000-8000-000000000001",
        "date": "2026-04-22",
        "status": "booked",
        "points_cost": cost,
        "standard_points_cost": 8,
        "monthly_update_discount_applied": discount_applied,
        "founder_tools_account_linked": explicitly_linked,
        "founder_tools_connection_type": (
            "explicit" if explicitly_linked else None
        ),
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
        self.operation_ids = []
        self.balance_calls = []

    async def book_coworking(
        self, slack_user_id, booking_date, channel_id, *, operation_id=None
    ):
        self.book_calls.append((slack_user_id, booking_date, channel_id))
        self.operation_ids.append(operation_id)
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
    store = intent_store(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id=slack_user_id,
        requested_by_slack_id=requested_by_slack_id,
        booking_date="2026-04-22",
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    return store, intent, store.reserve_for_processing(intent["id"], owner="test-worker")


def capture_delivery(
    monkeypatch,
    *,
    dm_response=None,
    dm_error=None,
    post_error=None,
):
    direct_messages = []
    channel_messages = []
    ephemeral_messages = []

    def send_dm(user_id, text, **kwargs):
        direct_messages.append({"user_id": user_id, "text": text, **kwargs})
        if dm_error is not None:
            raise dm_error
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
    store = intent_store(tmp_path / "intents.db")

    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert intent["status"] == "pending"
    assert intent["idempotency_key"] == "coworking:U123:2026-04-22"

    leased = store.reserve_for_processing(intent["id"], owner="test-worker")
    assert leased["status"] == "processing"
    assert leased["attempt_count"] == 1

    retry = store.mark_retryable_failure(
        intent["id"],
        owner=leased["locked_by"],
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
        owner=claimed[0]["locked_by"],
        backend_result={"id": "booking-1", "date": "2026-04-22"},
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["backend_booking_id"] == "booking-1"
    assert store.counts_by_status() == {"confirmed": 1}


def test_new_user_action_after_terminal_intent_gets_new_operation(tmp_path):
    store = intent_store(tmp_path / "intents.db")
    first = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts=None,
    )
    leased = store.reserve_for_processing(first["id"], owner="worker")
    store.mark_confirmed(
        first["id"],
        owner=leased["locked_by"],
        backend_result=booking_result(),
        notification_required=False,
    )

    second = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts=None,
    )

    assert second["id"] != first["id"]
    assert coworking.build_coworking_operation_id(second["idempotency_key"]) != (
        coworking.build_coworking_operation_id(first["idempotency_key"])
    )


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
    UUID(client.operation_ids[0])
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
    assert channel_messages[0]["client_msg_id"]
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
    store, intent, leased = leased_intent(tmp_path)
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
    monkeypatch.setattr(coworking, "retry_delay_seconds", lambda _attempt: 0)

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["status"] == "confirmed"
    assert result["notification_status"] == "pending_retry"
    stored = store.get(intent["id"])
    assert stored["status"] == "confirmed"
    assert stored["notification_status"] == "pending_retry"
    assert stored["notification_delivered_at"] is None
    assert len(direct_messages) == 1
    assert len(channel_messages) == 1
    assert "`@Roo link`" not in channel_messages[0]["text"]
    assert len(ephemeral_messages) == 1
    assert "private points details" in ephemeral_messages[0]["text"]

    # Simulate the worker restarting after Slack recovers. Only the durable
    # notification is retried; the backend booking is not called again.
    due = store.claim_due_notifications(owner="restarted-worker")
    assert len(due) == 1
    retry_dms, retry_channel_messages, _retry_ephemeral = capture_delivery(monkeypatch)

    recovered = await coworking.deliver_coworking_booking_notification(
        due[0],
        store=store,
        client=client,
    )

    assert recovered["notification_status"] == "delivered"
    assert store.get(intent["id"])["notification_status"] == "delivered"
    assert len(retry_dms) == 1
    assert "`@Roo link`" in retry_dms[0]["text"]
    assert direct_messages[0]["client_msg_id"] == retry_dms[0]["client_msg_id"]
    assert retry_dms[0]["raise_on_error"] is True
    assert retry_channel_messages == []
    assert client.book_calls == [("U123", "2026-04-22", "C123")]


@pytest.mark.asyncio
async def test_terminal_rejection_notification_survives_slack_failure_and_restart(
    tmp_path,
    monkeypatch,
):
    store, intent, leased = leased_intent(tmp_path)
    request = httpx.Request("POST", "https://backend.test/coworking")
    response = httpx.Response(400, request=request)
    client = FakeClient(
        error=httpx.HTTPStatusError(
            "sensitive backend rejection UPRIVATE private@example.com",
            request=request,
            response=response,
        )
    )
    attempts = []

    def post_message(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("Slack unavailable")
        return {"ok": True, "ts": "444.555"}

    slack_client_module = importlib.import_module("roo.slack_client")
    monkeypatch.setattr(slack_client_module, "post_message", post_message)
    monkeypatch.setattr(coworking, "retry_delay_seconds", lambda _attempt: 0)

    first = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    stored = store.get(intent["id"])
    assert first["status"] == "blocked"
    assert first["notification_status"] == "pending_retry"
    assert stored["status"] == "blocked"
    assert stored["notification_status"] == "pending_retry"
    assert stored["last_error"] == "backend_http_400"
    assert client.book_calls == [("U123", "2026-04-22", "C123")]

    due = store.claim_due_notifications(owner="restarted-worker")
    assert len(due) == 1
    recovered = await coworking.deliver_coworking_booking_notification(
        due[0],
        store=store,
        client=client,
    )

    assert recovered["status"] == "blocked"
    assert recovered["notification_status"] == "delivered"
    assert store.get(intent["id"])["notification_status"] == "delivered"
    assert client.book_calls == [("U123", "2026-04-22", "C123")]
    assert len(attempts) == 2
    assert attempts[0]["client_msg_id"] == attempts[1]["client_msg_id"]
    assert "No new booking was created" in attempts[1]["text"]
    assert "backend_http_400" not in attempts[1]["text"]
    assert "UPRIVATE" not in attempts[1]["text"]
    assert "private@example.com" not in attempts[1]["text"]


@pytest.mark.asyncio
async def test_duplicate_slack_confirmation_counts_as_durable_delivery(
    tmp_path,
    monkeypatch,
):
    class DuplicateMessageError(Exception):
        response = {"error": "duplicate_message"}

    store, intent, leased = leased_intent(tmp_path)
    client = FakeClient(result=booking_result())
    direct_messages, channel_messages, _ephemeral = capture_delivery(
        monkeypatch,
        dm_error=DuplicateMessageError("already accepted"),
    )

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    assert result["notification_status"] == "delivered"
    assert store.get(intent["id"])["notification_status"] == "delivered"
    assert len(direct_messages) == 1
    assert direct_messages[0]["client_msg_id"]
    assert direct_messages[0]["raise_on_error"] is True
    assert len(channel_messages) == 1


def test_stale_notification_worker_cannot_reopen_delivered_work(tmp_path):
    store, intent, leased = leased_intent(tmp_path)
    confirmed = store.mark_confirmed(
        intent["id"],
        owner=leased["locked_by"],
        backend_result=booking_result(),
        notification_required=True,
    )
    first = store.reserve_notification(
        confirmed["id"],
        owner="first-worker",
        lease_seconds=0,
    )
    assert first["notification_locked_by"] == "first-worker"

    second = store.reserve_notification(
        confirmed["id"],
        owner="replacement-worker",
    )
    assert second["notification_locked_by"] == "replacement-worker"
    store.mark_notification_delivered(
        confirmed["id"],
        owner="replacement-worker",
    )

    stale_result = store.mark_notification_retryable_failure(
        confirmed["id"],
        owner="first-worker",
        error="late failure",
        delay_seconds=0,
    )

    assert stale_result is None
    stored = store.get(confirmed["id"])
    assert stored["notification_status"] == "delivered"
    assert stored["notification_last_error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_outcome", ["success", "failure"])
async def test_stale_notification_outcome_cannot_change_winner(
    tmp_path,
    stale_outcome,
):
    store, intent, leased = leased_intent(tmp_path)
    confirmed = store.mark_confirmed(
        intent["id"],
        owner=leased["locked_by"],
        backend_result=booking_result(),
        notification_required=True,
    )
    stale = store.reserve_notification(
        confirmed["id"],
        owner="stale-notification-worker",
        lease_seconds=0,
    )

    class RacingExecutor:
        async def _deliver_coworking_booking_success(self, **kwargs):
            replacement = store.reserve_notification(
                confirmed["id"],
                owner="replacement-notification-worker",
            )
            delivered = store.mark_notification_delivered(
                confirmed["id"],
                owner=replacement["notification_locked_by"],
            )
            assert delivered is not None
            if stale_outcome == "failure":
                raise RuntimeError("late Slack failure")
            return {
                "message": "",
                "suppress_post": True,
                "data": {"delivery": "private_dm", "dm_delivered": True},
            }

    result = await coworking.deliver_coworking_booking_notification(
        stale,
        store=store,
        client=FakeClient(),
        executor=RacingExecutor(),
    )

    stored = store.get(confirmed["id"])
    assert result == {
        "status": "stale",
        "notification_status": "stale",
        "intent_id": confirmed["id"],
    }
    assert stored["notification_status"] == "delivered"
    assert stored["notification_last_error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_outcome", ["success", "failure"])
async def test_stale_blocked_notification_outcome_cannot_change_winner(
    tmp_path,
    monkeypatch,
    stale_outcome,
):
    store, intent, leased = leased_intent(tmp_path)
    blocked = store.mark_blocked(
        intent["id"],
        owner=leased["locked_by"],
        error="backend_http_400",
        notification_required=True,
    )
    stale = store.reserve_notification(
        blocked["id"],
        owner="stale-notification-worker",
        lease_seconds=0,
    )
    attempted_message_ids = []

    def race_delivery(**kwargs):
        attempted_message_ids.append(kwargs["client_msg_id"])
        replacement = store.reserve_notification(
            blocked["id"],
            owner="replacement-notification-worker",
        )
        delivered = store.mark_notification_delivered(
            blocked["id"],
            owner=replacement["notification_locked_by"],
        )
        assert delivered is not None
        if stale_outcome == "failure":
            raise RuntimeError("late Slack failure")
        return True

    monkeypatch.setattr(coworking, "_safe_post_message", race_delivery)

    result = await coworking.deliver_coworking_booking_notification(
        stale,
        store=store,
        client=FakeClient(),
    )

    stored = store.get(blocked["id"])
    assert result == {
        "status": "stale",
        "notification_status": "stale",
        "intent_id": blocked["id"],
    }
    assert stored["status"] == "blocked"
    assert stored["notification_status"] == "delivered"
    assert stored["notification_last_error"] is None
    assert len(attempted_message_ids) == 1
    assert attempted_message_ids[0]


@pytest.mark.parametrize("stale_transition", ["confirm", "retry", "block"])
def test_stale_mutation_worker_cannot_overwrite_replacement_confirmation(
    tmp_path,
    stale_transition,
):
    store = intent_store(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
    )
    stale = store.reserve_for_processing(
        intent["id"],
        owner="stale-worker",
        lease_seconds=0,
    )
    replacement = store.reserve_for_processing(
        intent["id"],
        owner="replacement-worker",
    )
    replacement_result = booking_result(cost=4, discount_applied=True)
    confirmed = store.mark_confirmed(
        intent["id"],
        owner=replacement["locked_by"],
        backend_result=replacement_result,
        notification_required=True,
    )
    assert confirmed["status"] == "confirmed"

    if stale_transition == "confirm":
        stale_result = store.mark_confirmed(
            intent["id"],
            owner=stale["locked_by"],
            backend_result=booking_result(cost=8),
            notification_required=True,
        )
    elif stale_transition == "retry":
        stale_result = store.mark_retryable_failure(
            intent["id"],
            owner=stale["locked_by"],
            error="late timeout",
            delay_seconds=0,
        )
    else:
        stale_result = store.mark_blocked(
            intent["id"],
            owner=stale["locked_by"],
            error="late rejection",
        )

    stored = store.get(intent["id"])
    assert stale_result is None
    assert stored["status"] == "confirmed"
    assert stored["notification_status"] == "pending"
    assert stored["last_error"] is None
    assert stored["backend_result_json"] == confirmed["backend_result_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_outcome", ["success", "retry", "block"])
async def test_stale_mutation_outcome_emits_no_message_and_keeps_winner(
    tmp_path,
    monkeypatch,
    stale_outcome,
):
    store = intent_store(tmp_path / "intents.db")
    intent = store.record_intent(
        slack_user_id="U123",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts="111.222",
    )
    stale = store.reserve_for_processing(
        intent["id"],
        owner="stale-worker",
        lease_seconds=0,
    )
    winner_result = booking_result(cost=4, discount_applied=True)

    class RacingClient(FakeClient):
        async def book_coworking(
            self, slack_user_id, booking_date, channel_id, *, operation_id=None
        ):
            replacement = store.reserve_for_processing(
                intent["id"],
                owner="replacement-worker",
            )
            store.mark_confirmed(
                intent["id"],
                owner=replacement["locked_by"],
                backend_result=winner_result,
                notification_required=True,
            )
            if stale_outcome == "retry":
                raise backend_module.MLAIBackendUnavailableError("late timeout")
            if stale_outcome == "block":
                request = httpx.Request("POST", "https://backend.test/coworking")
                response = httpx.Response(400, request=request)
                raise httpx.HTTPStatusError(
                    "late rejection",
                    request=request,
                    response=response,
                )
            return booking_result(cost=8)

    direct_messages, channel_messages, ephemeral_messages = capture_delivery(
        monkeypatch
    )
    result = await coworking.process_coworking_booking_intent(
        stale,
        store=store,
        client=RacingClient(),
        notify=True,
    )

    stored = store.get(intent["id"])
    assert result == {"status": "stale", "intent_id": intent["id"]}
    assert stored["status"] == "confirmed"
    assert stored["notification_status"] == "pending"
    assert stored["backend_result_json"]
    assert direct_messages == []
    assert channel_messages == []
    assert ephemeral_messages == []


def test_schema_scrubs_legacy_raw_request_text(tmp_path):
    db_path = tmp_path / "intents.db"
    v2_schema_module.migrate_coworking_booking_intents_v2(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO coworking_booking_intents (
                idempotency_key, slack_user_id, requested_by_slack_id,
                booking_date, channel_id, thread_ts, request_text, status,
                next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-raw-text",
                "U123",
                "U123",
                "2026-04-22",
                "C123",
                "111.222",
                "private raw Slack message",
                "pending",
                0.0,
                1.0,
                1.0,
            ),
        )
    schema_module.migrate_coworking_booking_intents_v3(db_path)

    assert coworking.CoworkingBookingIntentStore(db_path).get_by_key(
        "legacy-raw-text"
    )["request_text"] is None


def test_store_validation_fails_closed_without_explicit_migration(tmp_path):
    store = coworking.CoworkingBookingIntentStore(tmp_path / "uninitialized.db")

    with pytest.raises(RuntimeError, match="migrate_coworking_booking_intents_v3"):
        store.validate_schema()


def test_store_validation_rejects_shared_v2_predecessor(tmp_path):
    db_path = tmp_path / "v2-only.db"
    v2_schema_module.migrate_coworking_booking_intents_v2(db_path)

    with pytest.raises(RuntimeError, match=r"schema v3.*version=2"):
        coworking.CoworkingBookingIntentStore(db_path).validate_schema()


def test_explicit_schema_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "intents.db"

    schema_module.migrate_coworking_booking_intents_v3(db_path)
    schema_module.migrate_coworking_booking_intents_v3(db_path)

    coworking.CoworkingBookingIntentStore(db_path).validate_schema()


@pytest.mark.parametrize("legacy_status", ["confirmed", "blocked"])
def test_v1_terminal_rows_are_quarantined_when_delivery_is_unknown(
    tmp_path,
    legacy_status,
):
    db_path = tmp_path / "legacy-intents.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE coworking_booking_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                slack_user_id TEXT NOT NULL,
                requested_by_slack_id TEXT,
                booking_date TEXT NOT NULL,
                channel_id TEXT,
                thread_ts TEXT,
                request_text TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                locked_until REAL,
                locked_by TEXT,
                last_error TEXT,
                backend_booking_id TEXT,
                backend_result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                confirmed_at REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO coworking_booking_intents (
                idempotency_key, slack_user_id, requested_by_slack_id,
                booking_date, channel_id, thread_ts, request_text, status,
                next_attempt_at, last_error, backend_result_json,
                created_at, updated_at, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"legacy-{legacy_status}",
                "U123",
                "U123",
                "2026-04-22",
                "C123",
                "111.222",
                "private raw Slack message",
                legacy_status,
                0.0,
                "backend_http_400" if legacy_status == "blocked" else None,
                '{"id":"booking-1","date":"2026-04-22"}',
                1.0,
                2.0,
                2.0 if legacy_status == "confirmed" else None,
            ),
        )

    schema_module.migrate_coworking_booking_intents_v3(db_path)
    migrated = coworking.CoworkingBookingIntentStore(db_path).get_by_key(
        f"legacy-{legacy_status}"
    )

    assert migrated["request_text"] is None
    assert migrated["notification_status"] == "reconciliation_required"
    assert migrated["notification_last_error"] == "legacy_delivery_unknown"
    assert migrated["notification_next_attempt_at"] is None
    assert migrated["notification_delivered_at"] is None

    # A repeated migration must not turn the quarantine into an automatic
    # delivery or mark it safe for retention cleanup.
    schema_module.migrate_coworking_booking_intents_v3(db_path)
    assert coworking.CoworkingBookingIntentStore(db_path).get_by_key(
        f"legacy-{legacy_status}"
    )["notification_status"] == "reconciliation_required"


def test_store_schema_validation_is_read_only(tmp_path):
    db_path = tmp_path / "intents.db"
    schema_module.migrate_coworking_booking_intents_v3(db_path)
    before = hashlib.sha256(db_path.read_bytes()).digest()

    coworking.CoworkingBookingIntentStore(db_path).validate_schema()

    assert hashlib.sha256(db_path.read_bytes()).digest() == before


def test_v3_quarantines_indistinguishable_shared_v2_terminal_histories(tmp_path):
    db_path = tmp_path / "shared-v2.db"
    v2_schema_module.migrate_coworking_booking_intents_v2(db_path)
    with sqlite3.connect(db_path) as conn:
        for idempotency_key in (
            "v2-delivery-unknown",
            "v2-intentionally-not-required",
        ):
            conn.execute(
                """
                INSERT INTO coworking_booking_intents (
                    idempotency_key, slack_user_id, requested_by_slack_id,
                    booking_date, channel_id, thread_ts, status,
                    next_attempt_at, backend_result_json,
                    created_at, updated_at, confirmed_at,
                    notification_status, notification_delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    "U123",
                    "U123",
                    "2026-04-22",
                    "C123",
                    "111.222",
                    "confirmed",
                    0.0,
                    '{"id":"booking-1"}',
                    1.0,
                    2.0,
                    2.0,
                    "not_required",
                    None,
                ),
            )
        conn.execute(
            """
            INSERT INTO coworking_booking_intents (
                idempotency_key, slack_user_id, booking_date, status,
                next_attempt_at, created_at, updated_at, confirmed_at,
                notification_status, notification_delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "v2-proven-delivered",
                "U123",
                "2026-04-23",
                "confirmed",
                0.0,
                1.0,
                2.0,
                2.0,
                "delivered",
                2.0,
            ),
        )

    assert schema_module.migrate_coworking_booking_intents_v3(db_path) == 2

    store = coworking.CoworkingBookingIntentStore(db_path)
    for idempotency_key in (
        "v2-delivery-unknown",
        "v2-intentionally-not-required",
    ):
        migrated = store.get_by_key(idempotency_key)
        assert migrated["notification_status"] == "reconciliation_required"
        assert migrated["notification_last_error"] == "v2_delivery_provenance_unknown"
        assert migrated["notification_reconciliation_reference"] is None
    delivered = store.get_by_key("v2-proven-delivered")
    assert delivered["notification_status"] == "delivered"
    assert delivered["notification_delivered_at"] == 2.0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expects_delivery_time"),
    [
        ("delivered", "delivered", True),
        ("not_required", "not_required", False),
        ("retry", "pending", False),
    ],
)
def test_v3_quarantine_has_fenced_audited_recovery(
    tmp_path,
    outcome,
    expected_status,
    expects_delivery_time,
):
    db_path = tmp_path / f"reconcile-{outcome}.db"
    v2_schema_module.migrate_coworking_booking_intents_v2(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO coworking_booking_intents (
                idempotency_key, slack_user_id, booking_date, status,
                next_attempt_at, created_at, updated_at, confirmed_at,
                notification_status, backend_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"reconcile-{outcome}",
                "U123",
                "2026-04-22",
                "confirmed",
                0.0,
                1.0,
                2.0,
                2.0,
                "not_required",
                json.dumps(
                    {
                        "id": "legacy-booking-1",
                        "date": "2026-04-22",
                        "status": "booked",
                        "points_cost": 8,
                    }
                ),
            ),
        )
        intent_id = cursor.lastrowid
    schema_module.migrate_coworking_booking_intents_v3(db_path)

    reconciled = reconciliation_module.reconcile_coworking_notification(
        db_path,
        intent_id=intent_id,
        outcome=outcome,
        operator_reference="INC-648-evidence-1",
        now=1000.0,
    )

    assert reconciled["notification_status"] == expected_status
    assert reconciled["notification_reconciled_at"] == 1000.0
    assert reconciled["notification_reconciliation_reference"] == "INC-648-evidence-1"
    assert reconciled["notification_reconciliation_outcome"] == outcome
    assert (reconciled["notification_delivered_at"] is not None) is expects_delivery_time
    assert (reconciled["notification_next_attempt_at"] is not None) is (
        outcome == "retry"
    )
    if outcome == "retry":
        normalized = json.loads(reconciled["backend_result_json"])
        UUID(normalized["id"])
        assert normalized["founder_tools_account_linked"] is False
    with pytest.raises(ValueError, match="does not require reconciliation"):
        reconciliation_module.reconcile_coworking_notification(
            db_path,
            intent_id=intent_id,
            outcome=outcome,
            operator_reference="INC-648-evidence-2",
            now=1001.0,
        )


def test_reconciliation_does_not_create_a_missing_database(tmp_path):
    db_path = tmp_path / "missing.db"

    with pytest.raises(ValueError, match="database was not found"):
        reconciliation_module.reconcile_coworking_notification(
            db_path,
            intent_id=1,
            outcome="delivered",
            operator_reference="INC-648-missing-db",
        )

    assert not db_path.exists()


def test_retry_reconciliation_fails_closed_without_booking_result(tmp_path):
    db_path = tmp_path / "missing-result.db"
    v2_schema_module.migrate_coworking_booking_intents_v2(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO coworking_booking_intents (
                idempotency_key, slack_user_id, booking_date, status,
                next_attempt_at, created_at, updated_at, confirmed_at,
                notification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "missing-result",
                "U123",
                "2026-04-22",
                "confirmed",
                0.0,
                1.0,
                2.0,
                2.0,
                "not_required",
            ),
        )
        intent_id = cursor.lastrowid
    schema_module.migrate_coworking_booking_intents_v3(db_path)

    with pytest.raises(ValueError, match="recorded booking result"):
        reconciliation_module.reconcile_coworking_notification(
            db_path,
            intent_id=intent_id,
            outcome="retry",
            operator_reference="INC-648-no-result",
        )

    assert coworking.CoworkingBookingIntentStore(db_path).get(intent_id)[
        "notification_status"
    ] == "reconciliation_required"


def test_terminal_retention_preserves_unfinished_notifications(tmp_path):
    store = intent_store(tmp_path / "intents.db")

    delivered = store.record_intent(
        slack_user_id="UDELIVER1",
        booking_date="2026-04-20",
        channel_id="C123",
        thread_ts=None,
    )
    delivered_lease = store.reserve_for_processing(delivered["id"], owner="worker-1")
    delivered_confirmation = store.mark_confirmed(
        delivered["id"],
        owner=delivered_lease["locked_by"],
        backend_result=booking_result(),
        notification_required=True,
    )
    notification = store.reserve_notification(
        delivered_confirmation["id"],
        owner="notification-worker",
    )
    store.mark_notification_delivered(
        delivered_confirmation["id"],
        owner=notification["notification_locked_by"],
    )

    blocked = store.record_intent(
        slack_user_id="UBLOCKED1",
        booking_date="2026-04-21",
        channel_id="C123",
        thread_ts=None,
    )
    blocked_lease = store.reserve_for_processing(blocked["id"], owner="worker-2")
    store.mark_blocked(
        blocked["id"],
        owner=blocked_lease["locked_by"],
        error="terminal rejection",
    )

    blocked_pending = store.record_intent(
        slack_user_id="UBLOCKED2",
        booking_date="2026-04-21",
        channel_id="C123",
        thread_ts=None,
    )
    blocked_pending_lease = store.reserve_for_processing(
        blocked_pending["id"],
        owner="worker-4",
    )
    store.mark_blocked(
        blocked_pending["id"],
        owner=blocked_pending_lease["locked_by"],
        error="terminal rejection",
        notification_required=True,
    )

    unfinished = store.record_intent(
        slack_user_id="UPENDING1",
        booking_date="2026-04-22",
        channel_id="C123",
        thread_ts=None,
    )
    unfinished_lease = store.reserve_for_processing(unfinished["id"], owner="worker-3")
    store.mark_confirmed(
        unfinished["id"],
        owner=unfinished_lease["locked_by"],
        backend_result=booking_result(),
        notification_required=True,
    )

    pending_mutation = store.record_intent(
        slack_user_id="UPENDING2",
        booking_date="2026-04-23",
        channel_id="C123",
        thread_ts=None,
    )

    batch_pending = store.record_batch_intent(
        admin_slack_user_id="UADMIN",
        target_slack_user_ids=["UBATCH1"],
        booking_date="2026-04-24",
        channel_id="C123",
        thread_ts=None,
    )
    batch_lease = store.reserve_batch_for_processing(
        batch_pending["id"], owner="batch-worker"
    )
    store.mark_batch_blocked(
        batch_pending["id"],
        owner=batch_lease["locked_by"],
        error="terminal rejection",
    )

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE coworking_booking_intents SET updated_at = 0")

    deleted = store.purge_terminal(retention_days=30)

    assert deleted == 2
    assert store.get(delivered["id"]) is None
    assert store.get(blocked["id"]) is None
    assert store.get(blocked_pending["id"])["notification_status"] == "pending"
    assert store.get(unfinished["id"])["notification_status"] == "pending"
    assert store.get(pending_mutation["id"])["status"] == "pending"
    assert store.get(batch_pending["id"])["notification_status"] == "pending"


@pytest.mark.asyncio
async def test_retry_loop_runs_bounded_retention_cleanup(monkeypatch):
    calls = []
    health = []

    class FakeStore:
        def purge_terminal(self, *, retention_days):
            calls.append(("purge", retention_days))
            return 0

        def claim_due(self, *, limit, owner):
            calls.append(("mutations", limit, bool(owner)))
            return []

        def claim_due_batches(self, *, limit, owner):
            calls.append(("batches", limit, bool(owner)))
            return []

        def claim_due_notifications(self, *, limit, owner):
            calls.append(("notifications", limit, bool(owner)))
            return []

    async def stop_after_first_iteration(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        coworking,
        "get_settings",
        lambda: SimpleNamespace(COWORKING_INTENT_RETENTION_DAYS=14),
    )
    monkeypatch.setattr(coworking.asyncio, "sleep", stop_after_first_iteration)

    with pytest.raises(asyncio.CancelledError):
        await coworking.coworking_booking_retry_loop(
            store=FakeStore(),
            poll_seconds=0,
            health_reporter=health.append,
        )

    assert calls == [
        ("purge", 14),
        ("mutations", 10, True),
        ("batches", 10, True),
        ("notifications", 10, True),
    ]
    assert health[-1]["status"] == "ok"
    assert health[-1]["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_retry_loop_failure_degrades_health_before_next_poll(monkeypatch):
    health = []

    class BrokenStore:
        def purge_terminal(self, *, retention_days):
            raise sqlite3.OperationalError("simulated storage failure")

    async def stop_after_failure(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(coworking.asyncio, "sleep", stop_after_failure)

    with pytest.raises(asyncio.CancelledError):
        await coworking.coworking_booking_retry_loop(
            store=BrokenStore(),
            poll_seconds=0,
            health_reporter=health.append,
        )

    assert len(health) == 1
    assert health[0]["status"] == "degraded"
    assert health[0]["consecutive_failures"] == 1
    assert health[0]["last_error_type"] == "OperationalError"
    assert isinstance(health[0]["last_failure_at"], float)


@pytest.mark.asyncio
async def test_admin_retry_notifies_target_privately_without_shared_channel_details(
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
    assert channel_messages == []


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
    assert channel_messages[0]["client_msg_id"]
    assert "`@Roo link`" in channel_messages[0]["text"]


@pytest.mark.asyncio
async def test_public_ack_failure_does_not_repeat_a_delivered_private_confirmation(
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
    assert result["notification_status"] == "delivered"
    stored = store.get(intent["id"])
    assert stored["status"] == "confirmed"
    assert stored["notification_status"] == "delivered"
    assert store.claim_due_notifications(owner="retry-worker") == []
    assert client.book_calls == [("U123", "2026-04-22", "C123")]


@pytest.mark.asyncio
async def test_process_intent_keeps_retryable_backend_timeout_queued(tmp_path, capsys):
    store, intent, leased = leased_intent(tmp_path)
    sensitive_error = "offline UPRIVATE1 private@example.com\nforged"
    client = FakeClient(
        error=backend_module.MLAIBackendUnavailableError(sensitive_error)
    )

    result = await coworking.process_coworking_booking_intent(
        leased,
        store=store,
        client=client,
        notify=True,
    )

    updated = store.get(intent["id"])
    assert result["status"] == "pending_retry"
    assert updated["status"] == "pending_retry"
    assert updated["last_error"] == "backend_unavailable"
    assert result["error"] == "backend_unavailable"
    output = capsys.readouterr().out
    assert "UPRIVATE1" not in output
    assert "private@example.com" not in output
    assert "offline" not in output
    assert "forged" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_result", [[], {}, {"points_cost": 8}])
async def test_process_intent_treats_malformed_success_as_commit_uncertain(
    tmp_path,
    malformed_result,
):
    store, intent, leased = leased_intent(tmp_path)

    class MalformedClient(FakeClient):
        async def book_coworking(
            self, slack_user_id, booking_date, channel_id, *, operation_id=None
        ):
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
    assert updated["last_error"] == "invalid_backend_response"


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
