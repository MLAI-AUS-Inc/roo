import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

executor_module = importlib.import_module("roo.skills.executor")
backend_module = importlib.import_module("roo.clients.mlai_backend")
SkillExecutor = executor_module.SkillExecutor


@pytest.fixture(autouse=True)
def _roo_bot_identity(monkeypatch):
    monkeypatch.setattr(executor_module, "get_bot_user_id", lambda: "UROO")


def _settings(**overrides):
    values = {
        "MLAI_BACKEND_URL": "https://backend.test",
        "MLAI_API_KEY": "api-key",
        "ROO_API_KEY": "roo-api-key",
        "INTERNAL_API_KEY": "internal-key",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeDataBackendClient:
    catalog = {"resources": [{"key": "vibe_raising_companies", "operations": ["list", "count"], "fields": ["name", "domain"]}]}
    query_response = {
        "resource": "vibe_raising_companies",
        "rows": [{"count": 3}],
        "returned_count": 1,
        "limit": 1,
        "offset": 0,
        "has_more": False,
    }
    catalog_requesters = []
    queries = []
    linear_list_calls = []
    linear_list_responses = []
    linear_detail_calls = []
    linear_list_response = {
        "list": {"displayName": "MLAI_TECH · Todo"},
        "issues": [
            {
                "identifier": "TECH-16",
                "title": "[TECH_TEAM] Refresh volunteer onboarding",
                "url": "https://linear.app/mlai-aus/issue/TECH-16/refresh-volunteer-onboarding",
            },
            {
                "identifier": "TECH-19",
                "title": "[TECH_TEAM] Repair the deployment alerts",
                "url": "https://linear.app/mlai-aus/issue/TECH-19/repair-the-deployment-alerts",
            },
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
    linear_detail_response = {
        "issue": {
            "identifier": "TECH-16",
            "title": "[TECH_TEAM] Refresh volunteer onboarding",
            "description": "Document the <new> onboarding path & owners.",
            "url": "https://linear.app/mlai-aus/issue/TECH-16/refresh-volunteer-onboarding",
            "state": {"name": "Todo"},
            "assignee": {"displayName": "Alex"},
            "priorityLabel": "High",
            "attachments": [
                {"title": "Current guide", "url": "https://example.com/guide"}
            ],
        },
        "comments": [
            {
                "body": "Please include the Slack welcome flow.",
                "createdAt": "2026-08-27T01:02:03Z",
                "user": {"displayName": "Morgan"},
            }
        ],
        "commentsTruncated": False,
    }
    last_init = None

    def __init__(self, *args, **kwargs):
        self.__class__.last_init = {"args": args, "kwargs": kwargs}

    async def get_data_catalog(self, requester_slack_id):
        self.__class__.catalog_requesters.append(requester_slack_id)
        return self.catalog

    async def query_data(self, payload):
        self.__class__.queries.append(payload)
        return self.query_response

    async def list_linear_channel_issues(self, **kwargs):
        self.__class__.linear_list_calls.append(kwargs)
        if self.__class__.linear_list_responses:
            return self.__class__.linear_list_responses.pop(0)
        return self.linear_list_response

    async def get_linear_channel_issue(self, **kwargs):
        self.__class__.linear_detail_calls.append(kwargs)
        return self.linear_detail_response


def _reset_fake_client():
    FakeDataBackendClient.catalog = {
        "resources": [
            {
                "key": "vibe_raising_companies",
                "operations": ["list", "count"],
                "fields": ["name", "domain"],
            }
        ]
    }
    FakeDataBackendClient.query_response = {
        "resource": "vibe_raising_companies",
        "rows": [{"count": 3}],
        "returned_count": 1,
        "limit": 1,
        "offset": 0,
        "has_more": False,
    }
    FakeDataBackendClient.catalog_requesters = []
    FakeDataBackendClient.queries = []
    FakeDataBackendClient.linear_list_calls = []
    FakeDataBackendClient.linear_list_responses = []
    FakeDataBackendClient.linear_detail_calls = []
    FakeDataBackendClient.last_init = None


@pytest.mark.asyncio
async def test_linear_channel_issue_list_returns_titles_first(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="what Linear issues are in the MLAI_TECH Todo list at the moment?",
        params={"action": "list_linear_channel_issues"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
    )

    assert FakeDataBackendClient.linear_list_calls == [
        {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
            "requester_slack_id": "U123",
            "limit": 50,
        }
    ]
    assert FakeDataBackendClient.linear_detail_calls == []
    assert "*2 issues in MLAI_TECH · Todo*" in result["message"]
    assert "1. <https://linear.app/mlai-aus/issue/TECH-16/" in result["message"]
    assert "Refresh volunteer onboarding" in result["message"]
    assert "[TECH_TEAM]" not in result["message"]


@pytest.mark.asyncio
async def test_linear_channel_issue_detail_by_identifier(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="tell me more about TECH-16",
        params={"action": "get_linear_channel_issue"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
    )

    assert FakeDataBackendClient.linear_list_calls == []
    assert FakeDataBackendClient.linear_detail_calls == [
        {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
            "requester_slack_id": "U123",
            "issue_identifier": "TECH-16",
            "include_comments": True,
        }
    ]
    assert "*Status:* Todo" in result["message"]
    assert "Document the &lt;new&gt; onboarding path &amp; owners." in result["message"]
    assert "*Comments — 1*" in result["message"]
    assert "Please include the Slack welcome flow." in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["show me number 2", "2", "#2"])
async def test_linear_channel_issue_detail_resolves_number_from_thread(monkeypatch, text):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text=text,
        params={"action": "query"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
        thread_history=[
            {
                "is_bot": True,
                "user": "UROO",
                "text": "1. <https://linear.app/x|TECH-16> — First\n"
                "2. <https://linear.app/y|TECH-19> — Second",
            }
        ],
    )

    assert FakeDataBackendClient.linear_detail_calls[0]["issue_identifier"] == "TECH-19"
    assert FakeDataBackendClient.linear_list_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "show comments",
        "description",
        "who owns?",
        "status?",
        "show priority",
        "attachments?",
        "labels?",
        "due date",
        "relations",
        "tell me more about it",
    ],
)
async def test_linear_channel_issue_bare_detail_followup_executes_for_current_issue(
    monkeypatch,
    text,
):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text=text,
        params={"action": "query"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*<https://linear.app/x|TECH-16> — Main issue*\n"
                "*Relations — 1*\n• blocks: `TECH-19` — Related issue",
            }
        ],
    )

    assert FakeDataBackendClient.linear_detail_calls[0]["issue_identifier"] == "TECH-16"
    assert FakeDataBackendClient.linear_list_calls == []


