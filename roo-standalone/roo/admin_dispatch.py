"""Authenticated, replay-safe dispatch between the Roo gateway and Admin worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Mapping, Optional

import httpx

from .backend_identity import BackendActorContext
from .slack_security import get_slack_receipt_store


DISPATCH_KINDS = frozenset({"query", "feedback"})
SIGNATURE_RE = re.compile(r"^v1=[0-9a-f]{64}$")
TEAM_RE = re.compile(r"^T[A-Z0-9]+$")
USER_RE = re.compile(r"^[UW][A-Z0-9]+$")
CHANNEL_RE = re.compile(r"^[DGC][A-Z0-9]+$")
THREAD_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
ENVELOPE_KEYS = frozenset({"v", "kind", "actor", "payload", "iat", "exp", "nonce"})
ACTOR_KEYS = frozenset(
    {
        "slack_team_id",
        "acting_slack_user_id",
        "slack_channel_id",
        "slack_thread_ts",
        "event_id",
    }
)


class AdminDispatchError(ValueError):
    pass


class AdminDispatchUnavailable(RuntimeError):
    pass


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _signature(secret: str, envelope: Mapping[str, Any]) -> str:
    digest = hmac.new(
        str(secret).encode("utf-8"),
        _canonical_json(envelope),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def build_admin_dispatch(
    *,
    secret: str,
    kind: str,
    context: BackendActorContext,
    payload: Mapping[str, Any],
    issued_at: Optional[int] = None,
    ttl_seconds: int = 45,
    nonce: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    if len(str(secret or "")) < 32:
        raise AdminDispatchError("Admin dispatch secret is unavailable")
    if kind not in DISPATCH_KINDS:
        raise AdminDispatchError("Admin dispatch kind is invalid")
    now = int(time.time()) if issued_at is None else int(issued_at)
    envelope = {
        "v": 1,
        "kind": kind,
        "actor": {
            "slack_team_id": context.slack_team_id,
            "acting_slack_user_id": context.acting_slack_user_id,
            "slack_channel_id": context.slack_channel_id,
            "slack_thread_ts": context.slack_thread_ts or "",
            "event_id": context.event_id,
        },
        "payload": dict(payload),
        "iat": now,
        "exp": now + int(ttl_seconds),
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    _validate_envelope(envelope, now=now, max_age_seconds=max(ttl_seconds, 1))
    return envelope, _signature(secret, envelope)


def _validate_envelope(
    envelope: Mapping[str, Any],
    *,
    now: int,
    max_age_seconds: int,
) -> None:
    if not isinstance(envelope, Mapping) or set(envelope) != ENVELOPE_KEYS:
        raise AdminDispatchError("Admin dispatch schema is invalid")
    if envelope.get("v") != 1 or envelope.get("kind") not in DISPATCH_KINDS:
        raise AdminDispatchError("Admin dispatch version or kind is invalid")
    actor = envelope.get("actor")
    payload = envelope.get("payload")
    if not isinstance(actor, Mapping) or set(actor) != ACTOR_KEYS:
        raise AdminDispatchError("Admin dispatch actor schema is invalid")
    if not isinstance(payload, Mapping):
        raise AdminDispatchError("Admin dispatch payload must be an object")
    if not TEAM_RE.fullmatch(str(actor.get("slack_team_id") or "")):
        raise AdminDispatchError("Admin dispatch Slack team is invalid")
    if not USER_RE.fullmatch(str(actor.get("acting_slack_user_id") or "")):
        raise AdminDispatchError("Admin dispatch Slack actor is invalid")
    if not CHANNEL_RE.fullmatch(str(actor.get("slack_channel_id") or "")):
        raise AdminDispatchError("Admin dispatch Slack channel is invalid")
    thread_ts = str(actor.get("slack_thread_ts") or "")
    if thread_ts and not THREAD_RE.fullmatch(thread_ts):
        raise AdminDispatchError("Admin dispatch Slack thread is invalid")
    if not IDENTIFIER_RE.fullmatch(str(actor.get("event_id") or "")):
        raise AdminDispatchError("Admin dispatch event ID is invalid")
    if not NONCE_RE.fullmatch(str(envelope.get("nonce") or "")):
        raise AdminDispatchError("Admin dispatch nonce is invalid")
    if type(envelope.get("iat")) is not int or type(envelope.get("exp")) is not int:
        raise AdminDispatchError("Admin dispatch timestamps are invalid")
    issued_at = int(envelope["iat"])
    expires_at = int(envelope["exp"])
    if issued_at > now + 5 or expires_at <= now:
        raise AdminDispatchError("Admin dispatch is expired or not yet valid")
    if expires_at <= issued_at or expires_at - issued_at > max_age_seconds:
        raise AdminDispatchError("Admin dispatch lifetime is invalid")


def verify_and_claim_admin_dispatch(
    *,
    secret: str,
    signature: str,
    raw_body: bytes,
    receipt_db_path: str,
    max_age_seconds: int = 60,
    receipt_ttl_seconds: int = 600,
    now: Optional[int] = None,
) -> dict[str, Any]:
    if len(str(secret or "")) < 32:
        raise AdminDispatchError("Admin dispatch secret is unavailable")
    supplied = str(signature or "").strip().lower()
    if not SIGNATURE_RE.fullmatch(supplied):
        raise AdminDispatchError("Admin dispatch signature is invalid")
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminDispatchError("Admin dispatch body is invalid") from exc
    current_time = int(time.time()) if now is None else int(now)
    _validate_envelope(
        envelope,
        now=current_time,
        max_age_seconds=max_age_seconds,
    )
    expected = _signature(secret, envelope)
    if not hmac.compare_digest(expected, supplied):
        raise AdminDispatchError("Admin dispatch signature does not match")
    fingerprint = hashlib.sha256(_canonical_json(envelope)).hexdigest()
    if not get_slack_receipt_store(str(receipt_db_path)).claim(
        fingerprint,
        now=float(current_time),
        ttl_seconds=receipt_ttl_seconds,
    ):
        raise AdminDispatchError("Admin dispatch has already been used")
    return dict(envelope)


def actor_context_from_dispatch(envelope: Mapping[str, Any]) -> BackendActorContext:
    actor = envelope["actor"]
    return BackendActorContext(
        slack_team_id=str(actor["slack_team_id"]),
        acting_slack_user_id=str(actor["acting_slack_user_id"]),
        slack_channel_id=str(actor["slack_channel_id"]),
        slack_thread_ts=str(actor.get("slack_thread_ts") or ""),
        event_id=str(actor["event_id"]),
    )


class AdminDispatchClient:
    def __init__(self, *, base_url: str, secret: str, timeout: float = 25.0):
        self.base_url = str(base_url or "").rstrip("/")
        self.secret = str(secret or "")
        self.timeout = float(timeout)

    async def dispatch(
        self,
        *,
        kind: str,
        context: BackendActorContext,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        envelope, signature = build_admin_dispatch(
            secret=self.secret,
            kind=kind,
            context=context,
            payload=payload,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/internal/admin/{kind}",
                    json=envelope,
                    headers={"X-Roo-Dispatch-Signature": signature},
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdminDispatchUnavailable("Internal Admin Roo is unavailable") from exc
        if not isinstance(body, dict):
            raise AdminDispatchUnavailable("Internal Admin Roo returned an invalid response")
        return body
