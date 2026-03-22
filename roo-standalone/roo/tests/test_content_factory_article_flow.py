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
    generic_integration = None
    domain_integrations = {}
    auth_urls = {}
    saved_intents = []

    def __init__(self, *args, **kwargs):
        self.trigger_calls = []
        self.repo_scan_calls = []
        self.status_checks = []
        FakeContentFactoryClient.last_instance = self

    async def get_integration(self, user_id, domain=None):
        if domain is not None:
            return FakeContentFactoryClient.domain_integrations.get(domain)
        return FakeContentFactoryClient.generic_integration

    async def get_github_auth_url(self, user_id, domain=None):
        return {
            "auth_url": FakeContentFactoryClient.auth_urls.get(
                domain,
                FakeContentFactoryClient.auth_urls.get("default", "https://github.test/auth"),
            )
        }

    async def save_pending_intent(self, slack_user_id, intent_data):
        FakeContentFactoryClient.saved_intents.append(
            {
                "slack_user_id": slack_user_id,
                "intent_data": json.loads(intent_data),
            }
        )

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

    async def trigger_repo_scan(
        self,
        slack_user_id,
        slack_channel_id=None,
        slack_thread_ts=None,
        domain=None,
    ):
        self.repo_scan_calls.append(
            {
                "slack_user_id": slack_user_id,
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
                "domain": domain,
            }
        )
        return {"status": "accepted", "message": "Scan queued successfully."}


