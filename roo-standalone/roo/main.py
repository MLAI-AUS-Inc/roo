from __future__ import annotations

"""
Roo Standalone - FastAPI Application

Main entrypoint for the Roo AI agent service.
"""
import asyncio
import json
import hmac
import re
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from uuid import UUID, uuid4
import httpx

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from .config import get_settings, Settings, validate_runtime_security
from .agent import RooAgent, get_agent
from .addressing import (
    candidate_reason_for_message,
    contains_bot_mention,
    decide_addressing,
)
from .conversation_sessions import get_contextual_conversation_store
from .content_factory_progress import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    build_live_status_blocks,
    get_content_factory_article_cost_points,
    normalize_content_factory_domain,
)
from .content_factory_identity import (
    CONTENT_FACTORY_STALE_ACTION_TEXT,
    ContentFactoryIdentityResolutionError,
    build_content_factory_identity_payload as _content_factory_identity_payload,
    clean_slack_user_id as _clean_slack_user_id,
    is_delegated_content_factory_request as _is_delegated_content_factory_request,
    resolve_content_factory_action_identity,
)
from .points_request_approval import (
    APPROVAL_REACTION_NAMES,
    forget_points_request_summary,
    get_points_request_record_from_message,
    get_remembered_points_request_summary,
    remember_points_request_summary,
)
from .slack_client import (
    get_bot_user_id,
    get_message,
    get_recent_channel_messages,
    get_thread_messages,
    post_message,
    send_dm,
)
from .coworking_booking_intents import coworking_booking_retry_loop
from .boost_moderation import (
    boost_post_retry_loop,
    handle_boost_root_edit,
    handle_boost_root_post,
    mark_boost_root_removed,
)
from .link_love import handle_link_love_reply, link_love_retry_loop
from .slack_moderation import validate_slack_moderator_configuration
from .start_here_introductions import (
    handle_start_here_intro,
    normalize_intro_event,
    start_here_intro_retry_loop,
)
from .slack_security import (
    SlackRequestVerificationError,
    verify_and_claim_slack_request,
)
from .backend_identity import (
    BackendActorContext,
    get_backend_actor_context,
    use_backend_actor_context,
)
from .admin_dispatch import AdminDispatchClient
from .admin_brain import (
    ADMIN_ACTION_APPROVE,
    ADMIN_ACTION_REJECT,
    ADMIN_BRAIN_FEEDBACK_ACTIONS,
    ADMIN_BRAIN_INCORRECT_ACTION,
    ADMIN_BRAIN_INCORRECT_CALLBACK,
    build_admin_action_reject_modal,
    build_admin_action_response,
    build_incorrect_feedback_modal,
    parse_admin_action_reject_submission,
    parse_admin_action_value,
    parse_feedback_value,
    parse_incorrect_feedback_submission,
)

# Pending intents for auto-continue after prerequisite steps complete.
# Key: "{slack_user_id}:{domain}" → {"action": "write", "topic": "...", "channel_id": "...", "thread_ts": "..."}
_pending_intents: dict = {}
_pending_intents_by_job: dict[str, str] = {}
PENDING_INTENT_TTL_SECONDS = 30 * 60
APP_MENTION_DEDUPE_TTL_SECONDS = 10 * 60
_recent_app_mention_events: dict[str, float] = {}
CONTENT_FACTORY_WATCHDOG_POLL_SECONDS = 120
CONTENT_FACTORY_WATCHDOG_STOP_STATUSES = {
    "awaiting_confirmation",
    "awaiting_delivery_mode",
    "awaiting_approval",
    "approval_required",
    "auth_required",
    "completed",
    "failed",
    "error",
    "blocked",
    "blocked_verification",
    "denied",
    "cancelled",
}


def _is_duplicate_slack_request(request: Request) -> bool:
    state = getattr(request, "state", None)
    return bool(getattr(state, "slack_duplicate", False))


def _request_settings(request: Request) -> Settings:
    state = getattr(request, "state", None)
    return getattr(state, "roo_settings", None) or get_settings()


def _is_slack_context_allowed(
    settings: Settings,
    *,
    channel_id: Optional[str],
    user_id: Optional[str],
    channel_type: Optional[str] = None,
) -> bool:
    if getattr(settings, "ROO_SURFACE", "public") == "public":
        return True
    checker = getattr(settings, "is_slack_context_allowed", None)
    if not callable(checker):
        return False
    return bool(
        checker(
            channel_id=channel_id,
            user_id=user_id,
            channel_type=channel_type,
        )
    )


def _is_contextual_channel_enabled(settings: Settings, channel_id: Optional[str]) -> bool:
    """Return true only for explicitly allowlisted Public Roo pilot channels."""

    if getattr(settings, "ROO_SURFACE", "public") != "public":
        return False
    if not bool(getattr(settings, "ROO_CONTEXTUAL_RESPONSES_ENABLED", False)):
        return False
    configured = getattr(settings, "contextual_channel_ids", frozenset())
    return bool(channel_id and channel_id in configured)


def _log_addressing_decision(
    *,
    event: dict[str, Any],
    decision: str,
    candidate_reason: Optional[str],
    source: str,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    shadow_mode: bool = False,
) -> None:
    """Emit metadata-only addressing telemetry; never log channel message text."""

    payload = {
        "event": "addressing_decision",
        "decision": decision,
        "candidate_reason": candidate_reason,
        "source": source,
        "confidence": confidence,
        "reason": reason,
        "shadow_mode": shadow_mode,
        "channel_id": event.get("channel"),
        "thread_ts": event.get("thread_ts"),
        "message_ts": event.get("ts"),
        "user_id": event.get("user"),
    }
    print("ADDRESSING_DECISION " + json.dumps(payload, ensure_ascii=True, default=str))


async def _get_addressing_history(event: dict[str, Any]) -> list[dict]:
    channel_id = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "")
    message_ts = str(event.get("ts") or "")
    if not channel_id:
        return []
    if thread_ts:
        return await asyncio.to_thread(get_thread_messages, channel_id, thread_ts)
    return await asyncio.to_thread(
        get_recent_channel_messages,
        channel_id,
        before_ts=message_ts or None,
        limit=8,
        lookback_hours=1,
    )


