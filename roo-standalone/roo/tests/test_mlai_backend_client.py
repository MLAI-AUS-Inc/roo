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
