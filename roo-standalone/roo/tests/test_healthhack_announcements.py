import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

from roo.clients.mlai_backend import MLAIBackendClient
from roo.skills.executor import SkillExecutor
from roo.skills.loader import Skill


@pytest.fixture(autouse=True)
def backend_settings(monkeypatch):
    monkeypatch.setattr(
        "roo.clients.mlai_backend.get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            ROO_API_KEY="test-roo-key",
            MLAI_API_KEY=None,
            INTERNAL_API_KEY="test-internal-key",
        ),
    )


def healthhack_skill():
    return Skill(
        name="healthhack",
        description="HealthHack announcements",
        content="",
        path=Path("."),
        exclusive_channels=["healthhack"],
        actions=[
            {
                "name": "announce",
                "description": "Publish an announcement.",
                "params": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
            }
        ],
    )


@pytest.mark.asyncio
async def test_healthhack_executor_publishes_structured_announcement(monkeypatch):
    captured = {}

    async def fake_create(self, **kwargs):
        captured.update(kwargs)
        return {"id": "announcement-1", "created": True}

    monkeypatch.setattr(MLAIBackendClient, "healthhack_create_announcement", fake_create)
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: "healthhack")
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U0ROO00000")

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0SUPER123",
        "C0BHZ9NS21L",
        "1784286514.495879",
        "1784286520.123456",
    )

    assert result == 'Announcement *"Doors open"* has been posted to the HealthHack app.'
    assert captured == {
        "title": "Doors open",
        "body": "Come upstairs.",
        "requester_slack_id": "U0SUPER123",
        "author_slack_id": "U0ROO00000",
        "source_channel_id": "C0BHZ9NS21L",
        "source_message_ts": "1784286520.123456",
    }


@pytest.mark.asyncio
async def test_healthhack_executor_reports_idempotent_replay(monkeypatch):
    async def fake_create(self, **kwargs):
        return {"id": "announcement-1", "created": False}

    monkeypatch.setattr(MLAIBackendClient, "healthhack_create_announcement", fake_create)
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: "healthhack")
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U0ROO00000")

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0SUPER123",
        "C0BHZ9NS21L",
        None,
        "1784286520.123456",
    )

    assert result == 'Announcement *"Doors open"* was already posted to the HealthHack app.'


@pytest.mark.asyncio
async def test_healthhack_executor_relays_backend_authorisation(monkeypatch):
    async def fake_create(self, **kwargs):
        return {"status_code": 403, "detail": "forbidden"}

    monkeypatch.setattr(MLAIBackendClient, "healthhack_create_announcement", fake_create)
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: "healthhack")
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U0ROO00000")

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0PART1234",
        "C0BHZ9NS21L",
        None,
        "1784286520.123456",
    )

    assert "only authorised HealthHack organisers" in result


@pytest.mark.asyncio
async def test_healthhack_executor_rejects_wrong_channel(monkeypatch):
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: "general")

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0SUPER123",
        "CGENERAL123",
        None,
        "1784286520.123456",
    )

    assert result == "The HealthHack announcement skill is only available in #*healthhack*."


@pytest.mark.asyncio
async def test_healthhack_executor_fails_closed_when_channel_cannot_be_resolved(monkeypatch):
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: None)

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0SUPER123",
        "CUNKNOWN123",
        None,
        "1784286520.123456",
    )

    assert result == "The HealthHack announcement skill is only available in #*healthhack*."


@pytest.mark.asyncio
async def test_healthhack_executor_requires_source_message_provenance(monkeypatch):
    monkeypatch.setattr("roo.slack_client.get_channel_name", lambda channel_id: "healthhack")

    result = await SkillExecutor()._execute_healthhack(
        healthhack_skill(),
        "post the announcement",
        {"action": "announce", "title": "Doors open", "body": "Come upstairs."},
        "U0SUPER123",
        "C0BHZ9NS21L",
        None,
        None,
    )

    assert "couldn't identify the source Slack message" in result