async def _handle_contextual_slack_message(
    event: dict[str, Any],
    *,
    slack_team_id: str,
    trigger_source: str,
) -> Optional[dict[str, Any]]:
    """Gate one pilot-channel message before handing it to the existing agent."""

    settings = get_settings()
    team_id = str(slack_team_id or event.get("team") or "").strip()
    channel_id = str(event.get("channel") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    user_id = str(event.get("user") or "").strip()
    thread_ts = str(event.get("thread_ts") or "").strip() or None
    if not team_id or not channel_id or not message_ts or not user_id:
        return None

    # Resolve the cached bot identity before claiming the logical receipt. If
    # this Slack call fails, an app_mention delivery can still use its fallback.
    bot_user_id = await asyncio.to_thread(get_bot_user_id)
    store = get_contextual_conversation_store(settings.SLACK_CONTEXTUAL_STATE_DB_PATH)
    claimed = await asyncio.to_thread(
        store.claim_message,
        team_id=team_id,
        channel_id=channel_id,
        message_ts=message_ts,
        ttl_seconds=settings.ROO_CONTEXTUAL_MESSAGE_RECEIPT_TTL_SECONDS,
    )
    if not claimed:
        _log_addressing_decision(
            event=event,
            decision="ignore",
            candidate_reason=None,
            source="logical_dedupe",
            reason="duplicate_logical_message",
            shadow_mode=settings.ROO_CONTEXTUAL_SHADOW_MODE,
        )
        return None

    explicit_mention = trigger_source == "app_mention" or contains_bot_mention(
        str(event.get("text") or ""),
        bot_user_id,
    )
    session = await asyncio.to_thread(
        store.find_session,
        team_id=team_id,
        channel_id=channel_id,
        requester_user_id=user_id,
        thread_ts=thread_ts,
    )
    candidate_reason = candidate_reason_for_message(
        text=str(event.get("text") or ""),
        explicit_mention=explicit_mention,
        thread_ts=thread_ts,
        session=session,
    )
    if not candidate_reason:
        _log_addressing_decision(
            event=event,
            decision="ignore",
            candidate_reason=None,
            source="prefilter",
            reason="not_addressing_candidate",
            shadow_mode=settings.ROO_CONTEXTUAL_SHADOW_MODE,
        )
        if not thread_ts:
            await asyncio.to_thread(
                store.break_channel_adjacency,
                team_id=team_id,
                channel_id=channel_id,
            )
        return None

    history = await _get_addressing_history(event)
    address_decision = await decide_addressing(
        text=str(event.get("text") or ""),
        user_id=user_id,
        bot_user_id=bot_user_id,
        history=history,
        current_message_ts=message_ts,
        candidate_reason=candidate_reason,
        explicit_mention=explicit_mention,
        min_implicit_confidence=settings.ROO_CONTEXTUAL_MIN_CONFIDENCE,
        indirect_mention_confidence=settings.ROO_CONTEXTUAL_INDIRECT_MENTION_CONFIDENCE,
        model=(settings.ROO_CONTEXTUAL_MODEL or None),
        classifier_timeout_seconds=settings.ROO_CONTEXTUAL_CLASSIFIER_TIMEOUT_SECONDS,
    )
    shadow_mode = settings.ROO_CONTEXTUAL_SHADOW_MODE
    _log_addressing_decision(
        event=event,
        decision="respond" if address_decision.should_respond else "ignore",
        candidate_reason=candidate_reason,
        source=address_decision.source,
        confidence=address_decision.confidence,
        reason=address_decision.reason,
        shadow_mode=shadow_mode,
    )

    # Shadow mode observes implicit decisions but preserves direct-mention
    # behaviour and never sends an untagged reply.
    should_process = explicit_mention if shadow_mode else address_decision.should_respond
    if not should_process:
        if not thread_ts:
            await asyncio.to_thread(
                store.break_channel_adjacency,
                team_id=team_id,
                channel_id=channel_id,
            )
        return None

    routed_event = dict(event)
    routed_event["implicit_addressing"] = not explicit_mention
    routed_event["contextual_candidate_reason"] = candidate_reason
    outcome = await _handle_mention(routed_event)
    post_response = (outcome or {}).get("post_response") or {}
    bot_message_ts = str(post_response.get("ts") or "").strip()
    if bot_message_ts:
        await asyncio.to_thread(
            store.record_roo_response,
            team_id=team_id,
            channel_id=channel_id,
            requester_user_id=user_id,
            thread_ts=str((outcome or {}).get("thread_ts") or thread_ts or message_ts),
            bot_message_ts=bot_message_ts,
            adjacency_seconds=settings.ROO_CONTEXTUAL_ADJACENCY_SECONDS,
            thread_ttl_seconds=settings.ROO_CONTEXTUAL_THREAD_TTL_SECONDS,
        )
    elif not thread_ts:
        await asyncio.to_thread(
            store.break_channel_adjacency,
            team_id=team_id,
            channel_id=channel_id,
        )
    return outcome


async def _handle_contextual_slack_message_safely(
    event: dict[str, Any],
    *,
    slack_team_id: str,
    trigger_source: str,
) -> Optional[dict[str, Any]]:
    """Protect direct mentions from failures in the optional context layer."""

    try:
        return await _handle_contextual_slack_message(
            event,
            slack_team_id=slack_team_id,
            trigger_source=trigger_source,
        )
    except Exception as exc:
        _log_addressing_decision(
            event=event,
            decision="respond" if trigger_source == "app_mention" else "ignore",
            candidate_reason="explicit_mention" if trigger_source == "app_mention" else None,
            source="pipeline_error",
            reason=exc.__class__.__name__,
            shadow_mode=getattr(get_settings(), "ROO_CONTEXTUAL_SHADOW_MODE", True),
        )
        if trigger_source == "app_mention":
            return await _handle_mention(event)
        return None


def _looks_like_linear_meeting_file_request(text: str, has_files: bool = False) -> bool:
    text_lower = str(text or "").lower()
    has_linear = bool(re.search(r"\blinear\b", text_lower))
    has_source = bool(
        re.search(r"\b(file|pdf|docx?|document|image|screenshot|meeting|transcript|notes?|to-?dos?|action\s+items?)\b", text_lower)
    ) or has_files
    has_action = bool(re.search(r"\b(send|sync|turn|extract|create|add|tasks?|tickets?|issues?)\b", text_lower))
    return has_linear and has_source and has_action


def _is_manual_jobs_trigger_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False

    patterns = (
        r"\b(?:run|trigger|start)\b(?:\s+the)?\s+(?:daily\s+)?(?:ai\s+and\s+startup\s+)?jobs(?:\s+scrape)?(?:\s+now)?\b",
        r"\b(?:scrape|collect|fetch)\b(?:\s+the)?\s+(?:daily\s+)?(?:ai\s+and\s+startup\s+)?jobs(?:\s+now)?\b",
        r"\b(?:post|publish)\b(?:\s+the)?\s+(?:daily|today(?:'s)?)\s+(?:ai\s+and\s+startup\s+)?jobs(?:\s+now)?\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _app_mention_event_key(payload: dict[str, Any], event: dict[str, Any]) -> Optional[str]:
    team_id = (
        payload.get("team_id")
        or payload.get("team")
        or event.get("team")
        or ""
    )
    channel_id = str(event.get("channel") or "").strip()
    user_id = str(event.get("user") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    if not channel_id or not user_id or not message_ts:
        return None
    return f"{team_id}:{channel_id}:{user_id}:{message_ts}"


def _mark_app_mention_event_seen(
    payload: dict[str, Any],
    event: dict[str, Any],
    *,
    now: Optional[float] = None,
) -> bool:
    current_time = now if now is not None else time.monotonic()
    expired_keys = [
        key
        for key, recorded_at in _recent_app_mention_events.items()
        if current_time - recorded_at >= APP_MENTION_DEDUPE_TTL_SECONDS
    ]
    for key in expired_keys:
        _recent_app_mention_events.pop(key, None)
    if expired_keys:
        print(f"🧹 Slack app_mention dedupe TTL expired count={len(expired_keys)}")

    dedupe_key = _app_mention_event_key(payload, event)
    if not dedupe_key:
        return True

    if dedupe_key in _recent_app_mention_events:
        print(f"↩️ Slack app_mention dedupe hit key={dedupe_key}")
        return False

    _recent_app_mention_events[dedupe_key] = current_time
    return True


def _with_slack_delivery_context(event: dict, payload: dict) -> dict:
    """Attach envelope IDs only when Slack supplied both verified values."""

    team_id = str(
        payload.get("team_id") or payload.get("team") or event.get("team") or ""
    ).strip()
    event_id = str(payload.get("event_id") or "").strip()
    if not team_id or not event_id:
        return event
    return {
        **event,
        "_slack_team_id": team_id,
        "_slack_event_id": event_id,
    }


def _pending_intent_key(slack_user_id: Optional[str], domain: Optional[str]) -> Optional[str]:
    return _pending_intent_identity_key(slack_user_id, None, domain)


def _pending_intent_identity_key(
    requested_by_slack_user_id: Optional[str],
    effective_slack_user_id: Optional[str],
    domain: Optional[str],
) -> Optional[str]:
    if not requested_by_slack_user_id or not domain:
        return None
    effective_user = effective_slack_user_id or requested_by_slack_user_id
    return f"{requested_by_slack_user_id}:{effective_user}:{domain}"


def _pop_pending_intent_by_key(intent_key: Optional[str]) -> Optional[dict[str, Any]]:
    if not intent_key:
        return None
    pending = _pending_intents.pop(intent_key, None)
    if not pending:
        return None
    pending_job_id = pending.get("job_id")
    if pending_job_id and _pending_intents_by_job.get(pending_job_id) == intent_key:
        _pending_intents_by_job.pop(pending_job_id, None)
    return pending


def _prune_pending_intents(now: Optional[float] = None) -> None:
    current_time = now if now is not None else time.time()
    for intent_key, pending in list(_pending_intents.items()):
        expires_at = pending.get("expires_at")
        if expires_at is not None and expires_at <= current_time:
            _pop_pending_intent_by_key(intent_key)


def _content_factory_client_request_id(raw_value: Any) -> str:
    """Return a stable request id, generating one for older Slack buttons."""
    client_request_id = str(raw_value or "").strip()
    if client_request_id:
        return client_request_id
    return f"content-factory-{uuid4().hex}"


def _is_registry_driven_publish_target(target: Any) -> bool:
    if not isinstance(target, dict):
        return False
    strategy = target.get("registration_strategy")
    if not isinstance(strategy, dict):
        strategy = {}
    return (
        str(target.get("kind") or "").strip() == "registry_driven_seo"
        or str(target.get("delivery_adapter") or "").strip() == "registry_entry"
        or str(strategy.get("type") or "").strip() == "registry_entry_patch"
    )


def _registry_target_readiness(target: Any) -> dict[str, bool]:
    if not _is_registry_driven_publish_target(target):
        return {
            "structure_ready": False,
            "mapping_ready": False,
            "routing_ready": False,
            "safety_ready": False,
        }
    strategy = target.get("registration_strategy") if isinstance(target, dict) else {}
    if not isinstance(strategy, dict):
        strategy = {}
    readiness = target.get("readiness") if isinstance(target, dict) else {}
    if not isinstance(readiness, dict):
        readiness = strategy.get("readiness") if isinstance(strategy.get("readiness"), dict) else {}
    status = str(
        (target or {}).get("registry_status")
        or (target or {}).get("status")
        or readiness.get("status")
        or ""
    ).strip()
    all_ready = status == "publish_ready" or bool(readiness.get("publish_ready"))
    return {
        key: bool(readiness.get(key) or all_ready)
        for key in ("structure_ready", "mapping_ready", "routing_ready", "safety_ready")
    }


def _registry_target_publish_ready(target: Any) -> bool:
    if not _is_registry_driven_publish_target(target):
        return False
    return all(_registry_target_readiness(target).values())


def _registry_target_from_article_system(article_system: Any) -> Optional[dict]:
    if not isinstance(article_system, dict):
        return None
    if str(article_system.get("system_type") or "").strip() != "registry_driven_seo":
        return None
    registry = article_system.get("registry") if isinstance(article_system.get("registry"), dict) else {}
    mutation_target = article_system.get("publish_mutation_target")
    mutation_target_path = (
        mutation_target.get("registry_path")
        or mutation_target.get("path")
        or mutation_target.get("file")
        if isinstance(mutation_target, dict)
        else mutation_target
    )
    content_source = article_system.get("content_source")
    content_source_path = (
        content_source.get("path")
        or content_source.get("registry_path")
        or content_source.get("file")
        if isinstance(content_source, dict)
        else content_source
    )
    registry_path = (
        registry.get("path")
        or mutation_target_path
        or content_source_path
        or article_system.get("directory_path")
        or article_system.get("directory_name")
    )
    return {
        "kind": "registry_driven_seo",
        "delivery_adapter": "registry_entry",
        "readiness": article_system.get("readiness") if isinstance(article_system.get("readiness"), dict) else {},
        "diagnostics": article_system.get("diagnostics") if isinstance(article_system.get("diagnostics"), dict) else {},
        "observability": article_system.get("observability") if isinstance(article_system.get("observability"), dict) else {},
        "registration_strategy": {
            "type": "registry_entry_patch",
            "registry_path": registry_path,
            "route_template": article_system.get("route_template") or "",
        },
    }


def _best_registry_driven_target(*sources: Any) -> Optional[dict]:
    targets: list[dict] = []
    for source in sources:
        if not source:
            continue
        if isinstance(source, list):
            candidates = source
        elif isinstance(source, dict):
            candidates = source.get("publish_targets") or source.get("targets") or []
            if _is_registry_driven_publish_target(source):
                candidates = [source, *list(candidates or [])]
            else:
                direct_target = _registry_target_from_article_system(source)
                nested_target = _registry_target_from_article_system(source.get("article_system"))
                synthesized = [target for target in (direct_target, nested_target) if target]
                if synthesized:
                    candidates = [*list(candidates or []), *synthesized]
        else:
            candidates = []
        for candidate in candidates:
            if isinstance(candidate, dict) and _is_registry_driven_publish_target(candidate):
                targets.append(candidate)
    if not targets:
        return None
    return next((target for target in targets if _registry_target_publish_ready(target)), None) or targets[0]


def _registry_target_path(target: Optional[dict], article_system: Optional[dict] = None) -> str:
    target = target or {}
    article_system = article_system or {}
    strategy = target.get("registration_strategy") if isinstance(target.get("registration_strategy"), dict) else {}
    registry = article_system.get("registry") if isinstance(article_system.get("registry"), dict) else {}
    return str(
        strategy.get("registry_path")
        or target.get("registry_path")
        or target.get("content_source")
        or registry.get("path")
        or article_system.get("directory_path")
        or article_system.get("directory_name")
        or "the detected registry"
    ).strip()


def _registry_target_issues(target: Optional[dict], article_system: Optional[dict] = None) -> list[str]:
    issues: list[str] = []
    target = target or {}
    article_system = article_system or {}
    strategy = target.get("registration_strategy") if isinstance(target.get("registration_strategy"), dict) else {}
    for key, ready in _registry_target_readiness(target).items():
        if not ready:
            issues.append(f"{key.replace('_', ' ')} is not proven")
    for source in (
        target.get("diagnostics"),
        strategy.get("diagnostics"),
        article_system.get("diagnostics"),
        target.get("observability"),
        article_system.get("observability"),
    ):
        if not isinstance(source, dict):
            continue
        for key in ("issues", "blocking_issues", "fallback_reasons"):
            raw_items = source.get(key)
            if isinstance(raw_items, str):
                raw_items = [raw_items]
            for item in raw_items or []:
                text = str(item or "").strip()
                if text and text not in issues:
                    issues.append(text)
        fallback_reason = str(source.get("fallback_reason") or "").strip()
        if fallback_reason and fallback_reason not in issues:
            issues.append(fallback_reason)
    return issues


def _registry_target_summary(
    *,
    domain: Optional[str],
    target: Optional[dict],
    article_system: Optional[dict] = None,
) -> str:
    registry_path = _registry_target_path(target, article_system)
    if _registry_target_publish_ready(target):
        return (
            f"I found a registry-driven SEO system at `{registry_path}`. "
            "Roo can publish new SEO pages by adding typed registry entries through the existing page structure."
        )
    issues = _registry_target_issues(target, article_system)
    issue_text = ""
    if issues:
        issue_text = "\n\nBlockers:\n" + "\n".join(f"- {item}" for item in issues[:6])
    return (
        f"I found a registry-driven SEO system for *{domain or 'this domain'}* at `{registry_path}`, "
        f"but it is not safe to patch automatically yet."
        f"{issue_text}\n\n"
        "Use content-only delivery for now, or add/confirm a `.content-factory/target.yml` hook after the registry target is proven."
    )


def _delegated_content_factory_auth_error_text(
    *,
    effective_slack_user_id: Optional[str],
    domain: Optional[str],
) -> str:
    target = f"<@{effective_slack_user_id}>" if effective_slack_user_id else "that user"
    domain_label = normalize_content_factory_domain(domain) or domain or "this domain"
    return (
        f"❌ GitHub auth for {target} isn't available for *{domain_label}*. "
        f"Ask them to reconnect GitHub, then retry the delegated run."
    )


def _content_factory_action_denied_response(text: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "response_type": "ephemeral",
            "text": text,
        },
    )


@dataclass(frozen=True)
class _ContentFactoryActionContext:
    msg_channel: Optional[str]
    msg_ts: Optional[str]
    reply_channel: Optional[str]
    reply_thread_ts: Optional[str]
    requested_by_slack_user_id: str
    effective_slack_user_id: str


def _content_factory_thread_context(
    channel_id: Optional[str],
    thread_ts: Optional[str],
) -> Optional[dict[str, Any]]:
    if not channel_id or not thread_ts:
        return None
    try:
        thread_context = get_agent().get_thread_context(channel_id, thread_ts) or {}
    except Exception:
        return None
    return thread_context if isinstance(thread_context, dict) else None


def _resolve_content_factory_action_context(
    *,
    payload: dict[str, Any],
    value_data: Optional[dict[str, Any]],
    acting_slack_user_id: Optional[str],
    denial_text: str,
    stale_action_text: str = CONTENT_FACTORY_STALE_ACTION_TEXT,
) -> tuple[Optional[_ContentFactoryActionContext], Optional[JSONResponse]]:
    message = payload.get("message", {})
    msg_channel = payload.get("channel", {}).get("id")
    msg_ts = message.get("ts")
    resolved_value_data = value_data if isinstance(value_data, dict) else {}
    reply_channel = resolved_value_data.get("channel_id") or msg_channel
    reply_thread_ts = (
        resolved_value_data.get("thread_ts")
        or message.get("thread_ts")
        or msg_ts
    )
    thread_context = _content_factory_thread_context(
        reply_channel or msg_channel,
        reply_thread_ts,
    )

    try:
        identity = resolve_content_factory_action_identity(
            value_data=resolved_value_data,
            thread_context=thread_context,
        )
    except ContentFactoryIdentityResolutionError as exc:
        return None, _content_factory_action_denied_response(
            str(exc) or stale_action_text
        )

    owner_error = _enforce_content_factory_action_owner(
        acting_slack_user_id=acting_slack_user_id,
        requested_by_slack_user_id=identity.requested_by_slack_user_id,
        denial_text=denial_text,
    )
    if owner_error is not None:
        return None, owner_error

    return (
        _ContentFactoryActionContext(
            msg_channel=msg_channel,
            msg_ts=msg_ts,
            reply_channel=reply_channel,
            reply_thread_ts=reply_thread_ts,
            requested_by_slack_user_id=identity.requested_by_slack_user_id,
            effective_slack_user_id=identity.effective_slack_user_id,
        ),
        None,
    )


def _enforce_content_factory_action_owner(
    *,
    acting_slack_user_id: Optional[str],
    requested_by_slack_user_id: Optional[str],
    denial_text: str,
) -> Optional[JSONResponse]:
    if (
        requested_by_slack_user_id
        and acting_slack_user_id
        and acting_slack_user_id != requested_by_slack_user_id
    ):
        return _content_factory_action_denied_response(denial_text)
    return None


def _content_factory_delegated_backend_kwargs(
    requested_by_slack_user_id: Optional[str],
    effective_slack_user_id: Optional[str],
) -> dict[str, str]:
    if _is_delegated_content_factory_request(
        requested_by_slack_user_id,
        effective_slack_user_id,
    ):
        return {
            "requested_by_slack_user_id": str(requested_by_slack_user_id or "").strip()
        }
    return {}


def _remember_pending_intent(
    slack_user_id: Optional[str],
    domain: Optional[str],
    *,
    effective_slack_user_id: Optional[str] = None,
    intent_data: Optional[dict[str, Any]] = None,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    wait_for: str,
    job_id: Optional[str] = None,
    clear_job_id: bool = False,
) -> Optional[dict[str, Any]]:
    _prune_pending_intents()
    requested_by_slack_user_id = str(slack_user_id or "").strip() or None
    effective_slack_user_id = (
        str(effective_slack_user_id or "").strip() or requested_by_slack_user_id
    )
    intent_key = _pending_intent_identity_key(
        requested_by_slack_user_id,
        effective_slack_user_id,
        domain,
    )
    if not intent_key:
        return None

    existing = _pending_intents.get(intent_key, {})
    previous_job_id = existing.get("job_id")
    if previous_job_id and previous_job_id != job_id and _pending_intents_by_job.get(previous_job_id) == intent_key:
        _pending_intents_by_job.pop(previous_job_id, None)

    now = time.time()
    pending = dict(existing)
    if intent_data:
        pending.update(intent_data)
    if channel_id is not None:
        pending["channel_id"] = channel_id
    if thread_ts is not None:
        pending["thread_ts"] = thread_ts
    pending["requested_by_slack_user_id"] = requested_by_slack_user_id
    pending["effective_slack_user_id"] = effective_slack_user_id
    pending["wait_for"] = wait_for
    pending["created_at"] = pending.get("created_at", now)
    pending["updated_at"] = now
    pending["expires_at"] = now + PENDING_INTENT_TTL_SECONDS
    if job_id:
        pending["job_id"] = job_id
        _pending_intents_by_job[job_id] = intent_key
    elif clear_job_id:
        pending.pop("job_id", None)

    _pending_intents[intent_key] = pending
    return pending


def _get_pending_intent(
    slack_user_id: Optional[str],
    domain: Optional[str],
    *,
    effective_slack_user_id: Optional[str] = None,
    job_id: Optional[str] = None,
    wait_for: Optional[str] = None,
    consume: bool = False,
) -> Optional[dict[str, Any]]:
    _prune_pending_intents()

    intent_key = _pending_intents_by_job.get(job_id) if job_id else None
    if intent_key:
        pending = _pending_intents.get(intent_key)
        if not pending:
            _pending_intents_by_job.pop(job_id, None)
            return None
        if wait_for and pending.get("wait_for") != wait_for:
            return None
        return _pop_pending_intent_by_key(intent_key) if consume else pending

    intent_key = _pending_intent_identity_key(
        str(slack_user_id or "").strip() or None,
        str(effective_slack_user_id or "").strip() or None,
        domain,
    )
    if not intent_key:
        return None
    pending = _pending_intents.get(intent_key)
    if not pending:
        return None
    if wait_for and pending.get("wait_for") != wait_for:
        return None
    return _pop_pending_intent_by_key(intent_key) if consume else pending


def _extract_job_id(payload: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    for key in ("job_id", "run_id"):
        value = payload.get(key)
        if value:
            return str(value)

    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        nested_job_id = _extract_job_id(nested_data)
        if nested_job_id:
            return nested_job_id

    nested_job = payload.get("job")
    if isinstance(nested_job, dict):
        nested_job_id = _extract_job_id(nested_job)
        if nested_job_id:
            return nested_job_id

    return None


async def _watch_content_factory_quiet_run(job_id: str) -> None:
    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )
    consecutive_failures = 0

    while True:
        await asyncio.sleep(CONTENT_FACTORY_WATCHDOG_POLL_SECONDS)
        try:
            status_data = await client.check_generation_status(job_id)
            consecutive_failures = 0
            status_value = str(status_data.get("status") or "").strip().lower()
            if status_value in {"blocked", "blocked_verification"}:
                print(
                    "⏸️ Quiet-run watchdog stopping "
                    f"job_id={job_id} status={status_value} "
                    f"step={status_data.get('current_step') or status_data.get('blocked_step') or ''} "
                    f"error_code={status_data.get('error_code') or ''}"
                )
                return
            if not status_value or status_value in CONTENT_FACTORY_WATCHDOG_STOP_STATUSES:
                return
            await client.maybe_send_content_still_working(
                job_id,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
        except Exception as exc:
            consecutive_failures += 1
            print(f"⚠️ Quiet-run watchdog failed for {job_id} ({consecutive_failures}/5): {exc}")
            if consecutive_failures >= 5:
                return


async def _maybe_attach_content_factory_progress(
    result_data: Any,
    post_response: Optional[dict],
    *,
    channel_id: Optional[str],
    thread_ts: Optional[str],
) -> None:
    if not isinstance(result_data, dict):
        return

    job_id = str(result_data.get("content_factory_progress_job_id") or "").strip()
    if not job_id:
        return

    message_ts = str((post_response or {}).get("ts") or "").strip()
    if not message_ts:
        return

    workflow = str(
        result_data.get("content_factory_workflow")
        or result_data.get("content_factory_watchdog_mode")
        or ""
    ).strip() or None
    domain = str(result_data.get("content_factory_domain") or "").strip() or None
    requested_by_slack_user_id = _clean_slack_user_id(
        result_data.get("requested_by_slack_user_id")
    )
    effective_slack_user_id = _clean_slack_user_id(
        result_data.get("effective_slack_user_id")
    )

    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )
    try:
        await client.attach_content_progress_message(
            job_id,
            progress_message_ts=message_ts,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            slack_root_message_ts=thread_ts,
            request_source=CONTENT_FACTORY_REQUEST_SOURCE,
        )
    except Exception as exc:
        print(f"⚠️ Failed to attach progress message {message_ts} to job {job_id}: {exc}")
        return

    _remember_content_thread_context(
        channel_id,
        thread_ts,
        domain,
        workflow or "write",
        active_job_id=job_id,
        requested_by_slack_user_id=requested_by_slack_user_id,
        effective_slack_user_id=effective_slack_user_id,
    )

    if result_data.get("content_factory_watchdog"):
        asyncio.create_task(_watch_content_factory_quiet_run(job_id))


async def _trigger_article_generation_from_pending(
    pending: dict[str, Any],
    *,
    requested_by_slack_user_id: str,
    effective_slack_user_id: str,
    domain: str,
    fallback_channel_id: Optional[str] = None,
    fallback_thread_ts: Optional[str] = None,
) -> bool:
    intent_channel = pending.get("channel_id") or fallback_channel_id
    intent_thread = pending.get("thread_ts") or fallback_thread_ts
    topic = pending.get("topic")
    include_decision_stage = not bool(topic)

    print(f"🔄 Auto-continuing: triggering article generation for {domain} (topic: {topic})")

    progress_message_ts = None
    if intent_channel:
        topic_note = f" for *{topic}*" if topic else ""
        summary_text = (
            f"Articles directory is ready. Starting article generation{topic_note}. I'll keep this message updated."
            if topic
            else "Articles directory is ready. Starting discovery to find the best article opportunity. I'll keep this message updated."
        )
        response = post_message(
            channel=intent_channel,
            thread_ts=intent_thread,
            text=f"Starting Content Factory for {domain}",
            blocks=build_live_status_blocks(
                domain,
                summary_text=summary_text,
                include_decision_stage=include_decision_stage,
                current_stage="preparing",
            ),
        )
        progress_message_ts = str((response or {}).get("ts") or "").strip() or None

    _remember_content_thread_context(
        intent_channel,
        intent_thread,
        domain,
        "research" if include_decision_stage else "write",
        requested_by_slack_user_id=requested_by_slack_user_id,
        effective_slack_user_id=effective_slack_user_id,
    )

    from .clients.mlai_backend import MLAIBackendClient
    from .slack_client import get_user_info
    settings = get_settings()
    backend_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
    )
    try:
        slack_info = get_user_info(requested_by_slack_user_id)
        real_name = str(slack_info.get("real_name") or "").strip()
        name_parts = real_name.split(" ", 1) if real_name else []
        delegated_backend_kwargs = _content_factory_delegated_backend_kwargs(
            requested_by_slack_user_id,
            effective_slack_user_id,
        )
        result = await backend_client.trigger_article_generation(
            slack_user_id=effective_slack_user_id,
            domain=domain,
            topic=topic,
            target_keyword=pending.get("target_keyword"),
            slack_channel_id=intent_channel,
            slack_thread_ts=intent_thread,
            progress_message_ts=progress_message_ts,
            client_request_id=pending.get("client_request_id"),
            request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            user_email=str(slack_info.get("email") or "").strip().lower() or None,
            user_first_name=name_parts[0] if name_parts else None,
            user_last_name=name_parts[1] if len(name_parts) > 1 else None,
            user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
            **delegated_backend_kwargs,
        )
        print(f"✅ Auto-generation triggered for {domain}")
        if result.get("job_id") or result.get("run_id"):
            _remember_content_thread_context(
                intent_channel,
                intent_thread,
                domain,
                "research" if include_decision_stage else "write",
                active_job_id=result.get("job_id") or result.get("run_id"),
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )
            asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        return True
    except Exception as e:
        print(f"❌ Auto-generation failed: {e}")
        if intent_channel:
            post_message(
                channel=intent_channel,
                thread_ts=intent_thread,
                text=f"❌ Error starting article generation: {e}"
            )
        return False


def _remember_content_thread_context(
    channel_id: str | None,
    thread_ts: str | None,
    domain: str | None,
    workflow: str,
    *,
    active_job_id: str | None = None,
    requested_by_slack_user_id: str | None = None,
    effective_slack_user_id: str | None = None,
) -> None:
    """Keep content-factory as the active skill for follow-ups in this thread."""
    if not channel_id or not thread_ts:
        return

    try:
        context_kwargs: dict[str, Any] = {
            "domain": domain,
            "workflow": workflow,
            "active_job_id": active_job_id,
        }
        if requested_by_slack_user_id is not None:
            context_kwargs["requested_by_slack_user_id"] = requested_by_slack_user_id
        if effective_slack_user_id is not None:
            context_kwargs["effective_slack_user_id"] = effective_slack_user_id
        get_agent().remember_thread_context(
            "content-factory",
            channel_id,
            thread_ts,
            **context_kwargs,
        )
    except Exception as e:
        print(f"⚠️ Failed to persist content thread context: {e}")


def _build_article_delivery_mode_blocks(
    *,
    domain: str,
    job_id: str,
    topic: Optional[str] = None,
    recommended_delivery_mode: Optional[str] = None,
    requested_by_slack_user_id: Optional[str] = None,
    effective_slack_user_id: Optional[str] = None,
) -> list[dict]:
    topic_line = f"*Topic:* {topic}\n" if topic else ""
    recommended_line = ""
    if recommended_delivery_mode == "content_only":
        recommended_line = "\n_Recommended right now: content-only._"
    elif recommended_delivery_mode == "publish_code":
        recommended_line = "\n_Recommended right now: publish via code._"

    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Content Only", "emoji": True},
            "value": json.dumps(
                _content_factory_identity_payload(
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    job_id=job_id,
                    domain=domain,
                    delivery_mode="content_only",
                )
            ),
            "action_id": "select_article_delivery_mode",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Publish Via Code", "emoji": True},
            "value": json.dumps(
                _content_factory_identity_payload(
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    job_id=job_id,
                    domain=domain,
                    delivery_mode="publish_code",
                )
            ),
            "action_id": "select_article_delivery_mode",
        },
    ]
    if recommended_delivery_mode == "publish_code":
        buttons[1]["style"] = "primary"
    else:
        buttons[0]["style"] = "primary"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Choose how you want me to deliver the article for *{domain}*.\n\n"
                    f"{topic_line}"
                    "• *Content-only*: research and write the article for manual upload.\n"
                    "• *Publish via code*: continue through the repo/PR flow."
                    f"{recommended_line}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "article_delivery_mode_actions",
            "elements": buttons,
        },
    ]


def _build_confirm_topic_follow_up(
    result: Any,
    *,
    default_domain: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        result = {}

    cf_response = result.get("cf_response")
    if not isinstance(cf_response, dict):
        cf_response = {}

    top_level_status = str(result.get("status") or "").strip().lower()
    callback_status = str(cf_response.get("status") or "").strip().lower()
    requires_delivery_mode = "awaiting_delivery_mode" in {top_level_status, callback_status}
    if requires_delivery_mode:
        status_value = "awaiting_delivery_mode"
    elif callback_status in {"blocked", "blocked_verification", "auth_required"}:
        status_value = callback_status
    else:
        status_value = top_level_status or callback_status
    active_job_id = _extract_job_id(result) or _extract_job_id(cf_response)
    domain = str(result.get("domain") or cf_response.get("domain") or default_domain or "this domain").strip() or "this domain"

    return {
        "status": status_value,
        "active_job_id": active_job_id,
        "domain": domain,
        "message": (
            result.get("message")
            or result.get("error")
            or cf_response.get("message")
            or cf_response.get("error")
        ),
        "error_code": (
            result.get("error_code")
            or cf_response.get("error_code")
        ),
        "auth_url": (
            result.get("auth_url")
            or cf_response.get("auth_url")
        ),
        "recommended_delivery_mode": (
            result.get("recommended_delivery_mode")
            or cf_response.get("recommended_delivery_mode")
        ),
        "requires_delivery_mode": bool(active_job_id and requires_delivery_mode),
    }


def _maybe_schedule_content_factory_watchdog(active_job_id: Optional[str], status_value: str) -> None:
    if not active_job_id:
        return
    if status_value and status_value in CONTENT_FACTORY_WATCHDOG_STOP_STATUSES:
        return
    asyncio.create_task(_watch_content_factory_quiet_run(active_job_id))


def _confirm_topic_json_response(
    keyword: str,
    follow_up: dict[str, Any],
    *,
    requested_by_slack_user_id: Optional[str] = None,
    effective_slack_user_id: Optional[str] = None,
) -> JSONResponse:
    if follow_up.get("requires_delivery_mode"):
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": f"Choose delivery mode for {keyword}",
            "blocks": _build_article_delivery_mode_blocks(
                domain=follow_up["domain"],
                job_id=follow_up["active_job_id"],
                topic=keyword,
                recommended_delivery_mode=follow_up.get("recommended_delivery_mode"),
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            ),
        })

    status_value = str(follow_up.get("status") or "").strip().lower()
    if status_value in {"blocked", "blocked_verification"}:
        message = str(
            follow_up.get("message")
            or "This article run is blocked right now. Roo will retry when the dependency path recovers."
        ).strip()
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": f"⏸️ {message}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⏸️ {message}",
                    },
                }
            ],
        })

    if status_value == "auth_required":
        if _is_delegated_content_factory_request(
            requested_by_slack_user_id,
            effective_slack_user_id,
        ):
            message = _delegated_content_factory_auth_error_text(
                effective_slack_user_id=effective_slack_user_id,
                domain=follow_up.get("domain"),
            )
            block_text = message
        else:
            message = str(
                follow_up.get("message")
                or "GitHub authentication is required before Roo can continue this article."
            ).strip()
            auth_url = str(follow_up.get("auth_url") or "").strip()
            block_text = f"🔐 {message}"
            if auth_url:
                block_text = f"{block_text}\n<{auth_url}|Reconnect GitHub>"
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": f"🔐 {message}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": block_text,
                    },
                }
            ],
        })

    _maybe_schedule_content_factory_watchdog(
        follow_up.get("active_job_id"),
        status_value,
    )
    return JSONResponse(status_code=200, content={
        "response_type": "ephemeral",
        "replace_original": True,
        "text": f"⏳ Queued generation for `{keyword}`. No additional Roo points will be charged for this confirmation.",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏳ Queued generation for `{keyword}`.\n_No additional Roo points will be charged for topic confirmation._"
                }
            }
        ]
    })


