import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

executor_module = importlib.import_module("roo.skills.executor")
slack_client_module = importlib.import_module("roo.slack_client")
coworking_module = importlib.import_module("roo.coworking_booking_intents")
schema_module = importlib.import_module("roo.coworking_booking_schema_v3")
SkillExecutor = executor_module.SkillExecutor


def _batch_row(slack_user_id, booking_id, *, created=True):
    return {
        "slack_user_id": slack_user_id, "created": created,
        "already_booked": not created,
        "booking": {"id": booking_id, "date": "2026-07-04", "status": "booked", "points_cost": 8},
        "points_cost": 8, "standard_points_cost": 8,
        "monthly_update_discount_applied": False,
        "founder_tools_connection_type": None,
        "founder_tools_account_linked": False,
        "founder_tools_explicitly_linked": False,
    }


def _batch_result(*, first_created=True, second_created=True):
    created_count = int(first_created) + int(second_created)
    return {
        "date": "2026-07-04", "admin_slack_user_id": "UADMIN", "target_count": 2,
        "created_count": created_count, "already_booked_count": 2 - created_count,
        "standard_points_cost": 8,
        "results": [
            _batch_row(
                "U1", "00000000-0000-4000-8000-000000000001",
                created=first_created,
            ),
            _batch_row(
                "U2", "00000000-0000-4000-8000-000000000002",
                created=second_created,
            ),
        ],
    }


class FakeCoworkingClient:
    def __init__(self, *, admin_details=None, batch_result=None, batch_exc=None):
        self.admin_details = admin_details
        self.batch_result = batch_result or _batch_result()
        self.batch_exc = batch_exc
        self.admin_lookup_calls = []
        self.batch_calls = []
        self.single_calls = []

    async def get_admin_details(self, slack_user_id):
        self.admin_lookup_calls.append(slack_user_id)
        return self.admin_details

    async def book_coworking_many(
        self,
        *,
        admin_slack_user_id,
        target_slack_user_ids,
        booking_date,
        slack_channel_id=None,
        operation_id=None,
    ):
        self.batch_calls.append(
            {
                "admin_slack_user_id": admin_slack_user_id,
                "target_slack_user_ids": list(target_slack_user_ids),
                "booking_date": booking_date,
                "slack_channel_id": slack_channel_id,
                "operation_id": operation_id,
            }
        )
        if self.batch_exc:
            raise self.batch_exc
        return self.batch_result

    async def book_coworking(self, *args, **kwargs):
        self.single_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("single booking should not be called in this test")

    async def get_balance(self, slack_user_id):
        return {"balance": 12}


def _batch_http_error(payload, status_code=400):
    request = httpx.Request(
        "POST",
        "https://backend.test/api/v1/points/coworking/book-many/",
    )
    response = httpx.Response(status_code, request=request, json=payload)
    return httpx.HTTPStatusError("backend rejected batch", request=request, response=response)


async def _run_points_action(client, *, action, text, params=None, user_id="UADMIN"):
    executor = SkillExecutor()
    return await executor._handle_points_action(
        client=client,
        action=action,
        params=params or {"date": "2026-07-04"},
        text=text,
        user_id=user_id,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )


@pytest.fixture(autouse=True)
def batch_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")
    db_path = tmp_path / "coworking.db"
    schema_module.migrate_coworking_booking_intents_v3(db_path)
    store = coworking_module.CoworkingBookingIntentStore(db_path)
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    direct_messages = []
    def send_dm(user_id, text, **kwargs):
        direct_messages.append({"user": user_id, "text": text, **kwargs})
        return {"ok": True}
    monkeypatch.setattr(executor_module, "send_dm", send_dm)
    monkeypatch.setattr(
        coworking_module,
        "_safe_send_dm",
        lambda user_id, text, client_msg_id: direct_messages.append(
            {
                "user": user_id,
                "text": text,
                "client_msg_id": client_msg_id,
            }
        )
        or True,
    )
    return {"store": store, "db_path": db_path, "direct_messages": direct_messages}


@pytest.mark.asyncio
async def test_admin_checkin_coworking_batches_deduped_targets(batch_runtime):
    client = FakeCoworkingClient(
        admin_details={"role": "admin"},
        batch_result=_batch_result(second_created=False),
    )

    result = await _run_points_action(
        client,
        action="admin_checkin_coworking",
        text="<@UROO> check <@U1> <@U2> <@U1> in today",
    )

    UUID(client.batch_calls[0].pop("operation_id"))
    assert client.batch_calls == [
        {
            "admin_slack_user_id": "UADMIN",
            "target_slack_user_ids": ["U1", "U2"],
            "booking_date": "2026-07-04",
            "slack_channel_id": "C123",
        }
    ]
    assert client.single_calls == []
    assert "Processed **2** coworking check-ins" in result
    assert "<@U1>" not in result and "<@U2>" not in result and "8" not in result
    assert "Admin Roo Points were not charged" in result
    assert {message["user"] for message in batch_runtime["direct_messages"]} == {"U1", "U2"}
    rows = [batch_runtime["store"].get_by_key(
        coworking_module.build_coworking_intent_key(target, "2026-07-04"))
        for target in ("U1", "U2")]
    assert {row["notification_status"] for row in rows} == {"delivered"}

    duplicate_result = await _run_points_action(
        client,
        action="admin_checkin_coworking",
        text="check <@U2> <@U1> in today",
    )
    assert "already processed" in duplicate_result
    assert len(client.batch_calls) == 1
    assert len(batch_runtime["direct_messages"]) == 2


