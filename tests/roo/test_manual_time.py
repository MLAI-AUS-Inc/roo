import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from roo import manual_time
from roo.manual_time_modal import build_view, review_submission

# The application obtains this contract from the backend at runtime.
CATALOGUE = {
    "version": "test",
    "duration_options": [
        {"value": value, "label": label} for value, label in [
            ("30", "30 min"), ("60", "1 hr"), ("90", "1 hr 30 min"),
            ("120", "2 hr"), ("180", "3 hours"), ("240", "4 hours"), ("other", "Other"),
        ]
    ],
    "groups": [{"label": "Events", "actions": [
        {"id": "event.door", "label": "Staffed the door"},
        {"id": "event.pack", "label": "Packed down"},
        {"id": "other", "label": "Something else"},
    ]}],
}

def test_preset_and_other_preview():
    for duration, hours, minutes, expected in (
        ("180", "9", "30", 180), ("240", "", "", 240), ("other", "", "45", 45),
        ("other", "2", "15", 135),
    ):
        state = {"actions": ["event.door", "event.pack"], "duration": duration, "hours": hours, "minutes": minutes}
        view = build_view("member", CATALOGUE, state)
        fields = {block["block_id"] for block in view["blocks"] if block["type"] == "input"}
        assert ("hours" in fields and "minutes" in fields) == (duration == "other")
        assert review_submission("member", state, CATALOGUE)["volunteer_minutes"] == expected


def test_validation_does_not_conflate_other_fields():
    state = {"actions": ["event.pack"], "duration": "other", "minutes": "60"}
    assert "minutes" in review_submission("member", state, CATALOGUE)["errors"]
    state.update(minutes="45")
    assert review_submission("member", state, CATALOGUE)["volunteer_minutes"] == 45
    state.update(actions=["other"])
    assert "note" in review_submission("member", state, CATALOGUE)["errors"]


def test_submit_binds_actor_and_reuses_draft_key():
    settings = SimpleNamespace(SLACK_SIGNING_SECRET="unit-test", ROO_MANUAL_TIME_SLACK_WORKSPACE_ID="TTEST")
    key = "28dbfe4e-0af0-476e-9b54-9a4cec856602"
    draft = key + ".4000000000"
    data = {
        "type": "view_submission", "team": {"id": "TTEST"}, "user": {"id": "USER"},
        "view": {
            "callback_id": manual_time.CALLBACK_PREFIX + "member",
            "private_metadata": draft + "." + manual_time.signature(settings, draft, "USER", "TTEST", "member"),
            "state": {"values": {
                "duration": {"duration": {"selected_option": {"value": "other"}}},
                "minutes": {"minutes": {"value": "45"}},
                "actions": {"actions": {"selected_options": [{"value": "event.door"}, {"value": "event.pack"}]}},
            }},
        },
    }
    with patch.object(manual_time, "backend", AsyncMock(side_effect=[CATALOGUE, {"roo": "4.5", "status": "pending"}])) as backend:
        result = asyncio.run(manual_time.modal_action(settings, data))
        assert result["response_action"] == "update"
        assert "4.5 Roo" in str(result)
        assert backend.await_args.kwargs["submission_key"] == key
        assert backend.await_args.kwargs["volunteer_minutes"] == 45
        data["user"]["id"] = "UOTHER"
        assert asyncio.run(manual_time.modal_action(settings, data))["response_action"] == "errors"
        assert backend.await_count == 2


def test_retry_recovers_existing_private_message():
    settings = SimpleNamespace(ROO_MANUAL_TIME_SLACK_REVIEW_CHANNEL_ID="CREVIEW")
    delivery = {
        "id": 1, "stage": "time_slack_review", "attempts": 2, "created": 100,
        "request": {"id": 2, "display_name": "Alex", "roo": "9", "volunteer_minutes": 90, "activity": "Staffed the door"},
    }
    client = MagicMock()
    client.conversations_info.return_value = {"channel": {"is_private": True}}
    client.conversations_history.return_value = {"messages": [{
        "metadata": {"event_type": manual_time.METADATA_EVENT, "event_payload": {
            "delivery_id": 1, "stage": "time_slack_review", "request_id": 2,
        }},
        "ts": "101.1",
    }]}
    with patch.object(manual_time, "get_slack_client", return_value=client):
        assert manual_time.deliver(settings, delivery) == ("CREVIEW", "101.1")
        client.chat_postMessage.assert_not_called()
