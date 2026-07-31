import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))

backend_module = importlib.import_module("roo.clients.mlai_backend")
MLAIBackendClient = backend_module.MLAIBackendClient
MLAIBackendUnavailableError = backend_module.MLAIBackendUnavailableError
CONTENT_FACTORY_REQUEST_SOURCE = backend_module.CONTENT_FACTORY_REQUEST_SOURCE


@pytest.fixture(autouse=True)
def clear_backend_client_state():
    MLAIBackendClient._backend_transport_failures.clear()
    MLAIBackendClient._slack_user_registration_cache.clear()
    yield
    MLAIBackendClient._backend_transport_failures.clear()
    MLAIBackendClient._slack_user_registration_cache.clear()


class FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, **kwargs):
        return await self._handler(method, url, **kwargs)


@pytest.mark.asyncio
async def test_transport_errors_log_endpoint_and_exception_type(monkeypatch, capsys):
    async def handler(method, url, **kwargs):
        request = httpx.Request(method, url)
        raise httpx.ReadTimeout("backend timed out", request=request)

    monkeypatch.setattr(
        backend_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler),
    )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )

    with pytest.raises(MLAIBackendUnavailableError):
        await client.get_integration("U123", domain="mlai.au")

    output = capsys.readouterr().out
    assert "endpoint=/api/v1/integrations/github/U123/" in output
    assert "exc_type=ReadTimeout" in output
    assert "request_id=" in output


@pytest.mark.asyncio
async def test_circuit_breaker_probes_readiness_before_main_request(monkeypatch):
    calls = []

    async def handler(method, url, **kwargs):
        request = httpx.Request(method, url)
        calls.append(request.url.path)
        if request.url.path == "/healthz/ready":
            return httpx.Response(503, request=request, json={"status": "error"})
        raise AssertionError("main endpoint should not be called while circuit breaker is open")

    monkeypatch.setattr(
        backend_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: FakeAsyncClient(handler),
    )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    MLAIBackendClient._backend_transport_failures[client.base_url] = (
        MLAIBackendClient._transport_failure_threshold
    )

    with pytest.raises(MLAIBackendUnavailableError):
        await client.get_integration("U123", domain="mlai.au")

    assert calls == ["/healthz/ready"]