async def _resolve_confirm_follow_up_after_failure(
    client: Any,
    *,
    slack_user_id: str,
    slack_channel_id: Optional[str],
    slack_thread_ts: Optional[str],
    domain: Optional[str],
    job_id: str,
    requested_by_slack_user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    resolved_channel_id = str(slack_channel_id or "").strip()
    resolved_thread_ts = str(slack_thread_ts or "").strip()
    resolved_domain = str(domain or "").strip() or None

    if resolved_channel_id and resolved_thread_ts:
        try:
            delegated_backend_kwargs = _content_factory_delegated_backend_kwargs(
                requested_by_slack_user_id,
                slack_user_id,
            )
            resolution = await client.resolve_content_thread(
                slack_user_id=slack_user_id,
                slack_channel_id=resolved_channel_id,
                slack_thread_ts=resolved_thread_ts,
                requested_action="confirm_topic",
                domain=resolved_domain,
                job_id=job_id,
                **delegated_backend_kwargs,
            )
        except Exception as exc:
            print(f"⚠️ Failed to resolve confirm thread after error for {job_id}: {exc!r}")
        else:
            follow_up = _build_confirm_topic_follow_up(
                resolution,
                default_domain=resolved_domain,
            )
            if str(follow_up.get("status") or "").strip().lower() in {
                "queued",
                "generating",
                "awaiting_delivery_mode",
                "blocked",
                "blocked_verification",
                "auth_required",
                "completed",
            }:
                return follow_up

    try:
        status_result = await client.check_generation_status(job_id)
    except Exception as exc:
        print(f"⚠️ Failed to load confirm status after error for {job_id}: {exc!r}")
        return None

    follow_up = _build_confirm_topic_follow_up(
        status_result,
        default_domain=resolved_domain,
    )
    if str(follow_up.get("status") or "").strip().lower() in {
        "queued",
        "generating",
        "awaiting_delivery_mode",
        "blocked",
        "blocked_verification",
        "auth_required",
        "completed",
    }:
        return follow_up
    return None


def _build_confirm_follow_up_message(follow_up: dict[str, Any]) -> str:
    status_value = str(follow_up.get("status") or "").strip().lower()
    if status_value in {"blocked", "blocked_verification"}:
        return str(
            follow_up.get("message")
            or "⏸️ Article generation is blocked right now. Roo will resume when the dependency path recovers."
        ).strip()
    if status_value == "auth_required":
        return str(
            follow_up.get("message")
            or "🔐 GitHub authentication is required before Roo can continue this article."
        ).strip()
    if status_value == "completed":
        return "✅ This article run is already complete."
    return "✅ Generating article. No additional Roo points will be charged for this confirmation."


async def _medhack_daily_case_loop():
    """Background task that posts a new diagnosis case each day."""
    import asyncio
    from .slack_client import get_channel_id, post_message
    from .utils import get_current_date

    # Wait a bit for the app to fully start
    await asyncio.sleep(10)

    while True:
        try:
            today = get_current_date()
            # Try medhack-testing first (for dev), then medhack-frontiers (for prod)
            channel_id = get_channel_id("medhack-testing") or get_channel_id("medhack-frontiers")
            if not channel_id:
                print("⚠️ MedHack: neither #medhack-testing nor #medhack-frontiers channel found, skipping daily case")
                await asyncio.sleep(3600)  # Retry in an hour
                continue

            # Load the medhack client
            from pathlib import Path
            import sys
            settings = get_settings()
            skills_dir = Path(settings.SKILLS_DIR) / "medhack"
            client_path = skills_dir / "client.py"

            if not client_path.exists():
                print("⚠️ MedHack client.py not found, skipping daily case")
                await asyncio.sleep(3600)
                continue

            # Import the client
            import importlib.util
            spec = importlib.util.spec_from_file_location("medhack_daily_client", client_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            client = mod.MedHackClient()

            # Check if there's already a case for today
            current = await client.get_current_case(today)
            if current is None:
                # Start a new case
                new_case = await client.start_new_case(today)
                if new_case:
                    difficulty = new_case.get("difficulty", "medium").upper()
                    title = new_case.get("title", "")
                    title_str = f' - _{title}_' if title else ""
                    header = f"*GUESS THE DIAGNOSIS* - Daily Challenge [{difficulty}]{title_str}"

                    # New-style cases have an ed_first_look narrative + triage note
                    if new_case.get("ed_first_look"):
                        scene = new_case["ed_first_look"].strip()
                        triage = new_case["presenting_complaint"].strip()
                        message = (
                            f"{header}\n\n"
                            f"{scene}\n\n"
                            f"*Triage note:* {triage}\n\n"
                            f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                            f"Ask me anything you'd ask them and I'll relay their answer. "
                            f"You can also request examinations and investigations, but be specific — "
                            f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                            f"When you're ready, tell me your diagnosis!\n\n"
                            f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                            f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                        )
                    else:
                        complaint = new_case["presenting_complaint"].strip()
                        message = (
                            f"{header}\n\n"
                            f"{complaint}\n\n"
                            f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                            f"Ask me anything you'd ask them and I'll relay their answer. "
                            f"You can also request examinations and investigations, but be specific — "
                            f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                            f"When you're ready, tell me your diagnosis!\n\n"
                            f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                            f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                        )

                    # Build blocks with optional image
                    image_url = new_case.get("image_url", "")
                    if image_url:
                        blocks = [
                            {
                                "type": "image",
                                "image_url": image_url,
                                "alt_text": f"Guess the Diagnosis - {new_case.get('title', 'Daily Case')}",
                            },
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": message},
                            },
                        ]
                        post_message(channel=channel_id, text=message, blocks=blocks)
                    else:
                        post_message(channel=channel_id, text=message)
                    print(f"Posted new MedHack case #{new_case['id']} for {today}")
                else:
                    print("⚠️ No available MedHack cases to post")

        except Exception as e:
            print(f"❌ MedHack daily case error: {e}")
            import traceback
            traceback.print_exc()

        # Sleep until tomorrow (calculate seconds until next 10 AM AEST)
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(get_settings().TIMEZONE)
        now = datetime.now(tz)
        next_post = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= next_post:
            next_post += timedelta(days=1)
        sleep_seconds = (next_post - now).total_seconds()
        print(f"MedHack: Next case in {sleep_seconds/3600:.1f} hours")
        await asyncio.sleep(sleep_seconds)


def _build_jobs_status_url(base_api_url: str, status_url: Optional[str]) -> Optional[str]:
    cleaned_status_url = str(status_url or "").strip()
    if not cleaned_status_url:
        return None
    if cleaned_status_url.startswith("http://") or cleaned_status_url.startswith("https://"):
        return cleaned_status_url
    if cleaned_status_url.startswith("/"):
        parsed_base = urlparse(base_api_url)
        if parsed_base.scheme and parsed_base.netloc:
            return f"{parsed_base.scheme}://{parsed_base.netloc}{cleaned_status_url}"
    return urljoin(base_api_url.rstrip("/") + "/", cleaned_status_url.lstrip("/"))


def _build_jobs_trigger_payload(settings: Settings) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "collect_live": settings.JOBS_COLLECT_LIVE,
        "post_to_slack": settings.JOBS_POST_TO_SLACK,
        "post_to_notion": settings.JOBS_POST_TO_NOTION,
        "max_pages": settings.JOBS_MAX_PAGES,
        "per_keyword_limit": settings.JOBS_PER_KEYWORD_LIMIT,
    }
    if settings.JOBS_SLACK_CHANNEL:
        payload["slack_channel"] = settings.JOBS_SLACK_CHANNEL
    return payload


def _make_mlai_backend_client():
    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    return MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )


def _validate_jobs_trigger_settings(settings: Settings) -> None:
    if not settings.JOBS_API_URL:
        raise ValueError("JOBS_API_URL must be configured for Roo jobs triggers")
    parsed = urlparse(settings.JOBS_API_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("JOBS_API_URL must be a valid http(s) URL for Roo jobs triggers")
    if not settings.JOBS_TRIGGER_TOKEN or not settings.JOBS_TRIGGER_TOKEN.strip():
        raise ValueError("JOBS_TRIGGER_TOKEN must be configured for Roo jobs triggers")


def _validate_jobs_scheduler_settings(settings: Settings) -> None:
    _validate_jobs_trigger_settings(settings)
    if not 0 <= settings.JOBS_SCHEDULE_HOUR <= 23:
        raise ValueError("JOBS_SCHEDULE_HOUR must be between 0 and 23")
    if not 0 <= settings.JOBS_SCHEDULE_MINUTE <= 59:
        raise ValueError("JOBS_SCHEDULE_MINUTE must be between 0 and 59")
    if settings.JOBS_RETRY_ATTEMPTS < 1:
        raise ValueError("JOBS_RETRY_ATTEMPTS must be at least 1")
    if settings.JOBS_RETRY_DELAY_SECONDS < 1:
        raise ValueError("JOBS_RETRY_DELAY_SECONDS must be at least 1")
    if settings.JOBS_FAILURE_STOP_AFTER_DAYS < 1:
        raise ValueError("JOBS_FAILURE_STOP_AFTER_DAYS must be at least 1")
    if settings.JOBS_POST_TO_SLACK and not settings.JOBS_SLACK_CHANNEL:
        print("Warning: JOBS_POST_TO_SLACK is enabled but JOBS_SLACK_CHANNEL is not set; backend default channel will be used")


async def _trigger_jobs_daily_run_request() -> dict[str, Any]:
    settings = get_settings()
    _validate_jobs_trigger_settings(settings)
    url = settings.JOBS_API_URL.rstrip("/") + "/jobs/daily-run"
    headers: dict[str, str] = {}
    if settings.JOBS_TRIGGER_TOKEN:
        headers["X-API-Key"] = settings.JOBS_TRIGGER_TOKEN

    payload = _build_jobs_trigger_payload(settings)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def _trigger_jobs_daily_run() -> bool:
    settings = get_settings()
    try:
        data = await _trigger_jobs_daily_run_request()
        print(
            "Jobs scheduler triggered daily run "
            f"run_id={data.get('run_id')} status={data.get('status')} "
            f"status_url={_build_jobs_status_url(settings.JOBS_API_URL, data.get('status_url'))}"
        )
        return True
    except Exception as exc:
        print(f"Jobs scheduler trigger failed: {exc}")
        return False


async def _maybe_handle_manual_jobs_trigger(event: dict[str, Any]) -> bool:
    user_id = str(event.get("user") or "").strip()
    text = str(event.get("text") or "")
    channel_id = str(event.get("channel") or "").strip()
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not user_id or not channel_id or not thread_ts or not _is_manual_jobs_trigger_request(text):
        return False

    try:
        client = _make_mlai_backend_client()
        if not await client.is_admin(user_id):
            post_message(
                channel=channel_id,
                thread_ts=thread_ts,
                text="Sorry mate, only Points Admins can run the daily jobs scrape manually.",
            )
            return True
    except Exception as exc:
        print(f"⚠️ Manual jobs admin check failed for {user_id}: {exc}")
        post_message(
            channel=channel_id,
            thread_ts=thread_ts,
            text="I couldn't verify your admin access with the backend just now, so I didn't trigger the jobs run.",
        )
        return True

    settings = get_settings()
    try:
        data = await _trigger_jobs_daily_run_request()
    except Exception as exc:
        print(f"⚠️ Manual jobs trigger failed for {user_id}: {exc}")
        post_message(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"I couldn't trigger the daily jobs run: {exc}",
        )
        return True

    run_id = data.get("run_id") or "unknown"
    status = data.get("status") or "queued"
    lines = [
        "Queued the daily jobs run.",
        f"Run ID: `{run_id}`",
    ]
    if status and status != "queued":
        lines.append(f"Current status: `{status}`")
    lines.append("This usually takes a few minutes.")
    if settings.JOBS_POST_TO_SLACK:
        lines.append("If matches are found, the backend will post the final jobs roundup separately when the run finishes.")
    else:
        lines.append("Slack posting is off for this run, so this message is only confirming the trigger.")
    post_message(channel=channel_id, thread_ts=thread_ts, text="\n".join(lines))
    return True


async def _jobs_daily_run_loop() -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    settings = get_settings()
    tz = ZoneInfo(settings.TIMEZONE)
    consecutive_failed_days = 0
    last_attempted_day: Optional[date] = None

    await asyncio.sleep(10)

    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(
                hour=settings.JOBS_SCHEDULE_HOUR,
                minute=settings.JOBS_SCHEDULE_MINUTE,
                second=0,
                microsecond=0,
            )
            if now >= next_run:
                next_run += timedelta(days=1)

            sleep_seconds = max(1.0, (next_run - now).total_seconds())
            print(
                "Jobs scheduler waiting "
                f"{sleep_seconds / 3600:.2f} hours until {next_run.isoformat()}"
            )
            await asyncio.sleep(sleep_seconds)
            scheduled_day = next_run.date()
            if last_attempted_day == scheduled_day:
                continue

            success = False
            for attempt in range(1, settings.JOBS_RETRY_ATTEMPTS + 1):
                success = await _trigger_jobs_daily_run()
                if success:
                    consecutive_failed_days = 0
                    last_attempted_day = scheduled_day
                    break

                if attempt < settings.JOBS_RETRY_ATTEMPTS:
                    print(
                        "Jobs scheduler retrying "
                        f"attempt {attempt + 1}/{settings.JOBS_RETRY_ATTEMPTS} in "
                        f"{settings.JOBS_RETRY_DELAY_SECONDS} seconds"
                    )
                    await asyncio.sleep(settings.JOBS_RETRY_DELAY_SECONDS)

            if not success:
                last_attempted_day = scheduled_day
                consecutive_failed_days += 1
                print(
                    "Jobs scheduler exhausted retries "
                    f"for {scheduled_day.isoformat()} "
                    f"(consecutive failed days: {consecutive_failed_days})"
                )
                if consecutive_failed_days >= settings.JOBS_FAILURE_STOP_AFTER_DAYS:
                    print(
                        "Jobs scheduler stopped after repeated failures. "
                        "Please inspect JOBS_API_URL, JOBS_TRIGGER_TOKEN, and the jobs service."
                    )
                    return
        except Exception as exc:
            print(f"Jobs scheduler loop error: {exc}")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()
    validate_runtime_security(settings)
    app.state.startup_complete = False
    coworking_retry_task: Optional[asyncio.Task] = None
    boost_post_retry_task: Optional[asyncio.Task] = None
    link_love_task: Optional[asyncio.Task] = None
    start_here_intro_task: Optional[asyncio.Task] = None
    jobs_scheduler_task: Optional[asyncio.Task] = None
    print(f"🦘 Roo Standalone starting...")
    print(f"   Surface: {settings.ROO_SURFACE}")
    print(f"   LLM Provider: {settings.default_llm_provider}")
    print(f"   Skills Dir: {settings.SKILLS_DIR}")

    # Initialize agent on startup
    agent = get_agent()
    print(f"   Loaded {len(agent.skills)} skills")
    if settings.ROO_SURFACE == "public":
        coworking_retry_task = asyncio.create_task(coworking_booking_retry_loop())
        app.state.coworking_retry_task = coworking_retry_task
        if settings.BOOST_LINK_LOVE_ENABLED:
            link_love_task = asyncio.create_task(link_love_retry_loop())
            app.state.link_love_task = link_love_task
        if getattr(settings, "BOOST_POST_MODERATION_ENABLED", False):
            if getattr(settings, "BOOST_POST_AUTO_DELETE_ENABLED", False):
                moderator_status = await asyncio.to_thread(
                    validate_slack_moderator_configuration,
                    settings=settings,
                )
                print(
                    "   Slack boost moderator verified "
                    f"team_id={moderator_status['team_id']} "
                    f"user_id={moderator_status['user_id']}"
                )
            boost_post_retry_task = asyncio.create_task(boost_post_retry_loop())
            app.state.boost_post_retry_task = boost_post_retry_task
        if getattr(settings, "START_HERE_INTRO_ENABLED", True):
            start_here_intro_task = asyncio.create_task(start_here_intro_retry_loop())
            app.state.start_here_intro_task = start_here_intro_task
        if settings.JOBS_SCHEDULER_ENABLED:
            _validate_jobs_scheduler_settings(settings)
            jobs_scheduler_task = asyncio.create_task(_jobs_daily_run_loop())
            app.state.jobs_scheduler_task = jobs_scheduler_task
            print(
                "   Started jobs daily scheduler "
                f"for {settings.JOBS_SCHEDULE_HOUR:02d}:{settings.JOBS_SCHEDULE_MINUTE:02d} "
                f"{settings.TIMEZONE}"
            )
    else:
        print("   Public Roo background workflows disabled on Admin surface")
    app.state.startup_complete = True

    # MedHack daily case scheduler (currently disabled)
    # import asyncio
    # medhack_task = asyncio.create_task(_medhack_daily_case_loop())
    # print("   Started MedHack daily case scheduler")

    try:
        yield
    finally:
        if jobs_scheduler_task:
            jobs_scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await jobs_scheduler_task
        if coworking_retry_task:
            coworking_retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await coworking_retry_task
        if boost_post_retry_task:
            boost_post_retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await boost_post_retry_task
        if link_love_task:
            link_love_task.cancel()
            with suppress(asyncio.CancelledError):
                await link_love_task
        if start_here_intro_task:
            start_here_intro_task.cancel()
            with suppress(asyncio.CancelledError):
                await start_here_intro_task

    # Cancel the background task on shutdown (disabled)
    # medhack_task.cancel()
    print("🦘 Roo Standalone shutting down...")


_docs_enabled = not get_settings().is_production

app = FastAPI(
    title="Roo Standalone",
    description="AI Agent Service with Skills-based Architecture",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)


async def verify_slack_signature(
    request: Request,
    settings: Settings = Depends(get_settings)
) -> bool:
    """Verify every internet-facing Slack webhook before parsing its body."""
    try:
        is_duplicate = verify_and_claim_slack_request(
            signing_secret=settings.SLACK_SIGNING_SECRET,
            raw_body=await request.body(),
            headers=request.headers,
            receipt_db_path=settings.SLACK_RECEIPTS_DB_PATH,
            max_age_seconds=settings.SLACK_REQUEST_MAX_AGE_SECONDS,
            receipt_ttl_seconds=settings.SLACK_RECEIPT_TTL_SECONDS,
        )
    except SlackRequestVerificationError:
        raise HTTPException(status_code=403, detail="unauthorized")
    request.state.slack_duplicate = is_duplicate
    request.state.roo_settings = settings
    return True


def require_public_surface(
    settings: Settings = Depends(get_settings),
) -> Settings:
    """Hide Public Roo-only HTTP capabilities from the Admin deployment."""
    if settings.ROO_SURFACE != "public":
        raise HTTPException(status_code=404, detail="not found")
    return settings


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "roo",
        "surface": get_settings().ROO_SURFACE,
        "message": "G'day! Roo is awake and ready 🦘"
    }


@app.get("/healthz/ready")
async def readiness_check():
    if not getattr(app.state, "startup_complete", False):
        return JSONResponse(
            {
                "status": "not_ready",
                "service": "roo",
            },
            status_code=503,
        )
    settings = get_settings()
    payload = {
        "status": "ok",
        "service": "roo",
        "surface": settings.ROO_SURFACE,
        "message": "Roo startup complete",
    }
    if settings.ROO_SURFACE == "admin":
        payload.update(
            {
                "enabled_skills": sorted(settings.enabled_skill_names),
                "org_brain_enabled": settings.ORG_BRAIN_ENABLED,
                "org_brain_actions_enabled": settings.ORG_BRAIN_ACTIONS_ENABLED,
                "contextual_shadow_mode": settings.ROO_CONTEXTUAL_SHADOW_MODE,
            }
        )
    else:
        payload.update(
            {
                "unified_admin_routing_enabled": (
                    settings.ROO_UNIFIED_ADMIN_ROUTING_ENABLED
                ),
                "contextual_shadow_mode": settings.ROO_CONTEXTUAL_SHADOW_MODE,
            }
        )
    return payload


