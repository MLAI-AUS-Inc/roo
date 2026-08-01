import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo.backend_identity import (
    BackendActorContext,
    BackendIdentityError,
    build_org_memory_identity_headers,
    build_roo_gateway_identity_headers,
    build_victor_ai_identity_headers,
    get_backend_actor_context,
    use_backend_actor_context,
)
from roo.clients.mlai_backend import MLAIBackendClient


CONTRACT_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
CONTRACT_ASSERTION = (
    "eyJhY3Rpbmdfc2xhY2tfdXNlcl9pZCI6IlVBRE1JTjEyMyIsImV2ZW50X2lkIjoiRXYwMVRFU1QiLCJleHAiOjE3MDAwMDAwNDUsImlhdCI6MTcwMDAwMDAwMCwia2lkIjoiYWFhYWFhYWEtYWFhYS1hYWFhLWFhYWEtYWFhYWFhYWFhYWFhIiwibm9uY2UiOiJmaXhlZF9ub25jZV8xMjM0NTY3ODkwMTIzNDUiLCJyZXF1ZXN0X2lkIjoicm9vLXRlc3QtcmVxdWVzdCIsInNsYWNrX2NoYW5uZWxfaWQiOiJHQURNSU4xMjMiLCJzbGFja190ZWFtX2lkIjoiVE1MQUkxMjMiLCJzbGFja190aHJlYWRfdHMiOiIxNzAwMDAwMDAwLjEyMyIsInN1cmZhY2UiOiJhZG1pbl9yb28iLCJ2IjoxfQ."
    "l71Zpd8GgCU4I7CCa-1x2yzeeVbdvIfmePqQDDu-iuk"
)


def _service_token():
    return f"mlai_sp_{uuid4().hex}.{'s' * 48}"


def _actor_context():
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id="UADMIN123",
        slack_channel_id="GADMIN123",
        slack_thread_ts="1700000000.123",
        event_id="Ev01TEST",
    )


def _decode_payload(assertion):
    payload_part, _ = assertion.split(".", 1)
    padding = "=" * (-len(payload_part) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_part + padding))


def test_actor_assertion_binds_every_slack_and_request_identity_field():
    token = CONTRACT_TOKEN
    headers = build_org_memory_identity_headers(
        token,
        context=_actor_context(),
        request_id="roo-test-request",
        issued_at=1_700_000_000,
        nonce="fixed_nonce_123456789012345",
    )

    payload = _decode_payload(headers["X-MLAI-Actor-Assertion"])
    payload_part, signature_part = headers["X-MLAI-Actor-Assertion"].split(".", 1)
    expected_signature = hmac.new(
        token.encode("utf-8"),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_signature_text = base64.urlsafe_b64encode(expected_signature).rstrip(b"=").decode("ascii")

    assert headers["X-MLAI-Actor-Assertion"] == CONTRACT_ASSERTION
    assert signature_part == expected_signature_text
    assert payload == {
        "v": 1,
        "kid": str(UUID(hex=token.removeprefix("mlai_sp_").split(".", 1)[0])),
        "surface": "admin_roo",
        "slack_team_id": "TMLAI123",
        "acting_slack_user_id": "UADMIN123",
        "slack_channel_id": "GADMIN123",
        "slack_thread_ts": "1700000000.123",
        "event_id": "Ev01TEST",
        "request_id": "roo-test-request",
        "iat": 1_700_000_000,
        "exp": 1_700_000_045,
        "nonce": "fixed_nonce_123456789012345",
    }


def test_public_client_cannot_build_private_memory_headers():
    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="public-key",
        internal_api_key="public-key",
        service_principal_key=_service_token(),
        surface="public",
        actor_context=_actor_context(),
    )

    with pytest.raises(BackendIdentityError, match="Only Admin Roo"):
        client.org_memory_headers("roo-test-request")


def test_gateway_assertion_is_route_only_and_binds_verified_actor():
    token = _service_token()
    headers = build_roo_gateway_identity_headers(
        token,
        context=_actor_context(),
        request_id="roo-route-request",
        issued_at=1_700_000_000,
        nonce="fixed_nonce_123456789012345",
    )

    payload = _decode_payload(headers["X-MLAI-Actor-Assertion"])
    assert payload["surface"] == "roo_gateway"
    assert payload["acting_slack_user_id"] == "UADMIN123"
    assert payload["slack_channel_id"] == "GADMIN123"
    assert headers["X-Roo-Surface"] == "roo_gateway"

    gateway = MLAIBackendClient(
        base_url="https://backend.test",
        service_principal_key=token,
        surface="gateway",
        actor_context=_actor_context(),
    )
    with pytest.raises(BackendIdentityError, match="Only Admin Roo"):
        gateway.org_memory_headers("roo-private-request")


