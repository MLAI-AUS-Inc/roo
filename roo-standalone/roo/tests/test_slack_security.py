import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import main as main_module
from roo import slack_client as slack_client_module
from roo.config import Settings, get_settings
from roo.logging_safety import sanitize_log_value
from roo.slack_security import (
    SlackRequestReceiptStore,
    SlackRequestVerificationError,
    get_slack_receipt_store,
    verify_slack_request,
)


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + str(timestamp).encode("ascii") + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def _signed_headers(secret: str, timestamp: int, body: bytes, content_type: str):
    return {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": _signature(secret, timestamp, body),
        "Content-Type": content_type,
    }


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic",
        "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
        "SLACK_RECEIPTS_DB_PATH": str(tmp_path / "slack-receipts.db"),
        "OPENAI_API_KEY": "synthetic-openai-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_recursive_log_sanitizer_redacts_keys_values_and_workspace_ids():
    email = "private-link@example.com"
    token = "AUniqueAccountLinkToken_12345678901234567890"
    workspace_id = "TWORKSPACE123"

    sanitized = json.dumps(
        sanitize_log_value(
            {
                email: {
                    token: [workspace_id],
                }
            }
        )
    )

    for sentinel in (email, token, workspace_id):
        assert sentinel not in sanitized


@pytest.fixture(autouse=True)
def clear_receipt_store_cache():
    get_slack_receipt_store.cache_clear()
    yield
    get_slack_receipt_store.cache_clear()
    main_module.app.dependency_overrides.clear()


def test_raw_body_hmac_verification_rejects_tampering_and_old_requests():
    secret = "secret"
    now = 1_700_000_000
    body = b'{"type":"event_callback"}'
    signature = _signature(secret, now, body)

    fingerprint = verify_slack_request(
        signing_secret=secret,
        timestamp=str(now),
        signature=signature,
        raw_body=body,
        now=now,
    )

    assert len(fingerprint) == 64
    with pytest.raises(SlackRequestVerificationError, match="does not match"):
        verify_slack_request(
            signing_secret=secret,
            timestamp=str(now),
            signature=signature,
            raw_body=body + b" ",
            now=now,
        )
    with pytest.raises(SlackRequestVerificationError, match="replay window"):
        verify_slack_request(
            signing_secret=secret,
            timestamp=str(now - 301),
            signature=_signature(secret, now - 301, body),
            raw_body=body,
            now=now,
        )


def test_receipt_store_deduplicates_across_instances_and_expires(tmp_path):
    fingerprint = "a" * 64
    first_store = SlackRequestReceiptStore(tmp_path / "receipts.db")
    second_store = SlackRequestReceiptStore(tmp_path / "receipts.db")

    assert first_store.claim(fingerprint, now=1000, ttl_seconds=600)
    assert not second_store.claim(fingerprint, now=1001, ttl_seconds=600)
    assert second_store.claim(fingerprint, now=1601, ttl_seconds=600)


@pytest.mark.parametrize(
    ("path", "body", "content_type"),
    (
        (
            "/slack/events",
            b'{"type":"url_verification","challenge":"challenge-1"}',
            "application/json",
        ),
        (
            "/slack/commands",
            b"command=%2Froo&text=hello&user_id=U123&channel_id=C123",
            "application/x-www-form-urlencoded",
        ),
        (
            "/slack/actions",
            urlencode({"payload": json.dumps({"actions": []})}).encode("utf-8"),
            "application/x-www-form-urlencoded",
        ),
    ),
)
def test_all_slack_endpoints_reject_invalid_signatures(tmp_path, path, body, content_type):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    timestamp = int(time.time())
    headers = {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": "v0=" + "0" * 64,
        "Content-Type": content_type,
    }

    response = client.post(path, content=body, headers=headers)

    assert response.status_code == 403


