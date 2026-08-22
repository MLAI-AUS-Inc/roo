import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

backend_module = importlib.import_module("roo.clients.mlai_backend")
executor_module = importlib.import_module("roo.skills.executor")
MLAIBackendUnavailableError = backend_module.MLAIBackendUnavailableError
SkillExecutor = executor_module.SkillExecutor
LINK_TOKEN = "A" * 43


def future_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


class FakeLinkClient:
    def __init__(self, response=None, error=None):
        self.response = response or {
            "status": "link_required",
            "link_url": (
                "https://mlai.au/founder-tools/link-roo?"
                f"token={LINK_TOKEN}"
            ),
            "expires_at": future_expiry(),
        }
        self.error = error
        self.slack_user_ids = []

    async def start_founder_account_link(self, slack_user_id):
        self.slack_user_ids.append(slack_user_id)
        if self.error:
            raise self.error
        return self.response


async def execute_link(client, *, channel_id="D123", params=None):
    return await SkillExecutor()._handle_points_action(
        client=client,
        action="link_founder_account",
        params=params or {},
        text="link",
        user_id="U123",
        channel_id=channel_id,
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )


@pytest.mark.asyncio
async def test_link_in_dm_uses_event_identity_and_returns_accessible_button(monkeypatch):
    client = FakeLinkClient()
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("An existing DM must use its current channel"),
    )

    result = await execute_link(
        client,
        params={"slack_user_id": "UATTACKER", "target_user": "UATTACKER"},
    )

    assert client.slack_user_ids == ["U123"]
    assert result["data"] == {
        "action": "link_founder_account",
        "delivery": "direct_message",
    }
    button = result["blocks"][1]["elements"][0]
    assert button["url"].endswith(f"token={LINK_TOKEN}")
    assert button["accessibility_label"]
    assert LINK_TOKEN not in result["message"]
    context = result["blocks"][2]["elements"][0]["text"]
    assert "Founder Tools link expires <!date^" in context
    assert "expires in 30 minutes" not in context


@pytest.mark.asyncio
async def test_public_link_sends_private_button_and_posts_token_free_ack(monkeypatch):
    delivered = {}
    client = FakeLinkClient()
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda user_id, text, **kwargs: (
            delivered.update({"user_id": user_id, "text": text, **kwargs})
            or {"ok": True, "channel": "D123", "ts": "333.444"}
        ),
    )

    result = await execute_link(client, channel_id="C123")

    assert client.slack_user_ids == ["U123"]
    assert result["message"] == (
        "I sent <@U123> a private Founder Tools account-link button. Check your DMs."
    )
    assert LINK_TOKEN not in result["message"]
    assert "https://" not in result["message"]
    assert delivered["user_id"] == "U123"
    assert delivered["blocks"][1]["elements"][0]["url"].endswith(
        f"token={LINK_TOKEN}"
    )


@pytest.mark.asyncio
async def test_public_link_dm_failure_never_exposes_url(monkeypatch):
    client = FakeLinkClient()
    monkeypatch.setattr(executor_module, "send_dm", lambda *args, **kwargs: None)

    result = await execute_link(client, channel_id="C123")

    assert result["data"]["delivery_failed"] is True
    assert "DM Roo `link`" in result["message"]
    assert "Founder Tools account-link button" in result["message"]
    assert LINK_TOKEN not in result["message"]
    assert "https://" not in result["message"]


@pytest.mark.asyncio
async def test_already_linked_response_has_no_button(monkeypatch):
    client = FakeLinkClient(response={"status": "already_linked"})
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("Already-linked users need no DM"),
    )

    result = await execute_link(client, channel_id="C123")

    assert result == (
        "Your Roo Slack account is already linked to a Founder Tools account. "
        "If that is the wrong account, contact the MLAI team for help; Roo "
        "cannot relink accounts yet."
    )


@pytest.mark.asyncio
async def test_backend_failure_returns_retry_message_without_link():
    client = FakeLinkClient(error=MLAIBackendUnavailableError("offline"))

    result = await execute_link(client)

    assert result == (
        "I couldn't create a Founder Tools account link right now. "
        "Please try `link` again shortly."
    )


@pytest.mark.asyncio
async def test_untrusted_backend_link_is_not_delivered(monkeypatch):
    client = FakeLinkClient(
        response={
            "status": "link_required",
            "link_url": "javascript:alert(document.cookie)",
            "expires_at": future_expiry(),
        }
    )
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("Unsafe URLs must not be delivered"),
    )

    result = await execute_link(client, channel_id="C123")

    assert "trusted Founder Tools account link" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link_url",
    [
        f"https://evil.example/founder-tools/link-roo?token={LINK_TOKEN}",
        f"https://mlai.au.evil.example/founder-tools/link-roo?token={LINK_TOKEN}",
        f"https://mlai.au./founder-tools/link-roo?token={LINK_TOKEN}",
        f"https://user@mlai.au/founder-tools/link-roo?token={LINK_TOKEN}",
        f"https://mlai.au:444/founder-tools/link-roo?token={LINK_TOKEN}",
        f"https://mlai.au/not-link-roo?token={LINK_TOKEN}",
        f"https://mlai.au/founder-tools/link-roo?token={LINK_TOKEN}&next=/",
        f"https://mlai.au/founder-tools/link-roo?token={LINK_TOKEN}#fragment",
        f"https://[::1/founder-tools/link-roo?token={LINK_TOKEN}",
    ],
)
async def test_untrusted_or_malformed_backend_links_are_not_delivered(
    monkeypatch,
    link_url,
):
    client = FakeLinkClient(
        response={
            "status": "link_required",
            "link_url": link_url,
            "expires_at": future_expiry(),
        }
    )
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("Unsafe URLs must not be delivered"),
    )

    result = await execute_link(client, channel_id="C123")

    assert result == (
        "I couldn't create a trusted Founder Tools account link right now. "
        "Please try `link` again shortly."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [None, "not-a-date", "2020-01-01T00:00:00Z", "2099-01-01T00:00:00"])
async def test_missing_invalid_expired_or_naive_expiry_is_rejected(
    monkeypatch,
    expires_at,
):
    client = FakeLinkClient()
    client.response["expires_at"] = expires_at
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda *args, **kwargs: pytest.fail("Invalid link metadata must not be delivered"),
    )

    result = await execute_link(client, channel_id="C123")

    assert "trusted Founder Tools account link" in result


def test_localhost_link_requires_explicit_development_origin():
    link_url = f"http://localhost:3000/founder-tools/link-roo?token={LINK_TOKEN}"
    development = SimpleNamespace(
        founder_tools_link_origins={"http://localhost:3000"},
        is_production=False,
    )
    production = SimpleNamespace(
        founder_tools_link_origins={"http://localhost:3000"},
        is_production=True,
    )

    assert SkillExecutor._is_safe_founder_account_link_url(
        link_url,
        settings=development,
    )
    assert not SkillExecutor._is_safe_founder_account_link_url(
        link_url,
        settings=production,
    )


@pytest.mark.asyncio
async def test_unknown_slack_user_returns_registration_guidance():
    request = httpx.Request(
        "POST",
        "https://backend.test/api/v1/users/slack-founder-link/start/",
    )
    response = httpx.Response(404, request=request, json={"code": "slack_user_not_found"})
    client = FakeLinkClient(error=httpx.HTTPStatusError("not found", request=request, response=response))

    result = await execute_link(client)

    assert "points command first" in result
