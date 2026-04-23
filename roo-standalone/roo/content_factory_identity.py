from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from .agent import get_agent


CONTENT_FACTORY_STALE_ACTION_TEXT = (
    "This action came from an older or incomplete message and can't be resumed safely. Ask Roo again."
)


class ContentFactoryIdentityResolutionError(ValueError):
    """Raised when a Content Factory action identity cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedContentFactoryIdentity:
    requested_by_slack_user_id: str
    effective_slack_user_id: str
    source: str
    is_delegated: bool


def clean_slack_user_id(raw_value: Any) -> Optional[str]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"<@([A-Z0-9]+)>", value)
    if match:
        return match.group(1)
    return value


def is_delegated_content_factory_request(
    requested_by_slack_user_id: Optional[str],
    effective_slack_user_id: Optional[str],
) -> bool:
    requested_by = str(requested_by_slack_user_id or "").strip()
    effective = str(effective_slack_user_id or "").strip()
    return bool(requested_by and effective and requested_by != effective)


def resolve_content_factory_identity_context(
    *,
    requester_slack_user_id: Optional[str],
    requested_by_slack_user_id: Optional[str] = None,
    effective_slack_user_id: Optional[str] = None,
) -> ResolvedContentFactoryIdentity:
    requested_by = (
        clean_slack_user_id(requested_by_slack_user_id)
        or clean_slack_user_id(requester_slack_user_id)
        or clean_slack_user_id(effective_slack_user_id)
    )
    effective = clean_slack_user_id(effective_slack_user_id) or requested_by
    if not requested_by or not effective:
        raise ValueError("Both requested_by_slack_user_id and effective_slack_user_id must resolve to non-empty values.")
    return ResolvedContentFactoryIdentity(
        requested_by_slack_user_id=requested_by,
        effective_slack_user_id=effective,
        source="runtime",
        is_delegated=is_delegated_content_factory_request(requested_by, effective),
    )


def build_content_factory_identity_payload(
    *,
    requested_by_slack_user_id: Optional[str],
    effective_slack_user_id: Optional[str],
    **extra: Any,
) -> dict[str, Any]:
    identity = resolve_content_factory_identity_context(
        requester_slack_user_id=None,
        requested_by_slack_user_id=requested_by_slack_user_id,
        effective_slack_user_id=effective_slack_user_id,
    )
    payload = dict(extra)
    payload["requested_by_slack_user_id"] = identity.requested_by_slack_user_id
    payload["effective_slack_user_id"] = identity.effective_slack_user_id
    return payload


def _thread_context_identity(
    channel_id: Optional[str],
    thread_ts: Optional[str],
) -> Optional[ResolvedContentFactoryIdentity]:
    if not channel_id or not thread_ts:
        return None
    try:
        thread_context = get_agent().get_thread_context(channel_id, thread_ts) or {}
    except Exception:
        thread_context = {}

    requested_by = clean_slack_user_id(thread_context.get("requested_by_slack_user_id"))
    effective = clean_slack_user_id(thread_context.get("effective_slack_user_id"))
    if bool(requested_by) != bool(effective):
        raise ContentFactoryIdentityResolutionError(CONTENT_FACTORY_STALE_ACTION_TEXT)
    if not requested_by or not effective:
        return None
    return ResolvedContentFactoryIdentity(
        requested_by_slack_user_id=requested_by,
        effective_slack_user_id=effective,
        source="thread_context",
        is_delegated=is_delegated_content_factory_request(requested_by, effective),
    )


def resolve_content_factory_action_identity(
    *,
    value_data: Optional[dict[str, Any]] = None,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> ResolvedContentFactoryIdentity:
    resolved_value_data = value_data if isinstance(value_data, dict) else {}
    explicit_requested_by = clean_slack_user_id(
        resolved_value_data.get("requested_by_slack_user_id")
    )
    explicit_effective = clean_slack_user_id(
        resolved_value_data.get("effective_slack_user_id")
    )
    legacy_slack_user_id = clean_slack_user_id(resolved_value_data.get("slack_user_id"))

    if bool(explicit_requested_by) != bool(explicit_effective):
        raise ContentFactoryIdentityResolutionError(CONTENT_FACTORY_STALE_ACTION_TEXT)

    if explicit_requested_by and explicit_effective:
        return ResolvedContentFactoryIdentity(
            requested_by_slack_user_id=explicit_requested_by,
            effective_slack_user_id=explicit_effective,
            source="payload",
            is_delegated=is_delegated_content_factory_request(
                explicit_requested_by,
                explicit_effective,
            ),
        )

    if legacy_slack_user_id:
        return ResolvedContentFactoryIdentity(
            requested_by_slack_user_id=legacy_slack_user_id,
            effective_slack_user_id=legacy_slack_user_id,
            source="legacy_slack_user_id",
            is_delegated=False,
        )

    thread_identity = _thread_context_identity(channel_id, thread_ts)
    if thread_identity is not None:
        return thread_identity

    raise ContentFactoryIdentityResolutionError(CONTENT_FACTORY_STALE_ACTION_TEXT)
