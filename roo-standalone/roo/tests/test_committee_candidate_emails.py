import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-committee-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "committee-signing-test")
os.environ.setdefault("OPENAI_API_KEY", "committee-openai-test")

from roo import committee_candidate_emails as email_module
from roo.clients.mlai_backend import MLAIBackendClient
from roo.skills import executor as executor_module
from roo.skills.executor import SkillExecutor


def _candidate_data(emails=None):
    if emails is None:
        emails = ["alpha@example.com", "beta@example.com"]
    return {
        "eligible_count": len(emails),
        "threshold": 100,
        "metric": "lifetime_earned",
        "emails": emails,
    }


def _settings():
    return SimpleNamespace(
        MLAI_BACKEND_URL="https://backend.test",
        ROO_API_KEY="roo-key",
        MLAI_API_KEY=None,
        INTERNAL_API_KEY=None,
        ROO_SURFACE="public",
        ROO_UNIFIED_ADMIN_ROUTING_ENABLED=False,
    )


@pytest.mark.asyncio
async def test_backend_client_uses_private_email_export_contract(monkeypatch):
    client = MLAIBackendClient(base_url="https://backend.test", api_key="roo-key")
    calls = []

    async def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        request = httpx.Request(method, f"https://backend.test{endpoint}")
        return httpx.Response(200, request=request, json=_candidate_data())

    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.list_committee_candidate_emails("<@UADMIN>")

    assert result == _candidate_data()
    assert calls == [
        (
            "POST",
            "/api/v1/points/committee-candidates/emails/",
            {
                "json": {"requester_slack_id": "UADMIN"},
                "timeout": 10.0,
                "transport_retries": 1,
                "retry_backoff_seconds": 0.25,
                "circuit_breaker": True,
            },
        )
    ]


def test_formatter_returns_copy_ready_email_only_list():
    payloads = email_module.build_candidate_email_payloads(_candidate_data())

    assert len(payloads) == 1
    assert payloads[0]["message"].splitlines() == [
        "Eligible member emails (2)",
        "alpha@example.com",
        "beta@example.com",
    ]
    rendered = str(payloads[0]["blocks"])
    assert payloads[0]["blocks"][1]["text"] == {
        "type": "plain_text",
        "text": "alpha@example.com\nbeta@example.com",
    }
    assert "Slack" not in rendered
    assert "point balance" not in rendered


def test_formatter_preserves_every_address_across_large_slack_payloads():
    emails = [f"member-{index:05d}@example.com" for index in range(6000)]
    payloads = email_module.build_candidate_email_payloads(_candidate_data(emails))

    assert len(payloads) > 1
    rendered_emails = []
    for payload in payloads:
        assert len(payload["blocks"]) <= email_module.MAX_EMAIL_SECTIONS_PER_MESSAGE + 1
        assert len(payload["message"]) <= email_module.MAX_FALLBACK_CHARS
        for block in payload["blocks"][1:]:
            section = block["text"]["text"]
            assert len(section) <= 3000
            assert block["text"]["type"] == "plain_text"
            rendered_emails.extend(section.splitlines())
    assert rendered_emails == emails


def test_formatter_handles_no_eligible_members():
    payloads = email_module.build_candidate_email_payloads(_candidate_data([]))

    assert len(payloads) == 1
    assert "No active members" in payloads[0]["message"]
    assert "@" not in payloads[0]["message"]


@pytest.mark.asyncio
async def test_public_request_sends_private_email_list_using_event_actor(monkeypatch):
    captured = {"dms": []}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def list_committee_candidate_emails(self, requester_slack_id):
            captured["requester"] = requester_slack_id
            return _candidate_data()

    def fake_send_dm(user_id, text, **kwargs):
        captured["dms"].append((user_id, text, kwargs.get("blocks")))
        return {"ok": True}

    monkeypatch.setattr(executor_module, "get_settings", _settings)
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(executor_module, "send_dm", fake_send_dm)

    result = await SkillExecutor()._execute_committee_candidate_emails(
        text="list members with 100 earned points",
        params={"action": "list_eligible_emails", "requester_slack_id": "UFORGED"},
        user_id="UADMIN",
        channel_id="CGENERAL",
    )

    assert captured["requester"] == "UADMIN"
    assert captured["dms"][0][0] == "UADMIN"
    assert "alpha@example.com" in captured["dms"][0][1]
    assert result["message"] == "I've sent the eligible email list privately."
    assert "@example.com" not in result["message"]