@pytest.mark.asyncio
async def test_admit_boost_post_uses_strict_roo_key_headers(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, kwargs=kwargs)
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            201,
            request=request,
            json={"status": "approved", "charged_points": 8},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="admin-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.admit_boost_post(
        submission_key="boost-post:TTEAM123:CBOOST123:1800000000.123456",
        workspace_id="TTEAM123",
        channel_id="CBOOST123",
        root_message_ts="1800000000.123456",
        poster_slack_id="<@UPOSTER1>",
        root_text="Boost this",
        social_post_url="https://www.linkedin.com/posts/example-123",
        recheck_insufficient_points=True,
    )

    assert result["status"] == "approved"
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/points/boost-posts/admissions/"
    assert captured["kwargs"]["json"]["poster_slack_id"] == "UPOSTER1"
    assert captured["kwargs"]["json"]["recheck_insufficient_points"] is True
    assert "use_admin_headers" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_trigger_repo_scan_sends_request_source(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            202,
            request=request,
            json={"status": "scan_initiated", "message": "Scan queued successfully."},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.trigger_repo_scan(
        "U123",
        slack_channel_id="C123",
        slack_thread_ts="111.222",
        domain="mlai.au",
    )

    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/integrations/github/scan"
    assert captured["json"]["slack_user_id"] == "U123"
    assert captured["json"]["domain"] == "mlai.au"
    assert captured["json"]["slack_channel_id"] == "C123"
    assert captured["json"]["slack_thread_ts"] == "111.222"
    assert captured["json"]["request_source"] == CONTENT_FACTORY_REQUEST_SOURCE
    assert result["status"] == "scan_initiated"


@pytest.mark.asyncio
async def test_get_coworking_report_uses_canonical_endpoint(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = kwargs["params"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json={"totals": {"booked_user_days": 3}})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.get_coworking_report("<@U123>", "2026-01-01", "2026-01-31")

    assert result == {"totals": {"booked_user_days": 3}}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/api/v1/points/coworking/report/"
    assert captured["params"] == {
        "slack_user_id": "U123",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }


@pytest.mark.asyncio
async def test_book_coworking_many_uses_canonical_endpoint_and_deduped_payload(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(201, request=request, json={"created_count": 2, "results": []})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.book_coworking_many(
        admin_slack_user_id="<@UADMIN>",
        target_slack_user_ids=["<@U1>", "U2", "<@U1>"],
        booking_date="2026-07-04",
        slack_channel_id="C123",
    )

    assert result == {"created_count": 2, "results": []}
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/points/coworking/book-many/"
    assert captured["json"]["admin_slack_user_id"] == "UADMIN"
    assert captured["json"]["target_slack_user_ids"] == ["U1", "U2"]
    assert captured["json"]["date"] == "2026-07-04"
    assert captured["json"]["slack_channel_id"] == "C123"
    assert captured["json"]["current_time"]


@pytest.mark.asyncio
async def test_claim_office_manager_day_uses_verified_actor_payload(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            201,
            request=request,
            json={"status": "claimed", "points_charged": 0},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.claim_office_manager_day(
        "<@UVERIFIED>",
        "2026-08-03",
    )

    assert result == {"status": "claimed", "points_charged": 0}
    assert captured == {
        "method": "POST",
        "endpoint": "/api/v1/points/coworking/office-manager/claim/",
        "json": {
            "slack_user_id": "UVERIFIED",
            "date": "2026-08-03",
        },
    }


@pytest.mark.asyncio
async def test_get_data_catalog_uses_canonical_endpoint(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = kwargs["params"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json={"resources": [{"key": "vibe_raising_companies"}]})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.get_data_catalog("<@U123>")

    assert result == {"resources": [{"key": "vibe_raising_companies"}]}
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/api/v1/data/catalog/"
    assert captured["params"] == {"requester_slack_id": "U123"}


@pytest.mark.asyncio
async def test_query_data_uses_canonical_endpoint_and_payload(monkeypatch):
    captured = {}
    payload = {
        "requester_slack_id": "U123",
        "resource": "content_factory_jobs",
        "operation": "count",
    }

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json={"resource": "content_factory_jobs", "rows": [{"count": 2}]})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.query_data(payload)

    assert result == {"resource": "content_factory_jobs", "rows": [{"count": 2}]}
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/data/query/"
    assert captured["json"] == payload


@pytest.mark.asyncio
async def test_healthhack_announcement_uses_canonical_endpoint_and_provenance(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            201,
            request=request,
            json={"id": "announcement-1", "created": True},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.healthhack_create_announcement(
        title="Doors open",
        body="Registration opens at 10:30am.",
        requester_slack_id="U0SUPER123",
        author_slack_id="U0ROO00000",
        source_channel_id="C0BHZ9NS21L",
        source_message_ts="1784286514.495879",
    )

    assert result == {"id": "announcement-1", "created": True}
    assert captured == {
        "method": "POST",
        "endpoint": "/api/v1/hackathons/hospital/announcements/",
        "json": {
            "title": "Doors open",
            "body": "Registration opens at 10:30am.",
            "requester_slack_id": "U0SUPER123",
            "author_slack_id": "U0ROO00000",
            "source_channel_id": "C0BHZ9NS21L",
            "source_message_ts": "1784286514.495879",
        },
    }


@pytest.mark.asyncio
async def test_create_points_purchase_sends_slack_origin(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            201,
            request=request,
            json={
                "id": "purchase-123",
                "frontend_checkout_page_url": "https://mlai.test/roo/topup/purchase-123",
            },
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.create_points_purchase(
        slack_user_id="<@U123>",
        pack_id="topup_10",
        purchase_from={
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    )

    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/v1/points/purchases/"
    assert captured["json"] == {
        "slack_user_id": "U123",
        "pack_id": "topup_10",
        "purchase_from": {
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    }
    assert result["frontend_checkout_page_url"] == "https://mlai.test/roo/topup/purchase-123"


@pytest.mark.asyncio
async def test_create_points_purchase_503_raises_backend_unavailable(monkeypatch):
    async def fake_request(method, endpoint, **kwargs):
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            503,
            request=request,
            json={"message": "Points purchase checkout is temporarily unavailable."},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(MLAIBackendUnavailableError, match="Points purchase checkout is temporarily unavailable"):
        await client.create_points_purchase("<@U123>", pack_id="topup_10")


@pytest.mark.asyncio
async def test_get_coworking_report_503_raises_backend_unavailable(monkeypatch):
    async def fake_request(method, endpoint, **kwargs):
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            503,
            request=request,
            json={
                "status": "error",
                "message": "Points subsystem is temporarily unavailable",
                "error_code": "database_connection_interrupted",
                "retryable": True,
            },
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(MLAIBackendUnavailableError, match="Points subsystem is temporarily unavailable"):
        await client.get_coworking_report("U123", "2026-01-01", "2026-01-31")


@pytest.mark.asyncio
async def test_is_admin_excludes_report_only_partner(monkeypatch):
    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )

    async def fake_get_admin_details(slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "partner"}

    monkeypatch.setattr(client, "get_admin_details", fake_get_admin_details)

    assert await client.is_admin("UPARTNER") is False


@pytest.mark.asyncio
async def test_is_admin_includes_committee_member(monkeypatch):
    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )

    async def fake_get_admin_details(slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "committee"}

    monkeypatch.setattr(client, "get_admin_details", fake_get_admin_details)

    assert await client.is_admin("UCOMMITTEE") is True


@pytest.mark.asyncio
async def test_trigger_repo_scan_includes_requested_by_when_provided(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            202,
            request=request,
            json={"status": "scan_initiated", "message": "Scan queued successfully."},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    await client.trigger_repo_scan(
        "U0AQV5X9G0J",
        slack_channel_id="C123",
        slack_thread_ts="111.222",
        domain="studynash.co",
        requested_by_slack_user_id="U05QPB483K9",
    )

    assert captured["json"]["slack_user_id"] == "U0AQV5X9G0J"
    assert captured["json"]["requested_by_slack_user_id"] == "U05QPB483K9"


@pytest.mark.asyncio
async def test_confirm_article_topic_raises_backend_unavailable_on_503(monkeypatch):
    async def fake_request(method, endpoint, **kwargs):
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            503,
            request=request,
            json={
                "status": "backend_unavailable",
                "error_code": "CONTENT_FACTORY_UNAVAILABLE",
                "message": "Content Factory is unavailable right now.",
            },
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.raises(MLAIBackendUnavailableError, match="Content Factory is unavailable right now."):
        await client.confirm_article_topic(
            job_id="job-123",
            slack_user_id="U123",
            confirmed_keyword="ai agents",
        )


@pytest.mark.asyncio
async def test_confirm_article_topic_includes_requested_by_when_provided(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured["json"] = kwargs["json"]
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json={"status": "confirmed"})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    await client.confirm_article_topic(
        job_id="job-123",
        slack_user_id="U0AQV5X9G0J",
        confirmed_keyword="ai agents",
        requested_by_slack_user_id="U05QPB483K9",
    )

    assert captured["json"]["slack_user_id"] == "U0AQV5X9G0J"
    assert captured["json"]["requested_by_slack_user_id"] == "U05QPB483K9"



@pytest.mark.asyncio
async def test_statement_reconciliation_client_uses_run_scoped_guarded_endpoints(monkeypatch):
    captured = []

    async def fake_request(method, endpoint, **kwargs):
        captured.append({"method": method, "endpoint": endpoint, **kwargs})
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json={"run_id": "run/123"})

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    await client.get_statement_reconciliation_readiness("UADMIN")
    await client.start_statement_reconciliation_run(
        "UADMIN",
        instruction="Use the Aaron AI context.",
        statement_line_ids=["line-1"],
    )
    await client.retry_statement_reconciliation_run("UADMIN", "run/123")
    await client.get_statement_reconciliation_outcomes("UADMIN", limit=25)
    await client.decide_statement_reconciliation_learning_candidate(
        "UADMIN",
        "candidate/123",
        candidate_version="version-123",
        decision="promote",
    )
    await client.preview_statement_reconciliation_run("UADMIN", "run/123")
    await client.approve_ready_statement_reconciliation_run(
        "UADMIN", "run/123", decision_request_id="roo-decision-1"
    )
    await client.reject_statement_reconciliation_suggestions(
        "UADMIN",
        "run/123",
        [12],
        reason="Wrong project.",
        decision_request_id="roo-decision-2",
    )
    await client.execute_approved_statement_reconciliation_run(
        "UADMIN", "run/123", suggestion_ids=[10, 11]
    )

    assert [(item["method"], item["endpoint"]) for item in captured] == [
        ("GET", "/api/v1/integrations/reconciliation/readiness"),
        ("POST", "/api/v1/integrations/reconciliation/agent-runs"),
        ("POST", "/api/v1/integrations/reconciliation/agent-runs/run%2F123/retry"),
        ("GET", "/api/v1/integrations/reconciliation/outcomes"),
        ("POST", "/api/v1/integrations/reconciliation/learning-candidates/candidate%2F123"),
        ("GET", "/api/v1/integrations/reconciliation/agent-runs/run%2F123/preview"),
        ("POST", "/api/v1/integrations/reconciliation/agent-runs/run%2F123/decisions"),
        ("POST", "/api/v1/integrations/reconciliation/agent-runs/run%2F123/decisions"),
        ("POST", "/api/v1/integrations/reconciliation/agent-runs/run%2F123/execute"),
    ]
    assert captured[0]["params"] == {
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
    }
    assert captured[1]["json"]["statement_line_ids"] == ["line-1"]
    assert captured[2]["json"] == {
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
        "confirm": True,
    }
    assert captured[3]["params"]["limit"] == 25
    assert captured[4]["json"] == {
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
        "candidate_version": "version-123",
        "decision": "promote",
        "confirm": True,
    }
    assert captured[6]["json"] == {
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
        "confirm": True,
        "approve_all_ready": True,
        "decision_request_id": "roo-decision-1",
    }
    assert captured[7]["json"]["decisions"] == [{
        "suggestion_id": 12,
        "decision": "reject",
        "reason": "Wrong project.",
    }]
    assert captured[8]["json"]["suggestion_ids"] == [10, 11]

@pytest.mark.asyncio
async def test_event_finance_audit_uses_read_only_bounded_endpoint(monkeypatch):
    captured = {}

    async def fake_request(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, kwargs=kwargs)
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(
            200,
            request=request,
            json={"summary": {"event_count": 3}, "xero_writes": False},
        )

    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="roo-api-key",
        internal_api_key="roo-api-key",
    )
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.get_event_finance_audit(
        "<@UADMIN>",
        since="2026-02-02",
        until="2026-08-02",
        domain="MLAI.AU",
    )

    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/api/v1/integrations/reconciliation/event-finance-audit"
    assert captured["kwargs"]["params"] == {
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
        "since": "2026-02-02",
        "until": "2026-08-02",
    }
    assert result["xero_writes"] is False