@pytest.mark.asyncio
async def test_non_admin_tagged_booking_is_denied_before_booking_call():
    client = FakeCoworkingClient(admin_details=None)

    result = await _run_points_action(
        client,
        action="book_coworking",
        text="book <@U1> <@U2> in today",
        user_id="UNOTADMIN",
    )

    assert "full Points Admin" in result
    assert client.batch_calls == []
    assert client.single_calls == []


@pytest.mark.asyncio
async def test_book_coworking_with_tagged_users_routes_to_admin_batch_flow():
    client = FakeCoworkingClient(admin_details={"role": "committee"})

    result = await _run_points_action(
        client,
        action="book_coworking",
        text="book <@U1> <@U2> in today",
    )

    assert client.batch_calls[0]["target_slack_user_ids"] == ["U1", "U2"]
    UUID(client.batch_calls[0]["operation_id"])
    assert "Processed **2** coworking check-ins" in result
    assert "Admin Roo Points were not charged" in result


@pytest.mark.asyncio
async def test_backend_batch_failure_formats_no_bookings_created_response(batch_runtime):
    exc = _batch_http_error(
        {
            "error": "One or more users have insufficient Roo Points",
            "errors": [
                {
                    "slack_user_id": "U2",
                    "error": "Insufficient balance: 4 < 8 required",
                }
            ],
        }
    )
    client = FakeCoworkingClient(admin_details={"role": "portfolio_lead"}, batch_exc=exc)

    result = await _run_points_action(
        client,
        action="admin_checkin_coworking",
        text="check <@U1> <@U2> in today",
    )

    assert "No bookings were created" in result
    assert "One or more users have insufficient Roo Points" in result
    assert "<@U2>: There are not enough Roo Points for this action" in result
    assert "4 < 8" not in result
    intent = batch_runtime["store"].get_by_key(
        coworking_module.build_coworking_batch_intent_key(
            "UADMIN", ["U1", "U2"], "2026-07-04"
        )
    )
    assert intent["status"] == "batch_blocked"
    assert intent["notification_status"] == "not_required"


@pytest.mark.asyncio
async def test_malformed_batch_success_is_persisted_for_atomic_retry(
    monkeypatch, batch_runtime
):
    backend_module = importlib.import_module("roo.clients.mlai_backend")
    client = FakeCoworkingClient(
        admin_details={"role": "admin"},
        batch_exc=backend_module.MLAIBackendUnavailableError(
            "incomplete success response", reason_code="invalid_backend_response"
        ),
    )
    result = await _run_points_action(
        client, action="admin_checkin_coworking", text="check <@U1> <@U2> in today"
    )
    batch_key = coworking_module.build_coworking_batch_intent_key(
        "UADMIN", ["U1", "U2"], "2026-07-04"
    )
    batch = batch_runtime["store"].get_by_key(batch_key)
    assert batch["status"] == "batch_pending_retry"
    assert batch["last_error"] == "invalid_backend_response"
    assert "queued the same atomic batch" in result
    assert client.single_calls == [] and batch_runtime["direct_messages"] == []

    restart_store = coworking_module.CoworkingBookingIntentStore(batch_runtime["db_path"])
    current_time = coworking_module._now()
    monkeypatch.setattr(coworking_module, "_now", lambda: current_time + 60)
    client.batch_exc = None
    due = restart_store.claim_due_batches(limit=10, owner="batch-restart-worker")
    assert len(due) == 1
    retry_result = await coworking_module.process_coworking_booking_batch_intent(
        due[0], store=restart_store, client=client
    )
    assert retry_result["status"] == "batch_confirmed"
    assert restart_store.get(int(batch["id"]))["status"] == "batch_confirmed"
    assert all(
        restart_store.get_by_key(
            coworking_module.build_coworking_intent_key(target, "2026-07-04")
        )["notification_status"] == "pending"
        for target in ("U1", "U2")
    )


