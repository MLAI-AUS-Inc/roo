from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from roo.coworking_dates import CoworkingDateError, resolve_coworking_date
from roo.skills.executor import SkillExecutor


TODAY = date(2026, 9, 13)


@pytest.mark.parametrize("phrase, expected", [
    ("sept 16th", "2026-09-16"),
    ("sept 18", "2026-09-18"),
    ("Sept18th", "2026-09-18"),
    ("Sept. 18th", "2026-09-18"),
    ("18 September", "2026-09-18"),
    ("18th of September", "2026-09-18"),
    ("September 18, 2026", "2026-09-18"),
    ("18 Sept 2027", "2027-09-18"),
    ("2026-09-18", "2026-09-18"),
    ("today", "2026-09-13"),
    ("tomorrow", "2026-09-14"),
    ("tomorow", "2026-09-14"),
    ("day after tomorrow", "2026-09-15"),
    ("in two days", "2026-09-15"),
    ("in 3 days", "2026-09-16"),
    ("in one week", "2026-09-20"),
    ("Friday", "2026-09-18"),
    ("next Friday", "2026-09-18"),
    ("next Sun", "2026-09-20"),
    ("this Sunday", "2026-09-13"),
    ("this Friday", "2026-09-11"),
    ("yesterday", "2026-09-12"),
])
@pytest.mark.parametrize("router_extracted_date", [True, False])
def test_natural_dates_work_with_or_without_router_parameter(phrase, expected, router_extracted_date):
    assert resolve_coworking_date(
        phrase if router_extracted_date else None,
        f"<@U090FV0GTT4> pls book me in {phrase}",
        today=TODAY, default_to_today=True,
    ) == expected


@pytest.mark.parametrize("phrase", [
    "31 September", "2026-02-30", "29 February", "18/9", "09/10",
    "the 18th", "next week", "sometime next month", "Christmas",
    "sept 18 or sept 19", "sept 18 and 19", "sept 18-20",
    "every Friday", "not tomorrow", "tomorrow or Friday", "in 999999999999999999 days",
])
@pytest.mark.parametrize("router_value", [None, "2026-09-18"])
def test_unclear_dates_never_fall_back_to_today_or_routers_chosen_day(phrase, router_value):
    with pytest.raises(CoworkingDateError):
        resolve_coworking_date(
            router_value, f"book me in {phrase}", today=TODAY, default_to_today=True,
        )


def test_year_rollover_and_explicit_past_year():
    assert resolve_coworking_date(
        "Jan 2", "book me in Jan 2", today=date(2026, 12, 30), default_to_today=True,
    ) == "2027-01-02"
    assert resolve_coworking_date(
        "Sept 18 2025", "book me in Sept 18 2025", today=TODAY, default_to_today=True,
    ) == "2025-09-18"
    assert resolve_coworking_date(
        "29 Feb 2028", "book me in 29 Feb 2028", today=TODAY, default_to_today=True,
    ) == "2028-02-29"


def test_missing_date_defaults_only_when_requested_and_ignores_time():
    assert resolve_coworking_date(None, "book me in", today=TODAY, default_to_today=True) == "2026-09-13"
    assert resolve_coworking_date(None, "cancel coworking", today=TODAY, default_to_today=False) is None
    assert resolve_coworking_date(
        None, "book me in at 1:30 pm tomorrow", today=TODAY, default_to_today=True,
    ) == "2026-09-14"


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr("roo.utils.get_current_date", lambda: TODAY)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    executor = SkillExecutor()
    executor._book_coworking_with_intent = AsyncMock(return_value="Booked")
    executor._book_coworking_many_for_admin = AsyncMock(return_value="Booked group")
    return executor


async def run_action(executor, client, action, text, params):
    return await executor._handle_points_action(
        client=client, action=action, params=params, text=text,
        user_id="U123", channel_id="C123", thread_ts="1.2", skill=None,
    )


@pytest.mark.asyncio
async def test_screenshot_phrase_reaches_booking_with_iso_date(executor):
    result = await run_action(
        executor, SimpleNamespace(), "book_coworking",
        "pls book me in sept 16th", {"date": "sept 16th"},
    )
    assert result == "Booked"
    call = executor._book_coworking_with_intent.await_args.kwargs
    assert call["booking_date"] == "2026-09-16"
    assert call["target_user_id"] == "U123"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["book_coworking", "admin_checkin_coworking"])
async def test_admin_batch_uses_same_date_resolution(executor, action):
    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}))
    await run_action(executor, client, action, "book <@U1> <@U2> in sept 18", {"date": "sept 18"})
    assert executor._book_coworking_many_for_admin.await_args.kwargs["booking_date"] == "2026-09-18"
    executor._book_coworking_with_intent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [
    "book_coworking", "admin_checkin_coworking", "check_coworking",
])
async def test_clarification_precedes_any_client_or_intent_call(executor, action):
    # A client with no methods ensures no API or permission lookup happens.
    result = await run_action(executor, SimpleNamespace(), action, "coworking sept 18 or 19", {"date": "2026-09-18"})
    assert "Which" in result
    executor._book_coworking_with_intent.assert_not_awaited()
    executor._book_coworking_many_for_admin.assert_not_awaited()


@pytest.mark.asyncio
async def test_availability_accepts_natural_dates(executor):
    client = SimpleNamespace(check_coworking=AsyncMock(return_value=[]))
    await run_action(executor, client, "check_coworking", "coworking availability next Friday", {"date": "next Friday"})
    client.check_coworking.assert_awaited_once_with("2026-09-18", 7, slack_user_id="U123")


@pytest.mark.asyncio
async def test_availability_window_is_not_a_booking_date(executor):
    client = SimpleNamespace(check_coworking=AsyncMock(return_value=[]))
    await run_action(
        executor, client, "check_coworking", "coworking availability for the next 7 days", {"days": 7},
    )
    client.check_coworking.assert_awaited_once_with(None, 7, slack_user_id="U123")
