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
