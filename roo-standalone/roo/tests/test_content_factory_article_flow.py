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


@pytest.fixture(autouse=True)
def reset_roo_pending_state():
    main_module._pending_intents.clear()
    main_module._pending_intents_by_job.clear()
    yield
    main_module._pending_intents.clear()
    main_module._pending_intents_by_job.clear()


class FakeContentFactoryClient:
    last_instance = None
    generic_integration = None
    domain_integrations = {}
    org_configs = {}
    auth_urls = {}
    saved_intents = []
    balance_by_user = {}
    user_profiles = {}
    attached_progress_messages = []
    still_working_calls = []
    integration_requests = []
    delivery_mode_calls = []
    trigger_article_generation_result = None

    def __init__(self, *args, **kwargs):
        self.trigger_calls = []
        self.repo_scan_calls = []
        self.status_checks = []
        self.user_registration_calls = []
        self.balance_checks = []
        self.confirm_calls = []
        FakeContentFactoryClient.last_instance = self

    async def get_integration(self, user_id, domain=None):
        FakeContentFactoryClient.integration_requests.append(
            {"user_id": user_id, "domain": domain}
        )
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
        delivery_mode=None,
        delivery_mode_confirmed=None,
        slack_channel_id=None,
        slack_thread_ts=None,
        progress_message_ts=None,
        client_request_id=None,
        request_source="roo_slackbot",
        user_email=None,
        user_first_name=None,
        user_last_name=None,
        user_avatar_url=None,
    ):
        self.trigger_calls.append(
            {
                "slack_user_id": slack_user_id,
                "domain": domain,
                "topic": topic,
                "target_keyword": target_keyword,
                "context": context,
                "delivery_mode": delivery_mode,
                "delivery_mode_confirmed": delivery_mode_confirmed,
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
                "progress_message_ts": progress_message_ts,
                "client_request_id": client_request_id,
                "request_source": request_source,
                "user_email": user_email,
                "user_first_name": user_first_name,
                "user_last_name": user_last_name,
                "user_avatar_url": user_avatar_url,
            }
        )
        return FakeContentFactoryClient.trigger_article_generation_result or {
            "job_id": "job-123",
            "workflow": "auto_discovery" if not topic else "direct_generate",
        }

    async def ensure_slack_user_registered(
        self,
        slack_id,
        email,
        first_name=None,
        last_name=None,
        avatar_url=None,
    ):
        payload = {
            "slack_id": slack_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "avatar_url": avatar_url,
        }
        self.user_registration_calls.append(payload)
        return {"user_id": 1, "slack_id": slack_id, "email": email, "created": False}

    async def get_balance(self, slack_user_id):
        self.balance_checks.append(slack_user_id)
        return {"balance": FakeContentFactoryClient.balance_by_user.get(slack_user_id, 12)}

    async def confirm_article_topic(self, *args, **kwargs):
        self.confirm_calls.append({"args": args, "kwargs": kwargs})
        return {"job_id": "confirmed-job-123", "status": "confirmed"}

    async def set_article_delivery_mode(self, job_id, delivery_mode, **kwargs):
        FakeContentFactoryClient.delivery_mode_calls.append(
            {"job_id": job_id, "delivery_mode": delivery_mode, **kwargs}
        )
        return {
            "job_id": job_id,
            "status": "queued",
            "delivery_mode": delivery_mode,
        }

    async def attach_content_progress_message(self, job_id, **kwargs):
        FakeContentFactoryClient.attached_progress_messages.append(
            {"job_id": job_id, **kwargs}
        )
        return {"status": "attached", "job_id": job_id}

    async def maybe_send_content_still_working(self, job_id, **kwargs):
        FakeContentFactoryClient.still_working_calls.append(
            {"job_id": job_id, **kwargs}
        )
        return {"status": "noop", "job_id": job_id}

    async def check_generation_status(self, job_id):
        self.status_checks.append(job_id)
        return {"status": "completed", "progress": 100, "current_step": "publishing"}

    async def publish_article(self, job_id, slack_user_id):
        return {
            "preview_url": "https://preview.test/article",
            "pr_url": "https://github.test/pr/123",
        }

    async def get_content_org_config(self, slack_user_id, domain=None):
        if domain is not None:
            return FakeContentFactoryClient.org_configs.get(domain)
        if len(FakeContentFactoryClient.org_configs) == 1:
            return next(iter(FakeContentFactoryClient.org_configs.values()))
        return None

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
    FakeContentFactoryClient.domain_integrations = {
        "mlai.au": default_integration,
        "woofya.com.au": {
            **default_integration,
            "connected_domains": [
                {
                    "domain": "woofya.com.au",
                    "github_repo": "MLAI-AUS-Inc/mlai-au",
                    "scanned": True,
                }
            ],
        },
    }
    FakeContentFactoryClient.auth_urls = {"default": "https://github.test/auth"}
    FakeContentFactoryClient.org_configs = {}
    FakeContentFactoryClient.saved_intents = []
    FakeContentFactoryClient.balance_by_user = {}
    FakeContentFactoryClient.attached_progress_messages = []
    FakeContentFactoryClient.still_working_calls = []
    FakeContentFactoryClient.user_profiles = {
        "U05QPB483K9": {
            "id": "U05QPB483K9",
            "email": "sam@example.com",
            "real_name": "Sam Donegan",
            "image_192": "https://avatar.test/sam.png",
        },
        "U999FREE": {
            "id": "U999FREE",
            "email": "new.user@example.com",
            "real_name": "New User",
            "image_192": "https://avatar.test/new-user.png",
        },
    }
    FakeContentFactoryClient.last_instance = None
    FakeContentFactoryClient.integration_requests = []
    FakeContentFactoryClient.delivery_mode_calls = []
    FakeContentFactoryClient.trigger_article_generation_result = None

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_user_info",
        lambda user_id: FakeContentFactoryClient.user_profiles.get(
            user_id,
            {
                "id": user_id,
                "email": "default@example.com",
                "real_name": "Default User",
                "image_192": "https://avatar.test/default.png",
            },
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
        user_id="U999FREE",
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
    FakeContentFactoryClient.domain_integrations["woofya.com.au"] = None
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
    button_value = json.loads(button["value"])
    assert button_value["text"] == "write an article for my website woofya.com.au"
    assert button_value["params"]["domain"] == "woofya.com.au"
    assert button_value["params"]["client_request_id"].startswith("content-factory-")
    assert button_value["channel_id"] == "C123"
    assert button_value["thread_ts"] == "111.222"


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

    assert isinstance(result, dict)
    assert "choose whether you want me to research" in result["message"].lower()
    assert len(posted_messages) == 1
    assert "Status Report" in posted_messages[0]["args"][1]
    assert "woofya.com.au" in posted_messages[0]["args"][1]
    assert "GitHub not connected" not in json.dumps(posted_messages)
    assert "borderline-main" not in json.dumps(posted_messages)
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.saved_intents == []


@pytest.mark.asyncio
async def test_requested_domain_bypasses_generic_multi_domain_error(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = {
        "error": "Multiple domains connected. Please specify which domain to use.",
        "requires_domain_selection": True,
        "recommended_next_action": "select_domain",
        "connected_domains": [
            {
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "scanned": True,
            },
            {
                "domain": "woofya.com.au",
                "github_repo": "Woofya/woofya-web",
                "scanned": True,
            },
        ],
    }
    FakeContentFactoryClient.domain_integrations["woofya.com.au"] = {
        "github_repo": "Woofya/woofya-web",
        "domain_github_repo": "Woofya/woofya-web",
        "project_scanned": True,
        "scan_completed": True,
        "content_research_ready": True,
        "last_scanned_at": "2026-03-21T09:58:00Z",
        "last_article": None,
        "recommended_next_action": None,
        "connected_domains": [
            {
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "scanned": True,
            },
            {
                "domain": "woofya.com.au",
                "github_repo": "Woofya/woofya-web",
                "scanned": True,
            },
        ],
        "selected_domain": "woofya.com.au",
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for woofya.com.au",
        params={"domain": "woofya.com.au", "topic": "dog grooming tips"},
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "re-connect your GitHub account" not in result
    assert FakeContentFactoryClient.integration_requests[0] == {
        "user_id": "U05QPB483K9",
        "domain": "woofya.com.au",
    }
    assert FakeContentFactoryClient.last_instance.trigger_calls
    trigger_call = FakeContentFactoryClient.last_instance.trigger_calls[0]
    assert trigger_call["domain"] == "woofya.com.au"
    assert trigger_call["topic"] == "dog grooming tips"
    assert posted_messages
    assert "Status Report" in posted_messages[0]["args"][1]


@pytest.mark.asyncio
async def test_explicit_content_only_request_without_repo_starts_generation(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = None
    FakeContentFactoryClient.domain_integrations = {}
    FakeContentFactoryClient.org_configs["livestockmerchant.com.au"] = {
        "domain": "livestockmerchant.com.au",
        "github_repo": None,
        "article_delivery_mode": None,
        "scan_summary": None,
        "article_system": {},
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write a content-only article for livestockmerchant.com.au about cattle export trends",
        params={
            "domain": "livestockmerchant.com.au",
            "topic": "cattle export trends",
            "confirmed": True,
        },
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert isinstance(result, dict)
    assert result["data"]["content_factory_progress_job_id"] == "job-123"
    trigger_call = FakeContentFactoryClient.last_instance.trigger_calls[0]
    assert trigger_call["domain"] == "livestockmerchant.com.au"
    assert trigger_call["delivery_mode"] == "content_only"
    assert trigger_call["delivery_mode_confirmed"] is True
    assert "GitHub not connected" not in json.dumps(posted_messages)


@pytest.mark.asyncio
async def test_repo_less_request_awaits_delivery_mode_instead_of_showing_github_cta(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.generic_integration = None
    FakeContentFactoryClient.domain_integrations = {}
    FakeContentFactoryClient.org_configs["livestockmerchant.com.au"] = {
        "domain": "livestockmerchant.com.au",
        "github_repo": None,
        "article_delivery_mode": None,
        "scan_summary": None,
        "article_system": {},
    }
    FakeContentFactoryClient.trigger_article_generation_result = {
        "job_id": "job-awaiting-mode-1",
        "status": "awaiting_delivery_mode",
        "workflow": "direct_generate",
        "recommended_delivery_mode": "content_only",
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="write an article for livestockmerchant.com.au about cattle export trends",
        params={
            "domain": "livestockmerchant.com.au",
            "topic": "cattle export trends",
            "confirmed": True,
        },
        user_id="U05QPB483K9",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert isinstance(result, dict)
    assert "choose the delivery mode" in result["message"].lower()
    assert result["data"]["content_factory_progress_job_id"] == "job-awaiting-mode-1"
    action_ids = [element["action_id"] for element in result["blocks"][1]["elements"]]
    assert action_ids == ["select_article_delivery_mode", "select_article_delivery_mode"]
    assert "GitHub not connected" not in json.dumps(posted_messages)


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

    assert isinstance(result, dict)
    assert "choose whether you want me to research" in result["message"].lower()
    assert len(posted_messages) == 1
    assert "Status Report" in posted_messages[0]["args"][1]
    assert "woofya.com.au" in posted_messages[0]["args"][1]
    assert "Please select a repository" not in json.dumps(posted_messages)
    assert "borderline-main" not in json.dumps(posted_messages)
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.saved_intents == []


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
async def test_natural_language_content_factory_scan_phrase_prompts_for_confirmation(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.domain_integrations["woofya.com.au"] = {
        "github_repo": "Woofya/woofya-web",
        "domain_github_repo": "Woofya/woofya-web",
        "project_scanned": True,
        "last_scanned_at": "2026-03-21T09:58:00Z",
        "last_article": None,
        "recommended_next_action": None,
        "connected_domains": [
            {
                "domain": "woofya.com.au",
                "github_repo": "Woofya/woofya-web",
                "scanned": True,
            }
        ],
    }
    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append({"args": args, "kwargs": kwargs}) or {"ts": "111.222"},
    )

    result = await executor._execute_content_factory(
        skill=None,
        text="scan the repo for the domain woofya.com.au",
        params={"domain": "woofya.com.au"},
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

    assert isinstance(result, dict)
    assert result["message"] == "Starting Content Factory for mlai.au. I'll keep this message updated as the run moves forward."
    assert result["data"]["content_factory_progress_job_id"] == "job-123"
    assert result["data"]["content_factory_watchdog"] is True
    assert "Starting discovery to find the best article opportunity" in result["blocks"][0]["text"]["text"]
    assert created_tasks == []
    assert len(posted_messages) == 1
    trigger_call = FakeContentFactoryClient.last_instance.trigger_calls[0]
    assert trigger_call["slack_user_id"] == "U05QPB483K9"
    assert trigger_call["domain"] == "mlai.au"
    assert trigger_call["topic"] is None
    assert trigger_call["target_keyword"] == ""
    assert trigger_call["context"] == "research the best article for mlai.au"
    assert trigger_call["slack_channel_id"] == "C123"
    assert trigger_call["slack_thread_ts"] == "111.222"
    assert trigger_call["request_source"] == "roo_slackbot"
    assert trigger_call["client_request_id"].startswith("content-factory-")
    assert trigger_call["user_email"] == "sam@example.com"
    assert FakeContentFactoryClient.last_instance.balance_checks == []


@pytest.mark.asyncio
async def test_content_factory_blocks_when_user_has_insufficient_points(monkeypatch):
    executor = SkillExecutor()
    _patch_content_factory(monkeypatch)
    FakeContentFactoryClient.balance_by_user["U999FREE"] = 5
    monkeypatch.setattr(executor_module, "post_message", lambda *args, **kwargs: {"ts": "111.222"})

    result = await executor._execute_content_factory(
        skill=None,
        text="research the best article for woofya.com.au",
        params={"domain": "woofya.com.au"},
        user_id="U999FREE",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "Creating an article costs 6 Roo points" in result
    assert "currently have 5" in result
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.last_instance.balance_checks == ["U999FREE"]


@pytest.mark.asyncio
async def test_content_factory_blocks_when_slack_email_missing(monkeypatch):
    executor = SkillExecutor()
    _patch_content_factory(monkeypatch)
    monkeypatch.setattr(executor_module, "post_message", lambda *args, **kwargs: {"ts": "111.222"})
    FakeContentFactoryClient.user_profiles["U_NO_EMAIL"] = {
        "id": "U_NO_EMAIL",
        "email": "",
        "real_name": "No Email",
        "image_192": "",
    }

    result = await executor._execute_content_factory(
        skill=None,
        text="research the best article for mlai.au",
        params={"domain": "mlai.au"},
        user_id="U_NO_EMAIL",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "real Slack email" in result
    assert "articles for mlai.au are free" in result.lower()
    assert FakeContentFactoryClient.last_instance.trigger_calls == []
    assert FakeContentFactoryClient.last_instance.user_registration_calls == []


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

    assert isinstance(result, dict)
    assert result["data"]["content_factory_progress_job_id"] == "job-123"
    assert "Starting article generation for `AI for clinic workflows`" in result["blocks"][0]["text"]["text"]
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

    assert isinstance(result, dict)
    assert result["data"]["content_factory_progress_job_id"] == "job-123"
    assert "Starting article generation for `AI for clinic workflows`" in result["blocks"][0]["text"]["text"]
    assert len(created_tasks) == 0
    assert len(posted_messages) == 1
    trigger_call = FakeContentFactoryClient.last_instance.trigger_calls[0]
    assert trigger_call["slack_user_id"] == "U05QPB483K9"
    assert trigger_call["domain"] == "mlai.au"
    assert trigger_call["topic"] == "AI for clinic workflows"
    assert trigger_call["target_keyword"] == ""
    assert trigger_call["context"] == "write an article for mlai.au about AI for clinic workflows"
    assert trigger_call["slack_channel_id"] == "C123"
    assert trigger_call["slack_thread_ts"] == "111.222"
    assert trigger_call["request_source"] == "roo_slackbot"
    assert trigger_call["client_request_id"].startswith("content-factory-")
    assert trigger_call["user_email"] == "sam@example.com"


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
        lambda: SimpleNamespace(
            default_llm_provider="openai",
            SKILLS_DIR="skills",
            ROO_API_KEY="roo-api-key",
            MLAI_API_KEY="api-key",
        ),
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
    assert "free" in updated_blocks[-1]["elements"][0]["text"].lower()


def test_article_research_best_action_triggers_backend_with_client_request_id(monkeypatch):
    trigger_calls = []
    updated_messages = []
    remembered_context = []
    posted_messages = []
    scheduled_job_ids = []

    class FakeTriggerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def trigger_article_generation(self, **kwargs):
            trigger_calls.append(kwargs)
            return {"job_id": "job-123"}

    async def fake_watchdog(job_id):
        scheduled_job_ids.append(job_id)

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeTriggerClient)
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_user_info",
        lambda user_id: {
            "id": user_id,
            "email": "sam@example.com",
            "real_name": "Sam Donegan",
            "image_192": "https://avatar.test/sam.png",
        },
    )
    monkeypatch.setattr(
        main_module,
        "_remember_content_thread_context",
        lambda channel_id, thread_ts, domain, workflow: remembered_context.append(
            (channel_id, thread_ts, domain, workflow)
        ),
    )
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", fake_watchdog)
    monkeypatch.setattr(main_module, "post_message", lambda *args, **kwargs: posted_messages.append((args, kwargs)))
    monkeypatch.setattr(asyncio, "create_task", capture_task)

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
                "action_id": "article_research_best",
                "value": json.dumps(
                    {
                        "domain": "mlai.au",
                        "slack_user_id": "U05QPB483K9",
                        "channel_id": "C123",
                        "thread_ts": "111.222",
                        "client_request_id": "content-factory-request-123",
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
    assert remembered_context == [("C123", "111.222", "mlai.au", "research")]
    assert len(updated_messages) == 1
    assert posted_messages == []
    assert trigger_calls == [
        {
            "slack_user_id": "U05QPB483K9",
            "domain": "mlai.au",
            "delivery_mode": None,
            "delivery_mode_confirmed": None,
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
            "progress_message_ts": "111.222",
            "client_request_id": "content-factory-request-123",
            "request_source": "roo_slackbot",
            "user_email": "sam@example.com",
            "user_first_name": "Sam",
            "user_last_name": "Donegan",
            "user_avatar_url": "https://avatar.test/sam.png",
        }
    ]
    assert scheduled_job_ids == ["job-123"]


def test_article_research_best_action_preserves_explicit_delivery_mode(monkeypatch):
    trigger_calls = []
    updated_messages = []

    class FakeTriggerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def trigger_article_generation(self, **kwargs):
            trigger_calls.append(kwargs)
            return {"job_id": "job-123"}

    def capture_task(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeTriggerClient)
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_user_info",
        lambda user_id: {
            "id": user_id,
            "email": "sam@example.com",
            "real_name": "Sam Donegan",
            "image_192": "https://avatar.test/sam.png",
        },
    )
    monkeypatch.setattr(main_module, "_remember_content_thread_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", lambda job_id: None)
    monkeypatch.setattr(main_module, "post_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(asyncio, "create_task", capture_task)

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
                "action_id": "article_research_best",
                "value": json.dumps(
                    {
                        "domain": "mlai.au",
                        "slack_user_id": "U05QPB483K9",
                        "channel_id": "C123",
                        "thread_ts": "111.222",
                        "client_request_id": "content-factory-request-123",
                        "delivery_mode": "content_only",
                        "delivery_mode_confirmed": True,
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
    assert trigger_calls[0]["delivery_mode"] == "content_only"
    assert trigger_calls[0]["delivery_mode_confirmed"] is True


def test_select_article_delivery_mode_action_queues_run(monkeypatch):
    delivery_mode_calls = []
    scheduled_job_ids = []

    class FakeTriggerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def set_article_delivery_mode(self, job_id, delivery_mode, **kwargs):
            delivery_mode_calls.append(
                {"job_id": job_id, "delivery_mode": delivery_mode, **kwargs}
            )
            return {"job_id": job_id, "status": "queued", "delivery_mode": delivery_mode}

    async def fake_watchdog(job_id):
        scheduled_job_ids.append(job_id)

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeTriggerClient)
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", fake_watchdog)
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Choose delivery mode",
        },
        "actions": [
            {
                "action_id": "select_article_delivery_mode",
                "value": json.dumps(
                    {
                        "job_id": "job-awaiting-mode-1",
                        "domain": "mlai.au",
                        "delivery_mode": "content_only",
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
    body = json.loads(response.body.decode())
    assert body["replace_original"] is True
    assert delivery_mode_calls == [
        {
            "job_id": "job-awaiting-mode-1",
            "delivery_mode": "content_only",
            "request_source": "roo_slackbot",
        }
    ]
    assert scheduled_job_ids == ["job-awaiting-mode-1"]


def test_write_first_article_action_generates_client_request_id_when_missing(monkeypatch):
    trigger_calls = []
    updated_messages = []
    posted_messages = []
    scheduled_job_ids = []

    class FakeTriggerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def trigger_article_generation(self, **kwargs):
            trigger_calls.append(kwargs)
            return {"job_id": "job-123"}

    async def fake_watchdog(job_id):
        scheduled_job_ids.append(job_id)

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeTriggerClient)
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_user_info",
        lambda user_id: {
            "id": user_id,
            "email": "sam@example.com",
            "real_name": "Sam Donegan",
            "image_192": "https://avatar.test/sam.png",
        },
    )
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", fake_watchdog)
    monkeypatch.setattr(main_module, "post_message", lambda *args, **kwargs: posted_messages.append((args, kwargs)))
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Ready to write your first article?",
        },
        "actions": [
            {
                "action_id": "write_first_article",
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
    assert len(updated_messages) == 1
    assert posted_messages == []
    assert len(trigger_calls) == 1
    assert trigger_calls[0]["slack_user_id"] == "U05QPB483K9"
    assert trigger_calls[0]["domain"] == "mlai.au"
    assert trigger_calls[0]["client_request_id"].startswith("content-factory-")
    assert trigger_calls[0]["request_source"] == "roo_slackbot"
    assert scheduled_job_ids == ["job-123"]


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
            ROO_API_KEY="roo-api-key",
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


def test_prerequisite_scan_action_stores_pending_intent_after_accepted(monkeypatch):
    updated_messages = []

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
            return {
                "status": "scan_initiated",
                "job_id": "scan-job-123",
                "domain": domain,
            }

        async def get_github_auth_url(self, user_id, domain=None):
            return {"auth_url": "https://github.test/auth"}

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeScanTriggerClient)
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: SimpleNamespace(chat_update=lambda **kwargs: updated_messages.append(kwargs)),
    )
    monkeypatch.setattr(main_module, "post_message", lambda *args, **kwargs: None)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Scan codebase first",
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
                        "original_intent": {
                            "action": "write",
                            "topic": "AI for clinic workflows",
                            "target_keyword": "clinic ai",
                        },
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
    pending = main_module._pending_intents["U05QPB483K9:mlai.au"]
    assert pending["action"] == "write"
    assert pending["topic"] == "AI for clinic workflows"
    assert pending["wait_for"] == "scan_complete"
    assert pending["job_id"] == "scan-job-123"
    assert pending["channel_id"] == "C123"
    assert pending["thread_ts"] == "111.222"
    assert len(updated_messages) == 1


def test_generation_failed_callback_clears_pending_intent_for_failed_stage(monkeypatch):
    posted_messages = []

    main_module._remember_pending_intent(
        "U05QPB483K9",
        "mlai.au",
        intent_data={"action": "write", "topic": "AI for clinic workflows"},
        channel_id="C123",
        thread_ts="111.222",
        wait_for="scan_complete",
        job_id="scan-job-123",
    )

    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda *args, **kwargs: posted_messages.append((args, kwargs)),
    )

    payload = {
        "event_type": "generation_failed",
        "slack_user_id": "U05QPB483K9",
        "job_id": "scan-job-123",
        "domain": "mlai.au",
        "channel_id": "C123",
        "thread_ts": "111.222",
        "stage": "scan",
        "error_message": "Scan exploded",
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = asyncio.run(main_module.content_factory_callback(FakeRequest()))

    assert response == {"status": "ok"}
    assert main_module._pending_intents == {}
    assert main_module._pending_intents_by_job == {}
    assert posted_messages


def test_scan_complete_auto_write_clears_pending_when_scaffold_already_exists(monkeypatch):
    posted_messages = []
    trigger_calls = []
    scheduled_job_ids = []

    main_module._remember_pending_intent(
        "U05QPB483K9",
        "mlai.au",
        intent_data={
            "action": "write",
            "topic": "AI for clinic workflows",
            "target_keyword": "clinic ai",
        },
        channel_id="C123",
        thread_ts="111.222",
        wait_for="scan_complete",
    )

    class FakeAutoContinueClient:
        def __init__(self, *args, **kwargs):
            pass

        async def scaffold_articles(self, **kwargs):
            return {
                "status_code": 200,
                "data": {"pr_url": "https://github.test/pr/123"},
            }

        async def trigger_article_generation(self, **kwargs):
            trigger_calls.append(kwargs)
            return {"job_id": "article-job-123"}

    async def fake_watchdog(job_id):
        scheduled_job_ids.append(job_id)

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAutoContinueClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda *args, **kwargs: (
            posted_messages.append((args, kwargs)) or {"ts": "live-status-001"}
        ),
    )
    monkeypatch.setattr(
        slack_client_module,
        "get_user_info",
        lambda user_id: {
            "id": user_id,
            "email": "sam@example.com",
            "real_name": "Sam Donegan",
            "image_192": "https://avatar.test/sam.png",
        },
    )
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", fake_watchdog)
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "event_type": "scan_complete",
        "slack_user_id": "U05QPB483K9",
        "job_id": "scan-job-123",
        "domain": "mlai.au",
        "channel_id": "C123",
        "thread_ts": "111.222",
        "components_count": 3,
        "component_names": ["ArticleHero", "ArticleCard", "ArticleFAQ"],
        "pillar_count": 1,
        "pillar_names": ["SEO"],
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = asyncio.run(main_module.content_factory_callback(FakeRequest()))

    assert response == {"status": "ok"}
    assert len(trigger_calls) == 1
    assert trigger_calls[0]["slack_user_id"] == "U05QPB483K9"
    assert trigger_calls[0]["domain"] == "mlai.au"
    assert trigger_calls[0]["topic"] == "AI for clinic workflows"
    assert trigger_calls[0]["target_keyword"] == "clinic ai"
    assert trigger_calls[0]["progress_message_ts"] == "live-status-001"
    assert trigger_calls[0]["request_source"] == "roo_slackbot"
    assert trigger_calls[0]["user_email"] == "sam@example.com"
    assert scheduled_job_ids == ["article-job-123"]
    assert main_module._pending_intents == {}
    assert posted_messages


def test_confirm_content_factory_action_resumes_original_request(monkeypatch):
    resumed_events = []
    posted_messages = []

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    async def fake_handle_mention(event):
        resumed_events.append(event)

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
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
    assert resumed_events == [
        {
            "user": "U05QPB483K9",
            "text": "write an article for my website woofya.com.au",
            "channel": "C123",
            "thread_ts": "111.222",
            "param_overrides": {
                "domain": "woofya.com.au",
                "confirmed": True,
            },
        }
    ]


def test_confirm_topic_action_uses_roo_request_source_and_mentions_no_extra_charge(monkeypatch):
    confirm_calls = []
    scheduled_job_ids = []

    async def fake_watchdog(job_id):
        scheduled_job_ids.append(job_id)

    def capture_task(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
        return SimpleNamespace(cancel=lambda: None)

    class FakeConfirmClient:
        def __init__(self, *args, **kwargs):
            pass

        async def confirm_article_topic(self, **kwargs):
            confirm_calls.append(kwargs)
            return {"job_id": "job-123", "status": "confirmed"}

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeConfirmClient)
    monkeypatch.setattr(main_module, "_watch_content_factory_quiet_run", fake_watchdog)
    monkeypatch.setattr(asyncio, "create_task", capture_task)

    payload = {
        "user": {"id": "U05QPB483K9"},
        "channel": {"id": "C123"},
        "message": {
            "ts": "111.222",
            "thread_ts": "111.222",
            "text": "Choose a topic",
        },
        "actions": [
            {
                "action_id": "confirm_topic",
                "value": "confirm:ai agents:job-123",
            }
        ],
    }

    class FakeRequest:
        async def form(self):
            return {"payload": json.dumps(payload)}

    response = asyncio.run(main_module.slack_actions(FakeRequest()))
    body = json.loads(response.body)

    assert response.status_code == 200
    assert confirm_calls == [
        {
            "job_id": "job-123",
            "slack_user_id": "U05QPB483K9",
            "confirmed_keyword": "ai agents",
            "request_source": "roo_slackbot",
        }
    ]
    assert scheduled_job_ids == ["job-123"]
    assert "No additional Roo points will be charged" in body["text"]


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