@app.get("/healthz/dependencies")
async def dependency_health_check():
    if not getattr(app.state, "startup_complete", False):
        return JSONResponse(
            {
                "status": "not_ready",
                "service": "roo",
                "dependencies": {},
            },
            status_code=503,
        )

    settings = get_settings()
    backend_status: dict[str, Any] = {
        "status": "unconfigured",
    }

    if settings.MLAI_BACKEND_URL:
        from .clients.mlai_backend import MLAIBackendClient

        backend_status = {
            "status": "degraded",
            "base_url": settings.MLAI_BACKEND_URL,
        }
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            backend_status["readiness"] = await client.get_backend_readiness()
        except Exception as exc:
            backend_status["readiness_error"] = f"{exc.__class__.__name__}: {exc}"

        try:
            backend_status["points"] = await client.get_points_health()
        except Exception as exc:
            backend_status["points_error"] = f"{exc.__class__.__name__}: {exc}"

        if backend_status.get("readiness", {}).get("status") == "ok" and backend_status.get("points", {}).get("status") == "ok":
            backend_status["status"] = "ok"

    dependency_status = "ok" if backend_status.get("status") in {"ok", "unconfigured"} else "degraded"
    return {
        "status": dependency_status,
        "service": "roo",
        "dependencies": {
            "mlai_backend": backend_status,
        },
    }


@app.post("/slack/events")
async def slack_events(
    request: Request,
    _verified: bool = Depends(verify_slack_signature),
):
    """
    Slack Events API webhook.
    
    Handles:
    - url_verification challenges
    - app_mention events
    - direct messages
    - reaction approvals
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    # Handle URL verification challenge
    if payload.get("type") == "url_verification":
        print("✅ Slack URL verification challenge")
        return {"challenge": payload.get("challenge")}

    if _is_duplicate_slack_request(request):
        print("↩️ Ignoring duplicate signed Slack event request")
        return JSONResponse(status_code=200, content={})
    
    # Handle events
    event = payload.get("event", {}) or {}
    event_type = event.get("type")
    settings = _request_settings(request)

    if not _is_slack_context_allowed(
        settings,
        channel_id=event.get("channel"),
        user_id=event.get("user"),
        channel_type=event.get("channel_type"),
    ):
        print("🔒 Ignoring Slack event outside the deployment context allowlist")
        return JSONResponse(status_code=200, content={})

    if getattr(settings, "ROO_SURFACE", "public") == "admin":
        is_admin_dm = (
            event_type == "message"
            and event.get("channel_type") == "im"
        )
        if event_type != "app_mention" and not is_admin_dm:
            print(f"🔒 Admin Roo ignored unsupported Slack event type: {event_type}")
            return JSONResponse(status_code=200, content={})
    
    print(f"📨 Received Slack event: {event_type}")

    # Quests disabled for now
    # try:
    #     from .quests import handle_quests
    #     import asyncio
    #     asyncio.create_task(handle_quests(event))
    # except Exception as e:
    #     print(f"⚠️ Quest processing failed: {e}")
    
    if event_type == "app_mention":
        if not _mark_app_mention_event_seen(payload, event):
            return JSONResponse(status_code=200, content={})
        routed_event = _with_slack_delivery_context(event, payload)
        if _is_contextual_channel_enabled(settings, event.get("channel")):
            asyncio.create_task(
                _handle_contextual_slack_message_safely(
                    routed_event,
                    slack_team_id=str(payload.get("team_id") or ""),
                    trigger_source="app_mention",
                )
            )
        else:
            asyncio.create_task(_handle_mention(routed_event))
        return JSONResponse(status_code=200, content={})

    if event_type == "reaction_added":
        asyncio.create_task(_handle_reaction_added(event))
        return JSONResponse(status_code=200, content={})

    if event_type == "message" and event.get("subtype") == "message_deleted":
        boost_channel_id = str(
            getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or ""
        )
        deleted_ts = str(event.get("deleted_ts") or "")
        if (
            getattr(settings, "BOOST_POST_MODERATION_ENABLED", False)
            and boost_channel_id
            and event.get("channel") == boost_channel_id
            and deleted_ts
        ):
            mark_boost_root_removed(boost_channel_id, deleted_ts)
        return JSONResponse(status_code=200, content={})

    if event_type == "message" and event.get("subtype") == "message_changed":
        from .slack_client import get_channel_id

        boost_channel_id = str(
            getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or ""
        )
        if (
            getattr(settings, "BOOST_POST_MODERATION_ENABLED", False)
            and boost_channel_id
            and event.get("channel") == boost_channel_id
        ):
            asyncio.create_task(handle_boost_root_edit(event))
            return JSONResponse(status_code=200, content={})

        try:
            settings = get_settings()
            intro_enabled = bool(getattr(settings, "START_HERE_INTRO_ENABLED", True))
            intro_channel_name = str(
                getattr(settings, "START_HERE_INTRO_CHANNEL_NAME", "_start-here")
            )
        except Exception:
            intro_enabled = True
            intro_channel_name = "_start-here"
        start_here_id = get_channel_id(intro_channel_name) if intro_enabled else None
        if start_here_id and event.get("channel") == start_here_id:
            if normalize_intro_event(event) is not None:
                asyncio.create_task(_handle_start_here_intro(event))
            return JSONResponse(status_code=200, content={})
    
    if (
        event_type == "message"
        and not event.get("bot_id")
        and (not event.get("subtype") or event.get("subtype") == "file_share")
    ):
        from .slack_client import get_channel_id

        try:
            settings = get_settings()
            intro_enabled = bool(getattr(settings, "START_HERE_INTRO_ENABLED", True))
            intro_channel_name = str(
                getattr(settings, "START_HERE_INTRO_CHANNEL_NAME", "_start-here")
            )
        except Exception:
            intro_enabled = True
            intro_channel_name = "_start-here"
        start_here_id = get_channel_id(intro_channel_name) if intro_enabled else None
        if start_here_id and event.get("channel") == start_here_id and not event.get("subtype"):
            if event.get("thread_ts"):
                print(f"🧵 Ignoring thread reply in #_start-here from {event.get('user')}")
                return JSONResponse(status_code=200, content={})

            asyncio.create_task(_handle_start_here_intro(event))
            return JSONResponse(status_code=200, content={})

        try:
            settings = get_settings()
            boost_link_love_enabled = settings.BOOST_LINK_LOVE_ENABLED
            boost_channel_name = settings.BOOST_LINK_LOVE_CHANNEL_NAME
            configured_boost_channel_id = str(
                getattr(settings, "BOOST_LINK_LOVE_CHANNEL_ID", "") or ""
            )
        except Exception as exc:
            print(f"⚠️ Link-love config unavailable; skipping boost channel routing: {exc}")
            boost_link_love_enabled = False
            boost_channel_name = "boost-my-startup"
            configured_boost_channel_id = ""

        if boost_link_love_enabled:
            boost_channel_id = configured_boost_channel_id or get_channel_id(
                boost_channel_name
            )
            if boost_channel_id and event.get("channel") == boost_channel_id:
                thread_ts = str(event.get("thread_ts") or "")
                message_ts = str(event.get("ts") or "")
                if thread_ts and message_ts and thread_ts != message_ts:
                    asyncio.create_task(handle_link_love_reply(event))
                elif not thread_ts:
                    asyncio.create_task(
                        handle_boost_root_post(
                            event,
                            workspace_id=str(
                                payload.get("team_id") or payload.get("team") or ""
                            ),
                        )
                    )
                return JSONResponse(status_code=200, content={})
        
        is_dm = event.get("channel_type") == "im"
        if is_dm:
            event_files = event.get("files") if isinstance(event.get("files"), list) else []
            if event.get("subtype") == "file_share" and not _looks_like_linear_meeting_file_request(
                event.get("text", ""),
                has_files=bool(event_files),
            ):
                return JSONResponse(status_code=200, content={})
            print(f"📨 Received DM from {event.get('user')}")
            asyncio.create_task(
                _handle_mention(_with_slack_delivery_context(event, payload))
            )
            return JSONResponse(status_code=200, content={})

        if (
            not event.get("subtype")
            and _is_contextual_channel_enabled(settings, event.get("channel"))
        ):
            asyncio.create_task(
                _handle_contextual_slack_message_safely(
                    _with_slack_delivery_context(event, payload),
                    slack_team_id=str(payload.get("team_id") or ""),
                    trigger_source="channel_message",
                )
            )
            return JSONResponse(status_code=200, content={})
    
    return JSONResponse(status_code=200, content={})


async def _handle_start_here_intro(event: dict):
    """Delegate a #_start-here message event to the introduction skill."""
    return await handle_start_here_intro(event)


async def _handle_slack_mention(
    event: dict,
    *,
    slack_team_id: str,
    event_id: str,
):
    """Bind trusted Slack envelope identity to this task and its backend calls."""
    context = BackendActorContext(
        slack_team_id=slack_team_id,
        acting_slack_user_id=str(event.get("user") or ""),
        slack_channel_id=str(event.get("channel") or ""),
        slack_thread_ts=str(event.get("thread_ts") or event.get("ts") or ""),
        event_id=event_id,
    )
    with use_backend_actor_context(context):
        return await _handle_mention(event)


async def _handle_mention(event: dict):
    """Bind trusted Slack envelope identity to one routed task."""
    context = BackendActorContext(
        slack_team_id=str(event.get("_slack_team_id") or ""),
        acting_slack_user_id=str(event.get("user") or ""),
        slack_channel_id=str(event.get("channel") or ""),
        slack_thread_ts=str(
            event.get("thread_ts") or event.get("ts") or ""
        ),
        event_id=str(
            event.get("_slack_event_id")
            or event.get("client_msg_id")
            or event.get("ts")
            or ""
        ),
    )
    with use_backend_actor_context(context):
        return await _handle_mention_with_context(event)


async def _handle_mention_with_context(event: dict):
    """Handle one Slack message that has passed the addressing gate."""
    try:
        user_id = event.get("user")
        text = event.get("text", "")
        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        param_overrides = event.get("param_overrides")
        event_files = event.get("files") if isinstance(event.get("files"), list) else None
        
        print(f"\n🦘 ROO MENTION: from {user_id} in {channel_id}")
        print(f"   Text: {text[:100]}...")

        settings = get_settings()
        if (
            settings.ROO_SURFACE == "public"
            and not event.get("implicit_addressing")
            and await _maybe_handle_manual_jobs_trigger(event)
        ):
            return
        
        agent = get_agent()
        result = await agent.handle_mention(
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            param_overrides=param_overrides if isinstance(param_overrides, dict) else None,
            current_message_ts=event.get("ts"),
            event_files=event_files,
            implicit_addressing=bool(event.get("implicit_addressing")),
            contextual_candidate_reason=event.get("contextual_candidate_reason"),
            slack_team_id=get_backend_actor_context().slack_team_id,
            event_id=get_backend_actor_context().event_id,
        )

        response = None
        if result.get("message") and not result.get("suppress_post"):
            post_kwargs = {
                "channel": channel_id,
                "text": result["message"],
                "thread_ts": thread_ts,
            }
            if result.get("blocks"):
                post_kwargs["blocks"] = result["blocks"]
            response = post_message(**post_kwargs)
            await _maybe_attach_content_factory_progress(
                result.get("data"),
                response,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        print(f"✅ Mention handled successfully (skill: {result.get('skill_used')})")
        return {
            "result": result,
            "post_response": response,
            "thread_ts": thread_ts,
        }
        
    except Exception as e:
        print(f"❌ Error handling mention: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            post_message(
                channel=event.get("channel"),
                text="Sorry mate, I ran into a bit of trouble. Mind trying again? 🤔",
                thread_ts=event.get("thread_ts") or event.get("ts")
            )
        except Exception:
            pass
        return {"error": str(e), "thread_ts": event.get("thread_ts") or event.get("ts")}


async def _resume_intent(user_id: str, intent: dict):
    """Resume a pending intent after authentication."""
    try:
        text = intent.get("text")
        channel_id = intent.get("channel")
        thread_ts = intent.get("ts")
        
        print(f"🔄 Resuming intent for {user_id}: {text[:50]}...")
        
        if channel_id:
            post_message(channel_id, "✅ You're connected! Resuming your request...", thread_ts)
        
        agent = get_agent()
        result = await agent.handle_mention(
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts
        )
        
        if result.get("message") and not result.get("suppress_post"):
            post_kwargs = {
                "channel": channel_id,
                "text": result["message"],
                "thread_ts": thread_ts,
            }
            if result.get("blocks"):
                post_kwargs["blocks"] = result["blocks"]
            response = post_message(**post_kwargs)
            await _maybe_attach_content_factory_progress(
                result.get("data"),
                response,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

    except Exception as e:
        print(f"❌ Error resuming intent: {e}")
        if intent.get("channel"):
            post_message(intent["channel"], "Sorry, I had trouble resuming your request.", intent.get("ts"))


def _extract_backend_error_message(exc: Exception) -> str:
    """Pull the most useful error message from a backend exception."""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    return payload.get("error") or payload.get("detail") or str(exc)
            except Exception:
                pass
    except Exception:
        pass

    return str(exc)


async def _resolve_points_request_for_reaction(
    client: Any,
    *,
    channel_id: str,
    message_ts: str,
) -> Optional[dict[str, Any]]:
    """Resolve a pending points request from backend, cache, or Slack metadata."""
    try:
        request_record = await client.get_points_request_by_slack_message(channel_id, message_ts)
    except Exception as exc:
        print(
            "⚠️ Points request lookup by summary message failed; checking fallback sources: "
            f"channel={channel_id} message_ts={message_ts} error={exc!r}"
        )
        request_record = None

    if request_record:
        return request_record

    remembered_request = get_remembered_points_request_summary(channel_id, message_ts)
    if remembered_request:
        print(f"🧾 Using cached points request mapping for {channel_id}:{message_ts}")
        return remembered_request

    message = get_message(channel_id, message_ts)
    metadata_request = get_points_request_record_from_message(message)
    if metadata_request:
        remember_points_request_summary(channel_id, message_ts, metadata_request)
        print(f"🧾 Using Slack message metadata for points request {channel_id}:{message_ts}")
        return metadata_request

    return None


async def _handle_reaction_added(event: dict):
    """Handle Slack emoji approvals for pending points requests."""
    reactor_user_id = event.get("user")
    reaction = event.get("reaction")
    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if reaction not in APPROVAL_REACTION_NAMES:
        return
    if not reactor_user_id or item.get("type") != "message" or not channel_id or not message_ts:
        return

    print(f"✅ Reaction approval attempt from {reactor_user_id} on {channel_id}:{message_ts}")

    try:
        import httpx
        from .clients.mlai_backend import MLAIBackendClient

        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        request_record = await _resolve_points_request_for_reaction(
            client,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        if not request_record:
            return

        if request_record.get("status") not in (None, "", "pending"):
            forget_points_request_summary(channel_id, message_ts)
            return

        request_id = request_record.get("id")
        if not request_id:
            return

        try:
            result = await client.approve_points_request(int(request_id), reactor_user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 409):
                forget_points_request_summary(channel_id, message_ts)
                return
            send_dm(
                reactor_user_id,
                (
                    "I couldn't approve that points request. "
                    f"{_extract_backend_error_message(exc)}"
                ),
            )
            return
        except Exception as exc:
            send_dm(
                reactor_user_id,
                (
                    "I couldn't approve that points request just now. "
                    f"{_extract_backend_error_message(exc)}"
                ),
            )
            return

        requester = request_record.get("target_slack_id") or request_record.get("requester_slack_id")
        points = result.get("points_awarded", request_record.get("points", 0))
        reason = request_record.get("reason", "No reason provided")
        new_balance = result.get("new_balance")
        balance_line = f"\nNew balance: {new_balance} pts" if new_balance is not None else ""
        thread_ts = request_record.get("slack_thread_ts") or message_ts

        post_message(
            channel=channel_id,
            text=(
                f"✅ Points request approved by <@{reactor_user_id}>.\n\n"
                f"<@{requester}> received {points} points.\n"
                f"Reason: {reason}{balance_line}"
            ),
            thread_ts=thread_ts,
        )
        forget_points_request_summary(channel_id, message_ts)

    except Exception as exc:
        print(f"❌ Error handling reaction approval: {exc}")


@app.post("/slack/commands")
async def slack_commands(
    request: Request,
    _verified: bool = Depends(verify_slack_signature),
):
    """Slack Slash Commands webhook."""
    form = await request.form()
    if _is_duplicate_slack_request(request):
        print("↩️ Ignoring duplicate signed Slack command request")
        return {}
    command = form.get("command", "")
    text = form.get("text", "")
    user_id = form.get("user_id", "")
    settings = _request_settings(request)
    if not _is_slack_context_allowed(
        settings,
        channel_id=form.get("channel_id"),
        user_id=user_id,
        channel_type=None,
    ):
        return {
            "response_type": "ephemeral",
            "text": "This Roo deployment is not available in this context.",
        }
    if settings.ROO_SURFACE == "admin":
        return {
            "response_type": "ephemeral",
            "text": (
                "Admin Roo slash commands are not enabled in this "
                "read-only pilot."
            ),
        }
    
    print(f"📨 Slash command: {command} {text} from {user_id}")
    
    if command == "/roo" and "connect type" in text:  # Handle other connects?
        pass

    # Handle "connect github"
    if "connect github" in text.lower():
        settings = get_settings()
        from .clients.mlai_backend import MLAIBackendClient
        
        try:
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
            )
            
            # Get Auth URL
            auth_response = await client.get_github_auth_url(user_id)
            auth_url = auth_response.get("auth_url")
            
            if not auth_url:
                return {
                    "response_type": "ephemeral",
                    "text": "Sorry mate, I couldn't get the authorization URL. Please try again later."
                }
                
            return {
                "response_type": "ephemeral",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Click the button below to connect your GitHub account to Roo."
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Connect GitHub",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github_cmd",
                                "style": "primary"
                            }
                        ]
                    }
                ]
            }
            
        except Exception as e:
            print(f"Failed to handle connect github command: {e}")
            return {
                "response_type": "ephemeral",
                "text": "Sorry mate, ran into a snag getting the connection link."
            }
    
    return {
        "response_type": "ephemeral",
        "text": f"Command '{command} {text}' received! (Not yet implemented)"
    }


_INTERNAL_AI_BODY_LIMIT = 16 * 1024
_ALLOWED_CONTEST_STATES = {"eligible", "locked", "awaiting_claim", "completed"}


def _require_sim_patient_bearer(request: Request, settings: Settings) -> None:
    """Authenticate the MLAI Backend -> Roo service hop in constant time."""
    expected = (settings.SIM_PATIENT_API_KEY or "").strip()
    authentication_required = settings.is_production or bool(expected)
    if not authentication_required:
        return

    header = request.headers.get("Authorization", "")
    scheme, separator, candidate = header.partition(" ")
    valid = bool(
        expected
        and separator
        and scheme.lower() == "bearer"
        and hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
    )
    if not valid:
        # Missing and invalid credentials intentionally have one generic shape.
        raise HTTPException(status_code=401, detail="unauthorized")


async def _read_internal_json(request: Request) -> dict[str, Any]:
    """Read a small JSON object without allowing an unbounded request body."""
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")

    declared_length = request.headers.get("Content-Length")
    if declared_length:
        try:
            if int(declared_length) > _INTERNAL_AI_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="request too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length")

    body = await request.body()
    if len(body) > _INTERNAL_AI_BODY_LIMIT:
        raise HTTPException(status_code=413, detail="request too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="json object required")
    return payload


def _validated_player_id(value: Any) -> str:
    """Require the UUID minted and authenticated by MLAI Backend."""
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=422, detail="player_id must be a UUID")


def _validated_question(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="question required")
    question = value.strip()
    if not question or len(question) > maximum:
        raise HTTPException(status_code=422, detail=f"question required (1-{maximum} chars)")
    if any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in question):
        raise HTTPException(status_code=422, detail="question contains control characters")
    return question


def _validated_history(value: Any) -> list[dict[str, str]]:
    """Bound the canonical gateway transcript and discard unknown fields."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="history must be a list")
    result: list[dict[str, str]] = []
    for turn in value[-12:]:
        if not isinstance(turn, dict):
            raise HTTPException(status_code=422, detail="invalid history turn")
        role = str(turn.get("role") or "").strip().lower()
        text = turn.get("text")
        if role not in {"player", "patient"} or not isinstance(text, str):
            raise HTTPException(status_code=422, detail="invalid history turn")
        text = text.strip()
        if not text or len(text) > 1500:
            raise HTTPException(status_code=422, detail="invalid history turn")
        if any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in text):
            raise HTTPException(status_code=422, detail="invalid history turn")
        result.append({"role": role, "text": text})
    return result


def _validated_contest_state(value: Any) -> Optional[dict[str, Optional[str]]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="invalid contest_state")
    state = str(value.get("state") or "").strip().lower()
    if state not in _ALLOWED_CONTEST_STATES:
        raise HTTPException(status_code=422, detail="invalid contest_state")
    outcome = value.get("outcome")
    if outcome is not None and (not isinstance(outcome, str) or len(outcome) > 64):
        raise HTTPException(status_code=422, detail="invalid contest_state")
    return {"state": state, "outcome": outcome}


def _validated_case_id(value: Any, settings: Settings) -> int:
    """Resolve which contest case this request plays.

    Two wards run concurrently, so the authenticated gateway forwards the
    player's chosen case. Roo stays a second boundary: only cases in
    SIM_OPEN_CASE_IDS are selectable, and anything else is refused exactly
    like an unknown id, so hidden or retired cases can never leak dialogue
    or verdicts. An absent field keeps the pinned active case (older
    gateway payloads).
    """
    if value is None:
        return settings.SIM_ACTIVE_CASE_ID
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail="case_id must be an integer")
    if value not in settings.sim_open_case_ids:
        raise HTTPException(status_code=404, detail="unknown case_id")
    return value


@app.post("/api/mention")
async def api_mention(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Authenticated Public Roo entry point for trusted internal services."""
    if settings.ROO_SURFACE != "public" or not settings.INTERNAL_MENTION_API_KEY:
        raise HTTPException(status_code=404, detail="Not found")
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {settings.INTERNAL_MENTION_API_KEY}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid internal mention credentials",
        )

    payload = await request.json()
    agent = get_agent()
    return await agent.handle_mention(
        text=payload.get("text", ""),
        user_id=payload.get("user_id", ""),
        channel_id=payload.get("channel_id"),
        thread_ts=payload.get("thread_ts"),
    )