@pytest.mark.asyncio
async def test_generic_detail_query_is_not_reclassified_as_linear(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.query_response = {
        "resource": "content_factory_jobs",
        "rows": [{"job_id": "job-1", "status": "error"}],
        "returned_count": 1,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="show the status of failed Content Factory jobs",
        params={"action": "query", "resource": "content_factory_jobs"},
        user_id="UADMIN",
        channel_id="CTECH",
        slack_team_id="TMLAI",
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*2 issues in MLAI_TECH · Todo*\n"
                "1. <https://linear.app/x|TECH-16> — First\n"
                "2. <https://linear.app/y|TECH-19> — Second",
            }
        ],
    )

    assert FakeDataBackendClient.linear_list_calls == []
    assert FakeDataBackendClient.linear_detail_calls == []
    assert FakeDataBackendClient.queries[0]["resource"] == "content_factory_jobs"


@pytest.mark.asyncio
async def test_explicit_synced_linear_detail_query_is_not_reclassified_as_live(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.query_response = {
        "resource": "linear_issues",
        "rows": [{"identifier": "TECH-16", "title": "Synced issue"}],
        "returned_count": 1,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="show details for Linear issues synced for Studynash",
        params={"action": "query", "resource": "linear_issues"},
        user_id="UADMIN",
        channel_id="CTECH",
        slack_team_id="TMLAI",
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "Created <https://linear.app/mlai/issue/TECH-99|TECH-99> from the meeting.",
            }
        ],
    )

    assert FakeDataBackendClient.linear_list_calls == []
    assert FakeDataBackendClient.linear_detail_calls == []
    assert FakeDataBackendClient.queries[0]["resource"] == "linear_issues"


def test_linear_channel_issue_pronoun_uses_detail_heading_not_relation():
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text="show me its comments",
        params={},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*<https://linear.app/x|TECH-16> — Main issue*\n"
                "*Relations — 1*\n• blocks: `TECH-19` — Related issue",
            }
        ],
    )

    assert reference == "TECH-16"


@pytest.mark.parametrize(
    "text",
    [
        "show comments",
        "description",
        "who owns?",
        "status?",
        "show priority",
        "attachments?",
        "labels?",
        "due date",
        "relations",
        "tell me more about it",
    ],
)
def test_linear_channel_issue_bare_detail_followup_uses_current_issue(text):
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text=text,
        params={"action": "query"},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*<https://linear.app/x|TECH-16> — Main issue*\n"
                "*Relations — 1*\n• blocks: `TECH-19` — Related issue",
            }
        ],
    )

    assert reference == "TECH-16"


