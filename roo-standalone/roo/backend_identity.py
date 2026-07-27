"""Per-Slack-task identity and actor assertions for private backend requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional
from uuid import UUID


SERVICE_TOKEN_PATTERN = re.compile(
    r"^mlai_sp_(?P<credential_id>[0-9a-f]{32})\.(?P<secret>[A-Za-z0-9_-]{32,128})$"
)


class BackendIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class BackendActorContext:
    slack_team_id: str
    acting_slack_user_id: str
    slack_channel_id: str
    slack_thread_ts: str
    event_id: str


_current_backend_actor: ContextVar[Optional[BackendActorContext]] = ContextVar(
    "roo_backend_actor",
    default=None,
)


def get_backend_actor_context() -> Optional[BackendActorContext]:
    return _current_backend_actor.get()


@contextmanager
def use_backend_actor_context(context: BackendActorContext) -> Iterator[None]:
    token = _current_backend_actor.set(context)
    try:
        yield
    finally:
        _current_backend_actor.reset(token)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def build_org_memory_identity_headers(
    service_principal_token: str,
    *,
    context: BackendActorContext,
    request_id: str,
    issued_at: Optional[int] = None,
    ttl_seconds: int = 45,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    match = SERVICE_TOKEN_PATTERN.fullmatch(str(service_principal_token or "").strip())
    if not match:
        raise BackendIdentityError("ORG_BRAIN_API_KEY is not a service-principal credential")
    if not request_id:
        raise BackendIdentityError("A request ID is required for an actor assertion")
    if (
        not context.slack_team_id
        or not context.acting_slack_user_id
        or not context.slack_channel_id
        or not context.event_id
    ):
        raise BackendIdentityError("Slack team, actor, channel, and event identity are required")

    now = int(time.time()) if issued_at is None else int(issued_at)
    payload = {
        "v": 1,
        "kid": str(UUID(hex=match.group("credential_id"))),
        "surface": "admin_roo",
        "slack_team_id": context.slack_team_id,
        "acting_slack_user_id": context.acting_slack_user_id,
        "slack_channel_id": context.slack_channel_id or "",
        "slack_thread_ts": context.slack_thread_ts or "",
        "event_id": context.event_id,
        "request_id": request_id,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    encoded_payload = _b64url(_canonical_payload(payload))
    signature = hmac.new(
        service_principal_token.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    assertion = f"{encoded_payload}.{_b64url(signature)}"
    return {
        "Content-Type": "application/json",
        "Authorization": f"ServicePrincipal {service_principal_token}",
        "X-MLAI-Actor-Assertion": assertion,
        "X-Roo-Surface": "admin_roo",
        "X-Slack-Team-ID": context.slack_team_id,
        "X-Acting-Slack-User-ID": context.acting_slack_user_id,
        "X-Slack-Channel-ID": context.slack_channel_id or "",
        "X-Slack-Thread-TS": context.slack_thread_ts or "",
        "X-Slack-Event-ID": context.event_id,
        "X-Request-ID": request_id,
    }


def build_victor_ai_identity_headers(
    signing_secret: str,
    *,
    context: BackendActorContext,
    request_id: str,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    """Build a single-use, channel-bound assertion for Victor application reads."""

    secret = str(signing_secret or "")
    if len(secret) < 32:
        raise BackendIdentityError("VICTOR_AI_ROO_SIGNING_SECRET is not configured")
    if not request_id:
        raise BackendIdentityError("A request ID is required for a Victor AI assertion")
    if (
        not context.slack_team_id
        or not context.acting_slack_user_id
        or not context.slack_channel_id
        or not context.event_id
    ):
        raise BackendIdentityError("Slack team, actor, channel, and event identity are required")

    issued_at = int(time.time()) if timestamp is None else int(timestamp)
    assertion_nonce = nonce or secrets.token_urlsafe(24)
    payload = {
        "acting_slack_user_id": context.acting_slack_user_id,
        "event_id": context.event_id,
        "nonce": assertion_nonce,
        "request_id": request_id,
        "slack_channel_id": context.slack_channel_id,
        "slack_team_id": context.slack_team_id,
        "slack_thread_ts": context.slack_thread_ts or "",
        "surface": "public_roo",
        "timestamp": issued_at,
        "v": 1,
    }
    signature = hmac.new(
        secret.encode("utf-8"),
        _canonical_payload(payload),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Victor-Roo-Signature": f"v1={signature}",
        "X-Victor-Roo-Timestamp": str(issued_at),
        "X-Victor-Roo-Nonce": assertion_nonce,
        "X-Roo-Surface": "public_roo",
        "X-Slack-Team-ID": context.slack_team_id,
        "X-Acting-Slack-User-ID": context.acting_slack_user_id,
        "X-Slack-Channel-ID": context.slack_channel_id,
        "X-Slack-Thread-TS": context.slack_thread_ts or "",
        "X-Slack-Event-ID": context.event_id,
        "X-Request-ID": request_id,
    }