@app.post("/api/sim-patient")
async def api_sim_patient(
    request: Request,
    settings: Settings = Depends(require_public_surface),
):
    """Simulated-patient roleplay for the health-hack 3D ward.

    Runs the medhack "Guess the Diagnosis" case as an in-character narrator.
    Stateless: never touches medhack game state, points, or the guess lockout.
    role="nurse" runs Dr Snow's results agent and role="clerk" runs Nurse
    Paws' observations, examination, and final-guess preparation agent.

    Auth is mandatory and fail-closed in production (optional in local dev).
    Errors: 401 bad/missing token, 404 unknown case_id, 422 missing
    question or bad role, 502 LLM failure.
    """
    _require_sim_patient_bearer(request, settings)
    payload = await _read_internal_json(request)
    question = _validated_question(payload.get("question"), maximum=500)
    player_id = _validated_player_id(payload.get("player_id"))

    raw_role = payload.get("role") or "patient"
    if not isinstance(raw_role, str):
        raise HTTPException(status_code=422, detail="invalid role")
    role = raw_role.strip().lower()
    if role not in ("patient", "nurse", "clerk"):
        raise HTTPException(
            status_code=422,
            detail="role must be 'patient', 'nurse', or 'clerk'",
        )

    history = _validated_history(payload.get("history"))
    contest_state = _validated_contest_state(payload.get("contest_state"))
    # Roo stays a second boundary: the gateway's case_id is honored only
    # within SIM_OPEN_CASE_IDS, so a payload cannot select a hidden/old
    # contest case.
    case_id = _validated_case_id(payload.get("case_id"), settings)

    from .sim_patient import handle_question

    try:
        return await handle_question(
            question=question,
            history=history,
            case_id=case_id,
            player_id=player_id,
            role=role,
            contest_state=contest_state,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        print(
            "⚠️ sim-patient LLM failure "
            f"error_type={exc.__class__.__name__}"
        )
        raise HTTPException(status_code=502, detail="patient unavailable")


@app.post("/api/diagnosis-check")
async def api_diagnosis_check(
    request: Request,
    settings: Settings = Depends(require_public_surface),
):
    """Ward-clerk diagnosis contest: adjudicate ONE guess and record it.

    Fully scripted — no LLM anywhere in this path. The playable cases are
    pinned server-side (the SIM_OPEN_CASE_IDS allowlist): the gateway forwards
    the player's chosen open case, defaulting to SIM_ACTIVE_CASE_ID, and
    hidden/retired cases are refused before anything is recorded. The verdict
    comes from the same deterministic matcher as the Slack game (check_guess)
    and is recorded to mlai-backend BEFORE being revealed: if recording fails
    we return 503 with no verdict, so the guess is neither leaked nor burned
    (the backend row is what burns the player's single guess).

    Auth mirrors /api/sim-patient (mandatory in production).
    Errors: 401 bad token, 404 non-open case_id, 422 validation, 503
    contest/record unavailable.
    """
    _require_sim_patient_bearer(request, settings)
    payload = await _read_internal_json(request)
    raw_guess = payload.get("guess")
    if not isinstance(raw_guess, str):
        raise HTTPException(status_code=422, detail="guess required (1-200 chars)")
    guess = raw_guess.strip()
    if not guess or len(guess) > 200:
        raise HTTPException(status_code=422, detail="guess required (1-200 chars)")
    client_id = _validated_player_id(payload.get("client_id"))
    case_id = _validated_case_id(payload.get("case_id"), settings)

    from .sim_patient import check_guess, load_case, record_web_guess

    try:
        case = load_case(case_id)
    except KeyError as exc:
        # Server misconfiguration (an open case missing from cases.yaml), not
        # a client error.
        print(f"⚠️ diagnosis-check: case unavailable: {exc}")
        raise HTTPException(status_code=503, detail="contest unavailable")

    is_correct = check_guess(guess, case)

    try:
        record = await record_web_guess(
            settings,
            case_id=case.get("id"),
            case_title=str(case.get("title") or f"Case {case.get('id')}"),
            client_id=client_id,
            guess_text=guess,
            is_correct=is_correct,
        )
    except Exception as exc:
        # No verdict may leak when the registry is down (free-oracle otherwise),
        # and the guess is NOT burned — nothing was recorded.
        print(f"⚠️ diagnosis-check: record failed: {exc}")
        raise HTTPException(status_code=503, detail="record_failed")

    already = bool(record.get("already_guessed"))
    stored_correct = bool(record.get("is_correct"))
    is_first_solver = bool(record.get("is_first_solver"))
    winner_taken = bool(record.get("winner_taken"))
    if already:
        result = "already_guessed"
    elif stored_correct and is_first_solver:
        result = "correct_first"
    elif stored_correct:
        result = "correct_beaten"
    else:
        result = "incorrect"

    return {
        "result": result,
        "outcome": record.get("outcome"),
        "prize_kind": record.get("prize_kind"),
        "winner_taken": winner_taken,
        "case_id": case.get("id"),
        # The STORED verdict is authoritative (covers the already_guessed resume
        # path); the primary diagnosis is no longer secret once earned.
        "diagnosis": case.get("diagnosis") if stored_correct else None,
    }


def _format_tier_display(tier: str) -> str:
    """Format tier string for display with emoji."""
    tier_lower = tier.lower().replace("_", " ")
    if "blue" in tier_lower or "tier_1" in tier_lower or "tier 1" in tier_lower:
        return "🔵 Blue Ocean"
    elif "green" in tier_lower or "tier_2" in tier_lower or "tier 2" in tier_lower:
        return "🟢 Green"
    elif "yellow" in tier_lower or "tier_3" in tier_lower or "tier 3" in tier_lower:
        return "🟡 Yellow"
    elif "red" in tier_lower or "tier_4" in tier_lower or "tier 4" in tier_lower:
        return "🔴 Red"
    return tier.replace("_", " ").title()


@app.post("/api/callbacks/content-factory")
async def content_factory_callback(
    request: Request,
    _settings: Settings = Depends(require_public_surface),
):
    """
    Handle callbacks from mlai-backend Content Factory.

    Supports:
    - topic_confirmation_request: When research is complete and topic needs confirmation
    - topic_selection: Legacy format - When a topic is proposed for user confirmation
    - article_complete: When an article has been generated
    """
    try:
        payload = await request.json()
        event_type = payload.get("type") or payload.get("event_type")
        effective_slack_user_id = _clean_slack_user_id(payload.get("slack_user_id"))
        requested_by_slack_user_id = (
            _clean_slack_user_id(payload.get("requested_by_slack_user_id"))
            or effective_slack_user_id
        )
        recipient_slack_user_id = requested_by_slack_user_id or effective_slack_user_id

        print(
            "🏭 Content Factory event: "
            f"{event_type} for effective={effective_slack_user_id} recipient={recipient_slack_user_id}"
        )

        # Handle new topic_confirmation_request format
        if event_type == "topic_confirmation_request":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            data = payload.get("data", {})

            keyword = data.get("selected_keyword")
            reason = data.get("selection_reason", "")
            metrics = data.get("metrics", {})
            volume = metrics.get("volume", 0)
            difficulty = metrics.get("difficulty", 0)
            tier = metrics.get("tier", "")
            score = metrics.get("score", 0)
            alternatives = data.get("alternatives", [])

            # Format tier for display
            tier_display = _format_tier_display(tier)

            # Build Block Kit message matching the specification
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Content Research Complete",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommended Topic:* `{keyword}`\n\nI found this topic has high potential for *{domain}*."
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Volume:*\n{volume:,}/mo"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Difficulty:*\n{difficulty}/100"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Tier:*\n{tier_display}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Opportunity Score:*\n{score}"
                        }
                    ]
                }
            ]

            # Add alternatives section if present
            if alternatives:
                alt_text = "*Alternatives:*\n" + "\n".join(f"• {alt}" for alt in alternatives)
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": alt_text
                    }
                })

            # Build action elements
            action_elements = [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Write This Article",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": json.dumps(
                        _content_factory_identity_payload(
                            requested_by_slack_user_id=requested_by_slack_user_id,
                            effective_slack_user_id=effective_slack_user_id,
                            action="confirm_topic",
                            keyword=keyword,
                            job_id=job_id,
                            domain=domain,
                        )
                    ),
                    "action_id": "confirm_topic"
                }
            ]

            # Add static_select dropdown for alternatives if present
            if alternatives:
                alt_options = [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": alt[:75]  # Slack limit for option text
                        },
                        "value": json.dumps(
                            _content_factory_identity_payload(
                                requested_by_slack_user_id=requested_by_slack_user_id,
                                effective_slack_user_id=effective_slack_user_id,
                                action="confirm_topic",
                                keyword=alt,
                                job_id=job_id,
                                domain=domain,
                            )
                        ),
                    }
                    for alt in alternatives[:10]  # Limit to 10 options
                ]
                action_elements.append({
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Pick Alternative",
                        "emoji": True
                    },
                    "options": alt_options,
                    "action_id": "select_alternative"
                })

            # Add cancel button
            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": json.dumps(
                    _content_factory_identity_payload(
                        requested_by_slack_user_id=requested_by_slack_user_id,
                        effective_slack_user_id=effective_slack_user_id,
                        action="cancel_topic",
                        job_id=job_id,
                        domain=domain,
                    )
                ),
                "action_id": "cancel_topic"
            })

            blocks.append({
                "type": "actions",
                "block_id": "topic_confirmation_actions",
                "elements": action_elements
            })

            # Send DM
            dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Content research complete. Recommended topic: {keyword}",
                    blocks=blocks
                )
            return {"status": "ok"}

        # Handle legacy topic_selection format for backwards compatibility
        elif event_type == "topic_selection":
            selection = payload.get("selection", {})
            keyword = selection.get("selected_keyword")
            reason = selection.get("selection_reason")
            volume = selection.get("volume")
            difficulty = selection.get("difficulty")
            tier = selection.get("tier", "")
            score = selection.get("opportunity_index")
            alternatives = selection.get("top_alternatives", [])
            job_id = payload.get("job_id")
            domain = payload.get("domain")

            tier_display = _format_tier_display(tier)

            # Use the new Block Kit format for legacy events too
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Content Research Complete",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommended Topic:* `{keyword}`\n\nI found this topic has high potential for *{domain}*."
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Volume:*\n{volume:,}/mo" if volume else "*Volume:*\n--"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Difficulty:*\n{difficulty}/100" if difficulty else "*Difficulty:*\n--"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Tier:*\n{tier_display}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Opportunity Score:*\n{score}" if score else "*Opportunity Score:*\n--"
                        }
                    ]
                }
            ]

            if alternatives:
                alt_text = "*Alternatives:*\n" + "\n".join(f"• {alt}" for alt in alternatives)
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": alt_text
                    }
                })

            action_elements = [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Write This Article",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": json.dumps(
                        _content_factory_identity_payload(
                            requested_by_slack_user_id=requested_by_slack_user_id,
                            effective_slack_user_id=effective_slack_user_id,
                            action="confirm_topic",
                            keyword=keyword,
                            job_id=job_id,
                            domain=domain,
                        )
                    ),
                    "action_id": "confirm_topic"
                }
            ]

            if alternatives:
                alt_options = [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": alt[:75]
                        },
                        "value": json.dumps(
                            _content_factory_identity_payload(
                                requested_by_slack_user_id=requested_by_slack_user_id,
                                effective_slack_user_id=effective_slack_user_id,
                                action="confirm_topic",
                                keyword=alt,
                                job_id=job_id,
                                domain=domain,
                            )
                        ),
                    }
                    for alt in alternatives[:10]
                ]
                action_elements.append({
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Pick Alternative",
                        "emoji": True
                    },
                    "options": alt_options,
                    "action_id": "select_alternative"
                })

            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": json.dumps(
                    _content_factory_identity_payload(
                        requested_by_slack_user_id=requested_by_slack_user_id,
                        effective_slack_user_id=effective_slack_user_id,
                        action="cancel_topic",
                        job_id=job_id,
                        domain=domain,
                    )
                ),
                "action_id": "cancel_topic"
            })

            blocks.append({
                "type": "actions",
                "block_id": "topic_confirmation_actions",
                "elements": action_elements
            })

            dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Topic selected: {keyword}",
                    blocks=blocks
                )
            return {"status": "ok"}

        elif event_type == "article_complete":
            article_url = payload.get("article_url")
            pr_url = payload.get("pr_url")
            domain = payload.get("domain")
            topic = payload.get("topic", "")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "**✅ Article Published!**"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Topic:*\n{topic}" if topic else f"*Domain:*\n{domain}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*URL:*\n<{article_url}|View Article>"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*PR:* <{pr_url}|View Pull Request>"
                    }
                }
            ]

            dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Article published: {article_url}",
                    blocks=blocks
                )
            return {"status": "ok"}

        elif event_type == "scan_complete":
            job_id = payload.get("job_id")
            run_id = payload.get("run_id") or job_id
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            components_count = payload.get("components_count", 0)
            component_names = payload.get("component_names", [])
            pillar_count = payload.get("pillar_count")
            pillar_names = payload.get("pillar_names")
            requested_action = str(payload.get("requested_action") or "").strip()
            scaffold_status = str(payload.get("scaffold_status") or "").strip()
            scaffold_queued = bool(payload.get("scaffold_queued"))
            scaffold_job_id = str(payload.get("scaffold_job_id") or "").strip()
            article_system = payload.get("article_system") if isinstance(payload.get("article_system"), dict) else {}
            registry_target = _best_registry_driven_target(payload.get("publish_targets"), payload, article_system)
            registry_target_ready = _registry_target_publish_ready(registry_target)
            registry_summary = (
                _registry_target_summary(
                    domain=domain,
                    target=registry_target,
                    article_system=article_system,
                )
                if registry_target
                else ""
            )
            approval_required = (
                requested_action == "scaffold_publish_route"
                and scaffold_status == "approval_required"
            )

            print(f"📦 Scan complete for {domain}: {components_count} components, {pillar_count} pillars")
            _remember_content_thread_context(
                channel_id,
                thread_ts,
                domain,
                "scan",
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

            if components_count > 0:
                # Build component summary: "ArticleHeroHeader, ArticleCard, ArticleFAQ, +27 more"
                shown_names = component_names[:3]
                component_summary = ", ".join(shown_names)
                remaining = components_count - len(shown_names)
                if remaining > 0:
                    component_summary += f", +{remaining} more"

                summary = f"I've analysed your codebase and generated:\n• *{components_count} article components* ({component_summary})"

                if pillar_count and pillar_names:
                    pillar_list = ", ".join(pillar_names)
                    summary += f"\n• *{pillar_count} content pillars:* {pillar_list}"
                elif pillar_count:
                    summary += f"\n• *{pillar_count} content pillars*"

                if approval_required:
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"✅ *Scan complete for {domain}*\n\n{summary}"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Ready to create your articles directory? This will open a PR with:\n- An articles listing page\n- All {0} reusable components\n- A demo article showcasing how they look".format(components_count)
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "scaffold_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Create Articles Directory",
                                        "emoji": True
                                    },
                                    "style": "primary",
                                    "value": json.dumps(
                                        _content_factory_identity_payload(
                                            requested_by_slack_user_id=requested_by_slack_user_id,
                                            effective_slack_user_id=effective_slack_user_id,
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            scan_run_id=run_id,
                                            client_request_id=f"content-factory-{uuid4().hex}",
                                        )
                                    ),
                                    "action_id": "scaffold_confirm"
                                },
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Not Now",
                                        "emoji": True
                                    },
                                    "value": json.dumps(
                                        _content_factory_identity_payload(
                                            requested_by_slack_user_id=requested_by_slack_user_id,
                                            effective_slack_user_id=effective_slack_user_id,
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            scan_run_id=run_id,
                                        )
                                    ),
                                    "action_id": "scaffold_skip"
                                }
                            ]
                        }
                    ]
                else:
                    detail_text = (
                        registry_summary
                        if registry_summary
                        else "Scan completed successfully. If you need an articles directory scaffold, run a fresh scan so Roo can request approval-first scaffolding."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"✅ *Scan complete for {domain}*\n\n{summary}"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": detail_text
                            }
                        }
                    ]
            else:
                no_component_text = (
                    f"✅ *Scan complete for {domain}*\n\n{registry_summary}"
                    if registry_summary
                    else f"✅ *Scan complete for {domain}*\n\nNo new components were detected."
                )
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": no_component_text
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Scan complete for {domain}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted scan results to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Scan complete for {domain}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {recipient_slack_user_id}")
                dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Scan complete for {domain}",
                        blocks=blocks
                    )

            # Auto-continue: check for pending intent after scan completes
            pending = _get_pending_intent(
                requested_by_slack_user_id,
                domain,
                effective_slack_user_id=effective_slack_user_id,
                job_id=job_id,
                wait_for="scan_complete",
            )
            if pending:
                pending_action = pending.get("action")
                intent_channel = pending.get("channel_id") or channel_id
                intent_thread = pending.get("thread_ts") or thread_ts
                pending_requested_by_slack_user_id = str(
                    pending.get("requested_by_slack_user_id") or requested_by_slack_user_id or ""
                ).strip()
                pending_effective_slack_user_id = str(
                    pending.get("effective_slack_user_id") or effective_slack_user_id or ""
                ).strip()

                if approval_required and pending_action in ("scaffold", "write"):
                    print(f"⏸️ Waiting for scaffold approval before continuing {pending_action} for {domain}")
                elif pending_action in ("scaffold", "write"):
                    if scaffold_queued:
                        print(f"⏳ Scan already queued scaffold for {domain}; waiting for scaffold completion")
                        if pending_action == "write":
                            _remember_pending_intent(
                                pending_requested_by_slack_user_id,
                                domain,
                                effective_slack_user_id=pending_effective_slack_user_id,
                                intent_data=pending,
                                channel_id=intent_channel,
                                thread_ts=intent_thread,
                                wait_for="scaffold_complete",
                                job_id=scaffold_job_id or None,
                                clear_job_id=True,
                            )
                            if intent_channel:
                                post_message(
                                    channel=intent_channel,
                                    thread_ts=intent_thread,
                                    text=f"📁 Scan complete! I’m waiting for the articles directory PR for *{domain}* before continuing your article request."
                                )
                        else:
                            _get_pending_intent(
                                pending_requested_by_slack_user_id,
                                domain,
                                effective_slack_user_id=pending_effective_slack_user_id,
                                job_id=job_id,
                                wait_for="scan_complete",
                                consume=True,
                            )
                            if intent_channel:
                                post_message(
                                    channel=intent_channel,
                                    thread_ts=intent_thread,
                                    text=f"📁 Scan complete! The articles directory setup is already queued for *{domain}*."
                                )
                    elif scaffold_status == "not_needed" or registry_target_ready:
                        consumed = _get_pending_intent(
                            pending_requested_by_slack_user_id,
                            domain,
                            effective_slack_user_id=pending_effective_slack_user_id,
                            job_id=job_id,
                            wait_for="scan_complete",
                            consume=True,
                        ) or pending
                        if pending_action == "write":
                            await _trigger_article_generation_from_pending(
                                consumed,
                                requested_by_slack_user_id=pending_requested_by_slack_user_id,
                                effective_slack_user_id=pending_effective_slack_user_id,
                                domain=domain,
                                fallback_channel_id=channel_id,
                                fallback_thread_ts=thread_ts,
                            )
                        elif intent_channel:
                            post_message(
                                channel=intent_channel,
                                thread_ts=intent_thread,
                                text=(
                                    f"Scan complete. Your repo already has the publish route Roo needs for *{domain}*."
                                    if not registry_target_ready
                                    else f"Scan complete. Roo found a publish-ready registry-driven SEO system for *{domain}*."
                                )
                            )
                    elif registry_target:
                        _get_pending_intent(
                            pending_requested_by_slack_user_id,
                            domain,
                            effective_slack_user_id=pending_effective_slack_user_id,
                            job_id=job_id,
                            wait_for="scan_complete",
                            consume=True,
                        )
                        print(f"Registry-driven target for {domain} is not publish-ready; not auto-triggering scaffold")
                        if intent_channel:
                            post_message(
                                channel=intent_channel,
                                thread_ts=intent_thread,
                                text=registry_summary,
                            )
                    else:
                        _get_pending_intent(
                            pending_requested_by_slack_user_id,
                            domain,
                            effective_slack_user_id=pending_effective_slack_user_id,
                            job_id=job_id,
                            wait_for="scan_complete",
                            consume=True,
                        )
                        print(
                            f"⚠️ Scan for {domain} did not provide approval-ready scaffold metadata "
                            f"(scaffold_status={scaffold_status or 'unknown'}); not auto-triggering scaffold"
                        )
                        if intent_channel:
                            post_message(
                                channel=intent_channel,
                                thread_ts=intent_thread,
                                text=(
                                    f"⚠️ I need a fresh scan result with scaffold approval metadata before I can continue for *{domain}*. "
                                    f"Please run the scan again."
                                ),
                            )

            return {"status": "ok"}

        elif event_type == "scaffold_complete":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            pr_url = payload.get("pr_url")
            files_created = payload.get("files_created", 0)
            pillar_count = payload.get("pillar_count", 0)
            component_count = payload.get("component_count", 0)
            already_exists = payload.get("already_exists", False)
            preview_url = payload.get("preview_url")
            build_verified = payload.get("build_verified", False)
            client_request_id = _content_factory_client_request_id(payload.get("client_request_id"))

            print(f"📁 Scaffold complete for {domain}: PR={pr_url}")
            _remember_content_thread_context(
                channel_id,
                thread_ts,
                domain,
                "scaffold",
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

            if already_exists:
                detail_lines = []
                if pr_url:
                    detail_lines.append(f"• PR: <{pr_url}|View pull request>")
                if preview_url:
                    detail_lines.append(f"• Preview: <{preview_url}|View live preview> :eyes:")
                details = f"\n\n" + "\n".join(detail_lines) if detail_lines else ""
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"ℹ️ Articles directory already exists - no changes needed.{details}"
                        }
                    }
                ]
            elif pr_url:
                details = (
                    f"• {files_created} files created ({component_count} components, {pillar_count} content pillars)"
                    if files_created
                    else f"• Reused the existing scaffold branch/PR ({component_count} components, {pillar_count} content pillars)"
                )
                details += f"\n• PR: <{pr_url}|View pull request>"
                details += (
                    "\n• Build: Passed :white_check_mark:"
                    if build_verified
                    else "\n• Build: Not verified"
                )

                if preview_url:
                    preview_label = "View live preview"
                    if "stackblitz.com" in preview_url:
                        preview_label += " (may take a moment to load)"
                    details += f"\n• Preview: <{preview_url}|{preview_label}> :eyes:"

                extra_sections = []
                if preview_url:
                    extra_sections.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Check out the demo article in the preview to see how your components look!\n_Note: Preview may take a moment to load._"
                        }
                    })

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ *Articles directory created for {domain}*\n\n{details}"
                        }
                    },
                    *extra_sections,
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Ready to write your first article?"
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "write_article_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Write First Article",
                                    "emoji": True
                                },
                                "style": "primary",
                                "value": json.dumps(
                                    _content_factory_identity_payload(
                                        requested_by_slack_user_id=requested_by_slack_user_id,
                                        effective_slack_user_id=effective_slack_user_id,
                                        domain=domain,
                                        channel_id=channel_id,
                                        thread_ts=thread_ts,
                                        client_request_id=client_request_id,
                                    )
                                ),
                                "action_id": "write_first_article"
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Not Now",
                                    "emoji": True
                                },
                                "value": json.dumps(
                                    _content_factory_identity_payload(
                                        requested_by_slack_user_id=requested_by_slack_user_id,
                                        effective_slack_user_id=effective_slack_user_id,
                                        domain=domain,
                                    )
                                ),
                                "action_id": "write_article_skip"
                            }
                        ]
                    }
                ]
            else:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📁 Articles directory scaffolded for *{domain}*, but the PR could not be created. Check your repo for a `feature/articles-scaffolding` branch."
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Scaffold complete for {domain}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted scaffold results to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Scaffold complete for {domain}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {recipient_slack_user_id}")
                dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Scaffold complete for {domain}",
                        blocks=blocks
                    )

            # Auto-continue: check for pending write intent after scaffold completes
            pending = _get_pending_intent(
                requested_by_slack_user_id,
                domain,
                effective_slack_user_id=effective_slack_user_id,
                job_id=job_id,
                wait_for="scaffold_complete",
                consume=True,
            )
            if pending and pending.get("action") == "write":
                await _trigger_article_generation_from_pending(
                    pending,
                    requested_by_slack_user_id=str(
                        pending.get("requested_by_slack_user_id") or requested_by_slack_user_id or ""
                    ).strip(),
                    effective_slack_user_id=str(
                        pending.get("effective_slack_user_id") or effective_slack_user_id or ""
                    ).strip(),
                    domain=domain,
                    fallback_channel_id=channel_id,
                    fallback_thread_ts=thread_ts,
                )

            return {"status": "ok"}

        elif event_type == "generation_failed":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            error_message = payload.get("error_message") or payload.get("error", "Unknown error")
            error_code = payload.get("error_code")
            stage = payload.get("stage", "generation")  # scan, scaffold, generation

            print(f"❌ Generation failed for {domain} at {stage}: {error_message} (code: {error_code})")
            wait_for = {
                "scan": "scan_complete",
                "scaffold": "scaffold_complete",
            }.get(stage)
            if wait_for:
                cleared_pending = _get_pending_intent(
                    requested_by_slack_user_id,
                    domain,
                    effective_slack_user_id=effective_slack_user_id,
                    job_id=job_id,
                    wait_for=wait_for,
                    consume=True,
                )
                if cleared_pending:
                    print(
                        "🧹 Cleared pending intent after "
                        f"{stage} failure for {requested_by_slack_user_id}:{effective_slack_user_id}:{domain}"
                    )

            # Provide specific error messages based on error_code
            if error_code == "INVALID_CREDENTIALS":
                if _is_delegated_content_factory_request(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ):
                    user_message = _delegated_content_factory_auth_error_text(
                        effective_slack_user_id=effective_slack_user_id,
                        domain=domain,
                    )
                    auth_url = None
                else:
                    user_message = "❌ I need fresh GitHub access. Please reconnect your GitHub account to continue."
                    # Fetch auth URL so we can show a reconnect button
                    try:
                        from .clients.mlai_backend import MLAIBackendClient
                        settings = get_settings()
                        auth_client = MLAIBackendClient(
                            base_url=settings.MLAI_BACKEND_URL,
                            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
                        )
                        auth_response = await auth_client.get_github_auth_url(
                            effective_slack_user_id,
                            domain=domain,
                        )
                        auth_url = auth_response.get("auth_url")
                    except Exception as auth_err:
                        print(f"⚠️ Failed to fetch auth URL: {auth_err}")
                        auth_url = None

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]
                if auth_url:
                    blocks.append({
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Re-connect GitHub",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github",
                                "style": "danger"
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "I've Connected - Resume",
                                    "emoji": True
                                },
                                "action_id": "resume_scan",
                                "value": json.dumps(
                                    _content_factory_identity_payload(
                                        requested_by_slack_user_id=requested_by_slack_user_id,
                                        effective_slack_user_id=effective_slack_user_id,
                                        domain=domain,
                                    )
                                ),
                                "style": "primary"
                            }
                        ]
                    })
            elif error_code == "MISSING_CONFIG":
                user_message = "❌ I don't have scan data for this site. Let me run a scan first."
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]
            else:
                user_message = f"❌ Something went wrong setting up the articles directory: {error_message}\n\nLet me know if you'd like to try again."
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Error during {stage}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted error to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Error during {stage}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {recipient_slack_user_id}")
                dm_channel = from_slack_client_open_dm(recipient_slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Error during {stage}",
                        blocks=blocks
                    )

            return {"status": "ok"}

    except Exception as e:
        print(f"❌ Error handling content factory callback: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ignored"}