@pytest.mark.asyncio
async def test_direct_message_returns_list_without_second_dm(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_committee_candidate_emails(self, requester_slack_id):
            return _candidate_data()

    monkeypatch.setattr(executor_module, "get_settings", _settings)
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("A one-message DM result should render directly"),
    )

    result = await SkillExecutor()._execute_committee_candidate_emails(
        text="committee candidate emails",
        params={"action": "list_eligible_emails"},
        user_id="UADMIN",
        channel_id="DPRIVATE",
    )

    assert "alpha@example.com" in result["message"]
    assert result["blocks"]
    assert result["data"]["eligible_count"] == 2


@pytest.mark.asyncio
async def test_missing_channel_fails_closed_to_private_dm(monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_committee_candidate_emails(self, requester_slack_id):
            return _candidate_data()

    monkeypatch.setattr(executor_module, "get_settings", _settings)
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda user_id, text, **kwargs: captured.append((user_id, text)) or {"ok": True},
    )

    result = await SkillExecutor()._execute_committee_candidate_emails(
        text="committee candidate emails",
        params={"action": "list_eligible_emails"},
        user_id="UADMIN",
        channel_id=None,
    )

    assert captured and captured[0][0] == "UADMIN"
    assert "@example.com" not in result["message"]


@pytest.mark.asyncio
async def test_dm_failure_never_returns_emails_to_public_channel(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def list_committee_candidate_emails(self, requester_slack_id):
            return _candidate_data()

    monkeypatch.setattr(executor_module, "get_settings", _settings)
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FakeClient)
    monkeypatch.setattr(executor_module, "send_dm", lambda *args, **kwargs: {"ok": False})

    result = await SkillExecutor()._execute_committee_candidate_emails(
        text="committee candidate emails",
        params={"action": "list_eligible_emails"},
        user_id="UADMIN",
        channel_id="CGENERAL",
    )

    assert result["data"]["delivery_failed"] is True
    assert "@example.com" not in result["message"]
    assert "DM Roo" in result["message"]


@pytest.mark.asyncio
async def test_permission_denial_and_backend_failure_are_safe(monkeypatch):
    class DeniedClient:
        def __init__(self, **kwargs):
            pass

        async def list_committee_candidate_emails(self, requester_slack_id):
            request = httpx.Request("POST", "https://backend.test/candidates")
            response = httpx.Response(403, request=request, json={"error": "denied"})
            raise httpx.HTTPStatusError("denied", request=request, response=response)

    monkeypatch.setattr(executor_module, "get_settings", _settings)
    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", DeniedClient)
    denied = await SkillExecutor()._execute_committee_candidate_emails(
        text="committee candidate emails",
        params={"action": "list_eligible_emails"},
        user_id="UPARTNER",
        channel_id="DPRIVATE",
    )
    assert "admin or committee role" in denied["message"]
    assert denied["data"]["authorised"] is False

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def list_committee_candidate_emails(self, requester_slack_id):
            raise ValueError("backend response was invalid")

    monkeypatch.setattr("roo.clients.mlai_backend.MLAIBackendClient", FailingClient)
    failed = await SkillExecutor()._execute_committee_candidate_emails(
        text="committee candidate emails",
        params={"action": "list_eligible_emails"},
        user_id="UADMIN",
        channel_id="DPRIVATE",
    )
    assert "reach the MLAI backend" in failed["message"]
    assert "@" not in failed["message"]


@pytest.mark.asyncio
async def test_unknown_action_does_not_call_backend(monkeypatch):
    monkeypatch.setattr(
        "roo.clients.mlai_backend.MLAIBackendClient",
        lambda **kwargs: pytest.fail("Unknown action must not call the backend"),
    )

    result = await SkillExecutor()._execute_committee_candidate_emails(
        text="send invitations",
        params={"action": "send_invitations"},
        user_id="UADMIN",
        channel_id="DPRIVATE",
    )

    assert "list the emails" in result["message"]
    assert "send" not in result["data"]