def test_signed_url_verification_returns_challenge(tmp_path):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    body = b'{"type":"url_verification","challenge":"challenge-1"}'
    timestamp = int(time.time())

    response = client.post(
        "/slack/events",
        content=body,
        headers=_signed_headers(
            configured.SLACK_SIGNING_SECRET,
            timestamp,
            body,
            "application/json",
        ),
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-1"}


def test_duplicate_signed_command_is_acknowledged_without_reexecution(tmp_path):
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    body = b"command=%2Froo&text=hello&user_id=U123&channel_id=C123"
    timestamp = int(time.time())
    headers = _signed_headers(
        configured.SLACK_SIGNING_SECRET,
        timestamp,
        body,
        "application/x-www-form-urlencoded",
    )

    first = client.post("/slack/commands", content=body, headers=headers)
    second = client.post("/slack/commands", content=body, headers=headers)

    assert first.status_code == 200
    assert "received" in first.json()["text"]
    assert second.status_code == 200
    assert second.json() == {}


def test_admin_surface_ignores_signed_event_outside_allowlist(tmp_path, monkeypatch):
    configured = _settings(
        tmp_path,
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
    )
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append(coro)

    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "CPUBLIC123",
                "user": "UADMIN123",
                "ts": "1.2",
                "text": "hello",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = int(time.time())

    response = client.post(
        "/slack/events",
        content=body,
        headers=_signed_headers(
            configured.SLACK_SIGNING_SECRET,
            timestamp,
            body,
            "application/json",
        ),
    )

    assert response.status_code == 200
    assert scheduled == []


def test_internal_mention_endpoint_is_disabled_or_bearer_authenticated(tmp_path, monkeypatch):
    configured = _settings(tmp_path, INTERNAL_MENTION_API_KEY="internal-mention-key")
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    captured = {}

    class FakeAgent:
        async def handle_mention(self, **kwargs):
            captured.update(kwargs)
            return {"message": "ok", "skill_used": None}

    monkeypatch.setattr(main_module, "get_agent", lambda: FakeAgent())
    payload = {"text": "hello", "user_id": "UADMIN123", "channel_id": "C123"}

    denied = client.post("/api/mention", json=payload)
    allowed = client.post(
        "/api/mention",
        json=payload,
        headers={"Authorization": "Bearer internal-mention-key"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert captured["user_id"] == "UADMIN123"

    admin_settings = _settings(
        tmp_path,
        ROO_SURFACE="admin",
        ROO_ALLOWED_DM_USER_IDS="UADMIN123",
        INTERNAL_MENTION_API_KEY="internal-mention-key",
    )
    main_module.app.dependency_overrides[get_settings] = lambda: admin_settings
    admin_response = client.post(
        "/api/mention",
        json=payload,
        headers={"Authorization": "Bearer internal-mention-key"},
    )
    assert admin_response.status_code == 404


@pytest.mark.asyncio
async def test_account_link_mention_ingress_logs_no_identity_or_token_sentinels(
    monkeypatch,
    capsys,
):
    token = "AUniqueAccountLinkToken_12345678901234567890"
    email = "private-link@example.com"
    slack_user_id = "UACCOUNT123"
    channel_id = "CSECRET123"
    thread_ts = "1758000000.123456"
    text = (
        "<@UROOBOT123> link https://mlai.au/founder-tools/link-roo?token="
        f"{token} for {email} <@{slack_user_id}>"
    )

    class FakeAgent:
        async def handle_mention(self, **kwargs):
            assert kwargs["user_id"] == slack_user_id
            assert kwargs["channel_id"] == channel_id
            return {
                "message": "",
                "skill_used": "mlai-points",
                "data": {"action": "link_founder_account"},
                "suppress_post": True,
            }

    monkeypatch.setattr(main_module, "get_agent", lambda: FakeAgent())

    result = await main_module._handle_mention(
        {
            "user": slack_user_id,
            "channel": channel_id,
            "thread_ts": thread_ts,
            "ts": thread_ts,
            "text": text,
        }
    )

    assert result["result"]["data"]["action"] == "link_founder_account"
    output = capsys.readouterr().out
    for sentinel in (token, email, slack_user_id, channel_id, thread_ts):
        assert sentinel not in output
    assert "destination_type=channel" in output
    assert "[url]" in output


def test_account_link_button_click_log_excludes_slack_identity(tmp_path, capsys):
    configured = _settings(tmp_path, FOUNDER_ACCOUNT_LINK_ENABLED=True)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    client = TestClient(main_module.app)
    slack_user_id = "UACCOUNTLINKSENTINEL"
    payload = {
        "type": "block_actions",
        "user": {"id": slack_user_id},
        "channel": {"id": "DPRIVATE123"},
        "team": {"id": "TWORKSPACE123"},
        "message": {"ts": "1758000000.123456"},
        "actions": [{"action_id": "roo_link_founder_account"}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    timestamp = int(time.time())

    response = client.post(
        "/slack/actions",
        content=body,
        headers=_signed_headers(
            configured.SLACK_SIGNING_SECRET,
            timestamp,
            body,
            "application/x-www-form-urlencoded",
        ),
    )

    assert response.status_code == 200
    output = capsys.readouterr().out
    assert "FOUNDER_ACCOUNT_LINK_BUTTON_CLICKED" in output
    assert slack_user_id not in output


@pytest.mark.parametrize(
    "channel_payload",
    [
        {"id": "CPUBLIC123"},
        {"id": "GPRIVATE123"},
        {"id": ""},
        None,
        "DFAKE123",
    ],
)
def test_send_dm_never_posts_to_a_non_dm_channel(monkeypatch, channel_payload):
    class FakeSlackClient:
        def conversations_open(self, **kwargs):
            return {"ok": True, "channel": channel_payload}

    posted = []
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )
    monkeypatch.setattr(
        slack_client_module,
        "post_message",
        lambda *args, **kwargs: posted.append((args, kwargs)),
    )

    result = slack_client_module.send_dm("U123", "private")

    assert result is None
    assert posted == []


def test_send_dm_posts_only_after_validating_a_dm_channel(monkeypatch):
    class FakeSlackClient:
        def conversations_open(self, **kwargs):
            return {"ok": True, "channel": {"id": "DPRIVATE123"}}

    posted = []
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )
    monkeypatch.setattr(
        slack_client_module,
        "post_message",
        lambda *args, **kwargs: posted.append((args, kwargs)) or {"ok": True},
    )

    result = slack_client_module.send_dm("U123", "private", blocks=[])

    assert result == {"ok": True}
    assert posted == [
        (
            ("DPRIVATE123", "private"),
            {"_redact_destination": True, "blocks": []},
        )
    ]


def test_send_dm_open_failure_logs_no_identity_or_external_error(monkeypatch, capsys):
    class FakeSlackClient:
        def conversations_open(self, **kwargs):
            raise RuntimeError("Slack failed for UPRIVATE1 private@example.com\nforged")

    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )

    assert slack_client_module.send_dm("UPRIVATE1", "private") is None

    output = capsys.readouterr().out
    assert "UPRIVATE1" not in output
    assert "private@example.com" not in output
    assert "forged" not in output
    assert "error_type=RuntimeError" in output


def test_send_dm_post_failure_logs_no_dm_channel_or_error_payload(
    monkeypatch,
    capsys,
):
    class FakeSlackClient:
        def conversations_open(self, **kwargs):
            return {"ok": True, "channel": {"id": "DPRIVATE123"}}

        def chat_postMessage(self, **kwargs):
            return {
                "ok": False,
                "error": "ratelimited UPRIVATE1 private@example.com\nforged",
            }

    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )

    response = slack_client_module.send_dm("UPRIVATE1", "private")

    assert response["ok"] is False
    output = capsys.readouterr().out
    assert "DPRIVATE123" not in output
    assert "UPRIVATE1" not in output
    assert "private@example.com" not in output
    assert "ratelimited" not in output
    assert "forged" not in output
    assert "reason_code=slack_api_error" in output


@pytest.mark.parametrize("outcome", ["success", "api_error", "exception"])
def test_shared_message_logging_never_exposes_destination_or_transport_payload(
    monkeypatch,
    capsys,
    outcome,
):
    channel_id = "CSECRET123"
    thread_ts = "1758000000.123456"
    slack_user_id = "UACCOUNT123"
    email = "private-link@example.com"
    token = "AUniqueAccountLinkToken_12345678901234567890"

    class FakeSlackClient:
        def chat_postMessage(self, **kwargs):
            if outcome == "success":
                return {"ok": True}
            if outcome == "api_error":
                return {
                    "ok": False,
                    "error": f"failed {slack_user_id} {email} {token}",
                }
            raise RuntimeError(f"failed {slack_user_id} {email} {token}")

    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )

    if outcome == "exception":
        with pytest.raises(RuntimeError):
            slack_client_module.post_message(
                channel_id,
                "sensitive body",
                thread_ts=thread_ts,
            )
    else:
        slack_client_module.post_message(
            channel_id,
            "sensitive body",
            thread_ts=thread_ts,
        )

    output = capsys.readouterr().out
    for sentinel in (
        channel_id,
        thread_ts,
        slack_user_id,
        email,
        token,
    ):
        assert sentinel not in output
    assert "destination_type=channel" in output or "Slack message failed" in output