# Import helper to avoid circular imports if needed, or just use existing
from .slack_client import open_dm as from_slack_client_open_dm


async def _record_admin_brain_feedback(
    *,
    settings: Settings,
    user_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    feedback: dict[str, str],
    feedback_type: str,
    correction_text: Optional[str] = None,
) -> None:
    """Record signed Slack feedback with a fresh actor assertion."""

    query_id = str(feedback.get("query_id") or "").strip()
    claim_id = str(feedback.get("claim_id") or "").strip() or None
    requester_user_id = str(
        feedback.get("requester_user_id") or ""
    ).strip()
    if not query_id or requester_user_id != user_id:
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "Only the person who requested this Admin Roo answer "
                    "can submit its feedback."
                ),
            )
        return
    if feedback_type == "incorrect" and (
        not claim_id or not correction_text
    ):
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "I need the specific correction before I can send "
                    "this answer for review."
                ),
            )
        return

    context = BackendActorContext(
        slack_team_id=team_id,
        acting_slack_user_id=user_id,
        slack_channel_id=channel_id,
        slack_thread_ts=thread_ts,
        event_id=f"Ia{uuid4().hex}",
    )
    try:
        from .clients.mlai_backend import MLAIBackendClient

        with use_backend_actor_context(context):
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                service_principal_key=settings.ORG_BRAIN_API_KEY,
                surface=settings.ROO_SURFACE,
            )
            await client.submit_org_memory_feedback(
                query_id=query_id,
                feedback_type=feedback_type,
                claim_id=claim_id,
                correction_text=correction_text,
                timeout=float(
                    settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS
                ),
            )
    except Exception as exc:
        print(
            "ADMIN_BRAIN_FEEDBACK_FAILED "
            f"error={exc.__class__.__name__} query_id={query_id}"
        )
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "I couldn't record that Admin Roo feedback just now. "
                    "The answer itself was not changed."
                ),
            )
        return

    acknowledgement = {
        "relevant": "Thanks — I recorded this answer as helpful.",
        "stale": "Thanks — I flagged this answer as potentially stale.",
        "missing": (
            "Thanks — I recorded that this answer is missing context."
        ),
        "incorrect": (
            "Thanks — I sent your correction to the organisational-memory "
            "review queue."
        ),
    }.get(feedback_type, "Thanks — I recorded your feedback.")
    if channel_id:
        post_message(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=acknowledgement,
        )


async def _record_unified_admin_brain_feedback(
    *,
    settings: Settings,
    user_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    feedback: dict[str, str],
    feedback_type: str,
    correction_text: Optional[str] = None,
) -> None:
    """Re-authorize and relay Admin feedback without exposing its credential."""

    query_id = str(feedback.get("query_id") or "").strip()
    claim_id = str(feedback.get("claim_id") or "").strip() or None
    requester_user_id = str(feedback.get("requester_user_id") or "").strip()
    if not query_id or requester_user_id != user_id:
        message = "Only the person who requested this answer can submit its feedback."
    elif feedback_type == "incorrect" and (not claim_id or not correction_text):
        message = "I need the specific correction before I can send this answer for review."
    else:
        context = BackendActorContext(
            slack_team_id=team_id,
            acting_slack_user_id=user_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            event_id=f"Ia{uuid4().hex}",
        )
        try:
            from .clients.mlai_backend import MLAIBackendClient

            eligibility = await MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                service_principal_key=settings.ORG_BRAIN_ROUTER_API_KEY,
                surface="gateway",
                actor_context=context,
            ).get_admin_routing_eligibility()
            if not bool(eligibility.get("admin_brain_eligible")):
                message = "Your current Roo access does not allow internal-memory feedback."
            else:
                response = await AdminDispatchClient(
                    base_url=settings.ROO_ADMIN_INTERNAL_URL,
                    secret=settings.ROO_ADMIN_DISPATCH_SECRET,
                    timeout=float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS) + 5,
                ).dispatch(
                    kind="feedback",
                    context=context,
                    payload={
                        "query_id": query_id,
                        "claim_id": claim_id or "",
                        "feedback_type": feedback_type,
                        "correction_text": correction_text or "",
                        "requester_user_id": requester_user_id,
                    },
                )
                destination = response.get("destination") or {}
                if (
                    destination.get("channel_id") != channel_id
                    or str(destination.get("thread_ts") or "") != str(thread_ts or "")
                    or destination.get("requester_user_id") != user_id
                ):
                    raise ValueError("Admin feedback response destination did not match")
                message = str(response.get("message") or "").strip()
                if not message:
                    raise ValueError("Admin feedback response was empty")
        except Exception as exc:
            print(
                "UNIFIED_ADMIN_BRAIN_FEEDBACK_FAILED "
                f"error={exc.__class__.__name__} query_id={query_id}"
            )
            message = (
                "I couldn't record that internal-memory feedback just now. "
                "The answer itself was not changed."
            )

    if channel_id:
        post_message(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=message,
        )


def _open_admin_brain_correction_modal(
    *,
    payload: dict,
    action: dict,
    user_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    feedback = parse_feedback_value(action.get("value"))
    if feedback.get("requester_user_id") != user_id:
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "Only the person who requested this Admin Roo answer "
                    "can correct it."
                ),
            )
        return
    if not feedback.get("query_id") or not feedback.get("claim_id"):
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "This answer doesn't contain a reviewable claim. "
                    "Use Missing context instead."
                ),
            )
        return
    trigger_id = str(payload.get("trigger_id") or "").strip()
    if not trigger_id:
        return
    from .slack_client import get_slack_client

    get_slack_client().views_open(
        trigger_id=trigger_id,
        view=build_incorrect_feedback_modal(
            feedback,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        ),
    )


def _open_admin_action_rejection_modal(
    *,
    payload: dict,
    action: dict,
    team_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    proposal = parse_admin_action_value(action.get("value"))
    trigger_id = str(payload.get("trigger_id") or "").strip()
    if not proposal or not trigger_id:
        if channel_id:
            post_message(
                channel=channel_id,
                thread_ts=thread_ts or None,
                text=(
                    "This controlled-action card could not be verified. "
                    "Ask Admin Roo to open the proposal again."
                ),
            )
        return
    from .slack_client import get_slack_client

    get_slack_client().views_open(
        trigger_id=trigger_id,
        view=build_admin_action_reject_modal(
            proposal,
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        ),
    )


async def _review_admin_action(
    *,
    settings: Settings,
    decision: str,
    proposal_id: str,
    user_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    rejection_reason: Optional[str] = None,
) -> None:
    """Re-authenticate a reviewer and defer every transition to the backend."""

    if (
        settings.ROO_SURFACE != "admin"
        or not settings.ORG_BRAIN_ACTIONS_ENABLED
        or decision not in {"approve", "reject"}
    ):
        return
    context = BackendActorContext(
        slack_team_id=team_id,
        acting_slack_user_id=user_id,
        slack_channel_id=channel_id,
        slack_thread_ts=thread_ts,
        event_id=f"Ia{uuid4().hex}",
    )
    result = None
    failure_message = ""
    try:
        from .clients.mlai_backend import MLAIBackendClient

        with use_backend_actor_context(context):
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                service_principal_key=settings.ORG_BRAIN_API_KEY,
                surface=settings.ROO_SURFACE,
            )
            timeout = float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS)
            current = await client.get_org_memory_action(
                proposal_id,
                timeout=timeout,
            )
            status = str(current.get("status") or "")
            if decision == "reject":
                if status == "rejected":
                    result = current
                else:
                    result = await client.reject_org_memory_action(
                        proposal_id,
                        reason=str(rejection_reason or "").strip(),
                        idempotency_key=(
                            f"slack-reject-{proposal_id}-{user_id}"
                        ),
                        timeout=timeout,
                    )
            elif status in {"completed", "rejected", "failed", "reversed"}:
                result = current
            else:
                approved = current
                if status in {"awaiting_approval", "stale"}:
                    approved = await client.approve_org_memory_action(
                        proposal_id,
                        idempotency_key=(
                            f"slack-approve-{proposal_id}-{user_id}"
                        ),
                        timeout=timeout,
                    )
                if str(approved.get("status") or "") == "approved":
                    result = await client.execute_org_memory_action(
                        proposal_id,
                        idempotency_key=f"slack-execute-{proposal_id}",
                        timeout=timeout,
                    )
                else:
                    result = approved
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        try:
            detail = str(
                exc.response.json().get("detail") or ""
            ).strip()
        except (ValueError, AttributeError):
            detail = ""
        print(
            "ADMIN_ACTION_REVIEW_DENIED "
            f"status={status_code} decision={decision} "
            f"proposal_id={proposal_id}"
        )
        if status_code in {401, 403, 404}:
            failure_message = (
                "You are not authorised to review that controlled action, "
                "or it is no longer visible to you."
            )
        elif detail:
            failure_message = f"Nothing was changed. {detail}"
        else:
            failure_message = (
                "The action was not changed because the backend rejected "
                "the review."
            )
    except Exception as exc:
        print(
            "ADMIN_ACTION_REVIEW_FAILED "
            f"error={exc.__class__.__name__} decision={decision} "
            f"proposal_id={proposal_id}"
        )
        failure_message = (
            "The controlled-action gateway is unavailable. "
            "I did not retry or claim that anything changed."
        )

    if not channel_id:
        return
    if result is not None:
        rendered = build_admin_action_response(result)
        post_message(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=rendered["message"],
            blocks=rendered["blocks"],
        )
    else:
        post_message(
            channel=channel_id,
            thread_ts=thread_ts or None,
            text=failure_message,
        )