@pytest.mark.asyncio
async def test_gateway_eligibility_uses_one_matching_request_id(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            request = httpx.Request(method, url)
            return httpx.Response(
                200,
                request=request,
                json={"admin_brain_eligible": True},
            )

    monkeypatch.setattr("roo.clients.mlai_backend.httpx.AsyncClient", FakeAsyncClient)
    client = MLAIBackendClient(
        base_url="https://backend.test",
        service_principal_key=_service_token(),
        surface="gateway",
        actor_context=_actor_context(),
    )

    result = await client.get_admin_routing_eligibility()
    assertion = _decode_payload(captured["headers"]["X-MLAI-Actor-Assertion"])

    assert result["admin_brain_eligible"] is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/v1/org-memory/routing/eligibility")
    assert captured["json"] == {}
    assert assertion["request_id"] == captured["headers"]["X-Request-ID"]
    assert assertion["surface"] == "roo_gateway"


def test_victor_assertion_binds_verified_actor_context_with_hmac():
    secret = "victor-signing-secret-" + ("s" * 32)
    headers = build_victor_ai_identity_headers(
        secret,
        context=_actor_context(),
        request_id="roo-victor-request",
        timestamp=1_700_000_000,
        nonce="fixed_nonce_123456789012345",
    )
    payload = {
        "acting_slack_user_id": "UADMIN123",
        "event_id": "Ev01TEST",
        "nonce": "fixed_nonce_123456789012345",
        "request_id": "roo-victor-request",
        "slack_channel_id": "GADMIN123",
        "slack_team_id": "TMLAI123",
        "slack_thread_ts": "1700000000.123",
        "surface": "public_roo",
        "timestamp": 1_700_000_000,
        "v": 1,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()

    assert headers["X-Victor-Roo-Signature"] == f"v1={expected}"
    assert headers["X-Roo-Surface"] == "public_roo"
    assert headers["X-Slack-Team-ID"] == "TMLAI123"
    assert headers["X-Acting-Slack-User-ID"] == "UADMIN123"
    assert headers["X-Slack-Channel-ID"] == "GADMIN123"
    assert headers["X-Slack-Event-ID"] == "Ev01TEST"


@pytest.mark.asyncio
async def test_victor_client_uses_only_scoped_signed_identity(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            request = httpx.Request(method, url)
            return httpx.Response(
                200,
                request=request,
                json={"complete_count": 4, "lead_count": 1},
            )

    monkeypatch.setattr("roo.clients.mlai_backend.httpx.AsyncClient", FakeAsyncClient)
    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="ordinary-roo-key",
        victor_ai_signing_secret="v" * 48,
        surface="public",
        actor_context=_actor_context(),
    )

    result = await client.get_victor_application_summary()

    assert result == {"complete_count": 4, "lead_count": 1}
    assert captured["url"].endswith("/api/v1/victor-ai/roo/applications/summary/")
    assert "X-API-Key" not in captured["headers"]
    assert captured["headers"]["X-Victor-Roo-Signature"].startswith("v1=")
    assert captured["headers"]["X-Request-ID"].startswith("roo-")


@pytest.mark.asyncio
async def test_admin_memory_probe_uses_only_service_identity_headers(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            request = httpx.Request(method, url)
            return httpx.Response(200, request=request, json={"surface": "admin_roo"})

    monkeypatch.setattr("roo.clients.mlai_backend.httpx.AsyncClient", FakeAsyncClient)
    token = _service_token()
    client = MLAIBackendClient(
        base_url="https://backend.test",
        api_key="legacy-public-key",
        internal_api_key="legacy-internal-key",
        service_principal_key=token,
        surface="admin",
        actor_context=_actor_context(),
    )

    result = await client.get_org_memory_actor_context()

    assert result == {"surface": "admin_roo"}
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/api/v1/org-memory/auth/context")
    assert captured["headers"]["Authorization"] == f"ServicePrincipal {token}"
    assert "X-API-Key" not in captured["headers"]
    assert captured["headers"]["X-Acting-Slack-User-ID"] == "UADMIN123"
    assert captured["headers"]["X-Request-ID"].startswith("roo-")


@pytest.mark.asyncio
async def test_pilot_access_probe_uses_signed_identity_and_no_payload(
    monkeypatch,
):
    captured = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            request = httpx.Request(method, url)
            return httpx.Response(
                200,
                request=request,
                json={
                    "ready": True,
                    "code": "active_pilot_access_granted",
                },
            )

    monkeypatch.setattr(
        "roo.clients.mlai_backend.httpx.AsyncClient",
        FakeAsyncClient,
    )
    token = _service_token()
    client = MLAIBackendClient(
        base_url="https://backend.test",
        service_principal_key=token,
        surface="admin",
        actor_context=_actor_context(),
    )

    result = await client.get_org_memory_pilot_access_probe()

    assert result["ready"] is True
    assert captured["method"] == "GET"
    assert captured["url"].endswith(
        "/api/v1/org-memory/pilot/access-check"
    )
    assert captured.get("json") is None
    assert captured["headers"]["Authorization"] == (
        f"ServicePrincipal {token}"
    )
    assert "X-API-Key" not in captured["headers"]


@pytest.mark.asyncio
async def test_slack_task_context_is_available_only_during_mention(monkeypatch):
    captured = []

    async def fake_handle(event):
        captured.append(get_backend_actor_context())

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle)
    assert get_backend_actor_context() is None

    await main_module._handle_slack_mention(
        {
            "user": "UADMIN123",
            "channel": "GADMIN123",
            "ts": "1700000000.123",
        },
        slack_team_id="TMLAI123",
        event_id="Ev01TEST",
    )

    assert captured == [_actor_context()]
    assert get_backend_actor_context() is None


def test_nested_actor_context_resets_without_cross_request_leakage():
    first = _actor_context()
    second = BackendActorContext("TOTHER123", "UOTHER123", "GOTHER123", "2.3", "Ev02")

    with use_backend_actor_context(first):
        assert get_backend_actor_context() == first
        with use_backend_actor_context(second):
            assert get_backend_actor_context() == second
        assert get_backend_actor_context() == first
    assert get_backend_actor_context() is None