def test_shared_ephemeral_logging_never_exposes_identity_or_error_payload(
    monkeypatch,
    capsys,
):
    channel_id = "CSECRET123"
    slack_user_id = "UACCOUNT123"
    thread_ts = "1758000000.123456"
    token = "AUniqueAccountLinkToken_12345678901234567890"

    class FakeSlackClient:
        def chat_postEphemeral(self, **kwargs):
            return {
                "ok": False,
                "error": f"failed {slack_user_id} {token}",
            }

    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )

    slack_client_module.post_ephemeral(
        channel_id,
        slack_user_id,
        "private text",
        thread_ts=thread_ts,
    )

    output = capsys.readouterr().out
    for sentinel in (channel_id, slack_user_id, thread_ts, token):
        assert sentinel not in output
    assert "reason_code=slack_api_error" in output


def test_channel_context_failure_logs_no_destination_or_error_payload(
    monkeypatch,
    capsys,
):
    channel_id = "CSECRETCONTEXT123"
    slack_user_id = "UACCOUNT123"
    token = "AUniqueAccountLinkToken_12345678901234567890"

    class FakeSlackClient:
        def conversations_info(self, **kwargs):
            raise RuntimeError(f"failed {slack_user_id} {token}")

    slack_client_module._channel_context_cache.clear()
    monkeypatch.setattr(
        slack_client_module,
        "get_slack_client",
        lambda: FakeSlackClient(),
    )

    assert slack_client_module.get_channel_context(channel_id) == {}

    output = capsys.readouterr().out
    for sentinel in (channel_id, slack_user_id, token):
        assert sentinel not in output
    assert "error_type=RuntimeError" in output
