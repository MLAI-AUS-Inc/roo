import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "roo-standalone")))

from roo import notion_agenda as agenda
from roo.clients.mlai_backend import MLAIBackendUnavailableError
from roo.skills.contracts import SkillRequest
from roo.skills.executors.notion_agenda import NotionAgendaExecutor
from roo.skills.registry import EXECUTORS
import roo.skills.executors.notion_agenda as executor_module


def ts(utc: str) -> str:
    """Slack ts for a UTC ISO time."""
    return str(datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).timestamp())


# --- Melbourne date from the message ts (AC7) ---

@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-10-22T12:59:00", date(2026, 10, 22)),  # 23:59 AEDT
        ("2026-10-22T13:01:00", date(2026, 10, 23)),  # 00:01 AEDT
        ("2026-10-03T13:59:00", date(2026, 10, 3)),  # 23:59 AEST, night before DST starts (4 Oct)
        ("2026-10-03T14:01:00", date(2026, 10, 4)),  # 00:01 AEST on DST start day
        ("2027-04-03T12:59:00", date(2027, 4, 3)),  # 23:59 AEDT, night before DST ends (4 Apr)
        ("2027-04-03T13:01:00", date(2027, 4, 4)),  # 00:01 AEDT on DST end day
    ],
)
def test_melbourne_date_of_message_ts_around_midnight_and_dst(utc, expected):
    assert agenda.melbourne_date_of(ts(utc)) == expected


def test_tomorrow_is_relative_to_the_message_not_when_it_is_processed():
    # Sent 23:59 on the 22nd; however late Roo processes it, "tomorrow" is the 23rd.
    sent = ts("2026-10-22T12:59:00")
    assert agenda.resolve_meeting_date("tomorrow", "", "x", message_ts=sent) == "2026-10-23"


def test_router_date_phrase_is_resolved_without_scanning_the_item_text():
    text = "add monthly check-ins between presidents and committee members to the agenda for tomorrow's meeting"
    resolved = agenda.resolve_meeting_date(
        "tomorrow's meeting", text, "a rephrased item", message_ts=ts("2026-10-22T01:00:00")
    )
    assert resolved == "2026-10-23"


def test_without_a_phrase_the_message_is_scanned_with_the_item_removed():
    resolved = agenda.resolve_meeting_date(
        None, "add Q4 goals to Monday's agenda", "Q4 goals", message_ts=ts("2026-10-22T01:00:00")
    )
    assert resolved == "2026-10-26"


def test_missing_date_asks_which_meeting():
    with pytest.raises(agenda.AgendaInputError) as raised:
        agenda.resolve_meeting_date(None, "add sponsorship update to the agenda", "sponsorship update", message_ts=ts("2026-10-22T01:00:00"))
    assert str(raised.value) == agenda.DATE_QUESTION


# --- Key, links, replies ---

def test_idempotency_key_is_stable_sha256_of_workspace_channel_ts():
    key = agenda.idempotency_key("T1", "C1", "1761000000.000100")
    assert key == agenda.idempotency_key("T1", "C1", "1761000000.000100")
    assert len(key) == 64 and key != agenda.idempotency_key("T1", "C1", "1761000000.000200")


def test_page_id_from_slack_wrapped_notion_link_is_dashed():
    text = "add it to <https://www.notion.so/mlai/2026-10-23-Meeting-Notes-1a2b3c4d5e6f40718293a4b5c6d7e8f9?pvs=4|this one>"
    assert agenda.page_id_from_text(text) == "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"


def test_two_different_links_pick_no_page():
    text = "https://notion.so/A-" + "1" * 32 + " or https://notion.so/B-" + "2" * 32
    assert agenda.page_id_from_text(text) is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "added", "page_title": "2026-10-23 Meeting Notes", "page_url": "https://n/p", "created_page": True},
         "Added to 2026-10-23 Meeting Notes: https://n/p I created the page from the template."),
        ({"status": "duplicate", "duplicate_location": "if_time", "page_title": "T", "page_url": "https://n/p"},
         "That's already under *If time:* for T: https://n/p"),
        ({"code": "agenda_verify_failed", "uncertain": True}, agenda.UNCONFIRMED),
        ({"code": "agenda_requester_not_allowed", "uncertain": False}, agenda.ERROR_REPLIES["agenda_requester_not_allowed"]),
        ({"code": "something_new"}, "I couldn't add that to the agenda. Nothing was added."),
    ],
)
def test_reply_for_backend_answers(result, expected):
    assert agenda.reply_for(result) == expected


def test_ambiguous_reply_lists_the_links_and_adds_nothing():
    reply = agenda.reply_for({
        "status": "ambiguous",
        "choices": [{"page_id": "a", "page_url": "https://n/a"}, {"page_id": "b", "page_url": "https://n/b"}],
    })
    assert "1. https://n/a\n2. https://n/b" in reply and "didn't add anything" in reply


# --- Handler ---

class FakeBackend:
    answers = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def add_notion_agenda_item(self, **kwargs):
        self.__class__.calls.append(kwargs)
        answer = self.__class__.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


ADDED = {"status": "added", "page_title": "2026-10-23 Meeting Notes", "page_url": "https://n/p", "created_page": False}
SENT = ts("2026-10-22T01:00:00")


