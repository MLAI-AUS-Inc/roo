import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

executor_module = importlib.import_module("roo.skills.executor")
slack_client_module = importlib.import_module("roo.slack_client")
SkillExecutor = executor_module.SkillExecutor


class FakeCoworkingClient:
    def __init__(self, *, admin_details=None, batch_result=None, batch_exc=None):
        self.admin_details = admin_details
        self.batch_result = batch_result or {
            "date": "2026-07-04",
            "created_count": 2,
            "already_booked_count": 0,
            "results": [
                {"slack_user_id": "U1", "created": True, "already_booked": False, "points_cost": 8},
                {"slack_user_id": "U2", "created": True, "already_booked": False, "points_cost": 8},
            ],
        }
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
    ):
        self.batch_calls.append(
            {
                "admin_slack_user_id": admin_slack_user_id,
                "target_slack_user_ids": list(target_slack_user_ids),
                "booking_date": booking_date,
                "slack_channel_id": slack_channel_id,
            }
        )
        if self.batch_exc:
            raise self.batch_exc
        return self.batch_result

    async def book_coworking(self, *args, **kwargs):
        self.single_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("single booking should not be called in this test")


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


class FakeCancellationClient:
    def __init__(self, bookings):
        self.bookings = bookings
        self.list_calls = []
        self.cancel_calls = []

    async def get_my_bookings(self, slack_user_id):
        self.list_calls.append(slack_user_id)
        return list(self.bookings)

    async def cancel_coworking(self, slack_user_id, **kwargs):
        self.cancel_calls.append((slack_user_id, kwargs))
        return {"refunded": True, "refund_amount": 8}


@pytest.fixture(autouse=True)
def bot_user(monkeypatch):
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")


@pytest.mark.asyncio
async def test_date_cancellation_resolves_immutable_booking_id_before_mutation():
    client = FakeCancellationClient(
        [{"id": "booking-current", "date": "2026-07-04", "status": "booked"}]
    )

    result = await _run_points_action(
        client,
        action="cancel_coworking",
        text="cancel coworking 2026-07-04",
    )

    assert client.list_calls == ["UADMIN"]
    assert client.cancel_calls == [
        ("UADMIN", {"booking_id": "booking-current"})
    ]
    assert "8 point refunded" in result


@pytest.mark.asyncio
async def test_ambiguous_date_cancellation_fails_closed():
    client = FakeCancellationClient(
        [
            {"id": "booking-1", "date": "2026-07-04", "status": "booked"},
            {"id": "booking-2", "date": "2026-07-04", "status": "booked"},
        ]
    )

    result = await _run_points_action(
        client,
        action="cancel_coworking",
        text="cancel coworking 2026-07-04",
    )

    assert client.cancel_calls == []
    assert "more than one active booking" in result


@pytest.mark.asyncio
async def test_admin_checkin_coworking_batches_deduped_targets():
    client = FakeCoworkingClient(
        admin_details={"role": "admin"},
        batch_result={
            "date": "2026-07-04",
            "created_count": 1,
            "already_booked_count": 1,
            "results": [
                {"slack_user_id": "U1", "created": True, "already_booked": False, "points_cost": 8},
                {"slack_user_id": "U2", "created": False, "already_booked": True, "points_cost": 8},
            ],
        },
    )

    result = await _run_points_action(
        client,
        action="admin_checkin_coworking",
        text="<@UROO> check <@U1> <@U2> <@U1> in today",
    )

    assert client.batch_calls == [
        {
            "admin_slack_user_id": "UADMIN",
            "target_slack_user_ids": ["U1", "U2"],
            "booking_date": "2026-07-04",
            "slack_channel_id": "C123",
        }
    ]
    assert client.single_calls == []
    assert "<@U1>: booked" in result
    assert "<@U2>: already booked" in result
    assert "Admin Roo Points were not charged" in result


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
    assert "Checked **2** people" in result
    assert "Admin Roo Points were not charged" in result


@pytest.mark.asyncio
async def test_backend_batch_failure_formats_no_bookings_created_response():
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