@pytest.mark.parametrize("text", ["status?", "tell me more about it"])
def test_linear_channel_issue_context_stops_at_newer_ambiguous_list(text):
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text=text,
        params={"action": "query"},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*<https://linear.app/x|TECH-16> — Older detail*\n"
                "*Status:* Todo",
            },
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*2 issues in MLAI_TECH · Todo*\n"
                "1. <https://linear.app/y|TECH-19> — First\n"
                "2. <https://linear.app/z|TECH-42> — Second",
            },
        ],
    )

    assert reference == text


def test_linear_channel_issue_ordinal_uses_older_numbered_list_not_newer_detail():
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text="show me number 2",
        params={},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*2 issues in MLAI_TECH · Todo*\n"
                "1. <https://linear.app/x|TECH-16> — First\n"
                "2. <https://linear.app/y|TECH-19> — Second",
            },
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "*<https://linear.app/x|TECH-16> — Main issue*\n"
                "*Relations — 1*\n• blocks: `TECH-99` — Related issue",
            },
        ],
    )

    assert reference == "TECH-19"


def test_linear_channel_issue_ordinal_continues_past_newer_partial_list():
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text="show me number 2",
        params={},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "1. <https://linear.app/x|TECH-16> — First\n"
                "2. <https://linear.app/y|TECH-19> — Second",
            },
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "1. <https://linear.app/z|TECH-42> — New single result",
            },
        ],
    )

    assert reference == "TECH-19"


def test_linear_channel_issue_ordinal_ignores_newer_foreign_bot_list():
    executor = SkillExecutor()

    reference = executor._resolve_linear_channel_issue_reference(
        text="show me number 2",
        params={},
        thread_history=[
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "1. <https://linear.app/x|TECH-16> — First\n"
                "2. <https://linear.app/y|TECH-19> — Second",
            },
            {
                "bot_id": "BOTHER",
                "user": "UOTHER",
                "text": "1. <https://linear.app/a|TECH-98> — Foreign first\n"
                "2. <https://linear.app/b|TECH-99> — Foreign second",
            },
        ],
    )

    assert reference == "TECH-19"


def test_linear_channel_issue_context_ignores_foreign_bot_links():
    executor = SkillExecutor()

    assert not executor._linear_channel_issue_thread_context(
        [
            {
                "bot_id": "BOTHER",
                "user": "UOTHER",
                "text": "1. <https://linear.app/a|TECH-98> — Foreign issue",
            }
        ]
    )


def test_linear_channel_issue_context_ignores_other_roo_linear_outputs():
    executor = SkillExecutor()

    assert not executor._linear_channel_issue_thread_context(
        [
            {
                "bot_id": "BROO",
                "user": "UROO",
                "text": "Created <https://linear.app/mlai/issue/TECH-99|TECH-99> from the meeting.",
            }
        ]
    )