@pytest.fixture
def handler(monkeypatch):
    FakeBackend.answers, FakeBackend.calls = [], []
    profile = {"display_name": "Sam"}
    monkeypatch.setattr(
        executor_module, "get_settings",
        lambda: SimpleNamespace(MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="roo-key"),
    )
    monkeypatch.setattr(executor_module, "MLAIBackendClient", FakeBackend)
    monkeypatch.setattr(executor_module, "get_user_info", SimpleNamespace(__wrapped__=lambda user_id: profile))
    monkeypatch.setattr(executor_module.asyncio, "sleep", lambda seconds: _noop())

    async def run(text="add check-ins to tomorrow's agenda", params=None, message_ts=SENT):
        return await NotionAgendaExecutor()._execute_notion_agenda(
            text=text,
            params=params or {"action": "add_agenda_item", "item": "check-ins", "date": "tomorrow"},
            user_id="U123", channel_id="C1", slack_team_id="T1", request_message_ts=message_ts,
        )

    run.profile = profile
    return run


async def _noop():
    return None


@pytest.mark.asyncio
async def test_adds_item_with_display_name_and_message_date(handler):
    FakeBackend.answers = [ADDED]

    assert await handler() == "Added to 2026-10-23 Meeting Notes: https://n/p"
    assert FakeBackend.calls == [{
        "idempotency_key": agenda.idempotency_key("T1", "C1", SENT),
        "requester_slack_id": "U123", "owner_name": "Sam",
        "meeting_date": "2026-10-23", "item": "check-ins", "page_id": None,
    }]


@pytest.mark.asyncio
async def test_empty_display_name_writes_nothing(handler):
    handler.profile["display_name"] = ""

    assert await handler() == agenda.NO_DISPLAY_NAME
    assert FakeBackend.calls == []


@pytest.mark.asyncio
async def test_failed_profile_lookup_writes_nothing(handler):
    handler.profile.clear()
    handler.profile.update({"id": "U123", "name": "Unknown"})

    assert await handler() == agenda.PROFILE_UNAVAILABLE
    assert FakeBackend.calls == []


@pytest.mark.asyncio
async def test_missing_date_or_item_asks_and_writes_nothing(handler):
    assert await handler(text="add check-ins to the agenda", params={"item": "check-ins"}) == agenda.DATE_QUESTION
    assert await handler(params={"date": "tomorrow"}) == agenda.ITEM_QUESTION
    assert FakeBackend.calls == []


@pytest.mark.asyncio
async def test_uncertain_write_is_retried_once_with_the_same_key(handler):
    FakeBackend.answers = [
        {"code": "notion_unavailable", "uncertain": True},
        {"status": "duplicate", "duplicate_location": "agenda", "page_title": "T", "page_url": "https://n/p"},
    ]

    assert await handler() == "That's already on the agenda for T: https://n/p"
    assert len(FakeBackend.calls) == 2
    assert FakeBackend.calls[0]["idempotency_key"] == FakeBackend.calls[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_never_claims_success_or_failure_when_still_unconfirmed(handler):
    FakeBackend.answers = [MLAIBackendUnavailableError("timeout"), {"code": "agenda_verify_failed", "uncertain": True}]

    assert await handler() == agenda.UNCONFIRMED
    assert len(FakeBackend.calls) == 2


@pytest.mark.asyncio
async def test_slack_retry_of_the_same_message_reuses_the_key_and_gets_the_replay(handler):
    # AC4: the backend replays the stored result for the same key; nothing is written twice.
    FakeBackend.answers = [ADDED, {**ADDED, "replayed": True}]

    first, second = await handler(), await handler()

    assert first == second
    assert FakeBackend.calls[0]["idempotency_key"] == FakeBackend.calls[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_non_committee_requester_is_refused(handler):
    FakeBackend.answers = [{"code": "agenda_requester_not_allowed", "uncertain": False}]

    assert await handler() == agenda.ERROR_REPLIES["agenda_requester_not_allowed"]
    assert len(FakeBackend.calls) == 1


@pytest.mark.asyncio
async def test_pasted_meeting_link_answers_which_meeting(handler):
    FakeBackend.answers = [ADDED]
    text = "add check-ins to tomorrow's agenda <https://www.notion.so/Meeting-" + "ab" * 16 + ">"

    await handler(text=text)

    assert FakeBackend.calls[0]["page_id"] == "abababab-abab-abab-abab-abababababab"


def test_skill_is_registered_to_its_executor():
    assert EXECUTORS["notion-agenda"] is NotionAgendaExecutor


@pytest.mark.asyncio
async def test_dispatch_metadata_supplies_the_team_and_message_ts(handler):
    # The central SkillExecutor passes slack_team_id and current_message_ts as metadata.
    FakeBackend.answers = [ADDED]

    await NotionAgendaExecutor().execute(SkillRequest(
        skill=None, text="add check-ins to tomorrow's agenda", user_id="U123", channel_id="C1",
        params={"action": "add_agenda_item", "item": "check-ins", "date": "tomorrow"},
        metadata={"slack_team_id": "T1", "current_message_ts": SENT},
    ))

    assert FakeBackend.calls[0]["idempotency_key"] == agenda.idempotency_key("T1", "C1", SENT)
    assert FakeBackend.calls[0]["meeting_date"] == "2026-10-23"
