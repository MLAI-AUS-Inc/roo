import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import linear_context


def test_contextual_linear_reference_is_narrow():
    assert linear_context.is_contextual_linear_reference(
        "@Roo add this as a task for me in Linear"
    )
    assert linear_context.is_contextual_linear_reference(
        "make the request above a Linear issue"
    )
    assert linear_context.is_contextual_linear_reference(
        (
            "For the Linear project [Studio] Aaron AI, using this transcript "
            "and meeting notes, add the to-do items."
        )
    )
    assert not linear_context.is_contextual_linear_reference(
        "create a Linear task to change the name of this project"
    )


def test_top_level_contextual_request_reads_prior_channel_messages(monkeypatch):
    calls = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        return [
            {
                "user": "UJESS",
                "text": "Sam can you send me the Founder Games run sheet by EOW?",
                "ts": "1784592300.000001",
                "is_bot": False,
                "files": [],
            }
        ]

    users = {
        "UJESS": {
            "display_name": "Jess",
            "real_name": "Jessica Hex",
            "email": "jess@example.com",
        },
        "USAM": {
            "display_name": "Dr Sam",
            "real_name": "Sam Donegan",
            "email": "sam@example.com",
        },
    }
    monkeypatch.setattr(linear_context, "get_recent_channel_messages", fake_history)
    monkeypatch.setattr(linear_context, "get_user_info", lambda user_id: users[user_id])
    monkeypatch.setattr(
        linear_context,
        "get_channel_context",
        lambda channel_id: {
            "name": "founder-programs",
            "topic": "Founder Games planning",
            "purpose": "Plan founder programs",
            "is_private": False,
        },
    )

    context = linear_context.build_linear_slack_context(
        text="@Roo add this as a task for me in Linear",
        requester_user_id="USAM",
        channel_id="CFOUNDERS",
        thread_ts="1784595900.000002",
        current_message_ts="1784595900.000002",
        workspace_id="TMLAI",
        event_id="Ev1",
        timezone_name="Australia/Sydney",
    )

    assert calls == [
        {
            "channel": "CFOUNDERS",
            "before_ts": "1784595900.000002",
            "limit": 50,
            "lookback_hours": 24,
        }
    ]
    assert context["selection"]["mode"] == "recent_channel"
    assert [message["display_name"] for message in context["messages"]] == ["Jess", "Dr Sam"]
    assert context["messages"][0]["email"] == "jess@example.com"
    assert context["request"]["user_id"] == "USAM"
    assert context["request"]["event_id"] == "Ev1"
    assert context["channel"]["topic"] == "Founder Games planning"


def test_direct_linear_command_does_not_read_channel_history(monkeypatch):
    monkeypatch.setattr(
        linear_context,
        "get_recent_channel_messages",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("history should not be read")),
    )
    monkeypatch.setattr(
        linear_context,
        "get_user_info",
        lambda user_id: {"display_name": "Sam", "email": "sam@example.com"},
    )
    monkeypatch.setattr(linear_context, "get_channel_context", lambda channel_id: {})

    context = linear_context.build_linear_slack_context(
        text="create a Linear task to change the name of this project",
        requester_user_id="USAM",
        channel_id="C1",
        thread_ts="1784595900.000002",
        current_message_ts="1784595900.000002",
        thread_history=[],
    )

    assert context["selection"]["mode"] == "thread"
    assert len(context["messages"]) == 1
