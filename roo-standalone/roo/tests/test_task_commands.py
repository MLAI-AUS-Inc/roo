import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

executor_module = importlib.import_module("roo.skills.executor")
SkillExecutor = executor_module.SkillExecutor


class FakeTaskClient:
    def __init__(self):
        self.calls = []

    async def list_tasks(self, **kwargs):
        self.calls.append(kwargs)
        return [
            {
                "id": 42,
                "task_code": "ROO-0042",
                "title": "Fix volunteer docs",
                "points_estimate": 12,
                "portfolio": "tech",
                "status": "open",
            }
        ]


@pytest.mark.asyncio
async def test_tasks_and_tasks_open_use_identical_open_queue_filters():
    executor = SkillExecutor()
    first_client = FakeTaskClient()
    second_client = FakeTaskClient()

    first = await executor._handle_points_action(
        client=first_client,
        action="list_tasks",
        params={},
        text="tasks",
        user_id="U123",
        channel_id="C123",
        thread_ts="123.456",
        skill=None,
    )
    second = await executor._handle_points_action(
        client=second_client,
        action="list_tasks",
        params={},
        text="tasks open",
        user_id="U123",
        channel_id="C123",
        thread_ts="123.456",
        skill=None,
    )

    assert first_client.calls == [{"status": None, "portfolio": None, "claimable": True}]
    assert second_client.calls == [{"status": None, "portfolio": None, "claimable": True}]
    assert first == second


@pytest.mark.asyncio
async def test_tasks_quick_returns_explicit_unsupported_message():
    executor = SkillExecutor()
    client = FakeTaskClient()

    result = await executor._handle_points_action(
        client=client,
        action="list_tasks",
        params={},
        text="tasks quick",
        user_id="U123",
        channel_id="C123",
        thread_ts="123.456",
        skill=None,
    )

    assert result == 'Use `tasks` or `tasks open` for claimable work. `tasks quick` is no longer supported.'
    assert client.calls == []