@pytest.mark.asyncio
async def test_linear_channel_issue_detail_resolves_unique_title(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="details on deployment alerts",
        params={"action": "get_linear_channel_issue", "issue_reference": "deployment alerts"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
    )

    assert len(FakeDataBackendClient.linear_list_calls) == 1
    assert FakeDataBackendClient.linear_detail_calls[0]["issue_identifier"] == "TECH-19"


@pytest.mark.asyncio
async def test_linear_channel_issue_title_resolution_follows_page_cursor(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.linear_list_responses = [
        {
            "issues": [{"identifier": "TECH-16", "title": "First page issue"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        },
        {
            "issues": [{"identifier": "TECH-42", "title": "Repair deployment alerts"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    ]
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="details on deployment alerts",
        params={"action": "get_linear_channel_issue", "issue_reference": "deployment alerts"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
    )

    assert FakeDataBackendClient.linear_list_calls == [
        {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
            "requester_slack_id": "U123",
            "limit": 100,
        },
        {
            "slack_workspace_id": "TMLAI",
            "slack_channel_id": "CTECH",
            "requester_slack_id": "U123",
            "limit": 100,
            "after": "cursor-1",
        },
    ]
    assert FakeDataBackendClient.linear_detail_calls[0]["issue_identifier"] == "TECH-42"


@pytest.mark.asyncio
async def test_linear_channel_issue_title_resolution_detects_cross_page_ambiguity(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.linear_list_responses = [
        {
            "issues": [{"identifier": "TECH-19", "title": "Repair deployment alerts"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        },
        {
            "issues": [{"identifier": "TECH-42", "title": "Deployment alerts cleanup"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    ]
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="details on deployment alerts",
        params={"action": "get_linear_channel_issue", "issue_reference": "deployment alerts"},
        user_id="U123",
        channel_id="CTECH",
        slack_team_id="TMLAI",
    )

    assert "TECH-19" in result
    assert "TECH-42" in result
    assert FakeDataBackendClient.linear_detail_calls == []


@pytest.mark.asyncio
async def test_linear_channel_issue_actions_require_slack_context(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="show the MLAI_TECH Todo issues",
        params={"action": "list_linear_channel_issues"},
        user_id="U123",
    )

    assert "only available from its connected Slack channel" in result
    assert FakeDataBackendClient.linear_list_calls == []


@pytest.mark.asyncio
async def test_data_query_catalog_calls_backend_catalog(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="what data resources can Roo query?",
        params={},
        user_id="U123",
    )

    assert FakeDataBackendClient.catalog_requesters == ["U123"]
    assert FakeDataBackendClient.queries == []
    assert "Available data resources" in result["message"]
    assert "`vibe_raising_companies`" in result["message"]


@pytest.mark.asyncio
async def test_data_query_vibe_raising_count_payload(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="how many Vibe Raising companies do we have?",
        params={},
        user_id="U123",
    )

    assert FakeDataBackendClient.queries == [
        {
            "requester_slack_id": "U123",
            "resource": "vibe_raising_companies",
            "operation": "count",
            "offset": 0,
        }
    ]
    assert "`vibe_raising_companies` count: 3" in result["message"]


@pytest.mark.asyncio
async def test_data_query_specific_count_beats_bad_catalog_param(monkeypatch):
    _reset_fake_client()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="how many Vibe Raising companies do we have?",
        params={"action": "catalog"},
        user_id="U123",
    )

    assert FakeDataBackendClient.catalog_requesters == []
    assert FakeDataBackendClient.queries == [
        {
            "requester_slack_id": "U123",
            "resource": "vibe_raising_companies",
            "operation": "count",
            "offset": 0,
        }
    ]
    assert "`vibe_raising_companies` count: 3" in result["message"]


@pytest.mark.asyncio
async def test_data_query_content_factory_failed_jobs_payload(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.query_response = {
        "resource": "content_factory_jobs",
        "rows": [
            {
                "job_id": "job-1",
                "domain": "mlai.au",
                "status": "error",
                "selected_keyword": "ai grants",
                "article_url": "",
                "pr_url": "",
                "error_message": "generation failed",
                "created_at": "2026-06-10T01:00:00Z",
            }
        ],
        "returned_count": 1,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    result = await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="which content factory jobs failed last week?",
        params={},
        user_id="UADMIN",
    )

    payload = FakeDataBackendClient.queries[0]
    assert payload["requester_slack_id"] == "UADMIN"
    assert payload["resource"] == "content_factory_jobs"
    assert payload["operation"] == "list"
    assert payload["filters"] == [{"field": "status", "operator": "eq", "value": "error"}]
    assert payload["fields"] == [
        "job_id",
        "domain",
        "status",
        "selected_keyword",
        "article_url",
        "pr_url",
        "error_message",
        "created_at",
    ]
    assert payload["limit"] == 20
    assert "job-1" in result["message"]
    assert "generation failed" in result["message"]


@pytest.mark.asyncio
async def test_data_query_raw_text_beats_bad_resource_and_operation_params(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.query_response = {
        "resource": "content_factory_jobs",
        "rows": [
            {
                "job_id": "job-1",
                "domain": "mlai.au",
                "status": "error",
                "selected_keyword": "ai grants",
                "article_url": "",
                "pr_url": "",
                "error_message": "generation failed",
                "created_at": "2026-06-10T01:00:00Z",
            }
        ],
        "returned_count": 1,
        "limit": 20,
        "offset": 0,
        "has_more": False,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="which content factory jobs failed last week?",
        params={"resource": "gmail_messages", "operation": "count"},
        user_id="UADMIN",
    )

    payload = FakeDataBackendClient.queries[0]
    assert payload["resource"] == "content_factory_jobs"
    assert payload["operation"] == "list"
    assert payload["filters"] == [{"field": "status", "operator": "eq", "value": "error"}]


@pytest.mark.asyncio
async def test_data_query_uses_explicit_resource_params(monkeypatch):
    _reset_fake_client()
    FakeDataBackendClient.query_response = {
        "resource": "linear_issues",
        "rows": [{"identifier": "MLAI-1", "title": "Fix sync", "state_name": "Todo"}],
        "returned_count": 1,
        "limit": 5,
        "offset": 10,
        "has_more": False,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeDataBackendClient)

    await executor._execute_mlai_data_query(
        skill=SimpleNamespace(name="mlai-data-query"),
        text="show synced linear issues",
        params={
            "resource": "linear_issues",
            "fields": ["identifier", "title", "state_name"],
            "limit": 5,
            "offset": 10,
        },
        user_id="U123",
    )

    assert FakeDataBackendClient.queries == [
        {
            "requester_slack_id": "U123",
            "resource": "linear_issues",
            "operation": "list",
            "offset": 10,
            "fields": ["identifier", "title", "state_name"],
            "limit": 5,
        }
    ]
