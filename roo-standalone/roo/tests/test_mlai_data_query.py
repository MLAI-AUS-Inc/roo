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
    last_init = None

    def __init__(self, *args, **kwargs):
        self.__class__.last_init = {"args": args, "kwargs": kwargs}

    async def get_data_catalog(self, requester_slack_id):
        self.__class__.catalog_requesters.append(requester_slack_id)
        return self.catalog

    async def query_data(self, payload):
        self.__class__.queries.append(payload)
        return self.query_response


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
    FakeDataBackendClient.last_init = None


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