@app.post("/slack/actions")
async def slack_actions(
    request: Request,
    _verified: bool = Depends(verify_slack_signature),
):
    """Handle interactive actions (e.g. button clicks)."""
    form = await request.form()
    payload_json = form.get("payload")
    if not payload_json:
        return JSONResponse(status_code=400, content={"error": "Missing payload"})
        
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if _is_duplicate_slack_request(request):
        print("↩️ Ignoring duplicate signed Slack action request")
        return JSONResponse(status_code=200, content={})

    settings = _request_settings(request)
    payload_type = str(payload.get("type") or "")
    user_id = payload.get("user", {}).get("id")
    correction_submission = (
        parse_incorrect_feedback_submission(payload)
        if payload_type == "view_submission"
        else {}
    )
    action_rejection_submission = (
        parse_admin_action_reject_submission(payload)
        if payload_type == "view_submission"
        else {}
    )
    submission = correction_submission or action_rejection_submission
    channel_id = (
        payload.get("channel", {}).get("id")
        or submission.get("channel_id")
    )
    channel_type = (
        "im" if str(channel_id or "").startswith("D") else None
    )
    if not _is_slack_context_allowed(
        settings,
        channel_id=channel_id,
        user_id=user_id,
        channel_type=channel_type,
    ):
        print("🔒 Ignoring Slack action outside the deployment context allowlist")
        return JSONResponse(status_code=200, content={})

    if settings.ROO_SURFACE == "admin":
        if payload_type == "view_submission":
            if not submission:
                print("🔒 Admin Roo ignored an unsupported view submission")
                return JSONResponse(status_code=200, content={})
            team_id = str(payload.get("team", {}).get("id") or "")
            if correction_submission:
                if (
                    submission.get("requester_user_id") != user_id
                    or submission.get("team_id") != team_id
                    or not submission.get("query_id")
                    or not submission.get("claim_id")
                    or not submission.get("correction_text")
                ):
                    return JSONResponse(
                        status_code=200,
                        content={
                            "response_action": "errors",
                            "errors": {
                                "admin_brain_correction": (
                                    "This correction could not be verified. "
                                    "Reopen it from the original answer."
                                )
                            },
                        },
                    )
                asyncio.create_task(
                    _record_admin_brain_feedback(
                        settings=settings,
                        user_id=str(user_id or ""),
                        team_id=team_id,
                        channel_id=str(channel_id or ""),
                        thread_ts=submission.get("thread_ts") or "",
                        feedback=submission,
                        feedback_type="incorrect",
                        correction_text=submission.get("correction_text"),
                    )
                )
                return JSONResponse(
                    status_code=200,
                    content={"response_action": "clear"},
                )

            if (
                not settings.ORG_BRAIN_ACTIONS_ENABLED
                or submission.get("team_id") != team_id
                or not submission.get("proposal_id")
                or not submission.get("reason")
            ):
                return JSONResponse(
                    status_code=200,
                    content={
                        "response_action": "errors",
                        "errors": {
                            "admin_action_rejection": (
                                "This rejection could not be verified. "
                                "Reopen it from the original action card."
                            )
                        },
                    },
                )
            asyncio.create_task(
                _review_admin_action(
                    settings=settings,
                    decision="reject",
                    proposal_id=submission["proposal_id"],
                    user_id=str(user_id or ""),
                    team_id=team_id,
                    channel_id=str(channel_id or ""),
                    thread_ts=submission.get("thread_ts") or "",
                    rejection_reason=submission["reason"],
                )
            )
            return JSONResponse(
                status_code=200,
                content={"response_action": "clear"},
            )

        actions = payload.get("actions", [])
        if not actions:
            return JSONResponse(status_code=200, content={})
        action = actions[0]
        action_id = str(action.get("action_id") or "")
        message = payload.get("message", {})
        thread_ts = str(
            message.get("thread_ts") or message.get("ts") or ""
        )
        team_id = str(payload.get("team", {}).get("id") or "")
        if action_id in {ADMIN_ACTION_APPROVE, ADMIN_ACTION_REJECT}:
            if not settings.ORG_BRAIN_ACTIONS_ENABLED:
                print(
                    "🔒 Admin Roo ignored a controlled action while "
                    "actions are disabled"
                )
                return JSONResponse(status_code=200, content={})
            proposal = parse_admin_action_value(action.get("value"))
            if not proposal:
                if channel_id:
                    post_message(
                        channel=channel_id,
                        thread_ts=thread_ts or None,
                        text=(
                            "This controlled-action card could not be "
                            "verified. Ask Admin Roo to open the proposal "
                            "again."
                        ),
                    )
                return JSONResponse(status_code=200, content={})
            if action_id == ADMIN_ACTION_REJECT:
                _open_admin_action_rejection_modal(
                    payload=payload,
                    action=action,
                    team_id=team_id,
                    channel_id=str(channel_id or ""),
                    thread_ts=thread_ts,
                )
                return JSONResponse(status_code=200, content={})
            asyncio.create_task(
                _review_admin_action(
                    settings=settings,
                    decision="approve",
                    proposal_id=proposal["proposal_id"],
                    user_id=str(user_id or ""),
                    team_id=team_id,
                    channel_id=str(channel_id or ""),
                    thread_ts=thread_ts,
                )
            )
            return JSONResponse(status_code=200, content={})
        if action_id == ADMIN_BRAIN_INCORRECT_ACTION:
            _open_admin_brain_correction_modal(
                payload=payload,
                action=action,
                user_id=str(user_id or ""),
                team_id=team_id,
                channel_id=str(channel_id or ""),
                thread_ts=thread_ts,
            )
            return JSONResponse(status_code=200, content={})
        feedback_type = ADMIN_BRAIN_FEEDBACK_ACTIONS.get(action_id)
        if feedback_type:
            asyncio.create_task(
                _record_admin_brain_feedback(
                    settings=settings,
                    user_id=str(user_id or ""),
                    team_id=team_id,
                    channel_id=str(channel_id or ""),
                    thread_ts=thread_ts,
                    feedback=parse_feedback_value(action.get("value")),
                    feedback_type=feedback_type,
                )
            )
            return JSONResponse(status_code=200, content={})
        print(
            f"🔒 Admin Roo ignored unsupported interactive action: {action_id}"
        )
        return JSONResponse(status_code=200, content={})

    unified_admin = bool(
        settings.ROO_SURFACE == "public"
        and settings.ROO_UNIFIED_ADMIN_ROUTING_ENABLED
    )
    if unified_admin and payload_type == "view_submission" and correction_submission:
        team_id = str(payload.get("team", {}).get("id") or "")
        if (
            correction_submission.get("requester_user_id") != user_id
            or correction_submission.get("team_id") != team_id
            or not correction_submission.get("query_id")
            or not correction_submission.get("claim_id")
            or not correction_submission.get("correction_text")
        ):
            return JSONResponse(
                status_code=200,
                content={
                    "response_action": "errors",
                    "errors": {
                        "admin_brain_correction": (
                            "This correction could not be verified. "
                            "Reopen it from the original answer."
                        )
                    },
                },
            )
        asyncio.create_task(
            _record_unified_admin_brain_feedback(
                settings=settings,
                user_id=str(user_id or ""),
                team_id=team_id,
                channel_id=str(channel_id or ""),
                thread_ts=correction_submission.get("thread_ts") or "",
                feedback=correction_submission,
                feedback_type="incorrect",
                correction_text=correction_submission.get("correction_text"),
            )
        )
        return JSONResponse(status_code=200, content={"response_action": "clear"})

    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(status_code=200, content={})
        
    action_id = actions[0].get("action_id")
    # Interactive messages structure is slightly different for TS
    message = payload.get("message", {})
    thread_ts = message.get("thread_ts") or message.get("ts")
    
    print(f"🖱️ Action: {action_id} from {user_id}")

    if unified_admin and action_id == ADMIN_BRAIN_INCORRECT_ACTION:
        _open_admin_brain_correction_modal(
            payload=payload,
            action=actions[0],
            user_id=str(user_id or ""),
            team_id=str(payload.get("team", {}).get("id") or ""),
            channel_id=str(channel_id or ""),
            thread_ts=str(thread_ts or ""),
        )
        return JSONResponse(status_code=200, content={})
    unified_feedback_type = (
        ADMIN_BRAIN_FEEDBACK_ACTIONS.get(str(action_id or ""))
        if unified_admin
        else None
    )
    if unified_feedback_type:
        asyncio.create_task(
            _record_unified_admin_brain_feedback(
                settings=settings,
                user_id=str(user_id or ""),
                team_id=str(payload.get("team", {}).get("id") or ""),
                channel_id=str(channel_id or ""),
                thread_ts=str(thread_ts or ""),
                feedback=parse_feedback_value(actions[0].get("value")),
                feedback_type=unified_feedback_type,
            )
        )
        return JSONResponse(status_code=200, content={})

    if action_id in {"linear_meeting_approve", "linear_meeting_reject"}:
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        pending_id = str(value_data.get("pending_id") or "").strip()
        requested_by = str(value_data.get("requested_by") or "").strip()
        reply_channel = channel_id
        reply_thread_ts = thread_ts

        from .skills.executor import (
            get_pending_linear_meeting_action,
            pop_pending_linear_meeting_action,
        )

        pending = get_pending_linear_meeting_action(pending_id)
        if not pending:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text="That Linear meeting action is no longer available. Re-run the meeting notes request if you still need it.",
                )
            return JSONResponse(status_code=200, content={})

        pending_requested_by = str(pending.get("requested_by") or requested_by).strip()
        if pending_requested_by and user_id != pending_requested_by:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text="Only the person who requested this Linear meeting action can approve or reject it.",
                )
            return JSONResponse(status_code=200, content={})

        display = pending.get("display") or {}
        title = display.get("title") or "that action item"

        if action_id == "linear_meeting_reject":
            pop_pending_linear_meeting_action(pending_id)
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"Skipped Linear issue creation for: {title}",
                )
            return JSONResponse(status_code=200, content={})

        skill = get_agent()._get_skill_by_name("linear-meeting-actions")
        ClientClass = skill.get_client_class("LinearMeetingActionsClient") if skill else None
        if ClientClass is None:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text="Roo could not load the Linear meeting actions client for this approval.",
                )
            return JSONResponse(status_code=200, content={})

        try:
            issue = await ClientClass().create_issue(**pending["issue_input"])
            pop_pending_linear_meeting_action(pending_id)
        except Exception as exc:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"I couldn't create that Linear issue yet: {exc.__class__.__name__}: {exc}",
                )
            return JSONResponse(status_code=200, content={})

        issue_label = issue.get("identifier") or issue.get("title") or title
        if issue.get("url"):
            created_text = f"Created <{issue['url']}|{issue_label}> from: {title}"
        else:
            created_text = f"Created {issue_label} from: {title}"
        if display.get("effort_label"):
            created_text += (
                f"\nEffort: {display['effort_label']} — "
                f"{display.get('effort_rationale', '')}"
            )
        if reply_channel:
            post_message(channel=reply_channel, thread_ts=reply_thread_ts, text=created_text)
        return JSONResponse(status_code=200, content={})
    
    if action_id == "resume_scan":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this run can resume it.",
        )
        if action_error is not None:
            return action_error

        print(f"🔄 Resuming scan/writing for {user_id} via button click")

        agent = get_agent()
        asyncio.create_task(
            agent.handle_mention(
                text="scan repo",
                user_id=user_id,
                channel_id=action_context.reply_channel or channel_id,
                thread_ts=action_context.reply_thread_ts or thread_ts,
            )
        )

        return JSONResponse(status_code=200, content={})

    if action_id == "confirm_content_factory":
        print(f"✅ User {user_id} confirmed content factory requirements")

        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        original_text = value_data.get("text")
        original_channel_id = value_data.get("channel_id") or channel_id
        original_thread_ts = value_data.get("thread_ts") or thread_ts
        param_overrides = value_data.get("params") or {}
        if not isinstance(param_overrides, dict):
            param_overrides = {}

        if not original_text:
            if channel_id:
                post_message(
                    channel_id,
                    "I couldn't recover your original content request. Please resend the article request so I can continue.",
                    thread_ts=thread_ts,
                )
            return JSONResponse(status_code=200, content={})

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can continue it.",
        )
        if action_error is not None:
            return action_error

        resumed_params = {**param_overrides, "confirmed": True}

        asyncio.create_task(_handle_mention(
            {
                "user": user_id,
                "text": original_text,
                "channel": original_channel_id or action_context.reply_channel,
                "thread_ts": original_thread_ts or action_context.reply_thread_ts,
                "param_overrides": resumed_params,
            }
        ))
        return JSONResponse(status_code=200, content={})

    if action_id == "publish_content_pr":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        job_id = str(value_data.get("job_id") or "").strip()
        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can publish it as a PR.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel or channel_id
        reply_thread_ts = action_context.reply_thread_ts

        if not job_id:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text="⚠️ I couldn't determine which completed article bundle to publish from this button. Please use the original content-ready thread."
                )
            return JSONResponse(status_code=200, content={})

        _remember_content_thread_context(
            reply_channel,
            reply_thread_ts,
            domain,
            "publish_pr",
            active_job_id=job_id,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = []
            for block in original_blocks:
                if block.get("block_id") == "content_ready_publish_actions":
                    continue
                if block.get("type") == "actions":
                    elements = [
                        element for element in block.get("elements", [])
                        if element.get("action_id") != "publish_content_pr"
                    ]
                    if not elements:
                        continue
                    updated_blocks.append({**block, "elements": elements})
                    continue
                updated_blocks.append(block)
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "⏳ Publishing this article as a draft PR..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update publish-content-pr message: {e}")

        asyncio.create_task(_handle_mention(
            {
                "user": user_id,
                "text": "publish this article as a PR",
                "channel": reply_channel,
                "thread_ts": reply_thread_ts,
                "param_overrides": (
                    {
                        "action": "publish_pr",
                        "job_id": job_id,
                        "domain": domain,
                        "requested_by_slack_user_id": requested_by_slack_user_id or user_id,
                        "effective_slack_user_id": effective_slack_user_id or user_id,
                    }
                    if _is_delegated_content_factory_request(
                        requested_by_slack_user_id,
                        effective_slack_user_id,
                    )
                    else {
                        "action": "publish_pr",
                        "job_id": job_id,
                        "domain": domain,
                    }
                ),
            }
        ))
        return JSONResponse(status_code=200, content={})

    # Handler for confirm_topic (new format)
    # Value format: "confirm:{keyword}:{job_id}"
    if action_id == "confirm_topic":
        value = actions[0].get("value", "")
        value_data: dict[str, Any] = {}
        keyword: Optional[str] = None
        job_id: Optional[str] = None
        domain: Optional[str] = None
        try:
            parsed_value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            parsed_value = None
        if isinstance(parsed_value, dict):
            value_data = parsed_value
            keyword = str(value_data.get("keyword") or "").strip() or None
            job_id = str(value_data.get("job_id") or "").strip() or None
            domain = str(value_data.get("domain") or "").strip() or None
        elif value and value.startswith("confirm:"):
            parts = value.split(":", 2)
            if len(parts) >= 3:
                keyword = parts[1]
                job_id = parts[2]

        if not keyword or not job_id:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can confirm the topic.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id

        print(f"✅ User {user_id} confirmed topic '{keyword}' for job {job_id}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            confirm_kwargs = {
                "job_id": job_id,
                "slack_user_id": effective_slack_user_id or user_id,
                "confirmed_keyword": keyword,
                "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            }
            if domain:
                confirm_kwargs["domain"] = domain
            result = await client.confirm_article_topic(**confirm_kwargs)
            follow_up = _build_confirm_topic_follow_up(result)
            return _confirm_topic_json_response(
                keyword,
                follow_up,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            import traceback
            traceback.print_exc()
            recovered_follow_up = await _resolve_confirm_follow_up_after_failure(
                client,
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=action_context.reply_channel,
                slack_thread_ts=action_context.reply_thread_ts,
                domain=domain,
                job_id=job_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
            )
            if recovered_follow_up:
                return _confirm_topic_json_response(
                    keyword,
                    recovered_follow_up,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
            return _content_factory_action_denied_response(f"❌ Error confirming topic: {e}")

    # Handler for select_alternative (dropdown selection)
    # Value format: "confirm:{keyword}:{job_id}"
    if action_id == "select_alternative":
        selected_option = actions[0].get("selected_option", {})
        value = selected_option.get("value", "")
        value_data: dict[str, Any] = {}
        keyword: Optional[str] = None
        job_id: Optional[str] = None
        domain: Optional[str] = None
        try:
            parsed_value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            parsed_value = None
        if isinstance(parsed_value, dict):
            value_data = parsed_value
            keyword = str(value_data.get("keyword") or "").strip() or None
            job_id = str(value_data.get("job_id") or "").strip() or None
            domain = str(value_data.get("domain") or "").strip() or None
        elif value and value.startswith("confirm:"):
            parts = value.split(":", 2)
            if len(parts) >= 3:
                keyword = parts[1]
                job_id = parts[2]

        if not keyword or not job_id:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can confirm the topic.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id

        print(f"✅ User {user_id} selected alternative topic '{keyword}' for job {job_id}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            confirm_kwargs = {
                "job_id": job_id,
                "slack_user_id": effective_slack_user_id or user_id,
                "confirmed_keyword": keyword,
                "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            }
            if domain:
                confirm_kwargs["domain"] = domain
            result = await client.confirm_article_topic(**confirm_kwargs)
            follow_up = _build_confirm_topic_follow_up(result)
            return _confirm_topic_json_response(
                keyword,
                follow_up,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )
        except Exception as e:
            print(f"❌ Failed to confirm alternative topic: {e}")
            import traceback
            traceback.print_exc()
            recovered_follow_up = await _resolve_confirm_follow_up_after_failure(
                client,
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=action_context.reply_channel,
                slack_thread_ts=action_context.reply_thread_ts,
                domain=domain,
                job_id=job_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
            )
            if recovered_follow_up:
                return _confirm_topic_json_response(
                    keyword,
                    recovered_follow_up,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
            return _content_factory_action_denied_response(f"❌ Error confirming topic: {e}")

    # Handler for cancel_topic (new format)
    # Value format: "cancel:{job_id}"
    if action_id == "cancel_topic":
        value = actions[0].get("value", "")
        value_data: dict[str, Any] = {}
        job_id: Optional[str] = None
        domain: Optional[str] = None
        try:
            parsed_value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            parsed_value = None
        if isinstance(parsed_value, dict):
            value_data = parsed_value
            job_id = str(value_data.get("job_id") or "").strip() or None
            domain = str(value_data.get("domain") or "").strip() or None
        else:
            job_id = value.split(":", 1)[1] if ":" in value else None

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can cancel topic selection.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id

        print(f"❌ User {user_id} cancelled topic selection for job {job_id}")

        # Optionally notify backend to clean up the job
        if job_id:
            try:
                from .clients.mlai_backend import MLAIBackendClient
                settings = get_settings()
                client = MLAIBackendClient(
                    base_url=settings.MLAI_BACKEND_URL,
                    api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
                )
                await client.cancel_job(job_id, effective_slack_user_id or user_id)
            except Exception as e:
                print(f"⚠️ Failed to notify backend of cancellation: {e}")

        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": "❌ Job cancelled.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "❌ Job cancelled."
                    }
                }
            ]
        })

    # Handler for scaffold_confirm
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "scaffold_confirm":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        delivery_mode = value_data.get("delivery_mode")
        scan_run_id = str(value_data.get("scan_run_id") or "").strip()

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who initiated the scan can confirm this action.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        print(f"📁 User {user_id} confirmed scaffold for {domain}")

        if not scan_run_id:
            try:
                from .slack_client import get_slack_client
                slack_client = get_slack_client()
                original_blocks = payload.get("message", {}).get("blocks", [])
                updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                updated_blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "⚠️ This scaffold button is from an older scan. Run a fresh scan and try again."}]
                })
                slack_client.chat_update(
                    channel=msg_channel,
                    ts=msg_ts,
                    text=payload.get("message", {}).get("text", ""),
                    blocks=updated_blocks
                )
            except Exception as e:
                print(f"⚠️ Failed to update stale scaffold message: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"⚠️ I need a fresh scan result before I can create the articles directory for *{domain}*. Please run the scan again."
            )
            return JSONResponse(status_code=200, content={})

        # 1. Remove buttons and show status via context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Creating articles directory..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # 2. Call mlai-backend scaffold API
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.decide_scaffold(
                scan_run_id=scan_run_id,
                decision="approve",
                domain=domain,
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )

            status_code = result.get("status_code")
            data = result.get("data", {})

            if scan_run_id and status_code in {200, 202}:
                scaffold_job_id = data.get("scaffold_job_id")
                pending = _get_pending_intent(
                    requested_by_slack_user_id,
                    domain,
                    wait_for="scan_complete",
                    consume=True,
                    effective_slack_user_id=effective_slack_user_id,
                )
                if pending and pending.get("action") == "write" and scaffold_job_id:
                    _remember_pending_intent(
                        requested_by_slack_user_id,
                        domain,
                        effective_slack_user_id=effective_slack_user_id,
                        intent_data=pending,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scaffold_complete",
                        job_id=scaffold_job_id,
                        clear_job_id=True,
                    )
                print(f"✅ Scaffold initiated for {domain}")
            elif status_code == 200:
                # Already scaffolded
                pr_url = data.get("pr_url", "")
                preview_url = data.get("preview_url", "")
                details = []
                if pr_url:
                    details.append(f"PR: {pr_url}")
                if preview_url:
                    details.append(f"Preview: {preview_url}")
                detail_text = f" {' | '.join(details)}" if details else ""
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                )
            elif status_code == 400:
                error = data.get("error", "Unknown error")
                if data.get("needs_github_auth"):
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=(
                            _delegated_content_factory_auth_error_text(
                                effective_slack_user_id=effective_slack_user_id,
                                domain=domain,
                            )
                            if _is_delegated_content_factory_request(
                                requested_by_slack_user_id,
                                effective_slack_user_id,
                            )
                            else f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {data.get('oauth_url', '')}"
                        ),
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Could not start scaffolding: {error}"
                    )
            elif status_code == 404:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ No configuration found for *{domain}*."
                    )
            elif status_code == 202:
                print(f"✅ Scaffold initiated for {domain}")
            elif status_code == 409:
                error = data.get("detail") or data.get("error") or "This scaffold request is no longer awaiting approval."
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"⚠️ Could not create articles directory for *{domain}*: {error}"
                )
            else:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Unexpected response from backend (status {status_code})"
                )

        except Exception as e:
            print(f"❌ Failed to trigger scaffold: {e}")
            import traceback
            traceback.print_exc()
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error creating articles directory: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for scaffold_skip
    # Value format: JSON string with domain
    if action_id == "scaffold_skip":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain", "your site")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        scan_run_id = str(value_data.get("scan_run_id") or "").strip()

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who initiated the scan can confirm this action.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        print(f"⏭️ User {user_id} skipped scaffold for {domain}")

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Skipped. You can scaffold later with `@Roo scaffold {domain}`"}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        if scan_run_id:
            from .clients.mlai_backend import MLAIBackendClient
            settings = get_settings()
            backend_client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
            )
            try:
                result = await backend_client.decide_scaffold(
                    scan_run_id=scan_run_id,
                    decision="deny",
                    domain=domain,
                    slack_user_id=effective_slack_user_id or user_id,
                    slack_channel_id=reply_channel,
                    slack_thread_ts=reply_thread_ts,
                    **_content_factory_delegated_backend_kwargs(
                        requested_by_slack_user_id,
                        effective_slack_user_id,
                    ),
                )
                if result.get("status_code") not in {200, 202, 409}:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"⚠️ Could not record scaffold skip for *{domain}*."
                    )
                _get_pending_intent(
                    requested_by_slack_user_id,
                    domain,
                    wait_for="scan_complete",
                    consume=True,
                    effective_slack_user_id=effective_slack_user_id,
                )
            except Exception as e:
                print(f"⚠️ Failed to deny scaffold approval: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for write_first_article
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "write_first_article":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        client_request_id = _content_factory_client_request_id(value_data.get("client_request_id"))
        delivery_mode = value_data.get("delivery_mode")
        delivery_mode_confirmed = bool(value_data.get("delivery_mode_confirmed")) if delivery_mode is not None else None

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who initiated the scan can confirm this action.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        print(f"✍️ User {user_id} requested first article for {domain}")

        # 1. Remove buttons and show status via context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            updated_blocks = build_live_status_blocks(
                domain,
                summary_text="Starting article generation. I'll keep this message updated.",
                include_decision_stage=False,
                current_stage="preparing",
            )
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"Starting Content Factory for {domain}",
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # 2. Trigger article generation (no topic = auto-research)
        from .clients.mlai_backend import MLAIBackendClient
        from .slack_client import get_user_info
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            slack_info = get_user_info(requested_by_slack_user_id or user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            result = await backend_client.trigger_article_generation(
                slack_user_id=effective_slack_user_id or user_id,
                domain=domain,
                delivery_mode=delivery_mode,
                delivery_mode_confirmed=delivery_mode_confirmed,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                progress_message_ts=msg_ts,
                client_request_id=client_request_id,
                request_source="roo_slackbot",
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
            print(f"✅ Article generation triggered for {domain}: {result}")
            if result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "write",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        except Exception as e:
            print(f"❌ Failed to trigger article generation: {e}")
            import traceback
            traceback.print_exc()
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting article generation: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for write_article_skip
    # Value format: JSON string with domain
    if action_id in ("write_article_skip", "write_skip"):
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain", "your site")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this scan can skip the first article prompt.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts

        print(f"⏭️ User {user_id} skipped first article for {domain}")

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Skipped. Say `@Roo write me an article about [topic]` anytime."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for article_research_best
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "article_research_best":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        client_request_id = _content_factory_client_request_id(value_data.get("client_request_id"))
        delivery_mode = value_data.get("delivery_mode")
        delivery_mode_confirmed = bool(value_data.get("delivery_mode_confirmed")) if delivery_mode is not None else None

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who initiated this request can choose the article direction.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        print(f"🔍 User {user_id} chose article discovery for {domain}")
        _remember_content_thread_context(
            reply_channel,
            reply_thread_ts,
            domain,
            "research",
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            updated_blocks = build_live_status_blocks(
                domain,
                summary_text="Starting discovery to find the best article opportunity. I'll keep this message updated.",
                include_decision_stage=True,
                current_stage="preparing",
            )
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"Starting Content Factory for {domain}",
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        from .clients.mlai_backend import MLAIBackendClient
        from .slack_client import get_user_info

        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )
        try:
            slack_info = get_user_info(requested_by_slack_user_id or user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            result = await backend_client.trigger_article_generation(
                slack_user_id=effective_slack_user_id or user_id,
                domain=domain,
                delivery_mode=delivery_mode,
                delivery_mode_confirmed=delivery_mode_confirmed,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                progress_message_ts=msg_ts,
                client_request_id=client_request_id,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
            print(f"✅ Article discovery triggered for {domain}: {result}")
            if str(result.get("status") or "").strip().lower() == "awaiting_delivery_mode":
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "awaiting_delivery_mode",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
                try:
                    from .slack_client import get_slack_client
                    get_slack_client().chat_update(
                        channel=msg_channel,
                        ts=msg_ts,
                        text=f"Choose delivery mode for {domain}",
                        blocks=_build_article_delivery_mode_blocks(
                            domain=domain,
                            job_id=result.get("job_id") or result.get("run_id"),
                            recommended_delivery_mode=result.get("recommended_delivery_mode"),
                            requested_by_slack_user_id=requested_by_slack_user_id,
                            effective_slack_user_id=effective_slack_user_id,
                        ),
                    )
                except Exception as update_error:
                    print(f"⚠️ Failed to update delivery-mode prompt: {update_error}")
            elif result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "research",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        except Exception as e:
            print(f"❌ Failed to trigger article discovery: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting article discovery: {e}"
            )
        return JSONResponse(status_code=200, content={})

    # Handler for article_provide_topic
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "article_provide_topic":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        delivery_mode = value_data.get("delivery_mode")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who initiated this request can choose the article direction.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        is_dm = bool(reply_channel and reply_channel.startswith("D"))
        normalized_domain = normalize_content_factory_domain(domain) or domain or "this domain"
        article_cost_points = get_content_factory_article_cost_points(domain)
        cost_guidance = (
            f"Articles for {normalized_domain} are free."
            if article_cost_points == 0
            else f"Starting the article run deducts {article_cost_points} Roo points."
        )
        guidance_text = (
            "✅ Reply here with the topic you want me to write about. "
            "I'll still research the best keywords, title, and talking points so it has the best chance to rank. "
            f"{cost_guidance}"
            if is_dm else
            "✅ Reply in this thread with something like `@Roo write about AI for clinic workflows`. "
            "I'll still research the best keywords, title, and talking points so it has the best chance to rank. "
            f"{cost_guidance}"
        )
        if delivery_mode == "content_only":
            guidance_text += " I'll keep this in content-only mode unless you tell me otherwise."
        elif delivery_mode == "publish_code":
            guidance_text += " I'll keep this in publish-via-code mode unless you tell me otherwise."

        print(f"📝 User {user_id} will provide the article topic for {domain}")
        _remember_content_thread_context(
            reply_channel,
            reply_thread_ts,
            domain,
            "write",
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": guidance_text}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    if action_id == "select_article_delivery_mode":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        job_id = value_data.get("job_id")
        domain = value_data.get("domain")
        delivery_mode = value_data.get("delivery_mode")
        if not job_id or delivery_mode not in {"content_only", "publish_code"}:
            return JSONResponse(status_code=400, content={"error": "job_id and delivery_mode are required"})

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can choose the delivery mode.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id

        from .clients.mlai_backend import MLAIBackendClient

        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            result = await client.set_article_delivery_mode(
                job_id,
                delivery_mode,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
            if result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    action_context.reply_channel,
                    action_context.reply_thread_ts,
                    domain,
                    "awaiting_delivery_mode",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))

            selected_label = "content-only" if delivery_mode == "content_only" else "publish-via-code"
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": True,
                "text": f"⏳ {selected_label} selected. Roo is continuing the article run now.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⏳ *{selected_label}* selected for *{domain or 'this domain'}*.\nRoo is continuing the article run now.",
                        },
                    }
                ],
            })
        except httpx.HTTPStatusError as exc:
            error_data = {}
            try:
                error_data = exc.response.json()
            except Exception:
                error_data = {"error": str(exc)}

            if exc.response.status_code == 412 and error_data.get("error_code") == "PUBLISH_TARGET_ACTION_REQUIRED":
                if _is_delegated_content_factory_request(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ):
                    return JSONResponse(status_code=200, content={
                        "response_type": "ephemeral",
                        "replace_original": True,
                        "text": _delegated_content_factory_auth_error_text(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        ),
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": _delegated_content_factory_auth_error_text(
                                        effective_slack_user_id=effective_slack_user_id,
                                        domain=domain,
                                    ),
                                },
                            }
                        ],
                    })

                auth_url = None
                if domain:
                    auth_response = await client.get_github_auth_url(
                        effective_slack_user_id or user_id,
                        domain=domain,
                    )
                    auth_url = auth_response.get("auth_url")

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Publish mode isn't ready for *{domain or 'this domain'}* yet.\n\n"
                                "I need a connected GitHub repo before I can open the PR flow. "
                                "You can connect GitHub now or switch back to content-only."
                            ),
                        },
                    },
                ]
                if auth_url:
                    blocks.append(
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": f"Connect GitHub for {domain or 'this domain'}", "emoji": True},
                                    "url": auth_url,
                                    "action_id": "connect_github",
                                    "style": "primary",
                                }
                            ],
                        }
                    )
                return JSONResponse(status_code=200, content={
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "text": error_data.get("error") or error_data.get("message") or "Publish mode needs GitHub first.",
                    "blocks": blocks,
                })

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error selecting delivery mode: {error_data.get('error', str(exc))}",
            })

    # Handler for article_system_* decisions
    if action_id in {"article_system_use_detected", "article_system_rescan", "article_system_scaffold"}:
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can choose the article-system action.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        decision = {
            "article_system_use_detected": "use_detected",
            "article_system_rescan": "rescan",
            "article_system_scaffold": "scaffold",
        }[action_id]
        pending_wait_for = {
            "rescan": "scan_complete",
            "scaffold": "scaffold_complete",
        }.get(decision)

        progress_text = {
            "use_detected": "✅ Using detected article structure...",
            "rescan": "✅ Re-scanning repository...",
            "scaffold": "✅ Creating articles directory...",
        }[decision]

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": progress_text}],
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks,
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            result = await backend_client.decide_article_system(
                domain=domain,
                slack_user_id=effective_slack_user_id or user_id,
                decision=decision,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
            status_code = result.get("status_code")
            data = result.get("data", {})

            if status_code in {200, 202}:
                if original_intent and pending_wait_for:
                    pending = _remember_pending_intent(
                        requested_by_slack_user_id,
                        domain,
                        effective_slack_user_id=effective_slack_user_id,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for=pending_wait_for,
                        job_id=_extract_job_id(data),
                        clear_job_id=True,
                    )
                    if pending:
                        print(
                            "   📌 Stored pending intent: "
                            f"{original_intent.get('action')} for "
                            f"{requested_by_slack_user_id}:{effective_slack_user_id}:{domain}"
                        )
                elif decision == "use_detected":
                    _get_pending_intent(
                        requested_by_slack_user_id,
                        domain,
                        consume=True,
                        effective_slack_user_id=effective_slack_user_id,
                    )

                if decision == "use_detected":
                    detected = (
                        (data.get("article_system") or {}).get("directory_path")
                        or (data.get("article_system") or {}).get("directory_name")
                        or "the detected article directory"
                    )
                    if data.get("resume_triggered"):
                        post_message(
                            channel=reply_channel,
                            thread_ts=reply_thread_ts,
                            text=(
                                f"✅ Using the detected article system at `{detected}`.\n"
                                f"I've resumed your pending article request."
                            ),
                        )
                    else:
                        post_message(
                            channel=reply_channel,
                            thread_ts=reply_thread_ts,
                            text=f"✅ Using the detected article system at `{detected}`.",
                        )
                elif decision == "rescan":
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"🔄 Re-scanning *{domain}* now. I'll reply here when it's complete.",
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"📁 Setting up the articles directory for *{domain}* now.",
                    )
            else:
                error_msg = data.get("error", f"Unexpected backend response ({status_code})")
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Could not process that article-system decision: {error_msg}",
                )
        except Exception as e:
            print(f"❌ Failed to process article-system decision: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error processing article-system decision: {e}",
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_scan
    # User clicked "Scan Codebase" from a prerequisite message
    if action_id == "prerequisite_scan":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        print(f"🔍 User {user_id} triggered prerequisite scan for {domain}")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this run can start the prerequisite scan.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        # Remove buttons and show status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Scanning codebase..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # Trigger scan via backend
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.trigger_repo_scan(
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                domain=domain,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
            if result.get("error"):
                error_msg = result.get("message", "Unknown error")
                # Check if auth-related error — show reconnect button
                if result.get("needs_github_auth"):
                    if _is_delegated_content_factory_request(
                        requested_by_slack_user_id,
                        effective_slack_user_id,
                    ):
                        post_message(
                            channel=reply_channel,
                            thread_ts=reply_thread_ts,
                            text=_delegated_content_factory_auth_error_text(
                                effective_slack_user_id=effective_slack_user_id,
                                domain=domain,
                            ),
                        )
                        return JSONResponse(status_code=200, content={})
                    oauth_url = result.get("oauth_url")
                    if not oauth_url:
                        try:
                            auth_resp = await backend_client.get_github_auth_url(
                                effective_slack_user_id or user_id,
                                domain=domain,
                            )
                            oauth_url = auth_resp.get("auth_url")
                        except Exception:
                            oauth_url = None
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"❌ Scan failed: {error_msg}"
                            }
                        }
                    ]
                    if oauth_url:
                        blocks.append({
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Re-connect GitHub",
                                        "emoji": True
                                    },
                                    "url": oauth_url,
                                    "action_id": "connect_github",
                                    "style": "danger"
                                },
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "I've Connected - Resume",
                                        "emoji": True
                                    },
                                    "action_id": "resume_scan",
                                    "value": json.dumps(
                                        _content_factory_identity_payload(
                                            requested_by_slack_user_id=requested_by_slack_user_id,
                                            effective_slack_user_id=effective_slack_user_id,
                                            domain=domain,
                                            channel_id=reply_channel,
                                            thread_ts=reply_thread_ts,
                                        )
                                    ),
                                    "style": "primary"
                                }
                            ]
                        })
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scan failed: {error_msg}",
                        blocks=blocks
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scan failed: {error_msg}"
                    )
            else:
                if original_intent:
                    pending = _remember_pending_intent(
                        requested_by_slack_user_id,
                        domain,
                        effective_slack_user_id=effective_slack_user_id,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scan_complete",
                        job_id=_extract_job_id(result),
                        clear_job_id=True,
                    )
                    if pending:
                        print(
                            "   📌 Stored pending intent: "
                            f"{original_intent.get('action')} for "
                            f"{requested_by_slack_user_id}:{effective_slack_user_id}:{domain}"
                        )
                print(f"✅ Scan triggered for {domain}")
        except Exception as e:
            print(f"❌ Failed to trigger scan: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting scan: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_scaffold
    # User clicked "Set Up Articles Directory" from a prerequisite message
    if action_id == "prerequisite_scaffold":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        print(f"📁 User {user_id} triggered prerequisite scaffold for {domain}")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this run can create the articles directory.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts
        reply_channel = action_context.reply_channel
        reply_thread_ts = action_context.reply_thread_ts

        # Remove buttons and show status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Creating articles directory..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # Trigger scaffold via backend
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.scaffold_articles(
                domain=domain,
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )

            status_code = result.get("status_code")
            data = result.get("data", {})

            if status_code == 200:
                pr_url = data.get("pr_url", "")
                if original_intent and original_intent.get("action") == "write":
                    await _trigger_article_generation_from_pending(
                        {
                            **original_intent,
                            "channel_id": reply_channel,
                            "thread_ts": reply_thread_ts,
                        },
                        requested_by_slack_user_id=requested_by_slack_user_id or user_id,
                        effective_slack_user_id=effective_slack_user_id or user_id,
                        domain=domain,
                        fallback_channel_id=reply_channel,
                        fallback_thread_ts=reply_thread_ts,
                    )
                else:
                    preview_url = data.get("preview_url", "")
                    details = []
                    if pr_url:
                        details.append(f"PR: {pr_url}")
                    if preview_url:
                        details.append(f"Preview: {preview_url}")
                    detail_text = f" {' | '.join(details)}" if details else ""
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                    )
            elif status_code == 400:
                error = data.get("error", "Unknown error")
                if data.get("needs_github_auth"):
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=(
                            _delegated_content_factory_auth_error_text(
                                effective_slack_user_id=effective_slack_user_id,
                                domain=domain,
                            )
                            if _is_delegated_content_factory_request(
                                requested_by_slack_user_id,
                                effective_slack_user_id,
                            )
                            else f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {data.get('oauth_url', '')}"
                        ),
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scaffolding failed: {error}"
                    )
            elif status_code == 202:
                if original_intent:
                    pending = _remember_pending_intent(
                        requested_by_slack_user_id,
                        domain,
                        effective_slack_user_id=effective_slack_user_id,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scaffold_complete",
                        job_id=_extract_job_id(result),
                        clear_job_id=True,
                    )
                    if pending:
                        print(
                            "   📌 Stored pending intent: "
                            f"{original_intent.get('action')} for "
                            f"{requested_by_slack_user_id}:{effective_slack_user_id}:{domain}"
                        )
                print(f"✅ Scaffold initiated for {domain}")
            else:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Unexpected response (status {status_code})"
                )

        except Exception as e:
            print(f"❌ Failed to trigger scaffold: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error creating articles directory: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_cancel
    if action_id == "prerequisite_cancel":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            value_data = {}

        domain = value_data.get("domain", "")
        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=value_data,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this run can cancel it.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts

        print(f"⏭️ User {user_id} cancelled prerequisite for {domain}")

        # Clear any pending intent
        _get_pending_intent(
            requested_by_slack_user_id,
            domain,
            consume=True,
            effective_slack_user_id=effective_slack_user_id,
        )

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Cancelled."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for confirm_topic_btn_* (backend sends these during topic selection)
    # Value format: "confirm_topic:{job_id}:{option_index}"
    if action_id.startswith("confirm_topic_btn"):
        value = actions[0].get("value", "")
        if not value or not value.startswith("confirm_topic:"):
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        parts = value.split(":")
        if len(parts) < 2:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        job_id = parts[1]

        try:
            option_index = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            option_index = 0

        print(f"✅ User {user_id} confirmed topic for job {job_id}, option {option_index}")

        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=None,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can confirm the topic.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        effective_slack_user_id = action_context.effective_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts

        # Call confirm endpoint
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=effective_slack_user_id or user_id,
                option_index=option_index,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                **_content_factory_delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
            print(f"✅ Topic confirmed for job {job_id}")
            follow_up = _build_confirm_topic_follow_up(result)
            _remember_content_thread_context(
                action_context.reply_channel,
                action_context.reply_thread_ts,
                follow_up.get("domain"),
                "awaiting_delivery_mode" if follow_up.get("requires_delivery_mode") else "write",
                active_job_id=follow_up.get("active_job_id"),
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

            try:
                from .slack_client import get_slack_client

                slack_client = get_slack_client()
                if follow_up.get("requires_delivery_mode"):
                    updated_blocks = _build_article_delivery_mode_blocks(
                        domain=follow_up["domain"],
                        job_id=follow_up["active_job_id"],
                        recommended_delivery_mode=follow_up.get("recommended_delivery_mode"),
                        requested_by_slack_user_id=requested_by_slack_user_id,
                        effective_slack_user_id=effective_slack_user_id,
                    )
                    updated_text = f"Choose delivery mode for {follow_up['domain']}"
                else:
                    original_blocks = payload.get("message", {}).get("blocks", [])
                    updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                    updated_blocks.append({
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": _build_confirm_follow_up_message(follow_up)}]
                    })
                    updated_text = payload.get("message", {}).get("text", "")
                    _maybe_schedule_content_factory_watchdog(
                        follow_up.get("active_job_id"),
                        str(follow_up.get("status") or ""),
                    )

                slack_client.chat_update(
                    channel=msg_channel,
                    ts=msg_ts,
                    text=updated_text,
                    blocks=updated_blocks
                )
            except Exception as e:
                print(f"⚠️ Failed to update message: {e}")
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            import traceback
            traceback.print_exc()
            reply_channel = action_context.reply_channel
            reply_thread_ts = action_context.reply_thread_ts
            recovered_follow_up = await _resolve_confirm_follow_up_after_failure(
                client,
                slack_user_id=effective_slack_user_id or user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                domain=None,
                job_id=job_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
            )
            if recovered_follow_up:
                try:
                    from .slack_client import get_slack_client

                    slack_client = get_slack_client()
                    if recovered_follow_up.get("requires_delivery_mode"):
                        updated_blocks = _build_article_delivery_mode_blocks(
                            domain=recovered_follow_up["domain"],
                            job_id=recovered_follow_up["active_job_id"],
                            recommended_delivery_mode=recovered_follow_up.get("recommended_delivery_mode"),
                            requested_by_slack_user_id=requested_by_slack_user_id,
                            effective_slack_user_id=effective_slack_user_id,
                        )
                        updated_text = f"Choose delivery mode for {recovered_follow_up['domain']}"
                    else:
                        original_blocks = payload.get("message", {}).get("blocks", [])
                        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                        updated_blocks.append({
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": _build_confirm_follow_up_message(recovered_follow_up)}],
                        })
                        updated_text = payload.get("message", {}).get("text", "")
                        _maybe_schedule_content_factory_watchdog(
                            recovered_follow_up.get("active_job_id"),
                            str(recovered_follow_up.get("status") or ""),
                        )

                    slack_client.chat_update(
                        channel=msg_channel,
                        ts=msg_ts,
                        text=updated_text,
                        blocks=updated_blocks,
                    )
                except Exception as recovery_exc:
                    print(f"⚠️ Failed to update recovered confirm message: {recovery_exc}")
                return JSONResponse(status_code=200, content={})
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Error confirming topic: {e}"
                )

        return JSONResponse(status_code=200, content={})

    # Handler for cancel_topic_btn
    if action_id == "cancel_topic_btn":
        action_context, action_error = _resolve_content_factory_action_context(
            payload=payload,
            value_data=None,
            acting_slack_user_id=user_id,
            denial_text="⚠️ Only the user who requested this article can cancel topic selection.",
        )
        if action_error is not None:
            return action_error

        requested_by_slack_user_id = action_context.requested_by_slack_user_id
        msg_channel = action_context.msg_channel
        msg_ts = action_context.msg_ts

        print(f"❌ User {user_id} cancelled topic selection")

        # Remove buttons and show cancelled status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "❌ Cancelled."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})



    return JSONResponse(status_code=200, content={})