def _patch_content_factory(monkeypatch):
    default_integration = {
        "github_repo": "MLAI-AUS-Inc/mlai-au",
        "domain_github_repo": "MLAI-AUS-Inc/mlai-au",
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
    FakeContentFactoryClient.generic_integration = default_integration
    FakeContentFactoryClient.domain_integrations = {"mlai.au": default_integration}
    FakeContentFactoryClient.auth_urls = {"default": "https://github.test/auth"}
    FakeContentFactoryClient.saved_intents = []
    FakeContentFactoryClient.last_instance = None

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
async def test_requirements_prompt_serializes_original_request_for_resume(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = None
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for my website woofya.com.au",
        params={"domain": "woofya.com.au"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result == "Please review the requirements above to get started! 👆"
    button = posted_messages[0]["kwargs"]["blocks"][2]["elements"][0]
    assert button["action_id"] == "confirm_content_factory"
    assert json.loads(button["value"]) == {
        "text": "write an article for my website woofya.com.au",
        "params": {"domain": "woofya.com.au"},
        "channel_id": "C123",
        "thread_ts": "111.222",
    }


@pytest.mark.asyncio
async def test_new_domain_requests_domain_github_auth_without_falling_back_to_generic_repo(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = {
        "github_repo": "drsamdonegan/borderline-main",
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
    FakeContentFactoryClient.domain_integrations["woofya.com.au"] = {
        "project_scanned": False,
        "last_scanned_at": "Never",
        "last_article": None,
        "recommended_next_action": None,
        "connected_domains": [
            {
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "scanned": True,
            }
        ],
        "needs_github_auth": True,
        "oauth_url": "https://github.test/auth/woofya",
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for my website woofya.com.au",
        params={"domain": "woofya.com.au"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "GitHub isn't connected for woofya.com.au" in result
    assert len(posted_messages) == 1
    assert posted_messages[0]["args"][1] == "GitHub not connected for woofya.com.au"
    assert "woofya.com.au" in posted_messages[0]["kwargs"]["blocks"][0]["text"]["text"]
    assert "Status Report" not in json.dumps(posted_messages)
    assert "borderline-main" not in json.dumps(posted_messages)
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.saved_intents == [
        {
            "slack_user_id": "U05QPB483K9",
            "intent_data": {
                "skill": "content-factory",
                "params": {"domain": "woofya.com.au"},
                "text": "write an article for my website woofya.com.au",
                "channel": "C123",
                "ts": "111.222",
            },
        }
    ]


@pytest.mark.asyncio
async def test_new_domain_requires_repo_selection_without_falling_back_to_generic_repo(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = {
        "github_repo": "drsamdonegan/borderline-main",
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
    FakeContentFactoryClient.domain_integrations["woofya.com.au"] = {
        "project_scanned": False,
        "last_scanned_at": "Never",
        "last_article": None,
        "recommended_next_action": None,
        "connected_domains": [
            {
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "scanned": True,
            }
        ],
        "needs_github_auth": False,
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for my website woofya.com.au",
        params={"domain": "woofya.com.au"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result == "Please choose a repository to use with the Content Factory. 🔌"
    assert len(posted_messages) == 1
    assert posted_messages[0]["args"][1] == "Please select a repository"
    assert "woofya.com.au" in posted_messages[0]["kwargs"]["blocks"][0]["text"]["text"]
    assert "Status Report" not in json.dumps(posted_messages)
    assert "borderline-main" not in json.dumps(posted_messages)
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.saved_intents == [
        {
            "slack_user_id": "U05QPB483K9",
            "intent_data": {
                "skill": "content-factory",
                "params": {"domain": "woofya.com.au"},
                "text": "write an article for my website woofya.com.au",
                "channel": "C123",
                "ts": "111.222",
            },
        }
    ]


@pytest.mark.asyncio
async def test_execute_applies_param_overrides_after_extraction(monkeypatch):
    executor = SkillExecutor()
    captured_params = {}

    async def fake_extract(skill, text, user_id, history=None):
        return {"domain": "wrong-domain.com", "topic": "Original topic"}

    async def fake_content_factory(skill, text, params, user_id, channel_id, thread_ts, thread_history=None):
        captured_params.update(params)
        return "ok"

    monkeypatch.setattr(executor, "_extract_parameters", fake_extract)
    monkeypatch.setattr(executor, "_execute_content_factory", fake_content_factory)

    result = await executor.execute(
        skill=SimpleNamespace(name="content-factory"),
        text="write an article for woofya.com.au",
        user_id="U05QPB483K9",
        param_overrides={"domain": "woofya.com.au", "confirmed": True},
    )

    assert result.success is True
    assert captured_params == {
        "domain": "woofya.com.au",
        "topic": "Original topic",
        "confirmed": True,
    }


@pytest.mark.asyncio
async def test_explicit_content_factory_scan_with_existing_scan_prompts_for_confirmation(monkeypatch):
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
        text="scan mlai.au",
        params={"domain": "mlai.au", "action": "scan"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert isinstance(result, dict)
    assert "run another scan anyway" in result["message"].lower()
    assert posted_messages == []
    buttons = result["blocks"][1]["elements"]
    assert buttons[0]["action_id"] == "prerequisite_scan"
    assert buttons[0]["text"]["text"] == "Scan Again"
    assert json.loads(buttons[0]["value"]) == {
        "domain": "mlai.au",
        "slack_user_id": "U05QPB483K9",
        "channel_id": "C123",
        "thread_ts": "111.222",
        "rescan": True,
    }
    assert buttons[1]["action_id"] == "prerequisite_cancel"
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.last_instance.repo_scan_calls == []


@pytest.mark.asyncio
async def test_explicit_github_scan_with_existing_scan_prompts_for_confirmation(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_github_integration(
        skill=None,
        text="scan mlai.au",
        params={"domain": "mlai.au", "action": "scan"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert isinstance(result, dict)
    assert "run another scan anyway" in result["message"].lower()
    assert posted_messages == []
    assert result["blocks"][1]["elements"][0]["action_id"] == "prerequisite_scan"
    assert FakeContentFactoryClient.last_instance.repo_scan_calls == []


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


def test_prerequisite_scan_action_triggers_backend_from_repeat_scan_prompt(monkeypatch):
    trigger_calls = []
    updated_messages = []
    posted_messages = []

    class FakeScanTriggerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def trigger_repo_scan(
            self,
            slack_user_id,
            slack_channel_id=None,
            slack_thread_ts=None,
            domain=None,
        ):
            trigger_calls.append(
                {
                    "slack_user_id": slack_user_id,
                    "slack_channel_id": slack_channel_id,
                    "slack_thread_ts": slack_thread_ts,
                    "domain": domain,
                }
            )
            return {"status": "accepted", "message": "Scan queued successfully."}

        async def get_github_auth_url(self, user_id, domain=None):
            return {"auth_url": "https://github.test/auth"}

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeScanTriggerClient)
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "I already have a scan for mlai.au. Do you want me to run another scan anyway?",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Existing scan found."}},
                {"type": "actions", "elements": []},
            ],
        },
        "actions": [
            {
                "action_id": "prerequisite_scan",
                "value": json.dumps(
                    {
                        "domain": "mlai.au",
                        "slack_user_id": "U05QPB483K9",
                        "channel_id": "C123",
                        "thread_ts": "111.222",
                        "rescan": True,
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
    assert trigger_calls == [
        {
            "slack_user_id": "U05QPB483K9",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
            "domain": "mlai.au",
        }
    ]
    assert posted_messages == []
    assert len(updated_messages) == 1
    assert updated_messages[0]["blocks"][-1]["elements"][0]["text"] == "✅ Scanning codebase..."


def test_confirm_content_factory_action_resumes_original_request(monkeypatch):
    agent_calls = []
    posted_messages = []

    def capture_task(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    class FakeAgent:
        def handle_mention(self, **kwargs):
            agent_calls.append(kwargs)

            async def done():
                return {}

            return done()

    monkeypatch.setattr(main_module, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(main_module, "post_message", lambda *args, **kwargs: posted_messages.append((args, kwargs)))
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Please review the requirements above.",
        },
        "actions": [
            {
                "action_id": "confirm_content_factory",
                "value": json.dumps(
                    {
                        "text": "write an article for my website woofya.com.au",
                        "params": {"domain": "woofya.com.au"},
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
    assert posted_messages == []
    assert agent_calls == [
        {
            "text": "write an article for my website woofya.com.au",
            "user_id": "U05QPB483K9",
            "channel_id": "C123",
            "thread_ts": "111.222",
            "param_overrides": {
                "domain": "woofya.com.au",
                "confirmed": True,
            },
        }
    ]


def test_confirm_content_factory_action_without_context_prompts_to_resend(monkeypatch):
    agent_calls = []
    posted_messages = []

    def capture_task(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    class FakeAgent:
        def handle_mention(self, **kwargs):
            agent_calls.append(kwargs)

            async def done():
                return {}

            return done()

    monkeypatch.setattr(main_module, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Please review the requirements above.",
        },
        "actions": [
            {
                "action_id": "confirm_content_factory",
            }
        ],
    }

    class FakeRequest:
        async def form(self):
            return {"payload": json.dumps(payload)}

    response = asyncio.run(main_module.slack_actions(FakeRequest()))

    assert response.status_code == 200
    assert agent_calls == []
    assert posted_messages == [
        (
            (
                "C123",
                "I couldn't recover your original content request. Please resend the article request so I can continue.",
            ),
            {"thread_ts": "111.222"},
        )
    ]
