import importlib
import json
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)
sys.modules.pop("roo.main", None)

backend_module = importlib.import_module("roo.clients.mlai_backend")
executor_module = importlib.import_module("roo.skills.executor")
slack_client_module = importlib.import_module("roo.slack_client")
main_module = importlib.import_module("roo.main")
SkillExecutor = executor_module.SkillExecutor


class FakeContentFactoryClient:
    last_instance = None

    def __init__(self, *args, **kwargs):
        self.trigger_calls = []
        self.status_checks = []
        FakeContentFactoryClient.last_instance = self

    async def get_integration(self, user_id, domain=None):
        return {
            "github_repo": "MLAI-AUS-Inc/mlai-au",
            "project_scanned": True,
            "last_scanned_at": "2026-03-15T10:39:17.784628Z",
            "last_article": None,
            "recommended_next_action": None,
            "connected_domains": [
                {
                    "domain": "mlai.au",
                    "github_repo": "MLAI-AUS-Inc/mlai-au",
                    "scanned": True,
                }
            ],
        }

    async def trigger_article_generation(
        self,
        slack_user_id,
        domain,
        topic=None,
        target_keyword="",
        context=None,
        slack_channel_id=None,
        slack_thread_ts=None,
    ):
        self.trigger_calls.append(
            {
                "slack_user_id": slack_user_id,
                "domain": domain,
                "topic": topic,
                "target_keyword": target_keyword,
                "context": context,
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
            }
        )
        return {
            "job_id": "job-123",
            "workflow": "auto_discovery" if not topic else "direct_generate",
        }

    async def check_generation_status(self, job_id):
        self.status_checks.append(job_id)
        return {"status": "completed", "progress": 100, "current_step": "publishing"}

    async def publish_article(self, job_id, slack_user_id):
        return {
            "preview_url": "https://preview.test/article",
            "pr_url": "https://github.test/pr/123",
        }


def _patch_content_factory(monkeypatch):
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeContentFactoryClient)


@pytest.mark.asyncio
async def test_generic_article_request_prompts_for_direction(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for my website mlai.au",
        params={"domain": "mlai.au"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert isinstance(result, dict)
    assert "research the best article opportunity" in result["message"].lower()
    assert {element["action_id"] for element in result["blocks"][1]["elements"]} == {
        "article_research_best",
        "article_provide_topic",
    }
    assert "Status Report" in posted_messages[0]["args"][1]
    assert FakeContentFactoryClient.last_instance.trigger_calls == []


@pytest.mark.asyncio
async def test_explicit_research_request_starts_generation(monkeypatch):
    executor = SkillExecutor()
    _patch_content_factory(monkeypatch)
    created_tasks = []
    posted_messages = []
    monkeypatch.setattr(
        executor_module.asyncio,
        "create_task",
        lambda coro: created_tasks.append(coro) or coro,
    )
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="research the best article for mlai.au",
        params={"domain": "mlai.au"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "started researching the best article for mlai.au" in result.lower()
    assert created_tasks == []
    assert len(posted_messages) == 1
    assert FakeContentFactoryClient.last_instance.trigger_calls == [
        {
            "slack_user_id": "U05QPB483K9",
            "domain": "mlai.au",
            "topic": None,
            "target_keyword": "",
            "context": "research the best article for mlai.au",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        }
    ]


@pytest.mark.asyncio
async def test_topic_led_article_request_mentions_seo_optimization(monkeypatch):
    executor = SkillExecutor()
    _patch_content_factory(monkeypatch)
    created_tasks = []
    monkeypatch.setattr(
        executor_module.asyncio,
        "create_task",
        lambda coro: created_tasks.append(coro) or coro,
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for mlai.au about AI for clinic workflows",
        params={"domain": "mlai.au", "topic": "AI for clinic workflows"},
        user_id="U05QPB483K9",
        channel_id=None,
        thread_ts=None,
    )

    assert "best chance to rank" in result
    assert "keywords, title, and talking points" in result
    assert FakeContentFactoryClient.last_instance.trigger_calls[0]["topic"] == "AI for clinic workflows"
    assert len(created_tasks) == 0


@pytest.mark.asyncio
async def test_topic_led_article_request_starts_monitor_when_threaded(monkeypatch):
    executor = SkillExecutor()
    _patch_content_factory(monkeypatch)
    created_tasks = []
    posted_messages = []

    def capture_task(coro):
        created_tasks.append(coro)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(executor_module.asyncio, "create_task", capture_task)
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for mlai.au about AI for clinic workflows",
        params={"domain": "mlai.au", "topic": "AI for clinic workflows"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "best chance to rank" in result
    assert len(created_tasks) == 1
    assert len(posted_messages) == 1
    assert FakeContentFactoryClient.last_instance.trigger_calls == [
        {
            "slack_user_id": "U05QPB483K9",
            "domain": "mlai.au",
            "topic": "AI for clinic workflows",
            "target_keyword": "",
            "context": "write an article for mlai.au about AI for clinic workflows",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        }
    ]


@pytest.mark.asyncio
async def test_monitor_generation_returns_on_awaiting_confirmation(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []

    class AwaitingConfirmationClient:
        def __init__(self):
            self.calls = 0

        async def check_generation_status(self, job_id):
            self.calls += 1
            return {
                "status": "awaiting_confirmation",
                "progress": 40,
                "current_step": "researching",
            }

        async def publish_article(self, job_id, slack_user_id):
            raise AssertionError("publish_article should not be called for awaiting_confirmation")

    client = AwaitingConfirmationClient()

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(executor_module.asyncio, "sleep", no_sleep)

    await executor._monitor_generation(client, "job-123", "C123", "111.222", "U123")

    assert client.calls == 1
    assert posted_messages == [
        (
            ("C123", "📝 *Status Update*: Researching... (40%)", "111.222"),
            {},
        )
    ]


def test_article_provide_topic_action_updates_message(monkeypatch):
    updated_messages = []
    remembered_context = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(default_llm_provider="openai", SKILLS_DIR="skills"),
    )
    monkeypatch.setattr(main_module, "get_agent", lambda: SimpleNamespace(skills=[]))
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(
        main_module,
        "_remember_content_thread_context",
        lambda channel_id, thread_ts, domain, workflow: remembered_context.append(
            (channel_id, thread_ts, domain, workflow)
        ),
    )

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Choose article direction",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Before I start..."}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [
            {
                "action_id": "article_provide_topic",
                "value": json.dumps(
                    {
                        "domain": "mlai.au",
                        "slack_user_id": "U05QPB483K9",
                        "channel_id": "C123",
                        "thread_ts": "111.222",
                    }
                ),
            }
        ],
    }

    class FakeRequest:
        async def form(self):
            return {"payload": json.dumps(payload)}

    response = asyncio.run(main_module.slack_actions(FakeRequest()))

    assert response.status_code == 200
    assert remembered_context == [("C123", "111.222", "mlai.au", "write")]
    assert len(updated_messages) == 1
    updated_blocks = updated_messages[0]["blocks"]
    assert all(block.get("type") != "actions" for block in updated_blocks)
    assert "@Roo write about AI for clinic workflows" in updated_blocks[-1]["elements"][0]["text"]