@pytest.mark.asyncio
async def test_retry_terminal_batch_failure_notifies_requester_privately(
    monkeypatch, batch_runtime
):
    backend_module = importlib.import_module("roo.clients.mlai_backend")
    client = FakeCoworkingClient(
        admin_details={"role": "admin"},
        batch_exc=backend_module.MLAIBackendUnavailableError(
            "commit uncertain",
            reason_code="invalid_backend_response",
        ),
    )
    result = await _run_points_action(
        client,
        action="admin_checkin_coworking",
        text="check <@U1> <@U2> in today",
    )
    assert "queued the same atomic batch" in result
    batch_key = coworking_module.build_coworking_batch_intent_key(
        "UADMIN", ["U1", "U2"], "2026-07-04"
    )
    batch = batch_runtime["store"].get_by_key(batch_key)

    current_time = coworking_module._now()
    monkeypatch.setattr(coworking_module, "_now", lambda: current_time + 60)
    client.batch_exc = _batch_http_error(
        {"error": "One or more users have insufficient Roo Points"}
    )
    due = batch_runtime["store"].claim_due_batches(
        limit=10,
        owner="terminal-batch-worker",
    )
    retry_result = await coworking_module.process_coworking_booking_batch_intent(
        due[0],
        store=batch_runtime["store"],
        client=client,
    )

    assert retry_result["status"] == "batch_blocked"
    assert retry_result["notification_status"] == "delivered"
    assert batch_runtime["direct_messages"][-1]["user"] == "UADMIN"
    assert "No new bookings were created" in batch_runtime["direct_messages"][-1]["text"]
    stored = batch_runtime["store"].get(int(batch["id"]))
    assert stored["status"] == "batch_blocked"
    assert stored["notification_status"] == "delivered"


@pytest.mark.asyncio
async def test_terminal_batch_notification_retries_after_delivery_failure(
    monkeypatch, batch_runtime
):
    client = FakeCoworkingClient(
        admin_details={"role": "admin"},
        batch_exc=_batch_http_error({"error": "capacity changed"}),
    )
    batch = batch_runtime["store"].record_batch_intent(
        admin_slack_user_id="UADMIN",
        target_slack_user_ids=["U1", "U2"],
        booking_date="2026-07-04",
        channel_id="C123",
        thread_ts="111.222",
    )
    leased = batch_runtime["store"].reserve_batch_for_processing(
        int(batch["id"]), owner="terminal-batch-worker"
    )
    monkeypatch.setattr(
        coworking_module,
        "_safe_send_dm",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Slack down")),
    )

    retry_result = await coworking_module.process_coworking_booking_batch_intent(
        leased,
        store=batch_runtime["store"],
        client=client,
    )
    assert retry_result["status"] == "batch_blocked"
    assert retry_result["notification_status"] == "pending_retry"

    current_time = coworking_module._now()
    monkeypatch.setattr(coworking_module, "_now", lambda: current_time + 60)
    delivered = []
    monkeypatch.setattr(
        coworking_module,
        "_safe_send_dm",
        lambda **kwargs: delivered.append(
            (kwargs["user_id"], kwargs["text"], kwargs["client_msg_id"])
        )
        or True,
    )
    notification = batch_runtime["store"].claim_due_notifications(
        limit=10, owner="terminal-notification-restart"
    )[0]
    recovered = await coworking_module.deliver_coworking_booking_notification(
        notification,
        store=batch_runtime["store"],
        client=client,
    )
    assert recovered["notification_status"] == "delivered"
    assert delivered[0][0] == "UADMIN"


@pytest.mark.asyncio
async def test_batch_private_notifications_survive_delivery_failure_and_restart(
    monkeypatch, batch_runtime
):
    failed_message_ids = []
    def fail_dm(user_id, text, **kwargs):
        failed_message_ids.append(kwargs.get("client_msg_id"))
        raise RuntimeError("injected Slack failure")
    monkeypatch.setattr(executor_module, "send_dm", fail_dm)
    client = FakeCoworkingClient(admin_details={"role": "admin"})
    result = await _run_points_action(
        client, action="admin_checkin_coworking", text="check <@U1> <@U2> in today"
    )
    assert "failed delivery is queued for retry" in result
    child_rows = [batch_runtime["store"].get_by_key(
        coworking_module.build_coworking_intent_key(target, "2026-07-04"))
        for target in ("U1", "U2")]
    assert {row["notification_status"] for row in child_rows} == {"pending_retry"}

    restart_store = coworking_module.CoworkingBookingIntentStore(batch_runtime["db_path"])
    current_time = coworking_module._now()
    monkeypatch.setattr(coworking_module, "_now", lambda: current_time + 60)
    delivered = []
    def deliver_dm(user_id, text, **kwargs):
        delivered.append({"user": user_id, "client_msg_id": kwargs.get("client_msg_id")})
        return {"ok": True}
    monkeypatch.setattr(executor_module, "send_dm", deliver_dm)
    due = restart_store.claim_due_notifications(limit=10, owner="restart-worker")
    for notification in due:
        await coworking_module.deliver_coworking_booking_notification(
            notification, store=restart_store, client=client,
            executor=SkillExecutor(), post_public_message=False,
        )
    assert {message["user"] for message in delivered} == {"U1", "U2"}
    assert {message["client_msg_id"] for message in delivered} == set(failed_message_ids)
    assert all(restart_store.get(int(row["id"]))["notification_status"] == "delivered"
               for row in child_rows)
