from __future__ import annotations

"""
Skill Executor

Executes skill actions based on the skill definition.
Follows Anthropic's Agent Skills pattern for execution.
"""
import base64
import hashlib
import json
import re
import asyncio
import calendar
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, List
from difflib import SequenceMatcher
from uuid import uuid4
import httpx

from .loader import Skill
from ..content_factory_progress import (
    CONTENT_FACTORY_ARTICLE_COST_POINTS,
    CONTENT_FACTORY_REQUEST_SOURCE,
    build_live_status_blocks,
    get_content_factory_article_cost_points,
    is_free_content_factory_domain,
    normalize_content_factory_domain,
)
from ..content_factory_identity import (
    build_content_factory_identity_payload,
    is_delegated_content_factory_request,
    resolve_content_factory_identity_context,
)
from ..content_intent import detect_content_action, is_explicit_scan_request
from ..linear_meeting_sources import (
    ParsedSource,
    SourceParseResult,
    parse_linear_meeting_sources,
    source_text_chunks,
)
from ..linear_effort_sizing import (
    EFFORT_LABELS,
    assessment_metadata,
    assess_studio_effort_batch,
    is_project_issue,
    is_terminal_candidate,
)
from ..linear_inference import (
    LINEAR_SKILL_MODEL,
    LinearContextualIssueResult,
    LinearDirectIssueBatch,
    LinearInferenceTimeoutError,
    LinearMeetingActionBatch,
    LinearProjectSourceSummary,
    LinearProjectUpdateResult,
    LinearReasoningSignals,
    linear_safety_identifier,
    run_linear_structured_inference,
)
from ..llm import chat, embed, extract_text_from_image, get_llm_client
from ..points_request_approval import (
    build_points_request_metadata,
    build_points_request_record,
    remember_points_request_summary,
)
from ..points_flex import (
    build_points_flex_delete_blocks,
    build_points_flex_preview_blocks,
    get_points_flex_store,
    issue_points_flex_confirmation,
    issue_points_flex_deletion,
    parse_lifetime_earned,
)
from ..slack_client import (
    get_bot_user_id,
    get_channel_id,
    get_channel_name,
    post_ephemeral,
    post_message,
    send_dm,
    upload_file,
)
from ..config import get_settings
from ..backend_identity import BackendIdentityError, get_backend_actor_context
from ..admin_brain import (
    ADMIN_BRAIN_ACCESS_DENIED_MESSAGE,
    ADMIN_BRAIN_UNAVAILABLE_MESSAGE,
    build_admin_action_list_response,
    build_admin_action_response,
    build_admin_brain_response,
)
from ..clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from ..coworking_booking_intents import (
    get_coworking_intent_store,
    is_retryable_coworking_exception,
)
from ..meeting_room_booking import (
    MeetingRoomInputError,
    backend_error_message as meeting_room_backend_error_message,
    booking_preview,
    cancellation_selection,
    format_interval as format_meeting_room_interval,
    has_date_reference as has_meeting_room_date_reference,
    parse_backend_timestamp,
    room_selection_prompt,
    room_slug_from_text,
    resolve_availability_interval as resolve_meeting_room_availability_interval,
    resolve_interval as resolve_meeting_room_interval,
    resolve_local_date as resolve_meeting_room_date,
    supported_active_rooms,
)
from ..meeting_room_clarifications import (
    get_meeting_room_clarification_store,
    public_room_choice_prompt,
)


POINTS_SUPER_ADMIN_SLACK_ID = "U05QPB483K9"
FULL_POINTS_ADMIN_ROLES = {"admin", "committee", "portfolio_lead"}
COWORKING_REPORT_ROLES = {*FULL_POINTS_ADMIN_ROLES, "partner"}
VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE = (
    "Victor application data is not available in this conversation."
)
LINEAR_MEETING_TIMEOUT_RECOVERY_OVERLAP_CHARS = 300


class LinearMeetingExtractionDeadlineError(RuntimeError):
    """The complete meeting-action extraction exceeded its wall-clock budget."""


# Pack ids are opaque and kept stable; the id number no longer matches the
# points it grants (points were doubled for the same price). Must stay in sync
# with mlai-backend roo/services.py ROO_TOPUP_PACKS.
ROO_TOPUP_PACKS = {
    "topup_5": {
        "points": 10,
        "label": "10 Top-up Roo Points",
        "price": "A$19.99",
    },
    "topup_10": {
        "points": 20,
        "label": "20 Top-up Roo Points",
        "price": "A$36.99",
    },
    "topup_25": {
        "points": 50,
        "label": "50 Top-up Roo Points",
        "price": "A$63.99",
    },
}
ROO_TOPUP_PACK_BY_POINTS = {
    pack["points"]: pack_id for pack_id, pack in ROO_TOPUP_PACKS.items()
}


@dataclass
class SkillResult:
    """Result from skill execution."""
    success: bool
    message: str
    data: Optional[Any] = None
    error: Optional[str] = None
    blocks: Optional[list] = None
    suppress_post: bool = False


def _linear_task_sizing_setting(
    settings: Any,
    canonical_name: str,
    legacy_name: str,
    default: Any,
) -> Any:
    configured = getattr(settings, canonical_name, None)
    if configured in (None, ""):
        configured = getattr(settings, legacy_name, default)
    return default if configured in (None, "") else configured


class SkillExecutor:
    """
    Executes skills based on their SKILL.md definitions.
    
    The executor:
    1. Extracts parameters from the user's message using LLM
    2. Routes to skill-specific handlers if available
    3. Falls back to generic LLM execution with skill instructions
    """


    async def execute(
        self,
        skill: Skill,
        text: str,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
        param_overrides: Optional[dict] = None,
        **kwargs
    ) -> SkillResult:
        """
        Execute a skill with the given context.
        
        Args:
            skill: The skill to execute
            text: User's message
            user_id: Slack user ID
            channel_id: Channel ID
            thread_ts: Thread timestamp
            **kwargs: Additional context
        
        Returns:
            SkillResult with message and optional data
        """
        print(f"🎯 Executing skill: {skill.name}")
        
        try:
            # Router v2 supplies structured params (action + declared fields);
            # interactive callbacks pass explicit param_overrides. The free-form
            # LLM parameter-extraction call was removed in Phase 4 of the
            # routing redesign — handlers parse remaining details from the text.
            params = dict(param_overrides or {})
            if params:
                print(f"   Routed params: {params}")
            
            # Check for skill-specific implementation
            if skill.name == "admin-brain":
                result = await self._execute_admin_brain(
                    text=text,
                    params=params,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
            elif skill.name == "admin-actions":
                result = await self._execute_admin_actions(
                    text=text,
                    params=params,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
            elif skill.name == "content-factory":
                result = await self._execute_content_factory(skill, text, params, user_id, channel_id, thread_ts, thread_history)
            elif skill.name == "connect-users":
                result = await self._execute_connect_users(skill, text, params, user_id)
            elif skill.name == "mlai-points":
                result = await self._execute_mlai_points(
                    skill,
                    text,
                    params,
                    user_id,
                    channel_id,
                    thread_ts,
                    request_id=(
                        kwargs.get("event_id")
                        or kwargs.get("current_message_ts")
                    ),
                )
            elif skill.name == "committee-candidate-emails":
                result = await self._execute_committee_candidate_emails(
                    text=text,
                    params=params,
                    user_id=user_id,
                    channel_id=channel_id,
                )
            elif skill.name == "meeting-room-booking":
                result = await self._execute_meeting_room_booking(
                    text=text,
                    params=params,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    slack_team_id=kwargs.get("slack_team_id"),
                    request_message_ts=kwargs.get("current_message_ts"),
                )
            elif skill.name == "mlai-data-query":
                result = await self._execute_mlai_data_query(
                    skill,
                    text,
                    params,
                    user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    thread_history=(
                        kwargs.get("linear_thread_history")
                        if kwargs.get("linear_thread_history") is not None
                        else thread_history
                    ),
                    slack_team_id=kwargs.get("slack_team_id"),
                )
            elif skill.name == "github-integration":
                result = await self._execute_github_integration(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "linear-meeting-actions":
                result = await self._execute_linear_meeting_actions(
                    skill,
                    text,
                    params,
                    user_id,
                    channel_id,
                    thread_ts,
                    thread_history,
                    kwargs.get("event_files"),
                    kwargs.get("current_message_ts"),
                    kwargs.get("slack_context"),
                )
            elif skill.name == "tone-of-voice":
                result = await self._execute_tone_of_voice(skill, text, params, user_id)
            elif skill.name == "victor-ai-applications":
                result = await self._execute_victor_ai_applications(
                    text=text,
                    params=params,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    slack_team_id=kwargs.get("slack_team_id"),
                    event_id=kwargs.get("event_id"),
                    current_message_ts=kwargs.get("current_message_ts"),
                )
            elif skill.name == "healthhack":
                result = await self._execute_healthhack(
                    skill,
                    text,
                    params,
                    user_id,
                    channel_id,
                    thread_ts,
                    kwargs.get("current_message_ts"),
                )
            elif skill.name == "watt-the-hack":
                result = await self._execute_watt_the_hack(skill, text, params, user_id, channel_id, thread_ts, thread_history)
            elif skill.name == "luma-events":
                result = await self._execute_luma_events(skill, text, params, user_id, channel_id, thread_ts)
            elif skill.name == "reconciliation-report":
                result = await self._execute_reconciliation_report(skill, text, params, user_id, channel_id, thread_ts)
            else:
                # Generic LLM-based execution
                result = await self._execute_with_llm(skill, text, params, user_id, thread_history)
            
            # Skill handlers can return a dict with "message" + "blocks" for rich responses
            blocks = None
            result_data = params
            suppress_post = False
            if isinstance(result, dict) and "message" in result:
                blocks = result.get("blocks")
                result_data = result.get("data", params)
                suppress_post = bool(result.get("suppress_post", False))
                result = result["message"]

            return SkillResult(
                success=True,
                message=result,
                data=result_data,
                blocks=blocks,
                suppress_post=suppress_post
            )
            
        except Exception as e:
            print(f"❌ Skill execution failed: {e}")
            import traceback
            traceback.print_exc()
            
            return SkillResult(
                success=False,
                message="Sorry, I ran into a problem executing that skill. Can you try again?",
                error=str(e)
            )

    async def _execute_committee_candidate_emails(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
    ) -> dict:
        """Privately return copy-ready emails selected by the backend."""
        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )
        from roo.committee_candidate_emails import build_candidate_email_payloads

        action = str(params.get("action") or "").strip().lower()
        if action != "list_eligible_emails":
            return {
                "message": (
                    "Ask me to list the emails of members with at least 100 "
                    "lifetime-earned Roo Points."
                ),
                "data": {"action": action or None},
            }

        settings = get_settings()
        if not settings.MLAI_BACKEND_URL:
            return {
                "message": "The MLAI backend isn't configured, so I can't load candidates.",
                "data": {"action": action},
            }
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )
        try:
            data = await client.list_committee_candidate_emails(user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                return {
                    "message": (
                        "Only active Points Admins with the admin or committee role "
                        "can access this email list."
                    ),
                    "data": {"action": action, "authorised": False},
                }
            return {
                "message": "I couldn't load the eligible email list right now.",
                "data": {"action": action},
            }
        except (MLAIBackendUnavailableError, ValueError):
            return {
                "message": "I couldn't reach the MLAI backend. Try again shortly.",
                "data": {"action": action},
            }

        payloads = build_candidate_email_payloads(data)
        result_data = {
            "action": action,
            "eligible_count": int(data.get("eligible_count") or 0),
            "delivery": "direct_message",
        }

        is_direct_message = bool(channel_id and channel_id.startswith("D"))
        deliver_with_dm = not is_direct_message or len(payloads) > 1
        if deliver_with_dm:
            try:
                delivered = all(
                    bool(
                        (send_dm(user_id, payload["message"], blocks=payload["blocks"]) or {}).get("ok")
                    )
                    for payload in payloads
                )
            except Exception:
                delivered = False
            if not delivered:
                return {
                    "message": (
                        "I couldn't deliver the private email list. DM Roo `committee candidate emails` "
                        "and I'll show the list there."
                    ),
                    "data": {**result_data, "delivery_failed": True},
                }
            if is_direct_message:
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": result_data,
                }
            return {
                "message": "I've sent the eligible email list privately.",
                "data": result_data,
            }
        response = payloads[0]
        return {
            "message": response["message"],
            "blocks": response["blocks"],
            "data": result_data,
        }


    def _deliver_meeting_room_response(
        self,
        *,
        user_id: str,
        channel_id: Optional[str],
        message: str,
        blocks: Optional[list] = None,
        action: Optional[str] = None,
        client_msg_id: Optional[str] = None,
    ) -> dict:
        data = {"action": action, "delivery": "direct_message"}
        if channel_id and str(channel_id).startswith("D"):
            return {"message": message, "blocks": blocks, "data": data}
        if not channel_id:
            return {
                "message": (
                    "I could not verify a private Slack destination. "
                    "DM Roo `meeting room` and try again there."
                ),
                "data": {**data, "delivery_failed": True},
            }
        try:
            delivery_options = {}
            if blocks:
                delivery_options["blocks"] = blocks
            if client_msg_id:
                delivery_options["client_msg_id"] = client_msg_id
                delivery_options["raise_on_error"] = True
            dm_response = send_dm(user_id, message, **delivery_options)
        except Exception as exc:
            error_response = getattr(exc, "response", None)
            try:
                duplicate_message = bool(
                    error_response is not None
                    and error_response.get("error") == "duplicate_message"
                )
            except (AttributeError, TypeError):
                duplicate_message = False
            if duplicate_message:
                dm_response = {"error": "duplicate_message"}
            elif client_msg_id:
                raise
            else:
                dm_response = None
        delivered = bool(
            dm_response
            and (
                dm_response.get("ok")
                or dm_response.get("error") == "duplicate_message"
            )
        )
        if not delivered:
            return {
                "message": (
                    "I could not open a private Slack DM. DM Roo `meeting room` "
                    "and try again there."
                ),
                "data": {**data, "delivery_failed": True},
            }
        return {
            "message": "I've sent you a private reply about the Meeting Room.",
            "data": data,
        }

    @staticmethod
    def _meeting_room_date_is_present(text: str, params: dict) -> bool:
        return has_meeting_room_date_reference(text, params)

    @staticmethod
    def _meeting_room_time_is_present(text: str, params: dict) -> bool:
        if any(params.get(key) for key in ("starts_at", "start_time")):
            return True
        return bool(
            re.search(r"\b(?:at|from)\s+\d", str(text or "").lower())
            or re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", str(text or "").lower())
            or re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
                str(text or "").lower(),
            )
        )

    @staticmethod
    def _format_meeting_room_availability(result: dict) -> str:
        room = result.get("room") or {}
        room_name = str(room.get("name") or "Meeting Room")
        requested = result.get("requested_interval")
        if requested:
            starts_at = parse_backend_timestamp(requested.get("starts_at"))
            ends_at = parse_backend_timestamp(requested.get("ends_at"))
            if result.get("available"):
                if result.get("bookable") is False:
                    return (
                        f"The *{room_name}* is available {format_meeting_room_interval(starts_at, ends_at)} "
                        "(Melbourne time). This is an availability check only; one booking must be "
                        "between 1 and 2 hours."
                    )
                cost = int(result.get("points_cost") or 0)
                return (
                    f"The *{room_name}* is available {format_meeting_room_interval(starts_at, ends_at)} "
                    f"(Melbourne time). It costs {cost} Roo Point{'s' if cost != 1 else ''}."
                )
            return (
                f"The *{room_name}* is not available {format_meeting_room_interval(starts_at, ends_at)} "
                "(Melbourne time)."
            )

        busy = result.get("busy_intervals") or []
        if not busy:
            return (
                f"The *{room_name}* has no bookings or blocks currently shown for "
                "that date (Melbourne time). Ask Roo to check a specific future "
                "time before booking."
            )
        lines = [f"The *{room_name}* is unavailable at these times (Melbourne time):"]
        for interval in busy:
            starts_at = parse_backend_timestamp(interval.get("starts_at"))
            ends_at = parse_backend_timestamp(interval.get("ends_at"))
            lines.append(f"- {format_meeting_room_interval(starts_at, ends_at)}")
        lines.extend(
            [
                "",
                (
                    "No room bookings or blocks are currently shown outside these "
                    "periods. Ask Roo to check a specific future time before booking."
                ),
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_meeting_room_bookings(bookings: list[dict]) -> str:
        if not bookings:
            return "You do not have any upcoming Meeting Room bookings."
        lines = ["Your upcoming Meeting Room bookings:"]
        for booking in bookings:
            starts_at = parse_backend_timestamp(booking.get("starts_at"))
            ends_at = parse_backend_timestamp(booking.get("ends_at"))
            room_name = str((booking.get("room") or {}).get("name") or "Meeting Room")
            lines.append(
                f"- *{room_name}:* {format_meeting_room_interval(starts_at, ends_at)} "
                f"({booking.get('points_cost', 0)} Roo Points)"
            )
        return "\n".join(lines)

    @classmethod
    def _format_meeting_room_availability_list(
        cls,
        results: list[dict],
    ) -> str:
        if not results:
            return "No meeting rooms are accepting bookings right now."
        return "\n\n".join(
            cls._format_meeting_room_availability(result)
            for result in results
        )

    async def _execute_meeting_room_booking(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str] = None,
        slack_team_id: Optional[str] = None,
        request_message_ts: Optional[str] = None,
    ) -> dict:
        settings = get_settings()
        action = str(params.get("action") or "").strip()
        if not settings.MEETING_ROOM_BOOKING_ENABLED:
            return {
                "message": "Meeting-room booking is not enabled right now.",
                "data": {"action": action, "feature_disabled": True},
            }
        if not settings.MLAI_BACKEND_URL or not settings.ROO_API_KEY:
            return {
                "message": "Meeting-room booking is not configured right now.",
                "data": {"action": action, "configuration_error": True},
            }

        mentioned_users = []
        for mentioned_user in re.findall(
            r"<@([A-Z0-9]+)(?:\|[^>]+)?>",
            str(text or ""),
        ):
            if mentioned_user != user_id and mentioned_user not in mentioned_users:
                mentioned_users.append(mentioned_user)
        if len(mentioned_users) > 1:
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message="Tag exactly one member when booking the Meeting Room for someone else.",
                action=action,
            )
        target_slack_user_id = mentioned_users[0] if mentioned_users else None
        if target_slack_user_id and action not in (
            "check_room_availability",
            "book_meeting_room",
        ):
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message="Booking for another member supports availability checks and new bookings only.",
                action=action,
            )

        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY,
            internal_api_key=settings.ROO_API_KEY,
        )
        try:
            requested_room_slug = room_slug_from_text(text)
            if action == "check_room_availability":
                rooms = supported_active_rooms(await client.list_meeting_rooms())
                selected_rooms = (
                    [room for room in rooms if room["slug"] == requested_room_slug]
                    if requested_room_slug
                    else rooms
                )
                if not selected_rooms:
                    requested_name = (
                        "Small Meeting Room"
                        if requested_room_slug == "small-meeting-room"
                        else "Big Meeting Room"
                    )
                    raise MeetingRoomInputError(
                        "inactive_room",
                        f"The {requested_name} is not accepting bookings right now.",
                    )
                availability_results = []
                if self._meeting_room_time_is_present(text, params):
                    starts_at, ends_at = resolve_meeting_room_availability_interval(
                        text,
                        params,
                    )
                    for room in selected_rooms:
                        availability_results.append(
                            await client.check_meeting_room_availability(
                                user_id,
                                room_slug=room["slug"],
                                starts_at=starts_at.isoformat(),
                                ends_at=ends_at.isoformat(),
                                target_slack_user_id=target_slack_user_id,
                            )
                        )
                else:
                    local_date = resolve_meeting_room_date(text, params)
                    for room in selected_rooms:
                        availability_results.append(
                            await client.check_meeting_room_availability(
                                user_id,
                                room_slug=room["slug"],
                                date=local_date.isoformat(),
                                target_slack_user_id=target_slack_user_id,
                            )
                        )
                message = self._format_meeting_room_availability_list(
                    availability_results
                )
                return self._deliver_meeting_room_response(
                    user_id=user_id,
                    channel_id=channel_id,
                    message=message,
                    action=action,
                )

            if action == "book_meeting_room":
                starts_at, ends_at = resolve_meeting_room_interval(text, params)
                rooms = supported_active_rooms(await client.list_meeting_rooms())
                if not rooms:
                    raise MeetingRoomInputError(
                        "inactive_room",
                        "No meeting rooms are accepting bookings right now.",
                    )
                if requested_room_slug is None:
                    if channel_id and not str(channel_id).startswith("D"):
                        clarification_context = all(
                            str(value or "").strip()
                            for value in (
                                slack_team_id,
                                channel_id,
                                thread_ts,
                                request_message_ts,
                                user_id,
                            )
                        )
                        if not clarification_context:
                            return {
                                "message": (
                                    "I couldn't safely keep track of that public room choice. "
                                    "DM Roo `book the meeting room` and try again."
                                ),
                                "data": {
                                    "action": action,
                                    "delivery_failed": True,
                                },
                            }
                        clarification = get_meeting_room_clarification_store(
                            settings.SLACK_RECEIPTS_DB_PATH
                        ).record_prompt(
                            team_id=str(slack_team_id),
                            channel_id=str(channel_id),
                            thread_ts=str(thread_ts),
                            request_message_ts=str(request_message_ts),
                            owner_user_id=user_id,
                            starts_at=starts_at.isoformat(),
                            ends_at=ends_at.isoformat(),
                            available_room_slugs=[room["slug"] for room in rooms],
                            target_user_id=target_slack_user_id,
                            choice_mode="buttons",
                        )
                        prompt = public_room_choice_prompt(
                            clarification,
                            rooms,
                        )
                        return {
                            "message": prompt["message"],
                            "blocks": prompt["blocks"],
                            "data": {
                                "action": action,
                                "delivery": "public_thread_clarification",
                                "clarification_id": clarification.get("id"),
                            },
                        }
                    selection = room_selection_prompt(
                        rooms,
                        owner_slack_user_id=user_id,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        target_slack_user_id=target_slack_user_id,
                    )
                    return self._deliver_meeting_room_response(
                        user_id=user_id,
                        channel_id=channel_id,
                        message=selection["message"],
                        blocks=selection["blocks"],
                        action=action,
                    )
                selected_room = next(
                    (
                        room
                        for room in rooms
                        if room["slug"] == requested_room_slug
                    ),
                    None,
                )
                if selected_room is None:
                    requested_name = (
                        "Small Meeting Room"
                        if requested_room_slug == "small-meeting-room"
                        else "Big Meeting Room"
                    )
                    raise MeetingRoomInputError(
                        "inactive_room",
                        f"The {requested_name} is not accepting bookings right now.",
                    )
                availability = await client.check_meeting_room_availability(
                    user_id,
                    room_slug=selected_room["slug"],
                    starts_at=starts_at.isoformat(),
                    ends_at=ends_at.isoformat(),
                    target_slack_user_id=target_slack_user_id,
                )
                if not availability.get("available"):
                    message = self._format_meeting_room_availability(availability)
                    return self._deliver_meeting_room_response(
                        user_id=user_id,
                        channel_id=channel_id,
                        message=f"{message} No booking was created.",
                        action=action,
                    )
                preview = booking_preview(
                    availability,
                    owner_slack_user_id=user_id,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    expected_room_slug=selected_room["slug"],
                    target_slack_user_id=target_slack_user_id,
                )
                return self._deliver_meeting_room_response(
                    user_id=user_id,
                    channel_id=channel_id,
                    message=preview["message"],
                    blocks=preview["blocks"],
                    action=action,
                )

            if action == "list_my_room_bookings":
                bookings = await client.get_my_meeting_room_bookings(user_id)
                return self._deliver_meeting_room_response(
                    user_id=user_id,
                    channel_id=channel_id,
                    message=self._format_meeting_room_bookings(bookings),
                    action=action,
                )

            if action == "cancel_meeting_room":
                bookings = await client.get_my_meeting_room_bookings(user_id)
                if requested_room_slug:
                    bookings = [
                        row
                        for row in bookings
                        if str((row.get("room") or {}).get("slug") or "")
                        == requested_room_slug
                    ]
                booking_id = str(params.get("booking_id") or "").strip()
                if booking_id:
                    bookings = [row for row in bookings if str(row.get("id")) == booking_id]
                elif self._meeting_room_date_is_present(text, params):
                    local_date = resolve_meeting_room_date(text, params)
                    bookings = [
                        row
                        for row in bookings
                        if parse_backend_timestamp(row.get("starts_at")).date() == local_date
                    ]
                if not bookings:
                    return self._deliver_meeting_room_response(
                        user_id=user_id,
                        channel_id=channel_id,
                        message="I could not find an upcoming Meeting Room booking matching that request.",
                        action=action,
                    )
                if len(bookings) > 40:
                    return self._deliver_meeting_room_response(
                        user_id=user_id,
                        channel_id=channel_id,
                        message=(
                            "You have too many upcoming bookings to show safely in one Slack message. "
                            "Tell me the cancellation date so I can narrow the list."
                        ),
                        action=action,
                    )
                selection = cancellation_selection(
                    bookings,
                    owner_slack_user_id=user_id,
                )
                return self._deliver_meeting_room_response(
                    user_id=user_id,
                    channel_id=channel_id,
                    message=selection["message"],
                    blocks=selection["blocks"],
                    action=action,
                )

            return {
                "message": "I could not determine which Meeting Room action to perform.",
                "data": {"action": action},
            }
        except MeetingRoomInputError as exc:
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message=exc.message,
                action=action,
            )
        except (httpx.HTTPStatusError, MLAIBackendUnavailableError) as exc:
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message=meeting_room_backend_error_message(
                    exc,
                    target_slack_user_id=target_slack_user_id,
                    room_slug=requested_room_slug,
                ),
                action=action,
            )

    async def complete_meeting_room_room_choice(
        self,
        *,
        user_id: str,
        channel_id: str,
        room_slug: str,
        starts_at: str,
        ends_at: str,
        booking_client_request_id: str,
        target_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Privately continue a room booking from a durable public reply."""

        action = "book_meeting_room"
        settings = get_settings()
        if not settings.MEETING_ROOM_BOOKING_ENABLED:
            return {
                "message": "Meeting-room booking is not enabled right now.",
                "data": {"action": action, "feature_disabled": True},
            }
        if not settings.MLAI_BACKEND_URL or not settings.ROO_API_KEY:
            return {
                "message": "Meeting-room booking is not configured right now.",
                "data": {"action": action, "configuration_error": True},
            }

        requested_room_slug = str(room_slug or "").strip()
        try:
            parsed_start = parse_backend_timestamp(starts_at)
            parsed_end = parse_backend_timestamp(ends_at)
            if parsed_end <= parsed_start:
                raise MeetingRoomInputError(
                    "invalid_time",
                    "That room-choice request is no longer valid. Ask Roo to start again.",
                )
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY,
                internal_api_key=settings.ROO_API_KEY,
            )
            selected_room = next(
                (
                    room
                    for room in supported_active_rooms(
                        await client.list_meeting_rooms()
                    )
                    if room["slug"] == requested_room_slug
                ),
                None,
            )
            if selected_room is None:
                requested_name = (
                    "Small Meeting Room"
                    if requested_room_slug == "small-meeting-room"
                    else "Big Meeting Room"
                )
                raise MeetingRoomInputError(
                    "inactive_room",
                    f"The {requested_name} is not accepting bookings right now.",
                )
            availability = await client.check_meeting_room_availability(
                user_id,
                room_slug=selected_room["slug"],
                starts_at=parsed_start.isoformat(),
                ends_at=parsed_end.isoformat(),
                target_slack_user_id=target_slack_user_id,
            )
            if not availability.get("available"):
                message = self._format_meeting_room_availability(availability)
                return self._deliver_meeting_room_response(
                    user_id=user_id,
                    channel_id=channel_id,
                    message=f"{message} No booking was created.",
                    action=action,
                )
            preview = booking_preview(
                availability,
                owner_slack_user_id=user_id,
                starts_at=parsed_start,
                ends_at=parsed_end,
                expected_room_slug=selected_room["slug"],
                target_slack_user_id=target_slack_user_id,
                client_request_id=booking_client_request_id,
            )
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message=preview["message"],
                blocks=preview["blocks"],
                action=action,
                client_msg_id=booking_client_request_id,
            )
        except MeetingRoomInputError as exc:
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message=exc.message,
                action=action,
            )
        except (httpx.HTTPStatusError, MLAIBackendUnavailableError) as exc:
            return self._deliver_meeting_room_response(
                user_id=user_id,
                channel_id=channel_id,
                message=meeting_room_backend_error_message(
                    exc,
                    target_slack_user_id=target_slack_user_id,
                    room_slug=requested_room_slug,
                ),
                action=action,
            )

    async def _execute_admin_brain(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> dict:
        """Call the permission-filtered backend with no LLM/search fallback."""

        settings = get_settings()
        if (
            settings.ROO_SURFACE != "admin"
            or not settings.ORG_BRAIN_ENABLED
            or not settings.ORG_BRAIN_API_KEY
        ):
            return {
                "message": ADMIN_BRAIN_UNAVAILABLE_MESSAGE,
                "data": {"admin_brain": True},
            }

        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            service_principal_key=settings.ORG_BRAIN_API_KEY,
            surface=settings.ROO_SURFACE,
        )
        timeout = float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS)
        try:
            answer = await client.answer_org_memory(
                text,
                channel_id=channel_id,
                thread_ts=thread_ts,
                answer_mode=str(params.get("answer_mode") or "auto"),
                as_of=params.get("as_of"),
                time_start=params.get("time_start"),
                time_end=params.get("time_end"),
                max_context_tokens=int(settings.ORG_BRAIN_MAX_CONTEXT_TOKENS),
                timeout=timeout,
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            print(
                "ADMIN_BRAIN_REQUEST_DENIED "
                f"status={status_code} user_id={user_id} "
                f"channel_id={channel_id}"
            )
            message = (
                ADMIN_BRAIN_ACCESS_DENIED_MESSAGE
                if status_code in {401, 403, 404}
                else ADMIN_BRAIN_UNAVAILABLE_MESSAGE
            )
            return {
                "message": message,
                "data": {"admin_brain": True},
            }
        except (MLAIBackendUnavailableError, ValueError) as exc:
            print(
                "ADMIN_BRAIN_UNAVAILABLE "
                f"error={exc.__class__.__name__} user_id={user_id} "
                f"channel_id={channel_id}"
            )
            return {
                "message": ADMIN_BRAIN_UNAVAILABLE_MESSAGE,
                "data": {"admin_brain": True},
            }
        except Exception as exc:
            print(
                "ADMIN_BRAIN_FAILED "
                f"error={exc.__class__.__name__} user_id={user_id} "
                f"channel_id={channel_id}"
            )
            return {
                "message": ADMIN_BRAIN_UNAVAILABLE_MESSAGE,
                "data": {"admin_brain": True},
            }

        primary_claim_id = None
        query_id = str(answer.get("query_id") or "").strip()
        if query_id:
            try:
                trace = await client.get_org_memory_query_trace(
                    query_id,
                    timeout=timeout,
                )
                primary_claim_id = next(
                    (
                        str(value)
                        for value in (trace.get("selected_claim_ids") or [])
                        if value
                    ),
                    None,
                )
            except Exception as exc:
                print(
                    "ADMIN_BRAIN_TRACE_UNAVAILABLE "
                    f"error={exc.__class__.__name__} query_id={query_id}"
                )
        return build_admin_brain_response(
            answer,
            requester_user_id=user_id,
            primary_claim_id=primary_claim_id,
        )

    async def _execute_admin_actions(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> dict:
        """Create or review only backend-allowlisted controlled actions."""

        settings = get_settings()
        if (
            settings.ROO_SURFACE != "admin"
            or not settings.ORG_BRAIN_ENABLED
            or not settings.ORG_BRAIN_ACTIONS_ENABLED
            or not settings.ORG_BRAIN_API_KEY
        ):
            return {
                "message": "Controlled Admin Roo actions are not enabled.",
                "data": {"admin_action": True, "enabled": False},
            }

        action = str(params.get("action") or "").strip()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            service_principal_key=settings.ORG_BRAIN_API_KEY,
            surface=settings.ROO_SURFACE,
            actor_context=get_backend_actor_context(),
        )
        timeout = float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS)
        try:
            if action == "list_pending":
                payload = await client.list_org_memory_actions(
                    limit=50,
                    timeout=timeout,
                )
                return build_admin_action_list_response(payload)

            if action == "show_action":
                proposal_id = str(params.get("proposal_id") or "").strip()
                if not proposal_id:
                    return {
                        "message": (
                            "Give me the controlled-action proposal UUID to open."
                        ),
                        "data": {
                            "admin_action": True,
                            "action": action,
                        },
                    }
                proposal = await client.get_org_memory_action(
                    proposal_id,
                    timeout=timeout,
                )
                return build_admin_action_response(proposal)

            action_type, input_payload, missing = self._admin_action_payload(
                action=action,
                params=params,
                channel_id=channel_id,
            )
            if missing:
                return {
                    "message": (
                        "I haven't created a proposal. Please provide: "
                        + ", ".join(f"`{value}`" for value in missing)
                        + "."
                    ),
                    "data": {"admin_action": True, "action": action},
                }
            if not action_type:
                return {
                    "message": (
                        "Ask me to list pending actions, open a proposal UUID, "
                        "create a local Gmail/Slack/Notion draft, or propose a "
                        "scoped Linear issue create/update."
                    ),
                    "data": {
                        "admin_action": True,
                        "action": action or "help",
                    },
                }

            actor = get_backend_actor_context()
            idempotency_material = {
                "event_id": getattr(actor, "event_id", ""),
                "action_type": action_type,
                "configuration_id": str(
                    params.get("configuration_id") or ""
                ),
                "input_payload": input_payload,
            }
            proposal_key = "roo-proposal-" + hashlib.sha256(
                json.dumps(
                    idempotency_material,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            proposal = await client.create_org_memory_action(
                action_type=action_type,
                input_payload=input_payload,
                configuration_id=(
                    str(params.get("configuration_id") or "").strip()
                    or None
                ),
                idempotency_key=proposal_key,
                timeout=timeout,
            )
            if not proposal.get("requires_approval"):
                proposal = await client.execute_org_memory_action(
                    str(proposal.get("id") or ""),
                    idempotency_key=f"roo-draft-execute-{proposal['id']}",
                    timeout=timeout,
                )
            return build_admin_action_response(proposal)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            try:
                detail = str(
                    exc.response.json().get("detail") or ""
                ).strip()
            except (ValueError, AttributeError):
                detail = ""
            print(
                "ADMIN_ACTION_REQUEST_FAILED "
                f"status={status_code} action={action} user_id={user_id} "
                f"channel_id={channel_id}"
            )
            if status_code in {401, 403, 404}:
                message = (
                    "I can't access that controlled action in this context."
                )
            elif status_code == 400 and detail:
                message = f"I haven't changed anything. {detail}"
            else:
                message = (
                    "The controlled-action gateway is unavailable; "
                    "nothing was changed."
                )
            return {
                "message": message,
                "data": {"admin_action": True, "action": action},
            }
        except (
            BackendIdentityError,
            MLAIBackendUnavailableError,
            ValueError,
        ) as exc:
            print(
                "ADMIN_ACTION_UNAVAILABLE "
                f"error={exc.__class__.__name__} action={action} "
                f"channel_id={channel_id}"
            )
            return {
                "message": (
                    "The controlled-action gateway is unavailable; "
                    "nothing was changed."
                ),
                "data": {"admin_action": True, "action": action},
            }
        except Exception as exc:
            print(
                "ADMIN_ACTION_FAILED "
                f"error={exc.__class__.__name__} action={action} "
                f"channel_id={channel_id}"
            )
            return {
                "message": (
                    "The controlled-action request failed closed; "
                    "nothing was changed."
                ),
                "data": {"admin_action": True, "action": action},
            }

    @staticmethod
    def _admin_action_payload(
        *,
        action: str,
        params: dict,
        channel_id: Optional[str],
    ) -> tuple[Optional[str], dict, list[str]]:
        mapping = {
            "draft_gmail": "draft_gmail",
            "draft_slack_post": "draft_slack_post",
            "draft_notion_update": "draft_notion_update",
            "create_linear_issue": "create_linear_issue",
            "update_linear_issue": "update_linear_issue",
        }
        action_type = mapping.get(action)
        if not action_type:
            return None, {}, []

        if action == "draft_gmail":
            raw_recipients = params.get("to") or []
            recipients = (
                [
                    value.strip()
                    for value in raw_recipients.split(",")
                    if value.strip()
                ]
                if isinstance(raw_recipients, str)
                else [
                    str(value).strip()
                    for value in raw_recipients
                    if str(value).strip()
                ]
            )
            payload = {
                "to": recipients,
                "subject": str(params.get("subject") or "").strip(),
                "body": str(params.get("body") or "").strip(),
            }
            missing = [
                name
                for name, present in (
                    ("to", bool(payload["to"])),
                    ("subject", bool(payload["subject"])),
                    ("body", bool(payload["body"])),
                )
                if not present
            ]
            return action_type, payload, missing

        if action == "draft_slack_post":
            payload = {
                "channel_id": str(
                    params.get("channel_id") or channel_id or ""
                ).strip(),
                "text": str(params.get("text") or "").strip(),
            }
            if params.get("thread_ts"):
                payload["thread_ts"] = str(params["thread_ts"]).strip()
            missing = [
                name
                for name in ("channel_id", "text")
                if not payload.get(name)
            ]
            return action_type, payload, missing

        if action == "draft_notion_update":
            payload = {
                "page_id": str(params.get("page_id") or "").strip(),
                "title": str(params.get("title") or "").strip(),
                "body": str(params.get("body") or "").strip(),
            }
            missing = [name for name in payload if not payload[name]]
            return action_type, payload, missing

        payload = {
            key: str(params.get(key) or "").strip()
            for key in (
                "team_id",
                "project_id",
                "title",
                "description",
                "assignee_id",
                "due_date",
                "state_id",
            )
            if params.get(key) not in (None, "")
        }
        if params.get("priority") not in (None, ""):
            payload["priority"] = params["priority"]
        if params.get("label_ids"):
            raw_labels = params["label_ids"]
            payload["label_ids"] = (
                [
                    value.strip()
                    for value in str(raw_labels).split(",")
                    if value.strip()
                ]
                if isinstance(raw_labels, str)
                else [
                    str(value).strip()
                    for value in raw_labels
                    if str(value).strip()
                ]
            )
        if action == "update_linear_issue":
            payload["issue_id"] = str(
                params.get("issue_id") or ""
            ).strip()
        required = ["configuration_id", "project_id"]
        if action == "create_linear_issue":
            required.extend(("team_id", "title"))
        else:
            required.append("issue_id")
        missing = [
            name
            for name in required
            if not (
                params.get(name)
                if name == "configuration_id"
                else payload.get(name)
            )
        ]
        if action == "update_linear_issue":
            mutable = {
                "team_id",
                "title",
                "description",
                "assignee_id",
                "priority",
                "due_date",
                "label_ids",
                "state_id",
            }
            if not any(
                payload.get(name) not in (None, "", [])
                for name in mutable
            ):
                missing.append("at least one changed Linear field")
        return action_type, payload, missing

    async def _execute_victor_ai_applications(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        slack_team_id: Optional[str],
        event_id: Optional[str],
        current_message_ts: Optional[str],
    ) -> dict:
        """Run deterministic, read-only Victor application actions."""

        settings = get_settings()
        channel_name = get_channel_name(channel_id) if channel_id else None
        channel_allowed = settings.is_victor_ai_context_allowed(
            channel_name=channel_name,
        )
        if not channel_allowed and not channel_name and channel_id:
            channel_allowed = (
                get_channel_id(settings.victor_ai_slack_channel_name) == channel_id
            )
        if not channel_allowed:
            print(
                "VICTOR_AI_ACCESS_BLOCKED "
                f"user_id={user_id} channel_id={channel_id} channel_name={channel_name}"
            )
            return {
                "message": VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE,
                "data": {"victor_ai": True, "allowed": False},
            }

        action = self._resolve_victor_ai_action(text, params)
        if action == "help":
            return {
                "message": self._victor_ai_help_message(),
                "data": {"victor_ai": True, "action": "help"},
            }

        actor_context = {
            "slack_team_id": str(slack_team_id or "").strip(),
            "acting_slack_user_id": str(user_id or "").strip(),
            "slack_channel_id": str(channel_id or "").strip(),
            "slack_thread_ts": str(thread_ts or current_message_ts or "").strip(),
            "event_id": str(
                event_id or current_message_ts or thread_ts or ""
            ).strip(),
        }
        filters = self._victor_ai_filters(text, params)
        timeout = float(settings.VICTOR_AI_BACKEND_TIMEOUT_SECONDS)
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            victor_ai_signing_secret=settings.VICTOR_AI_ROO_SIGNING_SECRET,
            victor_ai_actor_context=actor_context,
        )
        try:
            if action == "summary":
                payload = await client.get_victor_application_summary(
                    filters=filters,
                    timeout=timeout,
                )
                return {
                    "message": self._format_victor_ai_summary(payload),
                    "data": {
                        "victor_ai": True,
                        "action": action,
                        "complete_count": payload.get("complete_count"),
                        "lead_count": payload.get("lead_count"),
                    },
                }

            if action == "list":
                limit = self._bounded_int(
                    params.get("limit"),
                    default=10,
                    minimum=1,
                    maximum=10,
                )
                offset = self._bounded_int(
                    params.get("offset"),
                    default=0,
                    minimum=0,
                    maximum=100000,
                )
                payload = await client.list_victor_applications(
                    filters=filters,
                    limit=limit,
                    offset=offset,
                    timeout=timeout,
                )
                return {
                    "message": self._format_victor_ai_list(payload),
                    "data": {
                        "victor_ai": True,
                        "action": action,
                        "total_count": payload.get("total_count"),
                        "returned_count": payload.get("returned_count"),
                        "offset": payload.get("offset"),
                        "has_more": payload.get("has_more"),
                    },
                }

            if action == "detail":
                application_id = self._victor_ai_application_id(text, params)
                if application_id is None:
                    return {
                        "message": (
                            "Which application should I open? Give me its numeric ID, "
                            "for example: `@Roo show Victor application 123`."
                        ),
                        "data": {"victor_ai": True, "action": action},
                    }
                payload = await client.get_victor_application(
                    application_id,
                    timeout=timeout,
                )
                return {
                    "message": self._format_victor_ai_detail(payload),
                    "data": {
                        "victor_ai": True,
                        "action": action,
                        "application_id": application_id,
                    },
                }

            csv_content, filename = await client.export_victor_applications_csv(
                filters=filters,
                timeout=timeout,
            )
            response = await asyncio.to_thread(
                upload_file,
                channel=channel_id,
                content=csv_content,
                filename=filename,
                title="Victor AI applications export",
                thread_ts=thread_ts,
            )
            if not response or not response.get("ok"):
                raise RuntimeError("Slack rejected the Victor AI CSV upload")
            return {
                "message": f"Done — I uploaded `{self._slack_safe(filename)}` to this thread.",
                "data": {
                    "victor_ai": True,
                    "action": "export_csv",
                    "filename": filename,
                },
            }
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            print(
                "VICTOR_AI_REQUEST_FAILED "
                f"status={status_code} action={action} user_id={user_id} channel_id={channel_id}"
            )
            if status_code in {401, 403}:
                message = VICTOR_AI_ACCESS_UNAVAILABLE_MESSAGE
            elif status_code == 404 and action == "detail":
                message = "I couldn't find a Victor AI application with that ID."
            elif status_code in {400, 413}:
                message = self._victor_ai_error_detail(exc) or (
                    "That request needs narrower or valid filters."
                )
            else:
                message = "I couldn't retrieve Victor applications just now. Try again in a tick."
            return {
                "message": message,
                "data": {"victor_ai": True, "action": action},
            }
        except (MLAIBackendUnavailableError, ValueError) as exc:
            print(
                "VICTOR_AI_UNAVAILABLE "
                f"error={exc.__class__.__name__} action={action} channel_id={channel_id}"
            )
            return {
                "message": "I couldn't retrieve Victor applications just now. Try again in a tick.",
                "data": {"victor_ai": True, "action": action},
            }
        except Exception as exc:
            print(
                "VICTOR_AI_FAILED "
                f"error={exc.__class__.__name__} action={action} channel_id={channel_id}"
            )
            return {
                "message": "I retrieved the data but couldn't finish that Slack response. Try again in a tick.",
                "data": {"victor_ai": True, "action": action},
            }

    @staticmethod
    def _resolve_victor_ai_action(text: str, params: dict) -> str:
        declared = str(params.get("action") or "").strip().lower()
        if declared in {"help", "summary", "list", "detail", "export_csv"}:
            return declared
        lowered = str(text or "").lower()
        if re.search(r"\b(csv|download|export)\b", lowered):
            return "export_csv"
        if re.search(r"\b(help|options|commands|what can i ask|how do i ask)\b", lowered):
            return "help"
        if re.search(r"\b(detail|full details?|in[- ]depth|open application)\b", lowered):
            return "detail"
        if re.search(r"\b(how many|count|summary|report|breakdown)\b", lowered):
            return "summary"
        if re.search(r"\b(list|show|latest|recent|find|search)\b", lowered):
            return "list"
        return "help"

    @staticmethod
    def _victor_ai_filters(text: str, params: dict) -> dict:
        filters = {}
        for key in (
            "stage",
            "role",
            "startup_stage",
            "industry_sector",
            "created_after",
            "created_before",
        ):
            value = params.get(key)
            if value not in (None, ""):
                filters[key] = str(value).strip()
        query = params.get("query") or params.get("q")
        if query not in (None, ""):
            filters["q"] = str(query).strip()
        lowered = str(text or "").lower()
        if "stage" not in filters:
            if re.search(r"\b(partial|lead|incomplete) applications?\b", lowered):
                filters["stage"] = "lead"
            elif re.search(r"\b(complete|completed) applications?\b", lowered):
                filters["stage"] = "complete"
        return filters

    @staticmethod
    def _victor_ai_application_id(text: str, params: dict) -> Optional[int]:
        raw = params.get("application_id")
        if raw in (None, ""):
            match = re.search(
                r"\b(?:application|app)\s*#?\s*(\d+)\b",
                str(text or ""),
                re.I,
            )
            raw = match.group(1) if match else None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _slack_safe(value: Any, *, empty: str = "—", maximum: int = 500) -> str:
        rendered = " ".join(str(value if value is not None else "").split())
        if not rendered:
            return empty
        rendered = rendered[:maximum]
        return rendered.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @classmethod
    def _victor_ai_help_message(cls) -> str:
        return (
            "*Victor AI application reports I can provide*\n"
            "• Summary: `@Roo how many Victor AI applications do we have?`\n"
            "• List: `@Roo list the latest Victor AI applications`\n"
            "• Full record: `@Roo show Victor application 123`\n"
            "• CSV: `@Roo export complete Victor applications to CSV`\n"
            "• Filters: stage (`complete` or `lead`), role, startup stage, industry, "
            "created-date range, or applicant/team search.\n"
            "I can only read and present the data; I can't edit application records."
        )

    @classmethod
    def _format_victor_ai_summary(cls, payload: dict) -> str:
        complete = int(payload.get("complete_count") or 0)
        leads = int(payload.get("lead_count") or 0)
        today = int(payload.get("complete_created_today") or 0)
        last_week = int(payload.get("complete_created_last_7_days") or 0)
        lines = [
            f"*Victor AI applications: {complete} complete*",
            f"{leads} partial lead{'s' if leads != 1 else ''} · {today} completed today · {last_week} in the last 7 days",
        ]
        breakdowns = payload.get("breakdowns") or {}
        for key, label in (
            ("startup_stage", "Startup stages"),
            ("industry_sector", "Industries"),
        ):
            rows = breakdowns.get(key) or []
            if rows:
                rendered = ", ".join(
                    f"{cls._slack_safe(row.get('value'), maximum=80)} ({int(row.get('count') or 0)})"
                    for row in rows[:5]
                )
                lines.append(f"*{label}:* {rendered}")
        if payload.get("filters"):
            lines.append("_This summary reflects the filters in your request._")
        return "\n".join(lines)

    @classmethod
    def _format_victor_ai_list(cls, payload: dict) -> str:
        applications = payload.get("applications") or []
        total = int(payload.get("total_count") or 0)
        offset = int(payload.get("offset") or 0)
        if not applications:
            return "I couldn't find any Victor AI applications matching those filters."
        start = offset + 1
        end = offset + len(applications)
        lines = [f"*Victor AI applications {start}–{end} of {total}*"]
        for application in applications:
            app_id = int(application.get("id") or 0)
            name = " ".join(
                value
                for value in (
                    cls._slack_safe(
                        application.get("first_name"),
                        empty="",
                        maximum=80,
                    ),
                    cls._slack_safe(
                        application.get("last_name"),
                        empty="",
                        maximum=80,
                    ),
                )
                if value
            ) or "Unnamed applicant"
            lines.extend(
                [
                    f"\n*#{app_id} — {name}* · {cls._slack_safe(application.get('team_name'), maximum=120)}",
                    f"{cls._slack_safe(application.get('email'), maximum=160)} · {cls._slack_safe(application.get('stage'), maximum=40)} · {cls._slack_safe(application.get('role'), maximum=100)}",
                    f"{cls._slack_safe(application.get('startup_stage'), maximum=100)} · {cls._slack_safe(application.get('industry_sector'), maximum=120)} · team {cls._slack_safe(application.get('team_size'), maximum=20)} · {cls._slack_safe(application.get('created_at'), maximum=60)}",
                ]
            )
        if payload.get("has_more"):
            lines.append(
                f"\n_More are available. Ask `@Roo list Victor applications from offset {end}`._"
            )
        lines.append("_For one full record, ask `@Roo show Victor application <ID>`._")
        return "\n".join(lines)

    @classmethod
    def _format_victor_ai_detail(cls, application: dict) -> str:
        app_id = int(application.get("id") or 0)
        name = " ".join(
            value
            for value in (
                cls._slack_safe(
                    application.get("first_name"),
                    empty="",
                    maximum=80,
                ),
                cls._slack_safe(
                    application.get("last_name"),
                    empty="",
                    maximum=80,
                ),
            )
            if value
        ) or "Unnamed applicant"
        lines = [
            f"*Victor AI application #{app_id} — {name}*",
            f"*Status:* {cls._slack_safe(application.get('stage'))}",
            f"*Contact:* {cls._slack_safe(application.get('email'))} · {cls._slack_safe(application.get('linkedin'))}",
            f"*Team:* {cls._slack_safe(application.get('team_name'))} · {cls._slack_safe(application.get('role'))} · size {cls._slack_safe(application.get('team_size'))}",
            f"*Startup:* {cls._slack_safe(application.get('startup_stage'))} · {cls._slack_safe(application.get('industry_sector'))} · {cls._slack_safe(application.get('location'))}",
            f"*Idea:* {cls._slack_safe(application.get('idea'), maximum=1000)}",
            f"*Support requested:* {cls._slack_safe(application.get('support'), maximum=1000)}",
        ]
        members = application.get("team_members") or []
        if members:
            lines.append("*Other team members:*")
            for member in members[:49]:
                member_name = " ".join(
                    value
                    for value in (
                        cls._slack_safe(
                            member.get("first_name"),
                            empty="",
                            maximum=80,
                        ),
                        cls._slack_safe(
                            member.get("last_name"),
                            empty="",
                            maximum=80,
                        ),
                    )
                    if value
                ) or "Unnamed"
                lines.append(
                    f"• {member_name} · {cls._slack_safe(member.get('role'), maximum=80)} · {cls._slack_safe(member.get('email'), maximum=160)}"
                )
        revenue = application.get("revenue_last_3_months") or {}
        if revenue:
            rendered_revenue = ", ".join(
                f"{cls._slack_safe(month, maximum=20)}: {cls._slack_safe(amount, maximum=40)}"
                for month, amount in sorted(revenue.items())
            )
            lines.append(f"*Revenue (last 3 months):* {rendered_revenue}")
        consent = "Yes" if application.get("consent") else "No"
        lines.append(
            f"*Consent:* {consent} · *Created:* {cls._slack_safe(application.get('created_at'), maximum=60)} · *Updated:* {cls._slack_safe(application.get('updated_at'), maximum=60)}"
        )
        return "\n".join(lines)

    @staticmethod
    def _victor_ai_error_detail(exc: httpx.HTTPStatusError) -> Optional[str]:
        try:
            payload = exc.response.json()
        except ValueError:
            return None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str):
            return detail[:500]
        if isinstance(payload, dict):
            return "; ".join(
                f"{key}: {value}"
                for key, value in payload.items()
            )[:500]
        return None


    def _should_prompt_for_article_direction(self, text: str, params: dict) -> bool:
        """Prompt for topic-vs-research only on generic article requests."""
        if (params.get("topic") or "").strip():
            return False

        text_lower = text.lower()
        explicit_research_phrases = (
            "research the best article",
            "discover the best article",
            "find the best article",
            "best article to write",
            "best article for me",
            "what should i write about",
            "recommend a topic",
            "suggest a topic",
            "suggest an article topic",
            "article topic idea",
            "auto write",
            "auto-write",
        )
        if any(phrase in text_lower for phrase in explicit_research_phrases):
            return False

        research_verbs = ("research", "discover", "find", "suggest", "recommend", "choose", "pick")
        research_nouns = ("topic", "keyword", "article", "blog post", "idea")
        if any(verb in text_lower for verb in research_verbs) and any(noun in text_lower for noun in research_nouns):
            return False

        return True

    def _build_article_direction_blocks(
        self,
        domain: str,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        client_request_id: str,
        delivery_mode: Optional[str] = None,
        delivery_mode_confirmed: bool = False,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> list[dict]:
        """Build the preflight prompt for generic article requests."""
        normalized_domain = normalize_content_factory_domain(domain) or domain
        cost_points = get_content_factory_article_cost_points(domain)
        cost_text = (
            f"*Cost:* Articles for *{normalized_domain}* are free. No Roo points will be deducted for this run."
            if cost_points == 0
            else f"*Cost:* Starting the article run deducts {cost_points} Roo points."
        )
        button_payload = build_content_factory_identity_payload(
            requested_by_slack_user_id=requested_by_slack_user_id or user_id,
            effective_slack_user_id=effective_slack_user_id or user_id,
            domain=domain,
            channel_id=channel_id,
            thread_ts=thread_ts,
            client_request_id=client_request_id,
        )
        if delivery_mode is not None:
            button_payload["delivery_mode"] = delivery_mode
            button_payload["delivery_mode_confirmed"] = delivery_mode_confirmed
        button_value = json.dumps(button_payload)

        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Before I start on *{domain}*, do you already have a topic in mind?\n\n"
                        "If you do, send it through and I'll still research the best keywords, title, "
                        "and talking points so the article has the best chance to rank.\n\n"
                        "If you don't, I can research the strongest article opportunity for you.\n\n"
                        f"{cost_text}"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "article_direction_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Research the Best Article for Me",
                            "emoji": True,
                        },
                        "style": "primary",
                        "value": button_value,
                        "action_id": "article_research_best",
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "I'll Give You the Topic",
                            "emoji": True,
                        },
                        "value": button_value,
                        "action_id": "article_provide_topic",
                    },
                ],
            },
        ]

    def _resolve_requested_article_delivery_mode(
        self,
        text: str,
        params: dict,
    ) -> tuple[Optional[str], bool]:
        raw_mode = str(params.get("delivery_mode") or "").strip().lower()
        if raw_mode in {"content_only", "publish_code"}:
            confirmed = params.get("delivery_mode_confirmed")
            return raw_mode, bool(True if confirmed is None else confirmed)

        lowered = text.lower()
        content_only_phrases = (
            "content-only",
            "content only",
            "just the copy",
            "manual upload",
            "copy only",
            "text only",
        )
        if any(phrase in lowered for phrase in content_only_phrases):
            return "content_only", True

        publish_phrases = (
            "publish code",
            "publish-mode",
            "publish mode",
            "open a pr",
            "create a pr",
            "draft pr",
            "push it to the repo",
            "push this to the repo",
        )
        if any(phrase in lowered for phrase in publish_phrases):
            return "publish_code", True

        return None, False

    def _build_article_delivery_mode_prompt(
        self,
        *,
        domain: str,
        job_id: str,
        topic: Optional[str],
        recommended_delivery_mode: Optional[str],
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        topic_line = f"*Topic:* {topic}\n" if topic else ""
        recommended_line = ""
        if recommended_delivery_mode == "content_only":
            recommended_line = "\n_Recommended for this domain right now: content-only._"
        elif recommended_delivery_mode == "publish_code":
            recommended_line = "\n_Recommended for this domain right now: publish via code._"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Choose how you want me to deliver the article for *{domain}*.\n\n"
                        f"{topic_line}"
                        "• *Content-only*: research and write the article package for manual upload.\n"
                        "• *Publish via code*: create the draft through the connected repo flow."
                        f"{recommended_line}"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "article_delivery_mode_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Content Only", "emoji": True},
                        "style": "primary" if recommended_delivery_mode != "publish_code" else None,
                        "value": json.dumps(
                            build_content_factory_identity_payload(
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
                        "style": "primary" if recommended_delivery_mode == "publish_code" else None,
                        "value": json.dumps(
                            build_content_factory_identity_payload(
                                requested_by_slack_user_id=requested_by_slack_user_id,
                                effective_slack_user_id=effective_slack_user_id,
                                job_id=job_id,
                                domain=domain,
                                delivery_mode="publish_code",
                            )
                        ),
                        "action_id": "select_article_delivery_mode",
                    },
                ],
            },
        ]
        for element in blocks[1]["elements"]:
            if element.get("style") is None:
                element.pop("style", None)

        return {
            "message": f"Choose the delivery mode for {domain} before I continue.",
            "blocks": blocks,
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": False,
                "content_factory_watchdog_mode": "awaiting_delivery_mode",
                "content_factory_domain": domain,
                "content_factory_workflow": "awaiting_delivery_mode",
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }

    def _is_explicit_scan_request(self, text: str, params: dict) -> bool:
        """Return True when the user is explicitly asking Roo to scan a repo/codebase."""
        return is_explicit_scan_request(text, params.get("action"))

    def _build_existing_scan_confirmation(
        self,
        *,
        domain: Optional[str],
        repo_name: Optional[str],
        last_scanned: Optional[str],
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Prompt the user before triggering a fresh scan when one already exists."""
        target = f"*{domain}*" if domain else f"`{repo_name or 'this repository'}`"
        repo_line = f"\n• Repository: `{repo_name}`" if repo_name else ""
        last_scanned_display = last_scanned or "Unknown"

        return {
            "message": (
                f"I already have a scan for {domain or repo_name or 'this codebase'}. "
                f"Last scanned: {last_scanned_display}. "
                "Do you want me to run another scan anyway?"
            ),
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"I've already scanned {target}.{repo_line}\n"
                            f"• Last scanned: {last_scanned_display}\n\n"
                            "Do you want me to run another scan anyway?"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": "repeat_scan_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Scan Again",
                                "emoji": True,
                            },
                            "style": "primary",
                            "value": json.dumps(
                                build_content_factory_identity_payload(
                                    requested_by_slack_user_id=requested_by_slack_user_id or user_id,
                                    effective_slack_user_id=effective_slack_user_id or user_id,
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    rescan=True,
                                )
                            ),
                            "action_id": "prerequisite_scan",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Keep Existing Scan",
                                "emoji": True,
                            },
                            "value": json.dumps(
                                build_content_factory_identity_payload(
                                    requested_by_slack_user_id=requested_by_slack_user_id or user_id,
                                    effective_slack_user_id=effective_slack_user_id or user_id,
                                    domain=domain,
                                )
                            ),
                            "action_id": "prerequisite_cancel",
                        },
                    ],
                },
            ],
        }

    def _build_github_reconnect_blocks(
        self,
        message: str,
        auth_url: str,
        *,
        button_label: str,
        include_resume: bool = False,
        resume_action: str = "resume_scan",
        resume_value: Optional[dict] = None,
    ) -> list[dict]:
        elements = [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": button_label,
                    "emoji": True,
                },
                "url": auth_url,
                "action_id": "connect_github",
                "style": "primary",
            }
        ]
        if include_resume:
            elements.append(
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "I've Connected - Retry",
                        "emoji": True,
                    },
                    "action_id": resume_action,
                    "value": json.dumps(resume_value or {}),
                    "style": "primary",
                }
            )
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message,
                },
            },
            {
                "type": "actions",
                "elements": elements,
            },
        ]

    @staticmethod
    def _content_factory_backend_unavailable_message() -> str:
        return (
            "I couldn't reach MLAI backend just now, so I haven't started the Content Factory flow. "
            "Please try again in a moment."
        )

    @staticmethod
    def _points_backend_unavailable_message(action: Optional[str] = None) -> str:
        if action == "book_coworking":
            return (
                "I couldn't confirm whether your coworking booking went through because MLAI backend timed out. "
                "Please retry the same booking in a moment. I won't double-book the same day."
            )

        return (
            "I couldn't reach the MLAI points backend just now, so I couldn't confirm that action. "
            "Please try again in a moment."
        )

    @staticmethod
    def _coworking_booking_queued_message(booking_date: str) -> str:
        return (
            f"I got your coworking booking request for **{booking_date}**, but MLAI backend "
            "didn't confirm it yet. I've queued it and will keep retrying automatically. "
            "I won't double-book the same day."
        )

    @staticmethod
    def _already_posted_response(message: str, *, blocks: Optional[list] = None) -> dict:
        response = {
            "message": message,
            "suppress_post": True,
        }
        if blocks is not None:
            response["blocks"] = blocks
        return response

    @staticmethod
    def _delegated_backend_kwargs(
        requested_by_slack_user_id: Optional[str],
        effective_slack_user_id: Optional[str],
    ) -> dict[str, str]:
        if is_delegated_content_factory_request(
            requested_by_slack_user_id,
            effective_slack_user_id,
        ):
            return {
                "requested_by_slack_user_id": requested_by_slack_user_id,
            }
        return {}

    @staticmethod
    def _delegated_content_factory_auth_required_message(
        *,
        effective_slack_user_id: Optional[str],
        domain: Optional[str],
    ) -> str:
        target = f"<@{effective_slack_user_id}>" if effective_slack_user_id else "that user"
        domain_label = normalize_content_factory_domain(domain) or domain or "this domain"
        return (
            f"GitHub auth for {target} isn't available for {domain_label}. "
            "Ask them to reconnect GitHub, then retry the delegated run."
        )

    async def _request_github_reconnect(
        self,
        api_client,
        *,
        user_id: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
        domain: Optional[str],
        github_repo: Optional[str],
        trigger: str,
        pending_action: Optional[str],
        channel_id: Optional[str],
        thread_ts: Optional[str],
        button_label: str,
        text: Optional[str] = None,
        params: Optional[dict] = None,
        save_pending: bool = False,
        include_resume: bool = False,
        resume_action: str = "resume_scan",
        resume_value: Optional[dict] = None,
    ) -> Optional[Any]:
        from ..clients.mlai_backend import MLAIBackendUnavailableError

        delegated_effective_slack_user_id = str(
            effective_slack_user_id or user_id or ""
        ).strip()
        delegated_requested_by_slack_user_id = str(
            requested_by_slack_user_id or user_id or ""
        ).strip()
        is_delegated = is_delegated_content_factory_request(
            delegated_requested_by_slack_user_id,
            delegated_effective_slack_user_id,
        )

        try:
            reconnect = await api_client.reconnect_content_factory_github(
                slack_user_id=user_id,
                domain=domain,
                github_repo=github_repo,
                trigger=trigger,
                pending_action=pending_action,
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        if reconnect.get("status") == "already_connected":
            return None

        if reconnect.get("status") != "auth_started":
            return None

        if is_delegated:
            return self._delegated_content_factory_auth_required_message(
                effective_slack_user_id=delegated_effective_slack_user_id,
                domain=domain,
            )

        if save_pending and text is not None and params is not None:
            await self._save_content_factory_pending_intent(
                api_client,
                user_id,
                params,
                text,
                channel_id,
                thread_ts,
            )

        auth_url = reconnect.get("auth_url")
        message = reconnect.get("message") or "Reconnect GitHub before I continue."
        if not auth_url:
            return message

        resolved_resume_value = None
        if include_resume:
            resolved_resume_value = build_content_factory_identity_payload(
                requested_by_slack_user_id=delegated_requested_by_slack_user_id,
                effective_slack_user_id=delegated_effective_slack_user_id,
                **(resume_value or {}),
            )

        blocks = self._build_github_reconnect_blocks(
            message,
            auth_url,
            button_label=button_label,
            include_resume=include_resume,
            resume_action=resume_action,
            resume_value=resolved_resume_value,
        )
        short_message = (
            f"{message} Use the button above to continue."
            if channel_id
            else f"{message}\n\n{auth_url}"
        )
        if channel_id:
            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
            return self._already_posted_response(short_message, blocks=blocks)
        return {
            "message": short_message,
            "blocks": blocks,
        }
    
    async def _execute_tone_of_voice(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str
    ) -> str:
        """Execute the tone-of-voice skill with a dedicated prompt structure."""

        # Extract the user's original text to rewrite
        # Strip common prefixes so the LLM gets just the raw content
        raw_text = params.get("text", text)

        system_prompt = skill.content

        user_prompt = f"""Here is the original text to rewrite. Follow these steps:

1. First, identify the key points and core message in the original text.
2. Then COMPLETELY rewrite it from scratch using the MLAI tone of voice described in your instructions. Do not lightly edit or rephrase. Write it fresh as if you were the MLAI writer producing this content for the first time.
3. Before returning your result, review it against these HARD RULES and fix any violations:
   - ZERO em dash characters (\u2014) or en dash characters (\u2013). Use a comma, period, or hyphen instead.
   - ZERO emoji characters of any kind.
   - ZERO corporate filler language.
   - Short paragraphs, punchy lines, high specificity.

Return ONLY the final rewritten text. No preamble, no explanation.

Original text:
{raw_text}"""

        # Use GPT-5.2 with thinking mode for higher quality tone rewrites
        openai_client = get_llm_client("openai")
        response = await openai_client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            model="gpt-5.2",
            max_tokens=8192,
            reasoning_effort="high"
        )

        return response.content

    async def _ensure_user_exists(self, user_id: str) -> None:
        """
        Ensure a user exists in the mlai-backend database.
        Uses the /api/v1/users/slack-user/ endpoint which handles both
        new user creation and returning existing users.

        This prevents errors when new users interact with backend-dependent features.
        """
        from ..clients.mlai_backend import MLAIBackendClient
        from ..slack_client import get_user_info

        try:
            backend = MLAIBackendClient()

            # Get Slack user profile
            slack_info = get_user_info(user_id)

            # Email is required by the backend
            email = slack_info.get("email")
            if not email:
                # If no email in Slack profile, generate a fallback
                email = f"{user_id}@slack.generated"
                print(f"⚠️ No email found for {user_id}, using generated: {email}")

            # Parse first_name and last_name from real_name
            real_name = slack_info.get("real_name", "")
            name_parts = real_name.split(" ", 1) if real_name else []
            first_name = name_parts[0] if len(name_parts) > 0 else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Get avatar URL (192x192 size is good for profiles)
            # Note: Slack user info structure varies, handle both formats
            avatar_url = slack_info.get("image_192")  # Direct from our get_user_info

            # Register/fetch user using the new endpoint
            # This endpoint returns existing users or creates new ones
            result = await backend.ensure_slack_user_registered(
                slack_id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url
            )

            if result.get("created"):
                print(f"✅ Created new user: {email} (Slack ID: {user_id})")
            else:
                if result.get("linked"):
                    print(f"✅ Linked Slack ID {user_id} to existing user: {email}")
                # Silently pass for existing users (no need to log on every message)

        except Exception as e:
            # Don't fail the request if user registration fails
            print(f"⚠️ Failed to register user {user_id}: {e}")

    async def _execute_watt_the_hack(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
    ) -> str:
        """Execute the Watt The Hack skill: publish announcements to the site.

        Authorisation is delegated to the backend — only Slack users who map to
        an MLAI Django superuser may publish. Announcements are authored by Roo
        (the bot identity), matching the HealthHack behaviour.
        """
        import json
        import re

        # Channel restriction: this skill only operates in the Watt channel.
        if skill.exclusive_channels and channel_id:
            from ..slack_client import get_channel_name
            channel_name = get_channel_name(channel_id)
            if channel_name and channel_name not in skill.exclusive_channels:
                channels_list = ", ".join(f"#*{ch}*" for ch in skill.exclusive_channels)
                return f"The Watt The Hack skill is only available in {channels_list}."

        text_lower = text.lower()
        # The router's validated action wins; keyword sniffing only covers
        # callers that did not route (e.g. direct invocations without params).
        requested_action = str(params.get("action") or "").strip().lower()
        if requested_action in ("announce", "event_qa"):
            is_announcement = requested_action == "announce"
        else:
            announce_keywords = ["announce", "announcement", "post announcement", "create announcement"]
            is_announcement = any(k in text_lower for k in announce_keywords)

        if not is_announcement:
            # Event Q&A: answer questions about Watt The Hack from the skill's
            # knowledge base. Falls back to the generic skill content only if the
            # knowledge file is missing.
            knowledge = ""
            try:
                knowledge_file = skill.path / "knowledge.md"
                if knowledge_file.exists():
                    knowledge = knowledge_file.read_text(encoding="utf-8")
            except Exception as e:
                print(f"⚠️ Watt The Hack: failed to load knowledge.md: {e}")

            if not knowledge:
                return await self._execute_with_llm(skill, text, params, user_id, thread_history)

            qa_system_prompt = (
                "You are Roo, the friendly assistant for the Watt The Hack hackathon, "
                "answering questions in the #watt-the-hack Slack channel.\n\n"
                "Answer the user's question using ONLY the Watt The Hack information below. "
                "Be concise, warm and helpful, and use simple Slack-friendly formatting. "
                "If the answer is not in the information, say you're not sure and suggest they "
                "check watt-the-hack.com or ask an organiser — do NOT invent facts, dates, "
                "prizes, names, times or venues.\n\n"
                "=== WATT THE HACK INFORMATION ===\n"
                f"{knowledge}\n"
                "=== END INFORMATION ==="
            )
            openai_client = get_llm_client("openai")
            qa_response = await openai_client.chat([
                {"role": "system", "content": qa_system_prompt},
                {"role": "user", "content": text},
            ], model="gpt-4o-mini", max_tokens=700)
            return qa_response.content.strip()

        # Prefer title/body the router already extracted from the message.
        ann_title = str(params.get("title") or "").strip() or None
        ann_body = str(params.get("body") or "").strip() or None
        if ann_title and ann_body:
            return await self._publish_watt_announcement(user_id, ann_title, ann_body)

        # Use an LLM to extract a clear title and body from the request.
        extract_prompt = f"""Extract the announcement title and body from this message.
The user wants to create an announcement for the Watt The Hack hackathon website.

User message: "{text}"

Return ONLY valid JSON with two keys: "title" and "body".
If you cannot determine a clear title or body from the message, set the missing field to null.

Example: {{"title": "Lunch is served", "body": "Pizza is in the atrium at 1pm."}}

JSON:"""
        openai_client = get_llm_client("openai")
        extract_response = await openai_client.chat([
            {"role": "system", "content": "You extract structured data from text. Return valid JSON only."},
            {"role": "user", "content": extract_prompt}
        ], model="gpt-4o-mini", max_tokens=1024)

        try:
            content = extract_response.content.strip()
            if content.startswith("```"):
                content = re.sub(r'^```\w*\n?', '', content)
                content = re.sub(r'\n?```$', '', content)
            extracted = json.loads(content)
        except json.JSONDecodeError:
            extracted = {}

        ann_title = ann_title or extracted.get("title")
        ann_body = ann_body or extracted.get("body")

        if not ann_title or not ann_body:
            return (
                f"<@{user_id}> I need both a *title* and *body* for the announcement. "
                f"Try something like:\n"
                f"_\"Post an announcement titled 'Lunch is served' with body 'Pizza is in the atrium at 1pm.'\"_"
            )

        return await self._publish_watt_announcement(user_id, ann_title, ann_body)

    async def _publish_watt_announcement(self, user_id: str, ann_title: str, ann_body: str) -> str:
        """Publish a Watt The Hack announcement via the backend.

        The requesting human (user_id) is checked against Django superusers;
        authorship is attributed to Roo's bot id so the website shows Roo as
        the author.
        """
        from ..clients.mlai_backend import MLAIBackendClient
        from ..slack_client import get_bot_user_id
        backend = MLAIBackendClient()
        bot_id = get_bot_user_id()
        result = await backend.generic_hackathon_create_announcement(
            slug="watt-the-hack",
            title=ann_title,
            body=ann_body,
            requester_slack_id=user_id,
            author_slack_id=bot_id,
        )

        if result is None:
            return f"<@{user_id}> Something went wrong creating the announcement. Please try again later."

        status_code = result.get("status_code")
        if status_code == 400:
            return f"<@{user_id}> The announcement couldn't be created — {result.get('detail', 'something is missing')}."
        if status_code in (401, 403):
            return (
                f"<@{user_id}> Sorry, only MLAI superusers can post announcements "
                f"to the Watt The Hack site."
            )
        if status_code is not None:
            return f"<@{user_id}> Unexpected error (HTTP {status_code}): {result.get('detail', 'unknown')}"

        # Success — the framework posts this confirmation back in-thread.
        return f"Announcement *\"{ann_title}\"* has been posted to the Watt The Hack website."

    async def _execute_healthhack(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        current_message_ts: Optional[str] = None,
    ) -> str:
        """Publish an announcement from #healthhack to the participant app."""
        import json
        import re

        if skill.exclusive_channels:
            from ..slack_client import get_channel_name

            channel_name = get_channel_name(channel_id) if channel_id else None
            if channel_name not in skill.exclusive_channels:
                return "The HealthHack announcement skill is only available in #*healthhack*."

        ann_title = str(params.get("title") or "").strip() or None
        ann_body = str(params.get("body") or "").strip() or None

        if not ann_title or not ann_body:
            extract_prompt = f"""Extract the announcement title and body from this message.
The user wants to publish an announcement to the HealthHack participant app.

User message: "{text}"

Return ONLY valid JSON with two keys: "title" and "body".
If you cannot determine a clear title or body, set the missing field to null.

Example: {{"title": "Lunch is served", "body": "Pizza is in the atrium at 1pm."}}

JSON:"""
            openai_client = get_llm_client("openai")
            extract_response = await openai_client.chat(
                [
                    {
                        "role": "system",
                        "content": "You extract structured data from text. Return valid JSON only.",
                    },
                    {"role": "user", "content": extract_prompt},
                ],
                model="gpt-4o-mini",
                max_tokens=1024,
            )

            try:
                content = extract_response.content.strip()
                if content.startswith("```"):
                    content = re.sub(r'^```\w*\n?', '', content)
                    content = re.sub(r'\n?```$', '', content)
                extracted = json.loads(content)
            except (json.JSONDecodeError, TypeError, AttributeError):
                extracted = {}

            ann_title = ann_title or extracted.get("title")
            ann_body = ann_body or extracted.get("body")

        if not ann_title or not ann_body:
            return (
                f"<@{user_id}> I need both a *title* and *body* for the announcement. "
                "Try: _\"Post an announcement titled 'Lunch is served' with body "
                "'Pizza is in the atrium at 1pm.'\"_"
            )

        if not channel_id or not current_message_ts:
            return (
                f"<@{user_id}> I couldn't identify the source Slack message, so I didn't "
                "publish the announcement. Please try again in #healthhack."
            )

        return await self._publish_healthhack_announcement(
            user_id=user_id,
            ann_title=str(ann_title).strip(),
            ann_body=str(ann_body).strip(),
            channel_id=channel_id,
            source_message_ts=current_message_ts,
        )

    async def _publish_healthhack_announcement(
        self,
        *,
        user_id: str,
        ann_title: str,
        ann_body: str,
        channel_id: str,
        source_message_ts: str,
    ) -> str:
        """Persist one backend-authorised HealthHack announcement."""
        from ..clients.mlai_backend import MLAIBackendClient
        from ..slack_client import get_bot_user_id

        backend = MLAIBackendClient()
        result = await backend.healthhack_create_announcement(
            title=ann_title,
            body=ann_body,
            requester_slack_id=user_id,
            author_slack_id=get_bot_user_id(),
            source_channel_id=channel_id,
            source_message_ts=source_message_ts,
        )

        if result is None:
            return f"<@{user_id}> Something went wrong creating the announcement. Please try again later."

        status_code = result.get("status_code")
        if status_code == 400:
            return f"<@{user_id}> The announcement couldn't be created — {result.get('detail', 'something is missing')}."
        if status_code == 409:
            return (
                f"<@{user_id}> That Slack message is already linked to a different "
                "HealthHack announcement, so I didn't overwrite it."
            )
        if status_code in (401, 403):
            return (
                f"<@{user_id}> Sorry, only authorised HealthHack organisers can "
                "publish announcements to the participant app."
            )
        if status_code is not None:
            return f"<@{user_id}> Unexpected error (HTTP {status_code}): {result.get('detail', 'unknown')}"

        if result.get("created") is False:
            return f"Announcement *\"{ann_title}\"* was already posted to the HealthHack app."
        return f"Announcement *\"{ann_title}\"* has been posted to the HealthHack app."

    async def _execute_with_llm(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        history: Optional[List[dict]] = None
    ) -> str:
        """Execute the skill using LLM to follow the skill's instructions."""
        
        # Check if skill has vector search action
        has_vector_search = "vector" in skill.content.lower() or "embedding" in skill.content.lower()
        
        context = ""
        # Note: Vector search is disabled until API endpoint is implemented
        # if has_vector_search and params.get("query"):
        #     try:
        #         search_results = await api_client.search_user_expertise(params["query"])
        #         if search_results:
        #             context = f"\n\nVector search results:\n{search_results}"
        #     except Exception as e:
        #         print(f"   Vector search failed: {e}")
        
        prompt = f"""You are Roo, executing the "{skill.name}" skill.

Skill description: {skill.description}

Skill instructions:
{skill.content}

User's original request: "{text}"
Extracted parameters: {params}
Requesting user ID: {user_id}
{context}

Previous Conversation Context (if any):
{history if history else 'None'}

Follow the skill instructions to generate an appropriate response.
Be helpful, friendly, and use casual Australian expressions occasionally.
Keep the response concise but informative."""

        response = await chat([
            {"role": "system", "content": "You are Roo, a friendly AI assistant for the MLAI community."},
            {"role": "user", "content": prompt}
        ])
        
        return response.content

    async def _execute_linear_meeting_actions(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        thread_history: Optional[List[dict]] = None,
        event_files: Optional[List[dict]] = None,
        current_message_ts: Optional[str] = None,
        slack_context: Optional[dict[str, Any]] = None,
    ) -> dict:
        settings = get_settings()
        params = self._apply_linear_meeting_project_hint_prepass(text, params)
        params = self._apply_linear_meeting_owner_hint_prepass(text, params)
        params = self._apply_linear_meeting_default_assignee_prepass(text, params)
        request_context = (slack_context or {}).get("request") or {}
        if request_context:
            params = {
                **params,
                "source_local_datetime": request_context.get("local_datetime"),
                "source_timezone": request_context.get("timezone"),
                "requester_slack_id": request_context.get("user_id") or user_id,
                "requester_display_name": request_context.get("display_name"),
                "requester_email": request_context.get("email"),
            }
        action = str(params.get("action") or "create").strip().lower().replace("-", "_")
        if action == "size_project_issues":
            ClientClass = skill.get_client_class("LinearMeetingActionsClient")
            if ClientClass is None:
                return {"message": "Linear meeting actions are missing their Linear client implementation."}
            return await self._execute_linear_project_issue_sizing(
                text=text,
                params=params,
                user_id=user_id,
                client=ClientClass(),
                settings=settings,
            )
        thread_reference_request = self._is_linear_thread_reference_request(text, params)
        if thread_reference_request and self._linear_request_assigns_to_requester(text):
            params["owner_hint"] = f"<@{user_id}>"
        direct_issue_request = self._is_linear_direct_issue_request(text, params)
        source_result = await self._build_linear_meeting_source_result(
            text=text,
            params=params,
            thread_history=thread_history,
            event_files=event_files,
            settings=settings,
            current_message_ts=current_message_ts,
            exclude_current_message=thread_reference_request,
        )
        transcript = source_result.combined_text()
        # When meeting-note files (PDF/DOCX/text/image) were parsed, always extract
        # action items from those sources. Only treat the message as a one-off "direct
        # issue" command when no document was attached — otherwise a phrase like
        # "add these to linear as tasks" hijacks the command path and the attached file
        # is parsed but never used.
        has_document_sources = source_result.files_parsed > 0
        use_direct_issue_path = (
            direct_issue_request
            and not thread_reference_request
            and not has_document_sources
        )
        if len(transcript.split()) < 8 and not use_direct_issue_path:
            warning_suffix = self._format_linear_meeting_source_warnings(source_result.warnings)
            selection_mode = str(
                ((slack_context or {}).get("selection") or {}).get("mode") or ""
            )
            if thread_reference_request and selection_mode == "recent_channel":
                return {
                    "message": (
                        "I couldn't find enough preceding Slack context to identify the task. "
                        "Reply to the source message in a thread and mention me there, or check "
                        "that Roo has channel-history access."
                        + warning_suffix
                    )
                }
            return {
                "message": (
                    "Paste the meeting transcript or summary in this thread, then ask me to turn it into Linear tasks."
                    + warning_suffix
                )
            }

        ClientClass = skill.get_client_class("LinearMeetingActionsClient")
        if ClientClass is None:
            return {"message": "Linear meeting actions are missing their Linear client implementation."}

        client = ClientClass()
        try:
            teams, users, projects, labels, recent_issues = await asyncio.gather(
                client.list_teams(),
                client.list_users(),
                client.list_active_projects(),
                client.list_issue_labels(),
                client.list_recent_open_issues(),
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return {
                "message": f"I couldn't read Linear context yet: {detail}"
            }

        projects, explicit_project_resolution = (
            await self._resolve_explicit_linear_project_context(
                client=client,
                params=params,
                projects=projects,
            )
        )
        resolution_status = str(
            (explicit_project_resolution or {}).get("status") or ""
        )
        if resolution_status in {"not_found", "ambiguous", "unavailable"}:
            return {
                "message": self._linear_explicit_project_resolution_error_message(
                    str(params.get("project_hint") or ""),
                    explicit_project_resolution or {},
                ),
                "data": {
                    "created_count": 0,
                    "review_count": 0,
                    "skipped_count": 0,
                    "project_resolution_status": resolution_status,
                },
            }

        project_update_requested = self._linear_meeting_project_update_requested(text, params)
        contextual_review_mode = False
        try:
            if use_direct_issue_path:
                candidates = await self._extract_linear_direct_issue_candidates(
                    text=text,
                    params=params,
                    users=users,
                    projects=projects,
                )
            else:
                candidates = await self._extract_linear_meeting_candidates_from_sources(
                    sources=source_result.sources,
                    params=params,
                    users=users,
                    projects=projects,
                )
            if not candidates and thread_reference_request and not project_update_requested:
                contextual_candidate = await self._extract_linear_thread_context_candidate(
                    sources=source_result.sources,
                    params=params,
                    users=users,
                    projects=projects,
                )
                if contextual_candidate:
                    candidates = [contextual_candidate]
                    contextual_review_mode = True
        except (LinearInferenceTimeoutError, LinearMeetingExtractionDeadlineError) as exc:
            print(
                "LINEAR_MEETING_EXTRACTION "
                + json.dumps(
                    {
                        "event": "aborted_before_writes",
                        "error_type": type(exc).__name__,
                        "project_hint": params.get("project_hint"),
                        "files_parsed": source_result.files_parsed,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return {
                "message": (
                    "I timed out while extracting action items from those notes, so I stopped "
                    "before creating any Linear tasks. Nothing was changed. Please try again."
                ),
                "data": {
                    "created_count": 0,
                    "review_count": 0,
                    "skipped_count": 0,
                    "timed_out": True,
                },
            }
        if not candidates and not project_update_requested:
            return {
                "message": (
                    "I couldn't find any concrete action items in those meeting notes."
                    + self._format_linear_meeting_source_warnings(source_result.warnings)
                )
            }

        request_project_match = self._resolve_linear_meeting_request_project(
            sources=source_result.sources,
            candidates=candidates,
            projects=projects,
            explicit_project_hint=params.get("project_hint"),
            channel_id=channel_id,
            channel_context=(slack_context or {}).get("channel"),
        )
        request_project = request_project_match.get("project") or {}
        request_project_hint = (
            str(request_project.get("name") or "").strip()
            if float(request_project_match.get("confidence") or 0.0) >= 0.78
            else ""
        )
        explicit_bulk_create = bool(
            has_document_sources
            and self._linear_meeting_explicit_creation_authorized(text, params)
        )

        auto_threshold = float(
            getattr(settings, "LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE", 0.85) or 0.85
        )
        uncertain_threshold = float(
            getattr(settings, "LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE", 0.65) or 0.65
        )
        contextual_auto_create_enabled = bool(
            getattr(settings, "LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED", True)
        )
        # Resolve the optional fallback assignee once ("if unsure, assign to X").
        # Used to rescue candidates whose own owner can't be confidently matched so
        # they remain assignable instead of being dropped.
        default_assignee_match: Optional[dict[str, Any]] = None
        default_assignee_hint = str(params.get("default_assignee_hint") or "").strip()
        if default_assignee_hint:
            resolved_default = self._match_linear_meeting_owner(default_assignee_hint, users)
            if resolved_default.get("user"):
                default_assignee_match = resolved_default

        created: list[dict[str, Any]] = []
        review_needed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        project_update: Optional[dict[str, Any]] = None
        project_update_error: Optional[str] = None
        base_source = {
            "workspace_id": str((slack_context or {}).get("workspace_id") or ""),
            "channel_id": channel_id,
            "channel_name": str(((slack_context or {}).get("channel") or {}).get("name") or ""),
            "thread_ts": thread_ts,
            "requester_slack_id": user_id,
            "request_message_ts": current_message_ts,
            "event_id": str(((slack_context or {}).get("request") or {}).get("event_id") or ""),
        }

        if project_update_requested:
            if source_result.files_seen and not source_result.files_parsed and len(transcript.split()) < 80:
                project_update_error = "Skipped project update because the attached file content could not be parsed."
            else:
                project_update_match = request_project_match
                project = project_update_match.get("project")
                if not project or float(project_update_match.get("confidence") or 0.0) < uncertain_threshold:
                    project_update_error = "Skipped project update because Roo could not confidently match the Linear project."
                else:
                    try:
                        update_input = await self._build_linear_meeting_project_update_input(
                            sources=source_result.sources,
                            params=params,
                            project=project,
                            candidates=candidates,
                            recent_issues=recent_issues,
                            settings=settings,
                        )
                        project_update = await client.create_project_update(**update_input)
                    except Exception as exc:
                        project_update_error = f"Could not create project update: {exc.__class__.__name__}: {exc}"

        prepared_candidates: list[dict[str, Any]] = []
        for candidate_index, raw_candidate in enumerate(candidates[:20]):
            candidate = self._normalize_linear_meeting_candidate(raw_candidate)
            if raw_candidate.get("contextual_review_only"):
                candidate["contextual_review_only"] = True
            if not candidate.get("title"):
                continue
            if is_terminal_candidate(candidate):
                skipped.append(
                    {
                        "title": candidate["title"],
                        "assignee": "Not applicable",
                        "project": candidate.get("project_hint") or "Unresolved",
                        "team": "Not applicable",
                        "source": candidate.get("source_label") or "Slack thread",
                        "evidence": candidate.get("evidence") or "",
                        "due_date": candidate.get("due_date"),
                        "confidence": candidate.get("confidence") or 0.0,
                        "reason": f"Work is already {candidate.get('work_status') or 'terminal'}",
                    }
                )
                continue

            if params.get("owner_hint") and (use_direct_issue_path or thread_reference_request):
                candidate["owner_hint"] = str(params["owner_hint"])
            candidate = self._normalize_linear_meeting_due_date(candidate, slack_context)
            evidence_message = self._resolve_linear_evidence_message(candidate, slack_context)
            source = {
                **base_source,
                "source_message_ts": str((evidence_message or {}).get("ts") or ""),
                "source_local_datetime": str(
                    (evidence_message or {}).get("local_datetime") or ""
                ),
                "source_permalink": self._linear_source_permalink(
                    channel_id,
                    str((evidence_message or {}).get("ts") or ""),
                ),
            }

            owner_match = self._match_linear_meeting_owner(candidate.get("owner_hint"), users)
            if (
                float(owner_match.get("confidence") or 0.0) < uncertain_threshold
                and default_assignee_match
                and default_assignee_match.get("user")
            ):
                owner_match = {
                    "user": default_assignee_match["user"],
                    "confidence": float(default_assignee_match.get("confidence") or 0.9),
                    "reason": "Fallback assignee",
                }
            effective_project_hint = (
                params.get("project_hint")
                or request_project_hint
                or candidate.get("project_hint")
            )
            project_match = self._match_linear_meeting_project(
                candidate,
                projects,
                owner_match.get("user"),
                effective_project_hint,
                channel_id=channel_id,
                channel_context=(slack_context or {}).get("channel"),
            )
            team_match = self._match_linear_meeting_team(
                project_match.get("project"),
                teams,
                params.get("team_hint") or candidate.get("team_hint"),
                getattr(settings, "LINEAR_DEFAULT_TEAM", None),
            )
            duplicate = self._find_linear_meeting_duplicate(
                candidate,
                recent_issues,
                project_match.get("project"),
            )
            decision, overall_confidence = self._linear_meeting_candidate_decision(
                candidate=candidate,
                owner_match=owner_match,
                project_match=project_match,
                team_match=team_match,
                duplicate=duplicate,
                auto_threshold=auto_threshold,
                uncertain_threshold=uncertain_threshold,
            )
            if candidate.get("contextual_review_only") and decision == "create":
                decision = "review"
            # A document request that explicitly asks Roo to create tasks is itself
            # write authorisation. Keep genuinely inferred discussion work and
            # contextual thread harvesting review-first.
            if not use_direct_issue_path and decision != "duplicate":
                contextual_explicit_create = bool(
                    contextual_auto_create_enabled
                    and thread_reference_request
                    and candidate.get("explicit_commitment")
                    and not candidate.get("contextual_review_only")
                    and decision == "create"
                )
                authorised_bulk_create = bool(
                    explicit_bulk_create
                    and candidate.get("explicit_commitment")
                    and not candidate.get("contextual_review_only")
                    and decision == "create"
                )
                if contextual_explicit_create or authorised_bulk_create:
                    decision = "create"
                elif owner_match.get("user") and team_match.get("team"):
                    decision = "review"
                else:
                    decision = "skip"

            display = self._build_linear_meeting_candidate_display(
                candidate,
                owner_match,
                project_match,
                team_match,
                overall_confidence,
            )

            if decision == "duplicate":
                skipped.append({
                    **display,
                    "reason": "Likely duplicate",
                    "duplicate": duplicate,
                })
                continue

            if decision == "skip":
                skip_reason = self._linear_meeting_skip_reason(
                    candidate,
                    owner_match,
                    project_match,
                    team_match,
                    uncertain_threshold,
                )
                if candidate.get("contextual_review_only"):
                    if float(owner_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Assignee unclear; mention who should own this and I can add it to Linear."
                    elif float(project_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Project unclear; mention the Linear project and I can add it."
                    elif float(team_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Linear team unclear; mention the team and I can add it."
                skipped.append({**display, "reason": skip_reason})
                continue

            candidate_key = (
                f"c{candidate_index}-"
                f"{hashlib.sha256(candidate['title'].encode('utf-8')).hexdigest()[:12]}"
            )
            idempotency_key = self._linear_meeting_idempotency_key(
                candidate=candidate,
                source=source,
                assignee_id=str((owner_match.get("user") or {}).get("id") or ""),
                project_id=str((project_match.get("project") or {}).get("id") or ""),
            )
            prepared_candidates.append(
                {
                    "candidate_key": candidate_key,
                    "idempotency_key": idempotency_key,
                    "candidate": candidate,
                    "owner_match": owner_match,
                    "project_match": project_match,
                    "team_match": team_match,
                    "source": source,
                    "decision": decision,
                    "overall_confidence": overall_confidence,
                    "display": display,
                }
            )

        sizing_shadow = await self._size_linear_studio_prepared_candidates(
            prepared_candidates=prepared_candidates,
            client=client,
            labels=labels,
            settings=settings,
            requester_slack_id=user_id,
        )
        sizing_mode = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_MODE",
                "LINEAR_STUDIO_SIZING_MODE",
                "off",
            )
        ).strip().lower()
        if sizing_mode not in {"off", "shadow", "review", "required"}:
            sizing_mode = "off"
        sizing_auto_threshold = float(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_AUTO_CREATE_MIN_CONFIDENCE",
                "LINEAR_STUDIO_SIZING_AUTO_CREATE_MIN_CONFIDENCE",
                0.75,
            )
            or 0.75
        )

        for prepared in prepared_candidates:
            candidate = prepared["candidate"]
            project = (prepared["project_match"].get("project") or {})
            team = prepared["team_match"].get("team") or {}
            display = prepared["display"]
            decision = prepared["decision"]
            project_candidate = is_project_issue(project)
            replay_issue = prepared.get("receipt_replay_issue")
            if isinstance(replay_issue, dict):
                created.append(
                    {
                        **display,
                        "issue": {**replay_issue, "idempotentReplay": True},
                    }
                )
                continue
            sizing_error = str(prepared.get("sizing_error") or "")
            if project_candidate and sizing_mode in {"review", "required"}:
                if sizing_error or not prepared.get("effort_assessment"):
                    skipped.append(
                        {
                            **display,
                            "reason": (
                                "Effort sizing could not be completed; no unlabeled issue was created. "
                                + (sizing_error or "Rerun the request after the sizing context is available.")
                            )[:700],
                        }
                    )
                    continue
                if sizing_mode == "review":
                    decision = "review"
                else:
                    assessment = prepared["effort_assessment"]
                    if (
                        float(assessment.get("confidence") or 0.0)
                        < sizing_auto_threshold
                        or not assessment.get("context_sufficient")
                    ):
                        decision = "review"

            meeting_action_ids = self._linear_compatible_label_ids(
                labels,
                label_name="meeting-action",
                team_id=str(team.get("id") or ""),
            )
            label_ids = meeting_action_ids[:1]
            if project_candidate and sizing_mode in {"review", "required"}:
                label_ids.append(str(prepared["effort_label_id"]))
            issue_input = self._build_linear_meeting_issue_input(
                candidate=candidate,
                owner_match=prepared["owner_match"],
                project_match=prepared["project_match"],
                team_match=prepared["team_match"],
                label_ids=label_ids,
                source=prepared["source"],
                sizing_metadata=prepared.get("effort_assessment"),
            )

            if decision == "create":
                try:
                    issue = await client.create_issue(**issue_input)
                    created.append({**display, "issue": issue})
                except Exception as exc:
                    review_needed.append(
                        {
                            **display,
                            "issue_input": issue_input,
                            "review_reason": (
                                f"Linear create failed: {exc.__class__.__name__}: {exc}"
                            ),
                        }
                    )
                continue

            review_needed.append(
                {
                    **display,
                    "issue_input": issue_input,
                    "review_reason": "Needs approval",
                }
            )

        review_batch: Optional[dict[str, Any]] = None
        if review_needed:
            source_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "source": base_source,
                        "items": [
                            item.get("issue_input", {}).get("idempotency_key")
                            or item.get("title")
                            for item in review_needed
                        ],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            try:
                review_batch = await client.create_action_batch(
                    requested_by_slack_user_id=user_id,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                    source_fingerprint=source_fingerprint,
                    items=[
                        {
                            "issue_input": item["issue_input"],
                            "display": {
                                key: value
                                for key, value in item.items()
                                if key not in {"issue_input", "review_reason"}
                            },
                            "reason": item.get("review_reason") or "Needs approval",
                        }
                        for item in review_needed
                    ],
                )
                persisted_by_position = {
                    int(item.get("position") or 0): item
                    for item in review_batch.get("items") or []
                    if isinstance(item, dict)
                }
                batch_id = str(review_batch.get("id") or "")
                if not batch_id:
                    raise RuntimeError("Approval storage returned no batch id")
                missing_positions = [
                    position
                    for position in range(len(review_needed))
                    if not str(
                        (persisted_by_position.get(position) or {}).get("id") or ""
                    )
                ]
                if missing_positions:
                    raise RuntimeError(
                        "Approval storage returned no item id for position(s) "
                        + ", ".join(str(position) for position in missing_positions)
                    )
                for position, item in enumerate(review_needed):
                    persisted = persisted_by_position.get(position) or {}
                    item["batch_id"] = batch_id
                    item["item_id"] = str(persisted.get("id") or "")
                    item.pop("issue_input", None)
                    item.pop("review_reason", None)
            except Exception as exc:
                staging_error = (
                    "Approval storage was unavailable; no review buttons were created. "
                    f"Retry the original request. ({exc.__class__.__name__}: {exc})"
                )
                skipped.extend(
                    {**item, "reason": staging_error}
                    for item in review_needed
                )
                review_needed = []

        message = self._format_linear_meeting_result_message(
            created,
            review_needed,
            skipped,
            project_update=project_update,
            project_update_error=project_update_error,
            contextual_review_mode=contextual_review_mode,
        )
        message += self._format_linear_meeting_source_warnings(source_result.warnings)
        blocks = (
            self._build_linear_meeting_review_blocks(
                message,
                review_needed,
                user_id,
                batch_id=str((review_batch or {}).get("id") or ""),
            )
            if review_needed
            else None
        )
        return {
            "message": message,
            "blocks": blocks,
            "data": {
                "created_count": len(created),
                "review_count": len(review_needed),
                "skipped_count": len(skipped),
                "review_batch_id": str((review_batch or {}).get("id") or "") or None,
                "effort_sizing_results": sizing_shadow,
            },
        }

    async def _execute_linear_project_issue_sizing(
        self,
        *,
        text: str,
        params: dict[str, Any],
        user_id: str,
        client: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """Build a durable, no-write preview for sizing one complete project."""

        project_hint = str(params.get("project_hint") or "").strip()
        if not project_hint:
            project_hint = str(
                self._extract_linear_meeting_project_hint_from_text(text) or ""
            ).strip()
        if not project_hint:
            return {
                "message": (
                    "Tell me the exact Linear project name or slug to size. "
                    "For example: @Roo size the unsized tasks in Linear project Aaron AI."
                )
            }

        try:
            projects, users, labels = await asyncio.gather(
                client.list_active_projects(),
                client.list_users(),
                client.list_issue_labels(),
            )
        except Exception as exc:
            return {
                "message": f"I couldn't read Linear context yet: {exc.__class__.__name__}: {exc}"
            }

        project_match = self._match_linear_meeting_project(
            {"project_hint": project_hint},
            projects,
            None,
            project_hint,
        )
        project = project_match.get("project")
        if not isinstance(project, dict) or float(
            project_match.get("confidence") or 0.0
        ) < 0.95:
            return {
                "message": (
                    f"I couldn't uniquely match {project_hint!r} to an active Linear project. "
                    "Use its exact project name or slug; nothing was changed."
                )
            }
        project_id = str(project.get("id") or "")

        requester_hint = (
            str(params.get("requester_email") or "").strip()
            or str(params.get("requester_display_name") or "").strip()
            or f"<@{user_id}>"
        )
        requester_match = self._match_linear_meeting_owner(requester_hint, users)
        requester = requester_match.get("user")
        if not isinstance(requester, dict) or float(
            requester_match.get("confidence") or 0.0
        ) < 0.9:
            return {
                "message": (
                    "I couldn't map your Slack identity to a Linear user, so I couldn't "
                    "verify project-sizing access. Nothing was changed."
                )
            }

        max_issues = max(
            1,
            int(getattr(settings, "LINEAR_PROJECT_SIZING_MAX_ISSUES", 500) or 500),
        )
        try:
            inventory, update_inventory, bounded_context = await asyncio.gather(
                client.list_project_issues(project_id, max_issues=max_issues),
                client.list_project_updates(project_id),
                client.get_project_sizing_context(project_id),
            )
        except Exception as exc:
            return {
                "message": (
                    f"I couldn't build a complete sizing snapshot for {project.get('name')!r}: "
                    f"{exc.__class__.__name__}: {exc}. Nothing was changed."
                )
            }

        live_project = inventory.get("project")
        if not isinstance(live_project, dict) or str(
            live_project.get("id") or ""
        ) != project_id:
            return {
                "message": "Linear returned mismatched project inventory; nothing was changed."
            }
        if str(live_project.get("name") or "") != str(project.get("name") or ""):
            return {
                "message": "The Linear project changed while I prepared the preview; nothing was changed."
            }

        terminal_types = {
            str(value).strip().lower()
            for value in inventory.get("terminalStateTypes") or []
            if str(value).strip()
        } or {"completed", "canceled", "cancelled", "duplicate"}
        mode = str(params.get("mode") or params.get("scope") or "").strip().lower()
        normalized_request = self._normalize_match_text(text)
        replace_requested = mode in {
            "replace_existing",
            "replace",
            "rescore",
            "resize",
        } or any(
            token in normalized_request
            for token in ("replaceexisting", "rescore", "resize", "reestimate")
        )
        run_mode = "replace_existing" if replace_requested else "missing_only"

        active_issues: list[dict[str, Any]] = []
        skipped_terminal = 0
        skipped_already_sized = 0
        for issue in inventory.get("nodes") or []:
            if not isinstance(issue, dict):
                continue
            state_type = str(((issue.get("state") or {}).get("type")) or "").lower()
            if state_type in terminal_types:
                skipped_terminal += 1
                continue
            issue_labels = self._linear_connection_nodes(issue.get("labels"))
            effort_labels = [
                label
                for label in issue_labels
                if str(label.get("name") or "") in EFFORT_LABELS
            ]
            if run_mode == "missing_only" and len(effort_labels) == 1:
                skipped_already_sized += 1
                continue
            active_issues.append(issue)

        if not active_issues:
            return {
                "message": (
                    f"{live_project.get('name')!r} has no eligible active issues to size "
                    f"({skipped_already_sized} already sized; {skipped_terminal} terminal). "
                    "Nothing was changed."
                )
            }

        team_ids = {
            str(((issue.get("team") or {}).get("id")) or "")
            for issue in active_issues
        }
        if "" in team_ids:
            return {
                "message": "At least one eligible issue has no Linear team; nothing was changed."
            }
        try:
            for team_id in sorted(team_ids):
                for effort_label in EFFORT_LABELS:
                    compatible = self._linear_compatible_label_ids(
                        labels,
                        label_name=effort_label,
                        team_id=team_id,
                        exact_name=True,
                    )
                    if len(compatible) != 1:
                        raise RuntimeError(
                            f"Expected exactly one compatible label named {effort_label!r} "
                            f"for team {team_id!r}; found {len(compatible)}."
                        )
        except Exception as exc:
            return {
                "message": f"Effort-label preflight failed: {exc} Nothing was changed."
            }

        return await self._finish_linear_project_issue_sizing_preview(
            text=text,
            params=params,
            user_id=user_id,
            client=client,
            settings=settings,
            requester=requester,
            live_project=live_project,
            inventory=inventory,
            update_inventory=update_inventory,
            bounded_context=bounded_context,
            active_issues=active_issues,
            run_mode=run_mode,
            skipped_already_sized=skipped_already_sized,
            skipped_terminal=skipped_terminal,
        )

    async def _finish_linear_project_issue_sizing_preview(
        self,
        *,
        text: str,
        params: dict[str, Any],
        user_id: str,
        client: Any,
        settings: Any,
        requester: dict[str, Any],
        live_project: dict[str, Any],
        inventory: dict[str, Any],
        update_inventory: dict[str, Any],
        bounded_context: dict[str, Any],
        active_issues: list[dict[str, Any]],
        run_mode: str,
        skipped_already_sized: int,
        skipped_terminal: int,
    ) -> dict[str, Any]:
        full_project_context = dict(bounded_context)
        full_project_context["project"] = live_project
        full_project_context["projectUpdates"] = {
            "nodes": update_inventory.get("nodes") or [],
            "returned": len(update_inventory.get("nodes") or []),
            "truncated": bool(update_inventory.get("truncated")),
        }
        terminal_types = {
            str(value).strip().lower()
            for value in inventory.get("terminalStateTypes") or []
            if str(value).strip()
        } or {"completed", "canceled", "cancelled", "duplicate"}
        context_active_issues = [
            issue
            for issue in inventory.get("nodes") or []
            if isinstance(issue, dict)
            and str(((issue.get("state") or {}).get("type")) or "").lower()
            not in terminal_types
        ]
        full_project_context["activeIssues"] = {
            "nodes": context_active_issues,
            "returned": len(context_active_issues),
            "truncated": bool(inventory.get("truncated")),
        }

        sizing_candidates: list[dict[str, Any]] = []
        issues_by_id: dict[str, dict[str, Any]] = {}
        for issue in active_issues:
            issue_id = str(issue.get("id") or "")
            issues_by_id[issue_id] = issue
            description = str(issue.get("description") or "").strip()
            sizing_candidates.append(
                {
                    "candidate_key": issue_id,
                    "id": issue_id,
                    "identifier": str(issue.get("identifier") or ""),
                    "title": str(issue.get("title") or ""),
                    "description": description,
                    "work_status": "open",
                    "completed_work": "",
                    "remaining_work": description,
                    "available_artifacts": [],
                    "dependencies": [],
                    "acceptance_criteria": [],
                    "due_date": issue.get("dueDate"),
                    "assignee": issue.get("assignee") or {},
                    "team": issue.get("team") or {},
                }
            )

        context_max_chars = int(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS",
                "LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS",
                40000,
            )
        )
        batch_size = int(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_BATCH_SIZE",
                "LINEAR_STUDIO_SIZING_BATCH_SIZE",
                3,
            )
        )
        assessments, errors = await self._assess_linear_studio_candidates_resilient(
            candidates=sizing_candidates,
            project_context=full_project_context,
            context_max_chars=context_max_chars,
            batch_size=batch_size,
            safety_identifier=linear_safety_identifier(user_id),
            max_concurrency=int(
                getattr(
                    settings,
                    "LINEAR_PROJECT_SIZING_INFERENCE_CONCURRENCY",
                    3,
                )
                or 3
            ),
        )
        if errors or len(assessments) != len(sizing_candidates):
            first_error = next(
                iter(errors.values()),
                "One or more assessments were missing.",
            )
            return {
                "message": (
                    f"I couldn't size all {len(sizing_candidates)} eligible issues reliably "
                    f"({len(errors)} failed): {first_error} Nothing was changed."
                ),
                "data": {"sizing_errors": errors},
            }

        rubric_version = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_RUBRIC_VERSION",
                "LINEAR_STUDIO_SIZING_RUBRIC_VERSION",
                "project-effort-v2",
            )
        )
        proposals: list[dict[str, Any]] = []
        for candidate in sizing_candidates:
            issue_id = str(candidate["candidate_key"])
            issue = issues_by_id[issue_id]
            assessment = assessments[issue_id]
            metadata = assessment_metadata(
                assessment,
                project=live_project,
                model=LINEAR_SKILL_MODEL,
                rubric_version=rubric_version,
            )
            proposals.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue.get("identifier"),
                    "title": issue.get("title"),
                    "team_id": str(((issue.get("team") or {}).get("id")) or ""),
                    "expected_updated_at": issue.get("updatedAt"),
                    "original_labels": self._linear_connection_nodes(
                        issue.get("labels")
                    ),
                    "effort_label": assessment.effort_label,
                    "rationale": assessment.rationale,
                    "sizing_metadata": metadata,
                }
            )

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "project": str(live_project.get("id") or ""),
                    "requester": user_id,
                    "mode": run_mode,
                    "snapshot": inventory.get("snapshotAt"),
                    "issues": [
                        [item["issue_id"], item["expected_updated_at"]]
                        for item in proposals
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            run = await client.create_project_sizing_run(
                project_id=str(live_project.get("id") or ""),
                payload={
                    "requested_by_slack_user_id": user_id,
                    "requested_by_linear_user_id": str(requester.get("id") or ""),
                    "project_name": str(live_project.get("name") or ""),
                    "mode": run_mode,
                    "model": LINEAR_SKILL_MODEL,
                    "rubric_version": rubric_version,
                    "source_snapshot_at": inventory.get("snapshotAt"),
                    "project_context": {
                        "projectUpdateCount": len(
                            update_inventory.get("nodes") or []
                        ),
                        "issueCount": len(inventory.get("nodes") or []),
                    },
                    "idempotency_key": fingerprint,
                    "items": proposals,
                },
            )
        except Exception as exc:
            return {
                "message": (
                    "I sized the issues but couldn't save a safe preview: "
                    f"{exc.__class__.__name__}: {exc}. Nothing was changed."
                )
            }

        run_id = str(run.get("id") or "")
        preview_lines = [
            (
                f"Effort sizing preview for *{live_project.get('name')}*: "
                f"{len(proposals)} issue(s)."
            ),
            (
                f"Mode: {run_mode} · model: {LINEAR_SKILL_MODEL} · "
                "no labels changed yet."
            ),
            "",
        ]
        for proposal in proposals[:15]:
            identifier = proposal.get("identifier") or proposal["issue_id"]
            preview_lines.append(
                f"- *{identifier}* {proposal['title']} -> "
                f"{proposal['effort_label']} — {proposal['rationale']}"
            )
        if len(proposals) > 15:
            preview_lines.append(f"- ...and {len(proposals) - 15} more issue(s).")
        preview_lines.append(
            f"Skipped {skipped_already_sized} already sized and "
            f"{skipped_terminal} terminal issue(s)."
        )
        message = "\n".join(preview_lines)
        value = json.dumps({"run_id": run_id, "requested_by": user_id})
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message[:3000]},
            },
            {
                "type": "actions",
                "block_id": f"linear_project_sizing_{run_id[:8]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Apply size labels",
                        },
                        "style": "primary",
                        "action_id": "linear_project_sizing_apply",
                        "value": value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "linear_project_sizing_cancel",
                        "value": value,
                    },
                ],
            },
        ]
        return {
            "message": message,
            "blocks": blocks,
            "data": {
                "run_id": run_id,
                "preview_count": len(proposals),
                "skipped_already_sized": skipped_already_sized,
                "skipped_terminal": skipped_terminal,
                "mode": run_mode,
            },
        }

    def _build_linear_meeting_transcript(
        self,
        text: str,
        params: dict,
        history: Optional[List[dict]] = None,
    ) -> str:
        explicit = str(params.get("transcript") or "").strip()
        parts: list[str] = []
        if explicit:
            parts.append(explicit)

        for message in history or []:
            if message.get("is_bot") or message.get("bot_id"):
                continue
            message_text = str(message.get("text") or "").strip()
            if not message_text:
                continue
            speaker = str(message.get("user") or "user").strip()
            parts.append(f"{speaker}: {message_text}")

        clean_text = str(text or "").strip()
        if clean_text and all(clean_text not in part for part in parts):
            parts.append(clean_text)

        return "\n".join(dict.fromkeys(parts)).strip()

    async def _build_linear_meeting_source_result(
        self,
        *,
        text: str,
        params: dict,
        thread_history: Optional[List[dict]],
        event_files: Optional[List[dict]],
        settings: Any,
        current_message_ts: Optional[str] = None,
        exclude_current_message: bool = False,
    ) -> SourceParseResult:
        image_parser = None
        if getattr(settings, "OPENAI_API_KEY", None):
            async def image_parser(image_bytes: bytes, mime_type: str, label: str) -> str:
                prompt = (
                    "Extract meeting transcript text or to-do/action items from this image. "
                    "Preserve names, due dates, checkboxes, bullets, and project labels. "
                    f"Return plain text only. Source label: {label}"
                )
                return await extract_text_from_image(
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    prompt=prompt,
                    model=getattr(settings, "OPENAI_VISION_MODEL", None),
                )

        return await parse_linear_meeting_sources(
            text=text,
            params=params,
            thread_history=thread_history,
            event_files=event_files,
            image_parser=image_parser,
            current_message_ts=current_message_ts,
            exclude_current_message=exclude_current_message,
        )

    def _format_linear_meeting_source_warnings(self, warnings: list[str]) -> str:
        if not warnings:
            return ""
        lines = ["", "", "Source notes:"]
        for warning in warnings[:8]:
            lines.append(f"- {warning}")
        if len(warnings) > 8:
            lines.append(f"- {len(warnings) - 8} more source note(s) omitted.")
        return "\n".join(lines)

    def _apply_linear_meeting_project_hint_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("project_hint"):
            return params
        project_hint = self._extract_linear_meeting_project_hint_from_text(text)
        if not project_hint:
            return params
        return {**params, "project_hint": project_hint}

    def _apply_linear_meeting_owner_hint_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("owner_hint") or params.get("owner"):
            return params
        owner_hint = self._extract_linear_meeting_owner_hint_from_text(text)
        if not owner_hint:
            return params
        return {**params, "owner_hint": owner_hint}

    def _apply_linear_meeting_default_assignee_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("default_assignee_hint"):
            return params
        default_assignee = self._extract_linear_meeting_default_assignee_from_text(text)
        if not default_assignee:
            return params
        return {**params, "default_assignee_hint": default_assignee}

    def _extract_linear_meeting_default_assignee_from_text(self, text: str) -> Optional[str]:
        """Parse a fallback assignee like "if you're not sure who to assign to, assign to X"."""
        value = str(text or "").strip()
        person = r'(<@[A-Z0-9]+>|@?[A-Z][\w.\-]*(?:\s+[A-Z][\w.\-]*){0,3})'
        patterns = (
            rf'\bif\b[^.!?]{{0,180}}?\b(?:can(?:not|[\'’]?t)|could(?:not|[\'’]?t))\s+find\b[^.!?]{{0,180}}?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\bif\s+(?:you(?:\'re|\s+are)?\s+)?(?:not\s+sure|unsure|in\s+doubt|you\s+don\'?t\s+know)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\bif\s+(?:it\'?s\s+)?(?:unclear|unknown|ambiguous)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\b(?:otherwise|by\s+default|as\s+a\s+fallback|default(?:ing)?)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\b(?:default|fallback)\s+assignee\s*[:\-]?\s*{person}',
        )
        for pattern in patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                return self._clean_linear_meeting_owner_hint(match.group(1))
        return None

    def _extract_linear_meeting_project_hint_from_text(self, text: str) -> Optional[str]:
        value = str(text or "").strip()
        for pattern in self._linear_meeting_project_hint_patterns():
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                project_hint = self._clean_linear_meeting_project_hint(match.group(1))
                normalized_hint = self._normalize_match_text(project_hint)
                if project_hint and normalized_hint not in {"update", "updates"} and not normalized_hint.startswith("update"):
                    return project_hint

        return None

    @staticmethod
    def _linear_meeting_project_hint_patterns() -> tuple[str, ...]:
        quoted = r'["\'“”‘’]([^"\'“”‘’]+)["\'“”‘’]'
        unquoted = (
            r'([A-Za-z0-9][A-Za-z0-9 _/&-]{1,80}?)'
            r'(?=\s+(?:to|and|assign|assigned|with|please|from|as|for|due|by)\b|[.!?,;]|$)'
        )
        return (
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?linear\s+project\s+{quoted}',
            rf'\blinear\s+project\s+{quoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?project\s+{quoted}',
            rf'\bproject\s+(?:called|named)\s+{quoted}',
            rf'\bproject\s+{quoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?linear\s+project\s+{unquoted}',
            rf'\bto\s+linear\s+project\s+{unquoted}',
            rf'\bproject\s+(?:called|named)\s+{unquoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?project\s+{unquoted}',
            rf'\bproject\s+{unquoted}',
        )

    @staticmethod
    def _clean_linear_meeting_project_hint(value: str) -> Optional[str]:
        cleaned = str(value or "").strip(" \t\n\r'\"“”‘’`")
        return cleaned or None

    def _extract_linear_meeting_owner_hint_from_text(self, text: str) -> Optional[str]:
        value = str(text or "").strip()
        mention_assignment = re.search(
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\s*(<@[A-Z0-9]+>)',
            value,
            flags=re.IGNORECASE,
        )
        if mention_assignment:
            return mention_assignment.group(1)

        assignment = re.search(
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\s*'
            r'(.+?)(?=\s+(?:in|to|under)\s+(?:the\s+)?linear\s+project\b|[.!?]\s|$)',
            value,
            flags=re.IGNORECASE,
        )
        if assignment:
            return self._clean_linear_meeting_owner_hint(assignment.group(1))

        for_owner = re.search(
            r'\bfor\s+(<@[A-Z0-9]+>|@?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s*$',
            value,
        )
        if for_owner:
            return self._clean_linear_meeting_owner_hint(for_owner.group(1))
        return None

    @staticmethod
    def _clean_linear_meeting_owner_hint(value: str) -> Optional[str]:
        cleaned = str(value or "").strip(" \t\n\r'\"“”`")
        cleaned = re.sub(r'^(?:@|to\s+)', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(" ,;:.!?")
        return cleaned or None

    def _is_linear_direct_issue_request(self, text: str, params: dict[str, Any]) -> bool:
        value = str(text or "").lower()
        if "linear" not in value:
            return False
        if re.search(r'\b(points?|rewards?|coworking|allowance|worth\s+\d+\s+points?)\b', value):
            return False
        if self._linear_meeting_project_update_requested(text, params):
            return False
        has_creation_intent = bool(re.search(r'\b(create|add|open|file|make)\b', value))
        has_issue_noun = bool(
            re.search(r'\b(?:to\s*do\s+items?|todo\s+items?|tasks?|issues?|tickets?)\b', value)
        )
        return has_creation_intent and has_issue_noun

    def _is_linear_thread_reference_request(self, text: str, params: dict[str, Any]) -> bool:
        from ..linear_context import is_contextual_linear_reference

        normalized = str(text or "").lower()
        if "linear" not in normalized:
            return False
        if self._normalize_match_text(params.get("action")) in {"threadreference", "addthread", "addthis"}:
            return True
        if is_contextual_linear_reference(text):
            return True
        return False

    @staticmethod
    def _linear_request_assigns_to_requester(text: str) -> bool:
        value = str(text or "")
        return bool(
            re.search(r"\bfor\s+me\b", value, flags=re.IGNORECASE)
            or re.search(
                r"\bassign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to\s+me\b",
                value,
                flags=re.IGNORECASE,
            )
        )

    async def _extract_linear_thread_context_candidate(
        self,
        *,
        sources: list[ParsedSource],
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        source_excerpt = "\n\n".join(
            chunk
            for source in sources
            for chunk in source_text_chunks(source, max_chars=5000)[:1]
        )[:12000]
        if len(source_excerpt.split()) < 8:
            return None

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        prompt = f"""Draft exactly one possible Linear issue from the referenced Slack thread.

This is not a formal meeting transcript. The user asked Roo to add the thread context to Linear, so infer the likely follow-up item from the prior conversation only.

Rules:
- Return null if the thread is too vague to turn into one useful Linear issue.
- Do not create a task called "add this to Linear" or similar.
- For a discussion or decision, write the issue as a concrete follow-up such as "Decide ..." or "Confirm ...".
- Infer the owner from direct address, mentions, and conversational responsibility. Prefer an exact Slack mention token like <@U123> when present.
- Use the explicit project hint if present.

Project hint: {params.get("project_hint") or "none"}
Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Referenced Slack thread:
{source_excerpt}

Return one structured issue, or null when the thread is too vague."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Draft one review-only Linear issue from Slack context. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearContextualIssueResult,
                signals=LinearReasoningSignals(
                    stage="contextual_issue",
                    source_chars=len(source_excerpt),
                    source_count=max(1, len(sources)),
                    explicit_project=bool(params.get("project_hint")),
                    explicit_owner=bool(
                        params.get("owner_hint") or params.get("owner")
                    ),
                    ambiguity=not bool(
                        params.get("project_hint")
                        and (params.get("owner_hint") or params.get("owner"))
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
        except ValueError:
            return None
        if inference.value.issue is None:
            return None
        issue = inference.value.issue.model_dump(mode="json")
        candidate = self._normalize_linear_meeting_candidate(issue)
        if not candidate.get("title"):
            return None
        if params.get("project_hint") and not candidate.get("project_hint"):
            candidate["project_hint"] = str(params["project_hint"])
        candidate["contextual_review_only"] = True
        candidate["source_label"] = candidate.get("source_label") or "Slack thread"
        candidate["priority"] = candidate.get("priority", 3)
        return candidate

    async def _extract_linear_direct_issue_candidates(
        self,
        *,
        text: str,
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        project_hint = str(params.get("project_hint") or "").strip()
        if not project_hint:
            project_hint = self._infer_linear_direct_project_hint_from_known_projects(text, projects)
            if project_hint:
                params = {**params, "project_hint": project_hint}
        task_body = self._extract_linear_direct_issue_body(text, params)
        if not task_body:
            return []

        owner_hint = str(params.get("owner_hint") or params.get("owner") or "").strip()
        fallback = {
            "title": self._linear_direct_issue_title_from_body(task_body),
            "description": task_body,
            "work_status": "open",
            "completed_work": "",
            "remaining_work": task_body,
            "available_artifacts": [],
            "dependencies": [],
            "acceptance_criteria": [],
            "owner_hint": owner_hint,
            "project_hint": project_hint,
            "evidence": text[:700],
            "source_label": "Slack command",
            "evidence_message_ts": "",
            "explicit_commitment": True,
            "confidence": 0.96,
        }

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        prompt = f"""Clean up this explicit Slack command into Linear issue fields.

The user is directly asking Roo to create Linear issue(s). Do not ignore the command as a meta-instruction.
Use the parsed task body as the work to create, not "create a Linear task" itself.

Parsed project hint: {project_hint or "none"}
Parsed assignee hint: {owner_hint or "none"}
Parsed task body: {task_body}

Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Slack command:
{text[:12000]}

Return the structured issue list. Preserve the parsed project and assignee hints."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Convert explicit Slack commands into Linear issue fields. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearDirectIssueBatch,
                signals=LinearReasoningSignals(
                    stage="direct_issue",
                    source_chars=len(text),
                    explicit_project=bool(project_hint),
                    explicit_owner=bool(owner_hint),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
            issues = [
                issue.model_dump(mode="json")
                for issue in inference.value.issues
            ]
        except Exception:
            issues = []

        candidates: list[dict[str, Any]] = []
        for issue in issues or []:
            if not isinstance(issue, dict):
                continue
            candidate = {
                **fallback,
                **issue,
                "owner_hint": issue.get("owner_hint") or owner_hint,
                "project_hint": issue.get("project_hint") or project_hint,
                "source_label": issue.get("source_label") or "Slack command",
                "evidence": issue.get("evidence") or text[:700],
                "confidence": issue.get("confidence", 0.96),
            }
            normalized = self._normalize_linear_meeting_candidate(candidate)
            if normalized.get("title"):
                candidates.append(normalized)
        return candidates or [fallback]

    def _infer_linear_direct_project_hint_from_known_projects(
        self,
        text: str,
        projects: list[dict[str, Any]],
    ) -> str:
        normalized_text = self._normalize_match_text(text)
        matches: list[tuple[int, str]] = []
        for project in projects:
            name = str(project.get("name") or "").strip()
            slug = str(project.get("slugId") or "").strip()
            for value in (name, slug):
                normalized_value = self._normalize_match_text(value)
                if normalized_value and normalized_value in normalized_text:
                    matches.append((len(normalized_value), name or slug))
                    break
        if not matches:
            return ""
        matches.sort(reverse=True)
        return matches[0][1]

    def _extract_linear_direct_issue_body(self, text: str, params: dict[str, Any]) -> str:
        value = str(text or "").strip()
        value = re.sub(r'<@[A-Z0-9]+>', ' ', value)
        value = re.sub(r'\s+', ' ', value).strip()

        assignment_pattern = (
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\b.*$'
        )
        without_assignment = re.sub(assignment_pattern, '', value, flags=re.IGNORECASE).strip(" ,.;:")

        project_match = None
        for pattern in self._linear_meeting_project_hint_patterns():
            match = re.search(pattern, without_assignment, flags=re.IGNORECASE)
            if match:
                project_match = match
                break

        if project_match:
            task_body = without_assignment[project_match.end():].strip()
            if not task_body:
                task_body = without_assignment[:project_match.start()].strip()
        else:
            task_body = without_assignment

        project_hint = str(params.get("project_hint") or "").strip()
        if project_hint:
            hint_pattern = re.escape(project_hint)
            direct_project_match = re.search(
                rf'\b(?:in|into|to|under)\s+{hint_pattern}\b',
                task_body,
                flags=re.IGNORECASE,
            )
            if direct_project_match:
                after_project = task_body[direct_project_match.end():].strip()
                before_project = task_body[:direct_project_match.start()].strip()
                task_body = after_project or before_project

        task_body = re.sub(
            r'^(?:please\s+)?(?:create|add|open|file|make)\s+(?:a|an|the)?\s*'
            r'(?:linear\s+)?(?:(?:to\s*do|todo)\s+items?|tasks?|issues?|tickets?)\s*',
            '',
            task_body,
            flags=re.IGNORECASE,
        )
        owner_hint = str(params.get("owner_hint") or params.get("owner") or "").strip()
        if owner_hint:
            task_body = re.sub(
                rf'^\s*for\s+{re.escape(owner_hint)}\s*$',
                '',
                task_body,
                flags=re.IGNORECASE,
            )
            task_body = re.sub(
                rf'\s+\bfor\s+{re.escape(owner_hint)}\s*$',
                '',
                task_body,
                flags=re.IGNORECASE,
            )
        task_body = re.sub(r'^(?:in|into|to|under|for|about|that|where|:|-)\s+', '', task_body, flags=re.IGNORECASE)
        task_body = re.sub(r'\s+', ' ', task_body).strip(" ,.;:-")
        return task_body

    @staticmethod
    def _linear_direct_issue_title_from_body(task_body: str) -> str:
        title = str(task_body or "").strip()
        title = re.sub(r'^(?:to\s+)+', '', title, flags=re.IGNORECASE).strip()
        if not title:
            return "Create Linear task"
        return title[:1].upper() + title[1:]

    async def _extract_linear_meeting_candidates_from_sources(
        self,
        *,
        sources: list[ParsedSource],
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        extraction_concurrency = max(
            1,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_CONCURRENCY",
                        3,
                    )
                ),
                6,
            ),
        )
        extraction_timeout_seconds = max(
            120.0,
            float(
                getattr(
                    settings,
                    "LINEAR_MEETING_EXTRACTION_TOTAL_TIMEOUT_SECONDS",
                    360.0,
                )
            ),
        )
        chunk_max_chars = max(
            2_000,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_CHUNK_MAX_CHARS",
                        8_000,
                    )
                ),
                12_000,
            ),
        )
        recovery_max_chars = max(
            1_000,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_RECOVERY_MAX_CHARS",
                        4_000,
                    )
                ),
                chunk_max_chars - 1,
            ),
        )
        recovery_depth_limit = max(
            0,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_RECOVERY_DEPTH",
                        2,
                    )
                ),
                3,
            ),
        )
        source_chunks = [
            (source, chunk)
            for source in sources
            for chunk in source_text_chunks(source, max_chars=chunk_max_chars)
        ]
        if not source_chunks:
            return []

        semaphore = asyncio.Semaphore(extraction_concurrency)
        batch_chunk_count = len(source_chunks)

        async def extract_chunk_with_recovery(
            source: ParsedSource,
            chunk: str,
            *,
            batch_chunk_index: int,
            recovery_depth: int = 0,
        ) -> list[dict[str, Any]]:
            try:
                return await self._extract_linear_meeting_candidates(
                    transcript=chunk,
                    params=params,
                    users=users,
                    projects=projects,
                    source_label=source.label,
                    # This model request contains one source chunk. The total
                    # batch size is logged separately and must not inflate
                    # reasoning effort for every individual request.
                    source_count=1,
                    batch_chunk_index=batch_chunk_index,
                    batch_chunk_count=batch_chunk_count,
                    recovery_depth=recovery_depth,
                )
            except LinearInferenceTimeoutError as exc:
                if recovery_depth >= recovery_depth_limit:
                    raise
                recovery_chunks = self._linear_meeting_timeout_recovery_chunks(
                    source=source,
                    chunk=chunk,
                    recovery_depth=recovery_depth,
                    recovery_max_chars=recovery_max_chars,
                )
                print(
                    "LINEAR_MEETING_EXTRACTION "
                    + json.dumps(
                        {
                            "event": "chunk_timeout_recovery",
                            "batch_chunk_index": batch_chunk_index,
                            "batch_chunk_count": batch_chunk_count,
                            "recovery_depth": recovery_depth + 1,
                            "retry_chunk_count": len(recovery_chunks),
                            "source_chars": len(chunk),
                            "reasoning_effort": exc.decision.effort,
                            "timeout_seconds": exc.decision.timeout_seconds,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                recovered_candidates: list[dict[str, Any]] = []
                # Keep the worker slot until its failed chunk has recovered. This
                # prevents a retry from sitting behind the entire initial queue and
                # makes the total deadline predictable for long PDFs.
                for recovery_chunk in recovery_chunks:
                    recovered_candidates.extend(
                        await extract_chunk_with_recovery(
                            source,
                            recovery_chunk,
                            batch_chunk_index=batch_chunk_index,
                            recovery_depth=recovery_depth + 1,
                        )
                    )
                return self._dedupe_linear_meeting_candidates(recovered_candidates)

        async def extract_chunk(
            source: ParsedSource,
            chunk: str,
            *,
            batch_chunk_index: int,
        ) -> list[dict[str, Any]]:
            async with semaphore:
                return await extract_chunk_with_recovery(
                    source,
                    chunk,
                    batch_chunk_index=batch_chunk_index,
                )

        async def extract_all_chunks() -> list[list[dict[str, Any]]]:
            tasks = [
                asyncio.create_task(
                    extract_chunk(
                        source,
                        chunk,
                        batch_chunk_index=index,
                    )
                )
                for index, (source, chunk) in enumerate(source_chunks, start=1)
            ]
            try:
                return await asyncio.gather(*tasks)
            except BaseException:
                # asyncio.gather does not cancel siblings when one raises. Stop
                # queued/in-flight calls so a terminal chunk failure fails fast and
                # no model work outlives the fail-closed extraction request.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        try:
            extracted_batches = await asyncio.wait_for(
                extract_all_chunks(),
                timeout=extraction_timeout_seconds,
            )
        except LinearInferenceTimeoutError:
            raise
        except asyncio.TimeoutError as exc:
            raise LinearMeetingExtractionDeadlineError(
                "Meeting-action extraction exceeded its "
                f"{extraction_timeout_seconds:g} second runtime budget."
            ) from exc

        candidates: list[dict[str, Any]] = []
        for (source, _), extracted in zip(source_chunks, extracted_batches):
            for item in extracted:
                item.setdefault("source_label", source.label)
                candidates.append(item)
        return self._dedupe_linear_meeting_candidates(candidates)

    @staticmethod
    def _linear_meeting_timeout_recovery_chunks(
        *,
        source: ParsedSource,
        chunk: str,
        recovery_depth: int,
        recovery_max_chars: int,
    ) -> list[str]:
        source_header = f"Source: {source.label}\n"
        source_text = (
            chunk[len(source_header):]
            if chunk.startswith(source_header)
            else chunk
        )
        recovery_source = ParsedSource(
            label=source.label,
            text=source_text,
            kind=source.kind,
            metadata=source.metadata,
        )
        return source_text_chunks(
            recovery_source,
            max_chars=max(1_000, recovery_max_chars // (2**recovery_depth)),
            hard_split_overlap_chars=min(
                LINEAR_MEETING_TIMEOUT_RECOVERY_OVERLAP_CHARS,
                max(1_000, recovery_max_chars // (2**recovery_depth)) - 1,
            ),
        ) or [chunk]

    async def _extract_linear_meeting_candidates(
        self,
        *,
        transcript: str,
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        source_label: Optional[str] = None,
        source_count: int = 1,
        batch_chunk_index: int = 1,
        batch_chunk_count: int = 1,
        recovery_depth: int = 0,
    ) -> list[dict[str, Any]]:
        from ..utils import get_current_date

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        source_local_datetime = str(params.get("source_local_datetime") or "").strip()
        source_date = source_local_datetime[:10] if source_local_datetime else get_current_date().isoformat()
        prompt = f"""Extract concrete action items from the Slack conversation or meeting notes.

Source-local date: {source_date}
Source-local datetime: {source_local_datetime or "unknown"}
Source timezone: {params.get("source_timezone") or "unknown"}
Source label: {source_label or "Slack thread"}
Project hint: {params.get("project_hint") or "none"}
Team hint: {params.get("team_hint") or "none"}
Default assignee if the owner is unclear: {params.get("default_assignee_hint") or "none"}
Requester: {params.get("requester_display_name") or "unknown"} (<@{params.get("requester_slack_id") or "unknown"}>, {params.get("requester_email") or "email unavailable"})

Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Meeting notes:
{transcript[:12000]}

Only include actionable work someone explicitly agreed, promised, or was asked to do. Preserve completed/cancelled/duplicate status when the source says the work is terminal so Roo can suppress creation. Separate completed work from the work remaining. Set explicit_commitment=false when you had to reframe a discussion into possible work. Do not include decisions, FYIs, vague follow-ups, or the user's instruction to create Linear tasks/project updates.
Resolve relative dates such as EOW from the source-local date above. In this Australian workspace, numeric dates are day/month unless the source makes another format explicit.
If an action item has no clear owner and a default assignee is given above, set its owner_hint to that default assignee.
Return the structured action_items list."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract structured meeting action items. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearMeetingActionBatch,
                signals=LinearReasoningSignals(
                    stage="meeting_actions",
                    source_chars=len(transcript),
                    source_count=max(1, source_count),
                    batch_chunk_index=max(1, batch_chunk_index),
                    batch_chunk_count=max(1, batch_chunk_count),
                    recovery_depth=max(0, recovery_depth),
                    explicit_project=bool(params.get("project_hint")),
                    explicit_owner=bool(
                        params.get("owner_hint")
                        or params.get("default_assignee_hint")
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
        except ValueError:
            return []
        return [
            item.model_dump(mode="json")
            for item in inference.value.action_items
        ]

    def _dedupe_linear_meeting_candidates(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            title = self._normalize_match_text(candidate.get("title") or candidate.get("task"))
            owner = self._normalize_match_text(candidate.get("owner_hint") or candidate.get("owner"))
            if not title or self._is_linear_meeting_meta_candidate(candidate):
                continue
            key = self._find_similar_linear_meeting_candidate_key(deduped, candidate) or f"{title}:{owner}"
            if key not in deduped:
                deduped[key] = dict(candidate)
                continue

            self._merge_linear_meeting_candidate(deduped[key], candidate)
        return list(deduped.values())

    def _find_similar_linear_meeting_candidate_key(
        self,
        deduped: dict[str, dict[str, Any]],
        candidate: dict[str, Any],
    ) -> Optional[str]:
        title = str(candidate.get("title") or candidate.get("task") or "")
        normalized_title = self._normalize_match_text(title)
        owner = self._normalize_match_text(candidate.get("owner_hint") or candidate.get("owner"))
        candidate_tokens = self._linear_meeting_candidate_tokens(title)
        for key, existing in deduped.items():
            existing_title = str(existing.get("title") or existing.get("task") or "")
            normalized_existing_title = self._normalize_match_text(existing_title)
            if normalized_title == normalized_existing_title:
                return key

            existing_owner = self._normalize_match_text(existing.get("owner_hint") or existing.get("owner"))
            existing_tokens = self._linear_meeting_candidate_tokens(existing_title)
            if not candidate_tokens or not existing_tokens:
                continue

            overlap = len(candidate_tokens & existing_tokens) / min(len(candidate_tokens), len(existing_tokens))
            similarity = SequenceMatcher(None, normalized_title, normalized_existing_title).ratio()
            owners_match = not owner or not existing_owner or owner == existing_owner
            if overlap >= 0.86 or (owners_match and (overlap >= 0.74 or (overlap >= 0.68 and similarity >= 0.68))):
                return key
            if owners_match and self._linear_meeting_candidates_share_outcome(
                candidate,
                existing,
                candidate_tokens=candidate_tokens,
                existing_tokens=existing_tokens,
            ):
                return key
        return None

    def _linear_meeting_candidates_share_outcome(
        self,
        candidate: dict[str, Any],
        existing: dict[str, Any],
        *,
        candidate_tokens: set[str],
        existing_tokens: set[str],
    ) -> bool:
        """Merge chunk-level paraphrases that describe the same deliverable.

        The family check prevents account setup from being merged into an Apollo
        comparison merely because both titles mention the same providers.
        """

        candidate_family = self._linear_meeting_action_family(
            candidate.get("title") or candidate.get("task")
        )
        existing_family = self._linear_meeting_action_family(
            existing.get("title") or existing.get("task")
        )
        if not candidate_family or candidate_family != existing_family:
            return False
        candidate_project = self._normalize_match_text(
            candidate.get("project_hint") or candidate.get("project")
        )
        existing_project = self._normalize_match_text(
            existing.get("project_hint") or existing.get("project")
        )
        if candidate_project and existing_project and candidate_project != existing_project:
            return False
        shared = candidate_tokens & existing_tokens
        if len(shared) < 2:
            return False
        coverage = len(shared) / min(len(candidate_tokens), len(existing_tokens))
        return coverage >= 0.4

    @staticmethod
    def _linear_meeting_action_family(value: Any) -> str:
        text = str(value or "").lower()
        families = (
            ("evaluation", r"\b(?:compare|evaluate|assess|test|benchmark|validate)\b"),
            ("implementation", r"\b(?:build|implement|develop|integrate|prototype)\b"),
            ("documentation", r"\b(?:document|write|draft|specify)\b|\brequirements?\b"),
            ("account_setup", r"\b(?:account|register|registration|sign\s*up|access)\b"),
            ("outreach", r"\b(?:recruit|secure|contact|outreach|invite)\b"),
            ("scheduling", r"\b(?:schedule|coordinate|book|calendar)\b"),
        )
        for family, pattern in families:
            if re.search(pattern, text):
                return family
        return ""

    def _merge_linear_meeting_candidate(self, existing: dict[str, Any], candidate: dict[str, Any]) -> None:
        try:
            existing_confidence = float(existing.get("confidence", 0.0))
            candidate_confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            existing_confidence = candidate_confidence = 0.0
        if candidate_confidence > existing_confidence:
            existing["confidence"] = candidate.get("confidence")
            for field in (
                "title",
                "description",
                "work_status",
                "completed_work",
                "remaining_work",
                "available_artifacts",
                "dependencies",
                "acceptance_criteria",
                "owner_hint",
                "project_hint",
                "team_hint",
                "due_date",
                "priority",
            ):
                if candidate.get(field):
                    existing[field] = candidate.get(field)

        labels = {
            str(existing.get("source_label") or "").strip(),
            str(candidate.get("source_label") or "").strip(),
        }
        labels.discard("")
        if labels:
            existing["source_label"] = ", ".join(sorted(labels))

        existing_evidence = str(existing.get("evidence") or "").strip()
        candidate_evidence = str(candidate.get("evidence") or "").strip()
        if candidate_evidence and candidate_evidence not in existing_evidence:
            existing["evidence"] = (
                f"{existing_evidence}\n{candidate_evidence}".strip()
                if existing_evidence
                else candidate_evidence
            )

    def _is_linear_meeting_meta_candidate(self, candidate: dict[str, Any]) -> bool:
        title = self._normalize_match_text(candidate.get("title") or candidate.get("task"))
        description = self._normalize_match_text(candidate.get("description") or "")
        combined = f"{title} {description}"
        if "linear" in combined and (
            "projectupdate" in combined
            or "createlineartask" in combined
            or "extract" in combined
            or "todo" in combined
        ):
            return True
        return title in {"postprojectupdate", "extractmeetingtodos", "createlineartasks"}

    def _linear_meeting_candidate_tokens(self, text: str) -> set[str]:
        replacements = {
            "builders": "talent",
            "builder": "talent",
            "people": "talent",
            "professionals": "talent",
            "technical": "tech",
            "pool": "supply",
            "pools": "supply",
            "model": "service",
        }
        stop_words = {
            "a", "an", "and", "as", "by", "for", "from", "in", "into", "of", "on",
            "or", "the", "to", "with", "will", "meeting", "linear", "task", "tasks",
            "action", "actions", "item", "items", "create", "build", "start", "run",
            "make", "prepare", "draft", "update", "rework", "revise", "add", "share",
            "recruit", "building", "publish", "initial", "vetted",
        }
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
            token = replacements.get(token, token)
            token = self._linear_meeting_singular_token(token)
            if len(token) < 3 or token in stop_words:
                continue
            tokens.add(token)
        return tokens

    def _linear_meeting_project_tokens(self, text: str) -> set[str]:
        stop_words = {
            "a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "the",
            "to", "with", "project", "update", "meeting", "notes", "note", "call",
            "pdf", "docx", "slack", "linear",
        }
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
            token = self._linear_meeting_singular_token(token)
            if len(token) < 3 or token in stop_words:
                continue
            tokens.add(token)
        return tokens

    @staticmethod
    def _linear_meeting_singular_token(token: str) -> str:
        if len(token) > 4 and token.endswith("ies"):
            return f"{token[:-3]}y"
        if len(token) > 4 and token.endswith("s"):
            return token[:-1]
        return token

    def _linear_meeting_project_update_requested(self, text: str, params: dict[str, Any]) -> bool:
        action = self._normalize_match_text(params.get("action"))
        if action in {"projectupdate", "updateproject", "projectstatus"}:
            return True
        request_text = re.sub(
            r"\s+",
            " ",
            str(text or "").lower().replace("’", "'"),
        ).strip()
        negative_patterns = (
            (
                r"\b(?:do\s+not|don't|dont|never)\s+"
                r"(?:write|create|add|post|make|include|generate|send)\s+"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
            (
                r"\b(?:no|not|without)\s+"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
            (
                r"\b(?:skip|omit|avoid)\s+"
                r"(?:(?:writing|creating|adding|posting|making|including|"
                r"generating|sending)\s+)?"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
        )
        if any(re.search(pattern, request_text) for pattern in negative_patterns):
            return False
        normalized_text = self._normalize_match_text(text)
        if "projectupdate" not in normalized_text:
            return False
        return any(
            token in normalized_text
            for token in (
                "linear",
                "meeting",
                "transcript",
                "summary",
                "notes",
                "file",
                "pdf",
                "docx",
                "document",
                "image",
            )
        )

    def _linear_meeting_explicit_creation_authorized(
        self,
        text: str,
        params: dict[str, Any],
    ) -> bool:
        """Return true when the requester clearly authorised task creation.

        This is intentionally narrower than merely mentioning Linear or asking Roo
        to inspect a transcript. Preview/extract-only language never grants writes.
        """

        action = self._normalize_match_text(params.get("action"))
        if action in {"extract", "preview", "review", "draft"}:
            return False
        normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        if re.search(
            r"\b(?:only\s+)?(?:extract|preview|review|draft|show|list)\b[^.!?]{0,80}"
            r"\b(?:tasks?|issues?|tickets?|to-?dos?|action\s+items?)\b",
            normalized,
        ) and not re.search(
            r"\b(?:create|add|write|send|sync|put|file|open)\b[^.!?]{0,100}"
            r"\b(?:tasks?|issues?|tickets?|to-?dos?|action\s+items?)\b",
            normalized,
        ):
            return False
        if action in {"create", "write", "sync", "add"}:
            return True
        has_write_verb = bool(
            re.search(r"\b(?:create|add|write|send|sync|put|file|open)\b", normalized)
        )
        has_task_noun = bool(
            re.search(r"\b(?:tasks?|issues?|tickets?|to-?dos?|todos?|action\s+items?)\b", normalized)
        )
        return has_write_verb and has_task_noun and "linear" in normalized

    def _resolve_linear_meeting_request_project(
        self,
        *,
        sources: list[ParsedSource],
        candidates: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        explicit_project_hint: Optional[str],
        channel_id: Optional[str],
        channel_context: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve one canonical destination shared by updates and task creation."""

        if explicit_project_hint:
            return self._match_linear_meeting_project(
                {"project_hint": explicit_project_hint},
                projects,
                owner_user=None,
                explicit_project_hint=explicit_project_hint,
                channel_id=channel_id,
                channel_context=channel_context,
            )

        source_match = self._match_linear_meeting_project_from_sources(
            sources,
            projects,
            None,
        )
        source_project = source_match.get("project") or {}
        if source_project and float(source_match.get("confidence") or 0.0) >= 0.86:
            return source_match

        matches_by_project: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            hint = str(candidate.get("project_hint") or candidate.get("project") or "").strip()
            if not hint:
                continue
            match = self._match_linear_meeting_project(
                candidate,
                projects,
                owner_user=None,
                explicit_project_hint=hint,
            )
            project = match.get("project") or {}
            project_id = str(project.get("id") or "").strip()
            if project_id and float(match.get("confidence") or 0.0) >= 0.78:
                matches_by_project.setdefault(project_id, []).append(match)

        if matches_by_project:
            ranked = sorted(
                matches_by_project.values(),
                key=lambda values: (len(values), max(float(value.get("confidence") or 0.0) for value in values)),
                reverse=True,
            )
            best = ranked[0]
            second_count = len(ranked[1]) if len(ranked) > 1 else 0
            if len(best) > second_count and len(best) >= max(1, len(candidates) // 2):
                strongest = max(best, key=lambda value: float(value.get("confidence") or 0.0))
                return {
                    **strongest,
                    "confidence": min(
                        0.96,
                        max(float(strongest.get("confidence") or 0.0), 0.9),
                    ),
                    "reason": "Matched project by action-item consensus",
                }

        if source_project:
            return source_match
        return self._match_linear_meeting_project(
            {},
            projects,
            owner_user=None,
            channel_id=channel_id,
            channel_context=channel_context,
        )

    def _match_linear_meeting_project_from_sources(
        self,
        sources: list[ParsedSource],
        projects: list[dict[str, Any]],
        explicit_project_hint: Optional[str],
    ) -> dict[str, Any]:
        if explicit_project_hint:
            return self._match_linear_meeting_project(
                {"project_hint": explicit_project_hint},
                projects,
                owner_user=None,
                explicit_project_hint=explicit_project_hint,
            )

        source_label_text = " ".join(source.label for source in sources if source.label)
        source_text = " ".join(
            f"{source.label} {source.text[:4000]}"
            for source in sources
            if source.text or source.label
        )
        normalized_source = self._normalize_match_text(source_text)
        source_tokens = self._linear_meeting_project_tokens(source_text)
        source_label_tokens = self._linear_meeting_project_tokens(source_label_text)
        best_project = None
        best_score = 0.0
        for project in projects:
            project_name = str(project.get("name") or "")
            core_project_name = self._linear_project_core_name(project_name)
            project_text = " ".join(
                str(value or "")
                for value in (core_project_name, project.get("slugId"))
                if value
            )
            project_tokens = self._linear_meeting_project_tokens(project_text)
            if project_tokens:
                label_overlap = len(project_tokens & source_label_tokens) / len(project_tokens)
                source_overlap = len(project_tokens & source_tokens) / len(project_tokens)
                token_score = max(label_overlap, source_overlap)
                if token_score >= 0.67:
                    score = 0.78 + (0.14 * token_score)
                    if label_overlap >= 0.67:
                        score += 0.04
                    if score > best_score:
                        best_project = project
                        best_score = min(score, 0.94)

            for value in (core_project_name, project.get("name"), project.get("slugId")):
                normalized_project = self._normalize_match_text(value)
                if not normalized_project:
                    continue
                if normalized_project in normalized_source:
                    return {
                        "project": project,
                        "confidence": 0.9,
                        "reason": "Matched project from meeting source",
                    }
                score = SequenceMatcher(None, normalized_project, normalized_source[: max(len(normalized_project) * 3, 80)]).ratio()
                if score > best_score:
                    best_project = project
                    best_score = score
        if best_project and best_score >= 0.65:
            return {
                "project": best_project,
                "confidence": min(best_score, 0.94),
                "reason": "Matched project by source similarity",
            }
        return {"project": None, "confidence": 0.0, "reason": "No project match"}

    @staticmethod
    def _linear_project_core_name(value: Any) -> str:
        """Remove organisational namespace prefixes such as ``[Studio]``."""

        return re.sub(r"^(?:\s*\[[^\]]+\]\s*)+", "", str(value or "")).strip()

    async def _build_linear_meeting_project_update_input(
        self,
        *,
        sources: list[ParsedSource],
        params: dict[str, Any],
        project: dict[str, Any],
        candidates: list[dict[str, Any]],
        recent_issues: Optional[list[dict[str, Any]]] = None,
        settings: Any,
    ) -> dict[str, Any]:
        project_name = str(project.get("name") or "the project")
        source_summary = await self._summarize_linear_project_update_sources(
            sources=sources,
            settings=settings,
            safety_identifier=linear_safety_identifier(
                params.get("requester_slack_id")
            ),
        )
        previous_update = self._format_linear_project_previous_update(project.get("lastUpdate"))
        raw_last_update = project.get("lastUpdate")
        last_update_health = (
            str(raw_last_update.get("health") or "")
            if isinstance(raw_last_update, dict)
            else ""
        )
        project_issue_context = self._format_linear_project_recent_issues(
            project,
            recent_issues or [],
        )
        candidate_lines = "\n".join(
            f"- {candidate.get('title') or candidate.get('task')} ({candidate.get('owner_hint') or 'owner unclear'})"
            for candidate in candidates[:15]
            if candidate.get("title") or candidate.get("task")
        )
        prompt = f"""Write a concise Linear project update for {project_name}.

Use the latest Linear project update as the baseline, and the meeting notes as the primary source for what changed since then.

Use this structure in the body:
- Summary
- Work done since last update
- Decisions made
- Risks / open questions, if any
- Next steps

Keep the update concise. Include only work done, decisions, risks, and next steps supported by the notes or Linear context. Do not invent dates, completion, blockers, or commitments.
End the body with exactly: _Generated by Roo from Slack meeting notes._

Latest Linear project update:
{previous_update or "No previous project update available."}

Relevant recent Linear issues:
{project_issue_context or "No recent project issues available."}

Meeting note summaries:
{source_summary or "No meeting notes available."}

Extracted action candidates:
{candidate_lines or "none"}"""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write concise Linear project updates from meeting notes. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearProjectUpdateResult,
                signals=LinearReasoningSignals(
                    stage="project_update_compose",
                    source_chars=sum(
                        len(value)
                        for value in (
                            source_summary,
                            previous_update,
                            project_issue_context,
                            candidate_lines,
                        )
                    ),
                    source_count=max(1, len(sources)),
                    candidate_count=max(1, len(candidates)),
                    explicit_project=True,
                    explicit_owner=all(
                        bool(candidate.get("owner_hint"))
                        for candidate in candidates
                    ),
                    ambiguity=any(
                        not candidate.get("owner_hint")
                        for candidate in candidates
                    ),
                    conflicting_context=bool(
                        last_update_health == "onTrack"
                        and re.search(
                            r"\b(?:at risk|off track|blocked|delayed|delay|risk)\b",
                            source_summary,
                            flags=re.IGNORECASE,
                        )
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
            body = inference.value.body.strip()
            health = inference.value.health
        except Exception:
            body = ""
            health = "onTrack"

        if not body:
            body = self._fallback_linear_meeting_project_update_body(project_name, candidates)
        body = self._ensure_linear_project_update_footer(body)
        return {
            "project_id": str(project.get("id") or ""),
            "body": body,
            "health": health,
        }

    async def _summarize_linear_project_update_sources(
        self,
        *,
        sources: list[ParsedSource],
        settings: Any,
        safety_identifier: Optional[str] = None,
    ) -> str:
        chunks: list[tuple[str, str]] = []
        for source in sources:
            for chunk in source_text_chunks(source, max_chars=8000):
                chunks.append((source.label, chunk))

        if not chunks:
            return ""

        summaries: list[str] = []
        for index, (label, chunk) in enumerate(chunks, start=1):
            prompt = f"""Summarize this meeting-notes chunk for a Linear project update.

Populate the structured work_done, decisions, risks_open_questions, and next_steps lists.

Only include facts supported by the chunk. Omit empty headings.

Chunk {index} source: {label}
{chunk[:8000]}"""
            try:
                inference = await run_linear_structured_inference(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Summarize meeting notes for concise project updates. "
                                "Return only the structured result."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_format=LinearProjectSourceSummary,
                    signals=LinearReasoningSignals(
                        stage="project_update_summary",
                        source_chars=len(chunk),
                        source_count=max(1, len(chunks)),
                        explicit_project=True,
                        explicit_owner=True,
                    ),
                    safety_identifier=safety_identifier,
                )
                summary = inference.value.to_markdown().strip()
            except Exception:
                summary = ""
            if summary:
                summaries.append(f"### Source chunk {index}: {label}\n{summary[:3000]}")
        return "\n\n".join(summaries)

    def _format_linear_project_previous_update(self, last_update: Any) -> str:
        if not isinstance(last_update, dict) or not last_update:
            return ""
        author = last_update.get("user") or {}
        author_label = (
            author.get("displayName")
            or author.get("name")
            or author.get("email")
            or "unknown"
        )
        parts = [
            f"- Created: {last_update.get('createdAt') or 'unknown'}",
            f"- Health: {last_update.get('health') or 'unknown'}",
            f"- Author: {author_label}",
        ]
        if last_update.get("url"):
            parts.append(f"- URL: {last_update['url']}")
        body = str(last_update.get("body") or "").strip()
        if body:
            parts.extend(["", body[:4000]])
        return "\n".join(parts).strip()

    def _format_linear_project_recent_issues(
        self,
        project: dict[str, Any],
        recent_issues: list[dict[str, Any]],
    ) -> str:
        project_id = str(project.get("id") or "")
        if not project_id:
            return ""
        lines: list[str] = []
        for issue in recent_issues:
            issue_project_id = str(((issue.get("project") or {}).get("id")) or "")
            if issue_project_id != project_id:
                continue
            assignee = issue.get("assignee") or {}
            assignee_label = (
                assignee.get("displayName")
                or assignee.get("name")
                or assignee.get("email")
                or "unassigned"
            )
            state = (issue.get("state") or {}).get("name") or "unknown"
            identifier = issue.get("identifier") or issue.get("id") or "issue"
            lines.append(f"- {identifier}: {issue.get('title') or 'Untitled'} [{state}, {assignee_label}]")
            if len(lines) >= 20:
                break
        return "\n".join(lines)

    @staticmethod
    def _ensure_linear_project_update_footer(body: str) -> str:
        footer = "_Generated by Roo from Slack meeting notes._"
        cleaned = str(body or "").strip()
        if footer.lower() in cleaned.lower():
            return cleaned
        return f"{cleaned}\n\n{footer}".strip()

    def _fallback_linear_meeting_project_update_body(
        self,
        project_name: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        lines = [
            f"## {project_name} meeting update",
            "",
            "Roo generated this update from meeting notes.",
            "",
            "### Action items",
        ]
        action_lines = [
            f"- {candidate.get('title') or candidate.get('task')}"
            for candidate in candidates[:12]
            if candidate.get("title") or candidate.get("task")
        ]
        lines.extend(action_lines or ["- No concrete action items were extracted."])
        return "\n".join(lines).strip()

    def _normalize_linear_meeting_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        title = str(candidate.get("title") or candidate.get("task") or "").strip()
        description = str(candidate.get("description") or candidate.get("details") or "").strip()
        work_status = str(
            candidate.get("work_status") or candidate.get("status") or "open"
        ).strip().lower()
        completed_work = str(candidate.get("completed_work") or "").strip()
        remaining_work = str(
            candidate.get("remaining_work") or description or title
        ).strip()

        def normalized_string_list(value: Any) -> list[str]:
            if isinstance(value, str):
                values = re.split(r"[\n,;]+", value)
            elif isinstance(value, (list, tuple, set)):
                values = list(value)
            else:
                values = []
            return [
                str(item).strip()[:500]
                for item in values
                if str(item).strip()
            ][:20]

        available_artifacts = normalized_string_list(
            candidate.get("available_artifacts") or candidate.get("artifacts")
        )
        dependencies = normalized_string_list(candidate.get("dependencies"))
        acceptance_criteria = normalized_string_list(
            candidate.get("acceptance_criteria")
        )
        owner_hint = str(candidate.get("owner_hint") or candidate.get("owner") or "").strip()
        project_hint = str(candidate.get("project_hint") or candidate.get("project") or "").strip()
        team_hint = str(candidate.get("team_hint") or candidate.get("team") or "").strip()
        due_expression = str(candidate.get("due_expression") or candidate.get("due") or "").strip()
        due_date = candidate.get("due_date") or candidate.get("dueDate")
        due_date = str(due_date).strip() if due_date else None
        if due_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
            due_date = None
        try:
            priority = int(candidate.get("priority", 3))
        except (TypeError, ValueError):
            priority = 3
        priority = min(max(priority, 0), 4)
        try:
            confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        evidence = str(candidate.get("evidence") or candidate.get("quote") or "").strip()
        evidence_message_ts = str(
            candidate.get("evidence_message_ts") or candidate.get("evidenceMessageTs") or ""
        ).strip()
        explicit_commitment_value = candidate.get("explicit_commitment", False)
        if isinstance(explicit_commitment_value, str):
            explicit_commitment = explicit_commitment_value.strip().lower() in {
                "1",
                "true",
                "yes",
            }
        else:
            explicit_commitment = bool(explicit_commitment_value)
        source_label = str(candidate.get("source_label") or candidate.get("source") or "").strip()
        return {
            "title": title[:180],
            "description": description,
            "work_status": work_status[:40],
            "completed_work": completed_work[:3000],
            "remaining_work": remaining_work[:3000],
            "available_artifacts": available_artifacts,
            "dependencies": dependencies,
            "acceptance_criteria": acceptance_criteria,
            "owner_hint": owner_hint,
            "project_hint": project_hint,
            "team_hint": team_hint,
            "due_expression": due_expression[:120],
            "due_date": due_date,
            "priority": priority,
            "evidence": evidence[:700],
            "evidence_message_ts": evidence_message_ts[:40],
            "explicit_commitment": explicit_commitment,
            "source_label": source_label[:300],
            "confidence": confidence,
        }

    def _normalize_linear_meeting_due_date(
        self,
        candidate: dict[str, Any],
        slack_context: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve common relative due expressions from the evidence timestamp."""
        expression = str(candidate.get("due_expression") or "").strip()
        evidence = str(candidate.get("evidence") or "")
        if not expression:
            relative_match = re.search(
                r"\b(EOW|end of (?:the )?week|today|tomorrow)\b",
                evidence,
                flags=re.IGNORECASE,
            )
            expression = relative_match.group(1) if relative_match else ""
        if not expression:
            return candidate

        normalized_expression = expression.lower().strip()
        supported_relative_expressions = {
            "eow",
            "end of week",
            "end of the week",
            "today",
            "tomorrow",
        }
        if normalized_expression not in supported_relative_expressions:
            return candidate

        evidence_message = self._resolve_linear_evidence_message(candidate, slack_context)
        local_value = str((evidence_message or {}).get("local_datetime") or "")
        if not local_value:
            local_value = str(((slack_context or {}).get("request") or {}).get("local_datetime") or "")
        try:
            base_date = datetime.fromisoformat(local_value).date()
        except (TypeError, ValueError):
            return candidate

        due: Optional[date] = None
        if normalized_expression in {"eow", "end of week", "end of the week"}:
            days_until_friday = 4 - base_date.weekday()
            if days_until_friday < 0:
                days_until_friday += 7
            due = base_date + timedelta(days=days_until_friday)
        elif normalized_expression == "today":
            due = base_date
        elif normalized_expression == "tomorrow":
            due = base_date + timedelta(days=1)

        if due:
            return {**candidate, "due_expression": expression, "due_date": due.isoformat()}
        return candidate

    def _resolve_linear_evidence_message(
        self,
        candidate: dict[str, Any],
        slack_context: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        messages = [
            message
            for message in ((slack_context or {}).get("messages") or [])
            if isinstance(message, dict) and not message.get("is_bot")
        ]
        if not messages:
            return None

        evidence_ts = str(candidate.get("evidence_message_ts") or "").strip()
        if evidence_ts:
            for message in messages:
                if str(message.get("ts") or "") == evidence_ts:
                    return message

        evidence = self._normalize_linear_evidence_text(candidate.get("evidence"))
        if evidence:
            for message in reversed(messages):
                message_text = self._normalize_linear_evidence_text(message.get("text"))
                if evidence in message_text or message_text in evidence:
                    return message

        request_ts = str(((slack_context or {}).get("request") or {}).get("message_ts") or "")
        prior_messages = [
            message for message in messages if str(message.get("ts") or "") != request_ts
        ]
        return prior_messages[-1] if prior_messages else messages[-1]

    @staticmethod
    def _normalize_linear_evidence_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip(" .,:;!?\"'“”")

    @staticmethod
    def _linear_source_permalink(
        channel_id: Optional[str],
        message_ts: str,
    ) -> Optional[str]:
        if not channel_id or not message_ts:
            return None
        try:
            from ..slack_client import get_message_permalink

            return get_message_permalink(channel_id, message_ts)
        except Exception:
            return None

    def _match_linear_meeting_owner(
        self,
        owner_hint: Optional[str],
        users: list[dict[str, Any]],
    ) -> dict[str, Any]:
        hint = str(owner_hint or "").strip()
        if not hint:
            return {"user": None, "confidence": 0.0, "reason": "No owner hint"}

        emails = set(re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", hint.lower()))
        mention_match = re.search(r"<@([A-Z0-9]+)>", hint)
        if mention_match:
            try:
                from ..slack_client import get_user_info

                slack_info = get_user_info(mention_match.group(1))
                slack_email = str(slack_info.get("email") or "").strip().lower()
                if slack_email:
                    emails.add(slack_email)
            except Exception:
                pass

        for user in users:
            email = str(user.get("email") or "").strip().lower()
            if email and email in emails:
                return {"user": user, "confidence": 0.98, "reason": "Matched owner by email"}

        normalized_hint = self._normalize_match_text(hint)
        plain_matches = self._find_unique_linear_user_name_matches(hint, users)
        if len(plain_matches) == 1:
            return {
                "user": plain_matches[0],
                "confidence": 0.94,
                "reason": "Matched owner by unique name",
            }
        if len(plain_matches) > 1:
            return {"user": None, "confidence": 0.0, "reason": "Ambiguous owner hint"}

        best_user = None
        best_score = 0.0
        best_reason = "No user match"
        for user in users:
            names = [
                user.get("displayName"),
                user.get("name"),
                user.get("email"),
            ]
            for name in names:
                normalized_name = self._normalize_match_text(name)
                if not normalized_name:
                    continue
                if normalized_hint == normalized_name:
                    return {"user": user, "confidence": 0.92, "reason": "Matched owner by name"}
                if normalized_name in normalized_hint or normalized_hint in normalized_name:
                    score = 0.86
                else:
                    score = SequenceMatcher(None, normalized_hint, normalized_name).ratio()
                if score > best_score:
                    best_user = user
                    best_score = score
                    best_reason = "Matched owner by name similarity"

        if best_user and best_score >= 0.82:
            return {"user": best_user, "confidence": min(best_score, 0.84), "reason": best_reason}
        return {"user": None, "confidence": 0.0, "reason": "No user match"}

    def _find_unique_linear_user_name_matches(
        self,
        owner_hint: str,
        users: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        hint_tokens = self._linear_person_name_tokens(owner_hint)
        if not hint_tokens:
            return []
        matches: list[dict[str, Any]] = []
        for user in users:
            user_tokens = self._linear_user_name_tokens(user)
            if not user_tokens:
                continue
            if hint_tokens & user_tokens:
                matches.append(user)
        return self._dedupe_linear_users_by_id(matches)

    def _linear_user_name_tokens(self, user: dict[str, Any]) -> set[str]:
        values = [user.get("displayName"), user.get("name"), user.get("email")]
        tokens: set[str] = set()
        for value in values:
            tokens.update(self._linear_person_name_tokens(str(value or "").split("@", 1)[0]))
        return tokens

    @staticmethod
    def _linear_person_name_tokens(value: str) -> set[str]:
        stop_words = {"assign", "assigned", "owner", "assignee", "to", "for", "it", "this", "task", "issue"}
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 3 and token not in stop_words
        }
        return tokens

    @staticmethod
    def _dedupe_linear_users_by_id(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for user in users:
            key = str(user.get("id") or user.get("email") or user.get("name") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(user)
        return deduped

    async def _resolve_explicit_linear_project_context(
        self,
        *,
        client: Any,
        params: dict[str, Any],
        projects: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        hint = str(params.get("project_hint") or "").strip()
        if not hint:
            return projects, None
        context_project_count = len(projects)

        snapshot_match = self._match_linear_meeting_project(
            {},
            projects,
            None,
            hint,
        )
        snapshot_confidence = float(snapshot_match.get("confidence") or 0.0)
        if snapshot_match.get("project") and snapshot_confidence >= 0.95:
            payload = {
                "status": "matched_snapshot",
                "project": snapshot_match["project"],
                "confidence": snapshot_confidence,
                "reason": snapshot_match.get("reason"),
            }
            self._log_linear_project_resolution(
                hint=hint,
                active_project_count=context_project_count,
                payload=payload,
            )
            return projects, payload

        resolver = getattr(client, "resolve_project", None)
        if not callable(resolver):
            return projects, None

        try:
            payload = await resolver(hint)
        except Exception as exc:
            if snapshot_match.get("project") and snapshot_confidence >= 0.78:
                payload = {
                    "status": "matched_snapshot",
                    "project": snapshot_match["project"],
                    "confidence": snapshot_confidence,
                    "reason": snapshot_match.get("reason"),
                    "lookupError": exc.__class__.__name__,
                }
                self._log_linear_project_resolution(
                    hint=hint,
                    active_project_count=context_project_count,
                    payload=payload,
                )
                return projects, payload
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": exc.__class__.__name__,
            }
            self._log_linear_project_resolution(
                hint=hint,
                active_project_count=context_project_count,
                payload=payload,
            )
            return projects, payload

        if not isinstance(payload, dict):
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": "Invalid project resolver response",
            }
        status = str(payload.get("status") or "")
        resolved_project = payload.get("project")
        resolved_confidence = float(payload.get("confidence") or 0.0)
        if (
            status == "matched"
            and isinstance(resolved_project, dict)
            and resolved_project.get("id")
            and resolved_confidence >= 0.82
        ):
            canonical_name = str(resolved_project.get("name") or "").strip()
            if canonical_name:
                params["project_hint"] = canonical_name
            projects = self._dedupe_linear_projects_by_id(
                [resolved_project, *projects]
            )
        elif status not in {"not_found", "ambiguous"}:
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": "Invalid project resolver response",
            }
        self._log_linear_project_resolution(
            hint=hint,
            active_project_count=context_project_count,
            payload=payload,
        )
        return projects, payload

    @staticmethod
    def _log_linear_project_resolution(
        *,
        hint: str,
        active_project_count: int,
        payload: dict[str, Any],
    ) -> None:
        project = payload.get("project")
        project = project if isinstance(project, dict) else {}
        print(
            "LINEAR_PROJECT_RESOLUTION "
            + json.dumps(
                {
                    "event": "explicit_project_lookup",
                    "hint": hint,
                    "active_project_count": active_project_count,
                    "status": payload.get("status"),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "matched_project_id": project.get("id"),
                    "matched_project_name": project.get("name"),
                    "is_inactive": payload.get("isInactive"),
                    "candidate_count": payload.get("candidateCount"),
                    "lookup_error": payload.get("lookupError"),
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    @staticmethod
    def _linear_explicit_project_resolution_error_message(
        hint: str,
        payload: dict[str, Any],
    ) -> str:
        status = str(payload.get("status") or "")
        if status == "ambiguous":
            candidate_names = [
                str(candidate.get("name") or "").strip()
                for candidate in payload.get("candidates") or []
                if isinstance(candidate, dict) and candidate.get("name")
            ]
            choices = f" Matches: {', '.join(candidate_names)}." if candidate_names else ""
            return (
                f"I found multiple Linear projects matching {hint!r}.{choices} "
                "Please use the exact project title. Nothing was changed."
            )
        if status == "not_found":
            return (
                f"I couldn't find a Linear project matching {hint!r} in the full "
                "workspace. Check the exact title or Roo's access. Nothing was changed."
            )
        return (
            f"I couldn't verify the Linear project {hint!r} because the full project "
            "lookup is temporarily unavailable. Nothing was changed. Please try again."
        )

    def _match_linear_meeting_project(
        self,
        candidate: dict[str, Any],
        projects: list[dict[str, Any]],
        owner_user: Optional[dict[str, Any]],
        explicit_project_hint: Optional[str] = None,
        *,
        channel_id: Optional[str] = None,
        channel_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        hint = str(explicit_project_hint or candidate.get("project_hint") or "").strip()
        scored_by_project: dict[str, tuple[float, dict[str, Any], str]] = {}

        def record(project: dict[str, Any], score: float, reason: str) -> None:
            key = str(project.get("id") or project.get("name") or project.get("slugId") or "")
            if not key:
                return
            existing = scored_by_project.get(key)
            if existing is None or score > existing[0]:
                scored_by_project[key] = (score, project, reason)

        if hint:
            normalized_hint = self._normalize_match_text(hint)
            normalized_core_hint = self._normalize_match_text(
                self._linear_project_core_name(hint)
            )
            for project in projects:
                project_name = project.get("name")
                project_slug = project.get("slugId")
                normalized_project_name = self._normalize_match_text(project_name)
                if normalized_project_name and normalized_hint == normalized_project_name:
                    record(project, 0.98, "Matched project by exact name")
                normalized_core_project_name = self._normalize_match_text(
                    self._linear_project_core_name(project_name)
                )
                if (
                    normalized_core_hint
                    and normalized_core_project_name
                    and normalized_core_hint == normalized_core_project_name
                ):
                    record(project, 0.97, "Matched project by namespace-independent name")
                normalized_slug = self._normalize_match_text(project_slug)
                if normalized_slug and normalized_hint == normalized_slug:
                    record(project, 0.96, "Matched project by exact slug")
                for value in (
                    project_name,
                    self._linear_project_core_name(project_name),
                    project_slug,
                ):
                    normalized_value = self._normalize_match_text(value)
                    if not normalized_value:
                        continue
                    if normalized_value in normalized_hint or normalized_hint in normalized_value:
                        record(project, 0.88, "Matched project by name similarity")
                    else:
                        similarity = SequenceMatcher(None, normalized_hint, normalized_value).ratio()
                        if similarity >= 0.78:
                            record(
                                project,
                                min(similarity, 0.86),
                                "Matched project by name similarity",
                            )

                semantic_score = self._linear_project_semantic_score(hint, project)
                if semantic_score >= 0.78:
                    record(project, semantic_score, "Matched project by Linear context")

        if channel_id:
            for project in projects:
                if str(project.get("slackChannelId") or "").strip() == str(channel_id):
                    record(project, 0.97, "Matched project's linked Slack channel")

        if not hint and channel_context:
            channel_hint = " ".join(
                str(channel_context.get(key) or "")
                for key in ("name", "topic", "purpose")
            ).strip()
            if channel_hint:
                for project in projects:
                    channel_score = self._linear_project_semantic_score(channel_hint, project)
                    if channel_score >= 0.82:
                        record(
                            project,
                            min(channel_score, 0.86),
                            "Matched project from Slack channel context",
                        )

        ranked = sorted(scored_by_project.values(), key=lambda item: item[0], reverse=True)
        if ranked:
            best_score, best_project, best_reason = ranked[0]
            second_score = ranked[1][0] if len(ranked) > 1 else 0.0
            if second_score >= 0.78 and best_score - second_score < 0.05:
                return {"project": None, "confidence": 0.0, "reason": "Ambiguous project hint"}
            if best_score >= 0.78:
                return {
                    "project": best_project,
                    "confidence": best_score,
                    "reason": best_reason,
                }

        owner_id = str((owner_user or {}).get("id") or "")
        owner_email = str((owner_user or {}).get("email") or "").strip().lower()
        member_projects = []
        if owner_id or owner_email:
            for project in projects:
                if project.get("membersSource") == "team_fallback":
                    continue
                members = self._linear_connection_nodes(project.get("members"))
                lead = project.get("lead")
                participants = members + ([lead] if isinstance(lead, dict) else [])
                for member in participants:
                    if str(member.get("id") or "") == owner_id or (
                        owner_email
                        and str(member.get("email") or "").strip().lower() == owner_email
                    ):
                        member_projects.append(project)
                        break
        if len(member_projects) == 1:
            return {
                "project": member_projects[0],
                "confidence": 0.7,
                "reason": "Only active project found for owner",
            }
        return {"project": None, "confidence": 0.0, "reason": "No project match"}

    def _linear_project_semantic_score(
        self,
        hint: str,
        project: dict[str, Any],
    ) -> float:
        hint_tokens = self._linear_project_match_tokens(hint)
        if not hint_tokens:
            return 0.0
        recent_issue_titles = " ".join(
            str(issue.get("title") or "")
            for issue in (project.get("recentIssues") or [])[:20]
            if isinstance(issue, dict)
        )
        context = " ".join(
            [
                str(project.get("name") or ""),
                str(project.get("slugId") or ""),
                str(project.get("description") or ""),
                str(project.get("content") or ""),
                str((project.get("lastUpdate") or {}).get("body") or ""),
                recent_issue_titles,
            ]
        )
        context_tokens = self._linear_project_match_tokens(context)
        if not context_tokens:
            return 0.0
        coverage = len(hint_tokens & context_tokens) / len(hint_tokens)
        normalized_hint = self._normalize_match_text(hint)
        normalized_context = self._normalize_match_text(context)
        if len(normalized_hint) >= 6 and normalized_hint in normalized_context:
            return 0.92
        if coverage == 1.0 and len(hint_tokens) >= 2:
            return 0.9
        if coverage >= 0.75:
            return 0.84
        return 0.0

    @staticmethod
    def _linear_project_match_tokens(value: Any) -> set[str]:
        stop_words = {
            "about", "active", "from", "into", "linear", "project", "slack",
            "task", "team", "that", "the", "this", "with",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 3 and token not in stop_words
        }

    @staticmethod
    def _dedupe_linear_projects_by_id(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for project in projects:
            key = str(project.get("id") or project.get("name") or project.get("slugId") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(project)
        return deduped

    def _match_linear_meeting_team(
        self,
        project: Optional[dict[str, Any]],
        teams: list[dict[str, Any]],
        team_hint: Optional[str],
        default_team: Optional[str],
    ) -> dict[str, Any]:
        if project:
            project_teams = self._linear_connection_nodes(project.get("teams"))
            if project_teams:
                accessible_project_teams: list[dict[str, Any]] = []
                accessible_by_id = {
                    str(team.get("id") or "").strip(): team
                    for team in teams
                    if str(team.get("id") or "").strip()
                }
                for project_team in project_teams:
                    project_team_id = str(project_team.get("id") or "").strip()
                    accessible_team = accessible_by_id.get(project_team_id)
                    if accessible_team:
                        accessible_project_teams.append(accessible_team)

                if not accessible_project_teams:
                    return {
                        "team": None,
                        "confidence": 0.0,
                        "reason": (
                            "Roo's Linear API key cannot access the matched project's team"
                        ),
                    }

                hint = str(team_hint or "").strip()
                if hint:
                    hinted_team = self._find_linear_team_by_hint(
                        accessible_project_teams,
                        hint,
                    )
                    if hinted_team:
                        return {
                            "team": hinted_team,
                            "confidence": 0.97,
                            "reason": "Using hinted team from matched project",
                        }
                return {
                    "team": accessible_project_teams[0],
                    "confidence": 0.96,
                    "reason": "Using matched project's team",
                }

        hint = str(team_hint or "").strip()
        if hint:
            match = self._find_linear_team_by_hint(teams, hint)
            if match:
                return {"team": match, "confidence": 0.9, "reason": "Matched team by hint"}

        if default_team:
            match = self._find_linear_team_by_hint(teams, default_team)
            if match:
                return {
                    "team": match,
                    "confidence": 0.72,
                    "reason": "Using configured default team",
                }
        return {"team": None, "confidence": 0.0, "reason": "No team match"}

    def _find_linear_team_by_hint(
        self,
        teams: list[dict[str, Any]],
        hint: str,
    ) -> Optional[dict[str, Any]]:
        normalized_hint = self._normalize_match_text(hint)
        for team in teams:
            values = [team.get("id"), team.get("key"), team.get("name")]
            if any(self._normalize_match_text(value) == normalized_hint for value in values):
                return team
        for team in teams:
            normalized_values = [
                self._normalize_match_text(value)
                for value in (team.get("key"), team.get("name"))
            ]
            if any(value and (value in normalized_hint or normalized_hint in value) for value in normalized_values):
                return team
        return None

    def _find_linear_meeting_duplicate(
        self,
        candidate: dict[str, Any],
        issues: list[dict[str, Any]],
        project: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        title = self._normalize_match_text(candidate.get("title"))
        if not title:
            return None
        project_id = str((project or {}).get("id") or "")
        for issue in issues:
            if project_id:
                issue_project_id = str(((issue.get("project") or {}).get("id")) or "")
                if issue_project_id and issue_project_id != project_id:
                    continue
            issue_title = self._normalize_match_text(issue.get("title"))
            if not issue_title:
                continue
            if title == issue_title or title in issue_title or issue_title in title:
                return issue
            title_tokens = self._linear_meeting_duplicate_tokens(candidate.get("title"))
            issue_tokens = self._linear_meeting_duplicate_tokens(issue.get("title"))
            if title_tokens and issue_tokens:
                overlap = len(title_tokens & issue_tokens) / min(len(title_tokens), len(issue_tokens))
                if overlap >= 0.75:
                    return issue
            if SequenceMatcher(None, title, issue_title).ratio() >= 0.88:
                return issue
        return None

    def _linear_meeting_candidate_decision(
        self,
        *,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        duplicate: Optional[dict[str, Any]],
        auto_threshold: float,
        uncertain_threshold: float,
    ) -> tuple[str, float]:
        if duplicate:
            return "duplicate", 1.0
        confidence_parts = [
            float(candidate.get("confidence") or 0.0),
            float(owner_match.get("confidence") or 0.0),
            float(project_match.get("confidence") or 0.0),
            float(team_match.get("confidence") or 0.0),
        ]
        overall = min(confidence_parts)
        if overall >= auto_threshold:
            return "create", overall
        if overall >= uncertain_threshold:
            return "review", overall
        return "skip", overall

    def _linear_meeting_skip_reason(
        self,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        uncertain_threshold: float,
    ) -> str:
        if float(candidate.get("confidence") or 0.0) < uncertain_threshold:
            return "Candidate confidence too low"
        if float(owner_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(owner_match.get("reason") or "")
            if "ambiguous" in reason.lower():
                return "Assignee unclear: multiple Linear users matched"
            return "Assignee unclear"
        if float(project_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(project_match.get("reason") or "")
            if "ambiguous" in reason.lower():
                return "Project unclear: multiple Linear projects matched"
            return "Project unclear"
        if float(team_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(team_match.get("reason") or "").strip()
            if "api key cannot access" in reason.lower():
                return reason
            return "Team unclear"
        return "Low confidence mapping"

    async def _assess_linear_studio_candidates_resilient(
        self,
        *,
        candidates: list[dict[str, Any]],
        project_context: dict[str, Any],
        context_max_chars: int,
        batch_size: int,
        safety_identifier: str,
        max_concurrency: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Size bounded batches and isolate a failed batch to single candidates."""

        assessments: dict[str, Any] = {}
        errors: dict[str, str] = {}
        batch_size = max(1, min(int(batch_size or 3), 10))
        chunks = [
            candidates[index : index + batch_size]
            for index in range(0, len(candidates), batch_size)
        ]
        semaphore = asyncio.Semaphore(max(1, min(int(max_concurrency or 1), 4)))

        async def assess_chunk(
            chunk_index: int,
            chunk: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, str]]:
            chunk_assessments: dict[str, Any] = {}
            chunk_errors: dict[str, str] = {}
            async with semaphore:
                try:
                    chunk_assessments.update(
                        await assess_studio_effort_batch(
                            candidates=chunk,
                            project_context=project_context,
                            context_max_chars=context_max_chars,
                            safety_identifier=safety_identifier,
                            batch_chunk_index=chunk_index,
                            batch_chunk_count=len(chunks),
                        )
                    )
                    return chunk_assessments, chunk_errors
                except Exception as batch_exc:
                    if len(chunk) == 1:
                        candidate_key = str(chunk[0]["candidate_key"])
                        chunk_errors[candidate_key] = (
                            f"{batch_exc.__class__.__name__}: {batch_exc}"
                        )
                        return chunk_assessments, chunk_errors

                for candidate in chunk:
                    candidate_key = str(candidate["candidate_key"])
                    try:
                        chunk_assessments.update(
                            await assess_studio_effort_batch(
                                candidates=[candidate],
                                project_context=project_context,
                                context_max_chars=context_max_chars,
                                safety_identifier=safety_identifier,
                                batch_chunk_index=chunk_index,
                                batch_chunk_count=len(chunks),
                                recovery_depth=1,
                            )
                        )
                    except Exception as candidate_exc:
                        chunk_errors[candidate_key] = (
                            f"{candidate_exc.__class__.__name__}: {candidate_exc}"
                        )
                return chunk_assessments, chunk_errors

        results = await asyncio.gather(
            *[
                assess_chunk(chunk_index, chunk)
                for chunk_index, chunk in enumerate(chunks, start=1)
            ]
        )
        for chunk_assessments, chunk_errors in results:
            assessments.update(chunk_assessments)
            errors.update(chunk_errors)
        return assessments, errors

    async def _size_linear_studio_prepared_candidates(
        self,
        *,
        prepared_candidates: list[dict[str, Any]],
        client: Any,
        labels: list[dict[str, Any]],
        settings: Any,
        requester_slack_id: str,
    ) -> list[dict[str, Any]]:
        mode = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_MODE",
                "LINEAR_STUDIO_SIZING_MODE",
                "off",
            )
        ).strip().lower()
        if mode not in {"off", "shadow", "review", "required"}:
            mode = "off"
        if mode == "off":
            return []

        project_candidates = [
            prepared
            for prepared in prepared_candidates
            if is_project_issue(prepared["project_match"].get("project") or {})
        ]
        receipt_reader = getattr(client, "get_issue_receipt", None)
        if callable(receipt_reader) and project_candidates:
            async def read_receipt(prepared: dict[str, Any]) -> dict[str, Any]:
                try:
                    return await receipt_reader(prepared["idempotency_key"])
                except Exception:
                    return {}

            receipts = await asyncio.gather(
                *[read_receipt(prepared) for prepared in project_candidates]
            )
            for prepared, receipt in zip(project_candidates, receipts):
                status = str(receipt.get("status") or "")
                issue = receipt.get("issue")
                if status == "completed" and isinstance(issue, dict):
                    prepared["receipt_replay_issue"] = issue
                    sizing = issue.get("sizingMetadata") or receipt.get("sizingMetadata")
                    if isinstance(sizing, dict):
                        prepared["display"]["effort_label"] = (
                            sizing.get("effort_label")
                            or sizing.get("effortLabel")
                            or ""
                        )
                        prepared["display"]["effort_rationale"] = sizing.get(
                            "rationale"
                        ) or ""
                elif status == "failed" and isinstance(
                    receipt.get("sizingMetadata"),
                    dict,
                ):
                    prepared["receipt_sizing_metadata"] = dict(
                        receipt["sizingMetadata"]
                    )

        groups: dict[str, list[dict[str, Any]]] = {}
        for prepared in project_candidates:
            project = prepared["project_match"].get("project") or {}
            project_id = str(project.get("id") or "")
            if project_id and not prepared.get("receipt_replay_issue"):
                groups.setdefault(project_id, []).append(prepared)

        shadow_results: list[dict[str, Any]] = []
        for project_id, group in groups.items():
            try:
                project_context = await client.get_project_sizing_context(project_id)
                live_project = project_context.get("project")
                if not isinstance(live_project, dict) or str(
                    live_project.get("id") or ""
                ) != project_id:
                    raise RuntimeError("Linear returned mismatched project sizing context.")
                if not is_project_issue(live_project):
                    raise RuntimeError("Linear returned project context without an ID.")
                registry = project_context.get("effortLabelRegistry")
                registry_labels = (
                    registry.get("nodes")
                    if isinstance(registry, dict)
                    and isinstance(registry.get("nodes"), list)
                    else labels
                )
            except Exception as exc:
                detail = f"{exc.__class__.__name__}: {exc}"
                shadow_results.append(
                    {
                        "project_id": project_id,
                        "error": detail,
                    }
                )
                if mode in {"review", "required"}:
                    for prepared in group:
                        prepared["sizing_error"] = detail
                        prepared["studio_sizing_error"] = detail
                continue

            sizing_candidates: list[dict[str, Any]] = []
            for prepared in group:
                if prepared.get("receipt_sizing_metadata"):
                    continue
                candidate = prepared["candidate"]
                source = prepared["source"]
                owner = prepared["owner_match"].get("user") or {}
                team = prepared["team_match"].get("team") or {}
                sizing_candidates.append(
                    {
                        "candidate_key": prepared["candidate_key"],
                        "title": candidate.get("title"),
                        "description": candidate.get("description"),
                        "work_status": candidate.get("work_status"),
                        "completed_work": candidate.get("completed_work"),
                        "remaining_work": candidate.get("remaining_work"),
                        "available_artifacts": candidate.get("available_artifacts"),
                        "dependencies": candidate.get("dependencies"),
                        "acceptance_criteria": candidate.get("acceptance_criteria"),
                        "evidence": candidate.get("evidence"),
                        "source_label": candidate.get("source_label"),
                        "source_permalink": source.get("source_permalink"),
                        "due_date": candidate.get("due_date"),
                        "assignee": {
                            "id": owner.get("id"),
                            "name": owner.get("displayName")
                            or owner.get("name")
                            or owner.get("email"),
                        },
                        "team": {
                            "id": team.get("id"),
                            "key": team.get("key"),
                            "name": team.get("name"),
                        },
                    }
                )
            context_max_chars = int(
                _linear_task_sizing_setting(
                    settings,
                    "LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS",
                    "LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS",
                    40000,
                )
            )
            assessments, sizing_errors = (
                await self._assess_linear_studio_candidates_resilient(
                    candidates=sizing_candidates,
                    project_context=project_context,
                    context_max_chars=context_max_chars,
                    batch_size=int(
                        _linear_task_sizing_setting(
                            settings,
                            "LINEAR_TASK_SIZING_BATCH_SIZE",
                            "LINEAR_STUDIO_SIZING_BATCH_SIZE",
                            3,
                        )
                    ),
                    safety_identifier=linear_safety_identifier(
                        requester_slack_id
                    ),
                )
                if sizing_candidates
                else ({}, {})
            )
            for prepared in group:
                try:
                    stored_metadata = prepared.get("receipt_sizing_metadata")
                    if isinstance(stored_metadata, dict):
                        metadata = stored_metadata
                        effort_label = str(
                            metadata.get("effort_label")
                            or metadata.get("effortLabel")
                            or ""
                        )
                        rationale = str(metadata.get("rationale") or "")
                        confidence = float(metadata.get("confidence") or 0.0)
                        context_sufficient = bool(
                            metadata.get("context_sufficient")
                            if "context_sufficient" in metadata
                            else metadata.get("contextSufficient", False)
                        )
                    else:
                        candidate_key = str(prepared["candidate_key"])
                        assessment = assessments.get(candidate_key)
                        if assessment is None:
                            raise RuntimeError(
                                sizing_errors.get(
                                    candidate_key,
                                    "Effort sizing returned no assessment.",
                                )
                            )
                        metadata = assessment_metadata(
                            assessment,
                            project=live_project,
                            model=LINEAR_SKILL_MODEL,
                            rubric_version=str(
                                _linear_task_sizing_setting(
                                    settings,
                                    "LINEAR_TASK_SIZING_RUBRIC_VERSION",
                                    "LINEAR_STUDIO_SIZING_RUBRIC_VERSION",
                                    "project-effort-v2",
                                )
                            ),
                        )
                        effort_label = assessment.effort_label
                        rationale = assessment.rationale
                        confidence = assessment.confidence
                        context_sufficient = assessment.context_sufficient
                    team = prepared["team_match"].get("team") or {}
                    compatible_label_ids = self._linear_compatible_label_ids(
                        registry_labels,
                        label_name=effort_label,
                        team_id=str(team.get("id") or ""),
                        exact_name=True,
                    )
                    if len(compatible_label_ids) != 1:
                        raise RuntimeError(
                            f"Expected exactly one compatible Linear label named "
                            f"{effort_label!r}; found {len(compatible_label_ids)}."
                        )
                    shadow_results.append(
                        {
                            "candidate_key": prepared["candidate_key"],
                            "project": str(live_project.get("name") or ""),
                            "effort_label": effort_label,
                            "rationale": rationale,
                            "confidence": confidence,
                            "context_sufficient": context_sufficient,
                            "reused_receipt_assessment": bool(stored_metadata),
                        }
                    )
                    if mode == "shadow":
                        continue
                    prepared["effort_assessment"] = metadata
                    prepared["effort_label_id"] = compatible_label_ids[0]
                    prepared["display"]["effort_label"] = effort_label
                    prepared["display"]["effort_rationale"] = rationale
                except Exception as exc:
                    detail = f"{exc.__class__.__name__}: {exc}"
                    shadow_results.append(
                        {
                            "candidate_key": prepared["candidate_key"],
                            "project_id": project_id,
                            "error": detail,
                        }
                    )
                    if mode in {"review", "required"}:
                        prepared["sizing_error"] = detail
                        prepared["studio_sizing_error"] = detail
        return shadow_results

    def _linear_compatible_label_ids(
        self,
        labels: list[dict[str, Any]],
        *,
        label_name: str,
        team_id: str,
        exact_name: bool = False,
    ) -> list[str]:
        selected: list[dict[str, Any]] = []
        for label in labels:
            if not isinstance(label, dict) or label.get("archivedAt"):
                continue
            name_matches = (
                str(label.get("name") or "") == label_name
                if exact_name
                else self._normalize_match_text(label.get("name"))
                == self._normalize_match_text(label_name)
            )
            if not name_matches:
                continue
            label_team = label.get("team")
            label_team_id = (
                str(label_team.get("id") or "")
                if isinstance(label_team, dict)
                else ""
            )
            if label_team_id and label_team_id != str(team_id or ""):
                continue
            selected.append(label)
        team_scoped = [
            label
            for label in selected
            if isinstance(label.get("team"), dict)
            and str((label.get("team") or {}).get("id") or "") == str(team_id or "")
        ]
        preferred = team_scoped or [
            label
            for label in selected
            if not isinstance(label.get("team"), dict)
            or not str((label.get("team") or {}).get("id") or "")
        ]
        return [str(label.get("id") or "") for label in preferred if label.get("id")]

    def _build_linear_meeting_issue_input(
        self,
        *,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        label_ids: list[str],
        source: dict[str, Any],
        sizing_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        team = team_match.get("team") or {}
        owner = owner_match.get("user") or {}
        project = project_match.get("project") or {}
        issue_input = {
            "title": candidate["title"],
            "team_id": str(team.get("id") or ""),
            "description": self._build_linear_meeting_issue_description(
                candidate,
                source,
                sizing_metadata=sizing_metadata,
            ),
            "assignee_id": str(owner.get("id") or "") or None,
            "project_id": str(project.get("id") or "") or None,
            "priority": candidate.get("priority", 3),
            "due_date": candidate.get("due_date"),
            "label_ids": label_ids,
        }
        if sizing_metadata:
            issue_input["sizing_metadata"] = sizing_metadata
        issue_input["idempotency_key"] = self._linear_meeting_idempotency_key(
            candidate=candidate,
            source=source,
            assignee_id=issue_input.get("assignee_id"),
            project_id=issue_input.get("project_id"),
        )
        return issue_input

    def _linear_meeting_idempotency_key(
        self,
        *,
        candidate: dict[str, Any],
        source: dict[str, Any],
        assignee_id: Optional[str],
        project_id: Optional[str],
    ) -> str:
        source_message_ts = (
            source.get("source_message_ts")
            or source.get("thread_ts")
            or source.get("request_message_ts")
            or ""
        )
        raw = "|".join(
            [
                str(source.get("workspace_id") or ""),
                str(source.get("channel_id") or ""),
                str(source_message_ts),
                self._normalize_match_text(candidate.get("title")),
                str(assignee_id or ""),
                str(project_id or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _build_linear_meeting_issue_description(
        self,
        candidate: dict[str, Any],
        source: dict[str, Any],
        *,
        sizing_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        lines = [
            "### Meeting action",
            candidate.get("remaining_work")
            or candidate.get("description")
            or candidate.get("title")
            or "",
            "",
        ]
        if candidate.get("completed_work"):
            lines.extend(
                [
                    "### Work already completed",
                    candidate["completed_work"],
                    "",
                ]
            )
        if candidate.get("available_artifacts"):
            lines.extend(
                [
                    "### Available artifacts",
                    *[f"- {item}" for item in candidate["available_artifacts"]],
                    "",
                ]
            )
        if candidate.get("dependencies"):
            lines.extend(
                [
                    "### Dependencies",
                    *[f"- {item}" for item in candidate["dependencies"]],
                    "",
                ]
            )
        if candidate.get("acceptance_criteria"):
            lines.extend(
                [
                    "### Acceptance criteria",
                    *[f"- {item}" for item in candidate["acceptance_criteria"]],
                    "",
                ]
            )
        if sizing_metadata:
            effort_label = (
                sizing_metadata.get("effort_label")
                or sizing_metadata.get("effortLabel")
                or ""
            )
            rationale = str(sizing_metadata.get("rationale") or "")
            lines.extend(
                [
                    "### Effort estimate",
                    f"**{effort_label}** — {rationale}",
                    "",
                ]
            )
        if candidate.get("evidence"):
            lines.extend([
                "### Evidence",
                f"> {candidate['evidence']}",
                "",
            ])
        source_lines = []
        if candidate.get("source_label"):
            source_lines.append(f"- Source document: `{candidate['source_label']}`")
        if source.get("channel_id"):
            channel_label = source.get("channel_name") or source["channel_id"]
            source_lines.append(f"- Slack channel: `{channel_label}` (`{source['channel_id']}`)")
        if source.get("thread_ts"):
            source_lines.append(f"- Slack thread: `{source['thread_ts']}`")
        if source.get("source_message_ts"):
            source_lines.append(f"- Evidence message: `{source['source_message_ts']}`")
        if source.get("source_permalink"):
            source_lines.append(f"- [Open source message in Slack]({source['source_permalink']})")
        if source.get("requester_slack_id"):
            source_lines.append(f"- Requested by: `<@{source['requester_slack_id']}>`")
        if source_lines:
            lines.extend(["### Source", *source_lines, ""])
        lines.append("_Generated by Roo from Slack context._")
        return "\n".join(line for line in lines if line is not None).strip()

    def _build_linear_meeting_candidate_display(
        self,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        confidence: float,
    ) -> dict[str, Any]:
        owner = owner_match.get("user") or {}
        project = project_match.get("project") or {}
        team = team_match.get("team") or {}
        return {
            "title": candidate.get("title") or "Untitled action",
            "assignee": owner.get("displayName") or owner.get("name") or owner.get("email") or "Unresolved",
            "project": project.get("name") or "Unresolved",
            "team": team.get("key") or team.get("name") or "Unresolved",
            "source": candidate.get("source_label") or "Slack thread",
            "evidence": candidate.get("evidence") or "",
            "due_date": candidate.get("due_date"),
            "confidence": confidence,
            "owner_reason": owner_match.get("reason"),
            "project_reason": project_match.get("reason"),
            "team_reason": team_match.get("reason"),
        }

    def _format_linear_meeting_result_message(
        self,
        created: list[dict[str, Any]],
        review_needed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        *,
        project_update: Optional[dict[str, Any]] = None,
        project_update_error: Optional[str] = None,
        contextual_review_mode: bool = False,
    ) -> str:
        lines: list[str] = []
        if project_update:
            project_update_label = (
                ((project_update.get("project") or {}).get("name"))
                or project_update.get("id")
                or "Linear project update"
            )
            if project_update.get("url"):
                lines.append(f"Created Linear project update: <{project_update['url']}|{project_update_label}>")
            else:
                lines.append(f"Created Linear project update: {project_update_label}")
        elif project_update_error:
            lines.append(project_update_error)

        if contextual_review_mode and review_needed:
            if lines:
                lines.append("")
            lines.append("I found a possible Linear issue from this thread. Please review before I create it.")

        if created:
            if lines:
                lines.append("")
            lines.append(f"Created {len(created)} Linear issue{'s' if len(created) != 1 else ''}:")
            for item in created[:10]:
                issue = item.get("issue") or {}
                issue_label = issue.get("identifier") or issue.get("title") or item["title"]
                if issue.get("url"):
                    detail = f"{item['assignee']} · {item['project']}"
                    if item.get("due_date"):
                        detail += f" · due {item['due_date']}"
                    if item.get("effort_label"):
                        detail += f" · {item['effort_label']}"
                    replay = " · already existed" if issue.get("idempotentReplay") else ""
                    lines.append(
                        f"- <{issue['url']}|{issue_label}> - {item['title']} "
                        f"({detail}{replay})"
                    )
                else:
                    lines.append(f"- {issue_label} - {item['title']}")
        if review_needed:
            if lines:
                lines.append("")
            lines.append(f"{len(review_needed)} action item{'s' if len(review_needed) != 1 else ''} need approval:")
            for item in review_needed[:20]:
                review_line = (
                    f"- {item['title']} -> {item['project']} / {item['assignee']} "
                    f"({item['confidence']:.0%})"
                )
                if item.get("effort_label"):
                    review_line += f" · {item['effort_label']}"
                lines.append(review_line)
        if skipped:
            if lines:
                lines.append("")
            lines.append(f"Skipped {len(skipped)} item{'s' if len(skipped) != 1 else ''}:")
            for item in skipped[:8]:
                duplicate = item.get("duplicate") or {}
                mapping = f"project: {item['project']}; assignee: {item['assignee']}"
                if duplicate.get("url"):
                    lines.append(f"- {item['title']} - {item['reason']} ({mapping}; <{duplicate['url']}|existing issue>)")
                else:
                    lines.append(f"- {item['title']} - {item['reason']} ({mapping})")
        return "\n".join(lines) if lines else "No Linear issues were created from those meeting notes."

    def _build_linear_meeting_review_blocks(
        self,
        message: str,
        review_needed: list[dict[str, Any]],
        user_id: str,
        *,
        batch_id: str,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message[:3000]}}
        ]
        batch_value = json.dumps(
            {"batch_id": batch_id, "requested_by": user_id}
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"linear_meeting_batch_{batch_id[:8]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve all"},
                        "style": "primary",
                        "action_id": "linear_meeting_approve_all",
                        "value": batch_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject all"},
                        "style": "danger",
                        "action_id": "linear_meeting_reject_all",
                        "value": batch_value,
                    },
                ],
            }
        )
        for item in review_needed[:20]:
            item_id = str(item.get("item_id") or "")
            summary = (
                f"*{item['title']}*\n"
                f"Project: `{item['project']}` | Assignee: `{item['assignee']}` | "
                f"Confidence: `{item['confidence']:.0%}`"
            )
            if item.get("effort_label"):
                summary += f" | Effort: `{item['effort_label']}`"
            value = json.dumps(
                {
                    "batch_id": batch_id,
                    "item_ids": [item_id],
                    "requested_by": user_id,
                }
            )
            blocks.extend([
                {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2500]}},
                {
                    "type": "actions",
                    "block_id": f"linear_meeting_{item_id[:8]}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "linear_meeting_approve",
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "action_id": "linear_meeting_reject",
                            "value": value,
                        },
                    ],
                },
            ])
        return blocks

    def _linear_connection_nodes(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            nodes = value.get("nodes") or []
            return [node for node in nodes if isinstance(node, dict)]
        if isinstance(value, list):
            return [node for node in value if isinstance(node, dict)]
        return []

    def _normalize_match_text(self, value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _linear_meeting_duplicate_tokens(self, value: Any) -> set[str]:
        aliases = {
            "doc": "documentation",
            "docs": "documentation",
        }
        tokens = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            tokens.add(aliases.get(token, token))
        return tokens
    
    async def _execute_connect_users(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str
    ) -> str:
        """Execute the connect_users skill with vector search."""
        query = params.get("query", "")
        
        if not query:
            # Try to extract from the text directly
            query = text
        
        # Note: Vector search is disabled until API endpoint is implemented
        # For now, fall back to LLM-based execution
        return await self._execute_with_llm(skill, text, params, user_id)

    async def _execute_luma_events(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Execute the Luma Events skill through mlai-backend."""
        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "Luma attendee reports need mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )

        event_date = self._resolve_luma_event_date(params, text)
        event_count = self._resolve_luma_event_count(params, text, default=1 if event_date else 3)
        include_csv = self._luma_request_includes_csv(params, text)
        approval_status = str(params.get("approval_status") or "approved").strip() or "approved"
        if include_csv and not channel_id:
            return "I need a Slack channel or DM to upload the Luma CSV files."

        try:
            report = await backend_client.get_luma_attendee_report(
                user_id,
                event_count=event_count,
                event_date=event_date,
                approval_status=approval_status,
                include_csv=include_csv,
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for Luma right now. Try again in a tick."
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            detail = self._extract_luma_http_error_detail(e)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin with the `admin`, "
                    "`committee`, or `partner` role to access Luma attendee data. 🔒"
                )
            if status_code == 503:
                return detail or "Luma isn't configured in mlai-backend yet. Ask the team to set `LUMA_API_KEY` there."
            if status_code == 429:
                return detail or "Luma rate-limited the report. Try again in a minute."
            return detail or f"mlai-backend returned HTTP {status_code} for the Luma report."

        events = report.get("events") or []
        if not events:
            if event_date:
                return f"I couldn't find an ended Luma event on {event_date}."
            return "I couldn't find any ended Luma events on the configured MLAI calendar."

        uploaded = []
        if include_csv:
            from ..slack_client import upload_file

            for event in events:
                csv_payload = event.get("csv") if isinstance(event, dict) else None
                if not isinstance(csv_payload, dict):
                    continue
                filename = str(csv_payload.get("filename") or "luma-attendees.csv")
                content_base64 = str(csv_payload.get("content_base64") or "")
                try:
                    csv_content = base64.b64decode(content_base64).decode("utf-8")
                except Exception:
                    return "mlai-backend returned a Luma CSV I couldn't decode. Ask the team to check the Luma report endpoint."
                title = f"{event.get('event_name') or 'Luma event'} attendees"
                upload_file(
                    channel=channel_id,
                    content=csv_content,
                    filename=filename,
                    title=title,
                    thread_ts=thread_ts,
                )
                uploaded.append(filename)

        message = self._format_luma_attendee_report(report, include_csv=include_csv, uploaded_filenames=uploaded)
        return {
            "message": message,
            "data": {
                "action": "luma_attendee_report",
                "approval_status": approval_status,
                "event_date": event_date,
                "include_csv": include_csv,
                "events": events,
                "uploaded_filenames": uploaded,
            },
        }

    def _resolve_luma_event_count(self, params: dict, text: str, default: int = 3) -> int:
        raw_count = params.get("event_count") or params.get("limit")
        if raw_count is None:
            text_lower = text.lower()
            if re.search(r"\blatest\s+(?:mlai\s+)?event\b", text_lower):
                raw_count = 1
            else:
                count_match = re.search(
                    r"\b(?:past|last|recent|latest)\s+(\d{1,2})\s+(?:mlai\s+)?events?\b",
                    text_lower,
                )
                raw_count = count_match.group(1) if count_match else default
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = default
        return max(1, min(count, 10))

    async def _execute_reconciliation_report(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Execute Points-Admin reporting or guarded Xero reconciliation actions."""
        action = str(params.get("action") or "generate_report").strip().lower()
        if action == "audit_event_finances":
            return await self._execute_event_finance_audit(
                params=params,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "Reconciliation needs mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )
        if action == "generate_report" and not channel_id:
            return "I need a Slack channel or DM to upload the reconciliation report."

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )

        if action != "generate_report":
            return await self._execute_statement_reconciliation_action(
                action=action,
                text=text,
                params=params,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                settings=settings,
                backend_client=backend_client,
            )

        days = self._resolve_reconciliation_days(params)
        since = self._clean_optional_iso_date(params.get("since"))
        until = self._clean_optional_iso_date(params.get("until"))

        try:
            report = await backend_client.get_reconciliation_report(
                user_id,
                days=days,
                since=since,
                until=until,
                include_workbook=True,
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for the reconciliation report right now. Try again in a tick."
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            detail = self._extract_luma_http_error_detail(e)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin (`admin`, `committee`, "
                    "or `portfolio_lead`) to run the reconciliation report. 🔒"
                )
            if status_code == 503:
                return detail or "Stripe isn't configured in mlai-backend yet. Ask the team to set `STRIPE_SECRET_KEY` there."
            if status_code == 429:
                return detail or "Stripe rate-limited the reconciliation report. Try again in a minute."
            return detail or f"mlai-backend returned HTTP {status_code} for the reconciliation report."

        from ..slack_client import upload_file

        uploaded: list = []
        brief = report.get("brief") if isinstance(report, dict) else None
        if isinstance(brief, dict) and brief.get("content_base64"):
            try:
                brief_text = base64.b64decode(brief["content_base64"]).decode("utf-8")
            except Exception:
                return "mlai-backend returned a brief I couldn't decode. Ask the team to check the reconciliation endpoint."
            brief_name = str(brief.get("filename") or "reconciliation.md")
            upload_file(
                channel=channel_id,
                content=brief_text,
                filename=brief_name,
                title="Reconciliation brief",
                thread_ts=thread_ts,
            )
            uploaded.append(brief_name)

        workbook = report.get("workbook") if isinstance(report, dict) else None
        if isinstance(workbook, dict) and workbook.get("content_base64"):
            try:
                xlsx_bytes = base64.b64decode(workbook["content_base64"])
            except Exception:
                xlsx_bytes = None
            if xlsx_bytes:
                wb_name = str(workbook.get("filename") or "reconciliation.xlsx")
                upload_file(
                    channel=channel_id,
                    content=xlsx_bytes,
                    filename=wb_name,
                    title="Reconciliation workbook",
                    thread_ts=thread_ts,
                )
                uploaded.append(wb_name)

        message = self._format_reconciliation_report(report, uploaded)
        return {
            "message": message,
            "data": {
                "action": "reconciliation_report",
                "payout_count": report.get("payout_count"),
                "charge_count": report.get("charge_count"),
                "unmatched_charge_count": report.get("unmatched_charge_count"),
                "uploaded_filenames": uploaded,
            },
        }

    async def _execute_event_finance_audit(
        self,
        *,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Audit recent events for expected revenue and cost evidence."""
        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "The event finance audit needs mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )
        if not channel_id:
            return "I need a Slack channel or DM to upload the event finance audit."

        since, until = self._resolve_event_finance_audit_period(params)
        if since > until:
            return "The audit start date needs to be on or before the end date."

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )
        try:
            audit = await backend_client.get_event_finance_audit(
                user_id,
                since=since.isoformat(),
                until=until.isoformat(),
                domain=getattr(settings, "RECONCILIATION_DOMAIN", "mlai.au"),
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for the event finance audit right now. Try again in a tick."
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            detail = self._extract_luma_http_error_detail(exc)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin (`admin`, `committee`, "
                    "or `portfolio_lead`) to run the event finance audit. 🔒"
                )
            return detail or f"mlai-backend returned HTTP {status_code} for the event finance audit."

        from ..slack_client import upload_file

        filename = f"event-finance-audit-{since.isoformat()}-to-{until.isoformat()}.md"
        upload_file(
            channel=channel_id,
            content=self._event_finance_audit_markdown(audit),
            filename=filename,
            title="Event finance completeness audit",
            thread_ts=thread_ts,
        )
        return {
            "message": self._format_event_finance_audit(audit, filename),
            "data": {
                "action": "event_finance_audit",
                "period_start": audit.get("period_start"),
                "period_end": audit.get("period_end"),
                "event_count": (audit.get("summary") or {}).get("event_count"),
                "complete_count": (audit.get("summary") or {}).get("complete_count"),
                "incomplete_count": (audit.get("summary") or {}).get("incomplete_count"),
                "uploaded_filenames": [filename],
                "xero_writes": bool(audit.get("xero_writes", False)),
            },
        }

    def _resolve_event_finance_audit_period(
        self,
        params: dict,
    ) -> tuple[date, date]:
        until_text = self._clean_optional_iso_date(params.get("until"))
        until = date.fromisoformat(until_text) if until_text else date.today()
        since_text = self._clean_optional_iso_date(params.get("since"))
        if since_text:
            return date.fromisoformat(since_text), until
        try:
            months = int(params.get("months") or 6)
        except (TypeError, ValueError):
            months = 6
        months = max(1, min(months, 24))
        month_index = until.year * 12 + until.month - 1 - months
        since_year, since_month_zero = divmod(month_index, 12)
        since_month = since_month_zero + 1
        since_day = min(until.day, calendar.monthrange(since_year, since_month)[1])
        return date(since_year, since_month, since_day), until

    @staticmethod
    def _markdown_cell(value: Any) -> str:
        return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

    def _event_finance_audit_markdown(self, audit: dict) -> str:
        summary = audit.get("summary") or {}
        requirements = audit.get("required_categories") or []
        labels = {item.get("key"): item.get("label") for item in requirements}
        lines = [
            "# Event finance completeness audit",
            "",
            f"Period: {audit.get('period_start')} to {audit.get('period_end')}",
            "",
            (
                f"Events: {summary.get('event_count', 0)}; complete: "
                f"{summary.get('complete_count', 0)}; incomplete: "
                f"{summary.get('incomplete_count', 0)}."
            ),
            "",
            "> Missing means no tracked evidence was found in the period. It does not prove the event had no such revenue or cost.",
            "",
            "| Event | Date | Ticket sales | Sponsorship | Catering | Contractors | Missing |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
        symbols = {"present": "Yes", "missing": "Missing"}
        for event in audit.get("events") or []:
            categories = event.get("categories") or {}
            start_at = str(event.get("start_at") or "")[:10] or "—"
            missing = ", ".join(
                str(labels.get(key) or key) for key in event.get("missing_categories") or []
            ) or "—"
            lines.append(
                "| "
                + " | ".join(
                    [
                        self._markdown_cell(event.get("event_name")),
                        start_at,
                        symbols.get((categories.get("ticket_sales") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("sponsorship_revenue") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("catering_cost") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("contractor_cost") or {}).get("status"), "Missing"),
                        self._markdown_cell(missing),
                    ]
                )
                + " |"
            )

        lines.extend(["", "## Evidence", ""])
        for event in audit.get("events") or []:
            lines.append(f"### {event.get('event_name')}")
            lines.append("")
            for requirement in requirements:
                category = (event.get("categories") or {}).get(requirement.get("key")) or {}
                evidence = category.get("evidence") or []
                if not evidence:
                    lines.append(f"- {requirement.get('label')}: **Missing**")
                    continue
                observations = []
                for item in evidence:
                    amount = int(item.get("amount_cents") or 0) / 100
                    if item.get("source_type") == "xero_bank_transaction_line":
                        context = "; ".join(
                            value
                            for value in (
                                str(item.get("date") or ""),
                                str(item.get("account_name") or item.get("account_code") or ""),
                                str(item.get("contact_name") or ""),
                                str(item.get("description") or ""),
                                str(item.get("reference") or ""),
                            )
                            if value
                        )
                    else:
                        context = f"{item.get('source_type')} {item.get('source_id')}"
                    observations.append(f"AUD {amount:,.2f} ({context})")
                lines.append(
                    f"- {requirement.get('label')}: **Present** — "
                    + "; ".join(observations)
                )
            lines.append("")

        warnings = audit.get("account_resolution_warnings") or []
        if warnings:
            lines.extend(
                [
                    "## Account resolution warnings",
                    "",
                    "The following category accounts could not be resolved from Xero: "
                    + ", ".join(warnings),
                    "",
                ]
            )
        lines.extend(["## Limitations", ""])
        lines.extend(f"- {item}" for item in audit.get("limitations") or [])
        lines.extend(["", "This audit is read-only and made no Xero writes.", ""])
        return "\n".join(lines)

    def _format_event_finance_audit(self, audit: dict, filename: str) -> str:
        summary = audit.get("summary") or {}
        event_count = int(summary.get("event_count") or 0)
        if not event_count:
            return (
                f"*Event finance audit* — no events were found from {audit.get('period_start')} "
                f"to {audit.get('period_end')}.\n📎 Uploaded `{filename}`.\n"
                "Read-only: no Xero changes were made."
            )
        missing_counts = summary.get("missing_counts") or {}
        labels = {
            "ticket_sales": "ticket sales",
            "sponsorship_revenue": "sponsorship",
            "catering_cost": "catering",
            "contractor_cost": "contractors",
        }
        lines = [
            f"*Event finance audit* — {audit.get('period_start')} to {audit.get('period_end')}",
            (
                f"✅ {summary.get('complete_count', 0)} of {event_count} event(s) have all four "
                f"categories; ⚠️ {summary.get('incomplete_count', 0)} are missing evidence."
            ),
            "Missing across events: "
            + ", ".join(
                f"{labels[key]} {int(missing_counts.get(key) or 0)}"
                for key in labels
            )
            + ".",
        ]
        incomplete = [
            event
            for event in audit.get("events") or []
            if event.get("completeness_status") == "incomplete"
        ]
        for event in incomplete[:10]:
            missing = ", ".join(
                labels.get(key, key) for key in event.get("missing_categories") or []
            )
            lines.append(f"• {event.get('event_name')}: missing {missing}")
        if len(incomplete) > 10:
            lines.append(f"• …and {len(incomplete) - 10} more in the attachment.")
        if audit.get("account_resolution_warnings"):
            lines.append("⚠️ One or more expected Xero accounts could not be resolved; see the attachment.")
        lines.extend(
            [
                "",
                f"📎 Uploaded `{filename}` with every event and its evidence.",
                "Read-only: no Xero changes were made. “Missing” means no tracked evidence in the period, not proof the category did not exist.",
            ]
        )
        return "\n".join(lines)

    async def _execute_statement_reconciliation_action(
        self,
        *,
        action: str,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        settings: Any,
        backend_client: Any,
    ) -> Any:
        """Run the preview → approval → execution contract for Xero writes."""
        from roo.clients.mlai_backend import MLAIBackendUnavailableError

        valid_actions = {
            "check_reconciliation_readiness",
            "start_statement_reconciliation",
            "reconciliation_outcomes",
            "decide_reconciliation_rule_candidate",
            "status_statement_reconciliation",
            "retry_statement_reconciliation",
            "preview_statement_reconciliation",
            "approve_ready_reconciliation",
            "reject_reconciliation_suggestions",
            "execute_approved_reconciliation",
        }
        if action not in valid_actions:
            return "I couldn't identify that reconciliation action. Ask me to check readiness, start, preview, approve, reject, or execute a run."

        domain = str(params.get("domain") or "mlai.au").strip().lower()
        run_id = str(params.get("run_id") or "").strip()
        if action not in {
            "check_reconciliation_readiness",
            "start_statement_reconciliation",
            "reconciliation_outcomes",
            "decide_reconciliation_rule_candidate",
        } and not run_id:
            return "Please include the reconciliation run ID so I operate on the exact preview you reviewed."

        try:
            if action == "check_reconciliation_readiness":
                result = await backend_client.get_statement_reconciliation_readiness(
                    user_id,
                    domain=domain,
                )
                return {
                    "message": self._format_statement_reconciliation_readiness(result),
                    "data": {"action": action, **result},
                }

            if action == "start_statement_reconciliation":
                agent_url = str(
                    getattr(settings, "RECONCILIATION_AGENT_URL", "") or ""
                ).strip()
                if agent_url:
                    if not channel_id:
                        return (
                            "Start this workflow in the private Roo bookkeeping "
                            "channel so I can post its review digest safely."
                        )
                    trigger = await self._trigger_reconciliation_agent_prepare(
                        settings=settings,
                        agent_url=agent_url,
                        user_id=user_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        instruction=str(params.get("instruction") or text).strip()[:4000],
                    )
                    duplicate_note = (
                        " The same request was already queued, so I did not start a duplicate."
                        if trigger.get("duplicate")
                        else ""
                    )
                    return {
                        "message": (
                            "I’ve started the full reconciliation preparation: the "
                            "dedicated treasurer mailbox will sync, new invoices and "
                            "receipts will be extracted, and every current Xero line "
                            "will be checked against the latest monthly update and "
                            "connected finance/event context. A detailed digest with "
                            "exact-preview approval buttons will appear in this private "
                            "channel. No Xero write happens until you approve a preview; "
                            "after that, Roo creates the green match and you make the "
                            "final Match/OK tick in Xero."
                            + duplicate_note
                        ),
                        "data": {"action": action, **trigger},
                    }
                line_ids = self._coerce_reconciliation_statement_line_ids(
                    params.get("statement_line_ids")
                )
                result = await backend_client.start_statement_reconciliation_run(
                    user_id,
                    domain=domain,
                    instruction=str(params.get("instruction") or text).strip()[:4000],
                    statement_line_ids=line_ids,
                )
                deterministic_count = int(
                    result.get("deterministic_suggestion_count") or 0
                )
                agent_line_count = int(result.get("agent_line_count") or 0)
                conflict_count = int(result.get("rule_conflict_count") or 0)
                deferred_bill_count = int(result.get("deferred_bill_count") or 0)
                summary_parts = [
                    f"{deterministic_count} prepared from verified rules",
                    f"{agent_line_count} sent for monthly-context analysis",
                ]
                if conflict_count:
                    summary_parts.append(f"{conflict_count} rule conflicts held for review")
                if deferred_bill_count:
                    summary_parts.append(
                        f"{deferred_bill_count} exact bill matches reserved for bill-payment analysis"
                    )
                if result.get("status") == "completed":
                    next_step = "The preview-only run is complete; ask me to preview it now."
                else:
                    next_step = (
                        "It is preview-only; ask me to preview that run once its status is completed."
                    )
                start_verb = (
                    "Reused existing reconciliation run"
                    if result.get("idempotent")
                    else "Started reconciliation run"
                )
                duplicate_note = (
                    " I did not create or dispatch a duplicate."
                    if result.get("idempotent")
                    else ""
                )
                return {
                    "message": (
                        f"{start_verb} `{result.get('run_id')}` against the fresh Xero queue. "
                        f"{'; '.join(summary_parts)}.{duplicate_note} {next_step}"
                    ),
                    "data": {"action": action, **result},
                }

            if action == "reconciliation_outcomes":
                try:
                    limit = int(params.get("limit") or 50)
                except (TypeError, ValueError):
                    limit = 50
                result = await backend_client.get_statement_reconciliation_outcomes(
                    user_id,
                    domain=domain,
                    limit=max(1, min(limit, 200)),
                )
                return {
                    "message": self._format_statement_reconciliation_outcomes(result),
                    "data": {"action": action, **result},
                }

            if action == "decide_reconciliation_rule_candidate":
                candidate_id = str(params.get("candidate_id") or "").strip()
                candidate_version = str(params.get("candidate_version") or "").strip()
                decision = str(params.get("decision") or "").strip().lower()
                reason = str(params.get("reason") or "").strip()
                if not candidate_id or not candidate_version:
                    return (
                        "Include the candidate ID and version from the reconciliation "
                        "outcomes preview."
                    )
                if decision not in {"promote", "reject"}:
                    return "Choose `promote` or `reject` for the reviewed rule candidate."
                if decision == "reject" and not reason:
                    return "Include a short reason for rejecting the rule candidate."
                result = await backend_client.decide_statement_reconciliation_learning_candidate(
                    user_id,
                    candidate_id,
                    candidate_version=candidate_version,
                    decision=decision,
                    reason=reason[:2000] or None,
                    domain=domain,
                )
                if decision == "promote":
                    rule = result.get("rule") or {}
                    return {
                        "message": (
                            f"Verified reconciliation rule #{rule.get('id')} "
                            f"`{rule.get('name')}` from candidate `{candidate_id}`"
                            + (" (already promoted)." if result.get("idempotent") else ".")
                            + " Future matching lines can now use this rule; no Xero transaction was created."
                        ),
                        "data": {"action": action, **result},
                    }
                return {
                    "message": (
                        f"Rejected rule candidate `{candidate_id}` with the supplied audit reason. "
                        "No rule or Xero transaction was created."
                    ),
                    "data": {"action": action, **result},
                }

            if action == "status_statement_reconciliation":
                result = await backend_client.get_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                count = len(result.get("suggestions") or [])
                retry_hint = (
                    " The context-analysis dispatch can be retried against this run."
                    if result.get("retry_available")
                    else ""
                )
                return {
                    "message": (
                        f"Run `{run_id}` is *{result.get('status', 'unknown')}* with {count} suggestion(s). "
                        f"Preview it before approving anything.{retry_hint}"
                    ),
                    "data": {"action": action, **result},
                }

            if action == "retry_statement_reconciliation":
                result = await backend_client.retry_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                if result.get("idempotent"):
                    message = (
                        f"Run `{run_id}` is already {result.get('status', 'queued')}; "
                        "I did not create or dispatch a duplicate."
                    )
                else:
                    message = (
                        f"Re-queued monthly-context analysis for run `{run_id}` against its original "
                        "fresh Xero scan. Existing deterministic suggestions were kept; no Xero "
                        "transaction was created."
                    )
                return {
                    "message": message,
                    "data": {"action": action, **result},
                }

            if action == "preview_statement_reconciliation":
                result = await backend_client.preview_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                return {
                    "message": self._format_statement_reconciliation_preview(result),
                    "data": {"action": action, **result},
                }

            if action == "approve_ready_reconciliation":
                result = await backend_client.approve_ready_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                recorded = int(result.get("recorded_count") or 0)
                requested = int(result.get("requested_count") or 0)
                blocked = max(0, requested - recorded)
                return {
                    "message": (
                        f"Approved {recorded} ready suggestion(s) in run `{run_id}`"
                        + (f"; {blocked} were not ready and remain unapproved" if blocked else "")
                        + ". No Xero transactions were created yet."
                    ),
                    "data": {"action": action, **result},
                }

            if action == "reject_reconciliation_suggestions":
                suggestion_ids = self._coerce_reconciliation_suggestion_ids(
                    params.get("suggestion_ids")
                )
                reason = str(params.get("reason") or "").strip()
                if not suggestion_ids:
                    return "Include the integer suggestion IDs you want to reject from the exact run preview."
                if not reason:
                    return "Include a short reason so the rejection is useful in the reconciliation audit trail."
                result = await backend_client.reject_statement_reconciliation_suggestions(
                    user_id,
                    run_id,
                    suggestion_ids,
                    reason=reason[:2000],
                    domain=domain,
                )
                recorded = int(result.get("recorded_count") or 0)
                return {
                    "message": (
                        f"Rejected {recorded}/{len(suggestion_ids)} selected suggestion(s) in run `{run_id}`. "
                        "No Xero transactions were created."
                    ),
                    "data": {"action": action, **result},
                }

            raw_suggestion_ids = params.get("suggestion_ids")
            suggestion_ids = self._coerce_reconciliation_suggestion_ids(raw_suggestion_ids)
            if raw_suggestion_ids not in (None, "", []) and suggestion_ids is None:
                return "suggestion_ids must contain integer IDs from the exact run preview."
            result = await backend_client.execute_approved_statement_reconciliation_run(
                user_id,
                run_id,
                domain=domain,
                suggestion_ids=suggestion_ids,
            )
            executed = int(result.get("executed_count") or 0)
            candidates = int(result.get("approved_candidate_count") or 0)
            failed = max(0, candidates - executed)
            message = (
                f"Created {executed} approved matching Xero transaction(s) from run `{run_id}`"
                + (f"; {failed} approved item(s) were safely blocked" if failed else "")
                + ". Open Xero and click the green Match/OK buttons to finish reconciliation."
            )
            return {"message": message, "data": {"action": action, **result}}
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for reconciliation right now. Try again in a tick."
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            detail = self._extract_luma_http_error_detail(exc)
            if status_code == 403:
                return "Sorry mate, you'll need to be a Points Admin to run Xero reconciliation. 🔒"
            if status_code == 409:
                if action == "decide_reconciliation_rule_candidate":
                    return detail or "The rule candidate changed or is no longer safe to promote."
                if "wait for completion" in detail.lower():
                    return detail
                return (
                    (detail + " ") if detail else ""
                ) + "Import the current Xero bank-feed queue with the Chrome backfill, then start a new run."
            if status_code == 404:
                return detail or f"I couldn't find reconciliation run `{run_id}`."
            return detail or f"mlai-backend returned HTTP {status_code} for reconciliation."

    @staticmethod
    async def _trigger_reconciliation_agent_prepare(
        *,
        settings: Any,
        agent_url: str,
        user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        instruction: str,
    ) -> dict:
        service_key = str(
            getattr(settings, "MLAI_API_KEY", "")
            or getattr(settings, "ROO_API_KEY", "")
            or getattr(settings, "INTERNAL_API_KEY", "")
            or ""
        ).strip()
        if not service_key:
            raise ValueError(
                "The reconciliation service key is not configured on Roo."
            )
        request_seed = "|".join(
            [channel_id, str(thread_ts or ""), user_id, instruction]
        )
        request_id = "roo-" + hashlib.sha256(request_seed.encode()).hexdigest()[:40]
        timeout = float(
            getattr(settings, "RECONCILIATION_AGENT_TIMEOUT_SECONDS", 30.0)
            or 30.0
        )
        async with httpx.AsyncClient(timeout=max(5.0, min(timeout, 120.0))) as client:
            response = await client.post(
                agent_url.rstrip("/") + "/internal/reconciliation/prepare",
                headers={"Authorization": f"Bearer {service_key}"},
                json={
                    "request_id": request_id,
                    "slack_user_id": user_id,
                    "channel_id": channel_id,
                    "thread_ts": str(thread_ts or ""),
                    "instruction": instruction
                    or "Plan every outstanding Xero reconciliation.",
                },
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("accepted") is not True:
            raise ValueError("The reconciliation service did not accept the request.")
        return payload

    @staticmethod
    def _coerce_reconciliation_statement_line_ids(raw: Any) -> Optional[list[str]]:
        if raw in (None, "", []):
            return None
        values = raw if isinstance(raw, list) else str(raw).split(",")
        cleaned = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        return cleaned or None

    @staticmethod
    def _format_statement_reconciliation_readiness(result: dict) -> str:
        scan = result.get("latest_statement_scan") or {}
        monthly = result.get("monthly_context") or {}
        candidate_count = int(scan.get("candidate_count") or 0)
        start_state = "ready to analyse" if result.get("ready_to_start") else "not ready to analyse"
        bank_state = (
            "Spend/Receive Money ready"
            if result.get("ready_to_execute_bank_transactions")
            else "Spend/Receive Money not ready"
        )
        bill_state = (
            "bill payments ready"
            if result.get("ready_to_execute_bill_payments")
            else "bill payments not ready"
        )
        context_state = (
            f"monthly context `{monthly.get('run_id')}`"
            if monthly.get("run_id")
            else "no completed monthly context"
        )
        lines = [
            (
                f"*Xero reconciliation is {start_state}*: {candidate_count} current "
                f"candidate(s), {context_state}; {bank_state}; {bill_state}."
            )
        ]
        blockers = [str(item).strip() for item in result.get("blockers") or [] if str(item).strip()]
        warnings = [str(item).strip() for item in result.get("warnings") or [] if str(item).strip()]
        if blockers:
            lines.append("*Blockers:* " + " ".join(blockers))
        if warnings:
            lines.append("*Setup notes:* " + " ".join(warnings))
        next_action = str(result.get("recommended_next_action") or "").strip()
        if next_action:
            lines.append(f"*Next:* {next_action}")
        return "\n".join(lines)

    @staticmethod
    def _coerce_reconciliation_suggestion_ids(raw: Any) -> Optional[list[int]]:
        if raw in (None, "", []):
            return None
        values = raw if isinstance(raw, list) else str(raw).split(",")
        result = []
        for value in values:
            try:
                item = int(value)
            except (TypeError, ValueError):
                continue
            if item not in result:
                result.append(item)
        return result or None

    @staticmethod
    def _format_statement_reconciliation_preview(result: dict) -> str:
        run_id = result.get("run_id") or "unknown"
        run_status = result.get("run_status") or "unknown"
        ready = int(result.get("ready_count") or 0)
        total = int(result.get("suggestion_count") or 0)
        approved = int(result.get("approved_count") or 0)
        lines = [
            f"*Reconciliation preview `{run_id}`* — run {run_status}; {ready}/{total} ready, {approved} approved."
        ]
        for item in (result.get("results") or [])[:10]:
            suggestion = item.get("suggestion") or {}
            preview = item.get("preview") or {}
            suggestion_id = suggestion.get("id")
            description = str(suggestion.get("description") or "Needs description").strip()
            project = (suggestion.get("project") or {}).get("tracking_option_name")
            event = (suggestion.get("event") or {}).get("tracking_option_name")
            allocation = project or event or "no event/project"
            state = "ready" if preview.get("ready") else "blocked"
            routing = suggestion.get("routing") or {}
            route_source = routing.get("source")
            if route_source == "verified_rule":
                route = f"verified rule #{routing.get('verified_rule_id')}"
            elif route_source == "exact_xero_bill":
                route = f"exact Xero bill {routing.get('xero_bill_id')}"
            elif route_source == "monthly_context_agent":
                route = "monthly context"
            elif route_source == "rule_conflict":
                route = "rule conflict"
            else:
                route = str(route_source or "legacy/manual").replace("_", " ")
            lines.append(
                f"• #{suggestion_id} — {description[:90]} — {allocation} — {route} — {state}."
            )
        if total > 10:
            lines.append(f"• …and {total - 10} more suggestion(s).")
        if ready:
            lines.append("Review these details, then explicitly ask me to approve the ready suggestions for this run.")
        else:
            lines.append("Nothing is ready to approve yet; review the blocking errors and source context.")
        return "\n".join(lines)

    @staticmethod
    def _format_statement_reconciliation_outcomes(result: dict) -> str:
        confirmed = int(result.get("confirmed_reconciled_count") or 0)
        pending = int(result.get("pending_human_match_count") or 0)
        review_candidates = int(result.get("rule_review_candidate_count") or 0)
        lines = [
            f"*Reconciliation outcomes* — {confirmed} confirmed reconciled; "
            f"{pending} still waiting for Xero Match/OK; {review_candidates} pattern(s) ready for rule review."
        ]
        recent = result.get("recent_confirmed") or []
        for item in recent[:5]:
            allocation = item.get("project_name") or item.get("event_name") or "no event/project"
            lines.append(
                f"• {item.get('transaction_date')} — {item.get('currency', '')} {item.get('amount')} — "
                f"{item.get('description') or item.get('contact_name')} — {allocation}."
            )
        candidates = [
            item
            for item in result.get("learning_candidates") or []
            if item.get("eligible_for_promotion")
            and item.get("review_status", "pending") == "pending"
        ]
        if candidates:
            lines.append("*Patterns for admin rule review:*")
            for item in candidates[:5]:
                rule = item.get("suggested_rule") or {}
                allocation = rule.get("project_name") or rule.get("event_name") or "no event/project"
                lines.append(
                    f"• `{item.get('candidate_id')}` ({item.get('confirmed_example_count')} confirmations) — "
                    f"{item.get('merchant_key')} → {rule.get('account_code')} - "
                    f"{rule.get('account_name')}; {allocation}. "
                    f"Version `{item.get('candidate_version')}`."
                )
        lines.append(
            "No rule was created automatically. After reviewing a candidate and version, "
            "an admin can explicitly promote or reject it."
        )
        return "\n".join(lines)


    def _resolve_reconciliation_days(self, params: dict) -> int:
        raw = params.get("days")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = 30
        return max(1, min(days, 92))

    @staticmethod
    def _clean_optional_iso_date(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        try:
            date.fromisoformat(text)
        except ValueError:
            return None
        return text

    def _format_reconciliation_report(self, report: dict, uploaded: list) -> str:
        payouts = report.get("payouts") or []
        if not payouts:
            return "No Stripe payouts landed in that window — nothing to reconcile. ✅"

        totals = report.get("currency_totals") or {}
        lines = [
            f"*Luma → Stripe reconciliation* — {report.get('payout_count', len(payouts))} "
            f"payout(s), {report.get('charge_count', 0)} charge(s)."
        ]
        for ccy, t in totals.items():
            lines.append(
                f"• {ccy}: {t.get('deposit', 0):,.2f} deposited across {t.get('payouts', 0)} "
                f"payout(s) (gross {t.get('gross', 0):,.2f}, Stripe fees {t.get('stripe_fee', 0):,.2f})."
            )

        unmatched = report.get("unmatched_charge_count") or 0
        if unmatched:
            lines.append(f"⚠ {unmatched} charge(s) had no Luma event match — flagged in the brief.")
        warn_payouts = [p for p in payouts if p.get("warnings")]
        if warn_payouts:
            lines.append(
                f"⚠ {len(warn_payouts)} payout(s) have warnings (refunds/adjustments or tie-out) — see the brief."
            )

        if uploaded:
            lines.append("")
            lines.append(f"📎 Uploaded {len(uploaded)} file(s): {', '.join(uploaded)}.")
            lines.append(
                "Hand the brief to Claude Cowork to reconcile in Xero — the report only "
                "prepares; a human clicks the final confirm."
            )
        return "\n".join(lines)

    def _luma_request_includes_csv(self, params: dict, text: str) -> bool:
        raw_value = params.get("include_csv")
        if raw_value is not None:
            return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}

        text_lower = text.lower()
        return bool(
            re.search(r"\bcsvs?\b", text_lower)
            or re.search(r"\bexport\b", text_lower)
            or re.search(r"\bguest\s+list\b", text_lower)
            or re.search(r"\battendee\s+list\b", text_lower)
        )

    def _resolve_luma_event_date(self, params: dict, text: str) -> Optional[str]:
        raw_value = params.get("event_date") or params.get("date")
        if raw_value:
            raw_text = str(raw_value).strip()
            iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw_text)
            if iso_match:
                return iso_match.group(1)

        iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        if iso_match:
            return iso_match.group(1)

        month_lookup = {
            name.lower(): index
            for index, names in enumerate(calendar.month_name)
            for name in [names]
            if name
        }
        month_lookup.update(
            {
                name.lower(): index
                for index, names in enumerate(calendar.month_abbr)
                for name in [names]
                if name
            }
        )
        month_pattern = "|".join(sorted(month_lookup, key=len, reverse=True))
        text_lower = text.lower()
        patterns = [
            rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})(?:\s+(20\d{{2}}))?\b",
        ]
        today = date.today()
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, text_lower)
            if not match:
                continue
            if index == 0:
                month_name, day_text, year_text = match.groups()
            else:
                day_text, month_name, year_text = match.groups()
            month = month_lookup[month_name]
            day = int(day_text)
            year = int(year_text) if year_text else today.year
            try:
                resolved = date(year, month, day)
            except ValueError:
                return None
            if not year_text and resolved > today:
                try:
                    resolved = date(year - 1, month, day)
                except ValueError:
                    return None
            return resolved.isoformat()

        return None

    def _format_luma_attendee_report(
        self,
        report: dict,
        *,
        include_csv: bool,
        uploaded_filenames: list[str],
    ) -> str:
        events = report.get("events") or []
        total_guest_count = report.get("total_guest_count")
        lines = ["*Luma attendee report*"]
        if total_guest_count is not None:
            lines.append(f"Total approved guests: {total_guest_count}")
        for event in events:
            event_name = event.get("event_name") or "Luma event"
            start_label = self._format_luma_event_date(event.get("start_at"))
            guest_count = int(event.get("guest_count") or 0)
            checked_in_count = int(event.get("checked_in_count") or 0)
            suffix = f" ({start_label})" if start_label else ""
            lines.append(
                f"• {event_name}{suffix}: {guest_count} approved guest"
                f"{'s' if guest_count != 1 else ''}, {checked_in_count} checked in"
            )
        if include_csv:
            if uploaded_filenames:
                lines.append("")
                lines.append(
                    f"Uploaded {len(uploaded_filenames)} CSV file"
                    f"{'s' if len(uploaded_filenames) != 1 else ''}: "
                    + ", ".join(f"`{filename}`" for filename in uploaded_filenames)
                )
            else:
                lines.append("")
                lines.append("No CSV files were returned by mlai-backend.")
        return "\n".join(lines)

    @staticmethod
    def _format_luma_event_date(raw_value: Any) -> str:
        if not raw_value:
            return ""
        try:
            raw = str(raw_value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return ""
        return parsed.date().isoformat()

    def _extract_luma_http_error_detail(self, exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except ValueError:
            return self._extract_http_error_detail(exc)
        except Exception:
            return ""

        if isinstance(payload, dict):
            for key in ("error", "detail", "message"):
                value = payload.get(key)
                if value:
                    return str(value)
        if exc.response.status_code >= 500:
            return self._extract_http_error_detail(exc)
        if payload in (None, "", [], {}):
            return ""
        return str(payload)

    async def _save_content_factory_pending_intent(
        self,
        api_client: Any,
        user_id: str,
        params: dict,
        text: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        ) -> None:
        """Persist the original content request so GitHub auth/repo selection can resume it."""
        from ..clients.mlai_backend import MLAIBackendUnavailableError

        params = dict(params or {})
        client_request_id = self._get_content_factory_client_request_id(params)
        params["client_request_id"] = client_request_id
        domain = normalize_content_factory_domain(params.get("domain"))
        action = str(params.get("action") or "").strip().lower()
        (
            requested_by_slack_user_id,
            effective_slack_user_id,
            is_delegated,
        ) = (
            lambda identity: (
                identity.requested_by_slack_user_id,
                identity.effective_slack_user_id,
                identity.is_delegated,
            )
        )(
            resolve_content_factory_identity_context(
                requester_slack_user_id=user_id,
                requested_by_slack_user_id=params.get("requested_by_slack_user_id"),
                effective_slack_user_id=params.get("effective_slack_user_id"),
            )
        )

        if is_delegated:
            return

        backend_intent: dict[str, Any] = {
            "type": "roo_content_factory",
            "skill": "content-factory",
            "action": action or None,
            "params": params,
            "text": text,
            "channel": channel_id,
            "ts": thread_ts,
        }
        if action == "write" and domain:
            backend_intent = {
                "type": "write_article",
                "article_request": {
                    "domain": domain,
                    "topic": params.get("topic"),
                    "target_keyword": params.get("target_keyword"),
                    "context": params.get("context"),
                    "delivery_mode": params.get("delivery_mode"),
                    "delivery_mode_confirmed": params.get("delivery_mode_confirmed"),
                    "slack_channel_id": channel_id,
                    "slack_thread_ts": thread_ts,
                    "slack_root_message_ts": thread_ts,
                    "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                    "client_request_id": client_request_id,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
                "resume_context": {
                    "text": text,
                    "params": params,
                    "channel": channel_id,
                    "ts": thread_ts,
                },
            }

        if action == "write" and domain:
            from .. import main as main_module

            main_module._remember_pending_intent(
                requested_by_slack_user_id,
                domain,
                effective_slack_user_id=effective_slack_user_id,
                intent_data={
                    "action": "write",
                    "topic": params.get("topic"),
                    "target_keyword": params.get("target_keyword"),
                    "context": params.get("context"),
                    "delivery_mode": params.get("delivery_mode"),
                    "delivery_mode_confirmed": params.get("delivery_mode_confirmed"),
                    "client_request_id": client_request_id,
                    "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                    "text": text,
                    "params": params,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                    "effective_slack_user_id": effective_slack_user_id,
                },
                channel_id=channel_id,
                thread_ts=thread_ts,
                wait_for="scan_complete",
                clear_job_id=True,
            )

        try:
            await api_client.save_pending_intent(requested_by_slack_user_id, backend_intent)
        except MLAIBackendUnavailableError as exc:
            print(
                "⚠️ Failed to persist content-factory pending intent to mlai-backend: "
                f"{exc!r}"
            )
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            print(
                "⚠️ Failed to persist content-factory pending intent to mlai-backend: "
                f"{detail}"
            )

    @staticmethod
    def _get_content_factory_client_request_id(params: dict) -> str:
        existing = str(params.get("client_request_id") or "").strip()
        if existing:
            return existing
        return f"content-factory-{uuid4().hex}"

    async def _validate_content_factory_paid_access(
        self,
        api_client: Any,
        user_id: str,
        domain: Optional[str],
    ) -> Optional[str]:
        from ..clients.mlai_backend import MLAIBackendUnavailableError
        from ..slack_client import get_user_info

        normalized_domain = normalize_content_factory_domain(domain) or "this domain"
        cost_points = get_content_factory_article_cost_points(domain)
        slack_info = get_user_info(user_id)
        email = str(slack_info.get("email") or "").strip().lower()
        if not email:
            if cost_points == 0:
                return (
                    "I need access to your real Slack email before I can start Content Factory for you. "
                    f"Once that's available, articles for {normalized_domain} are free."
                )
            return (
                "I need access to your real Slack email before I can start Content Factory for you. "
                f"Once that's available, creating an article costs {cost_points} Roo points."
            )

        real_name = str(slack_info.get("real_name") or "").strip()
        name_parts = real_name.split(" ", 1) if real_name else []
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        avatar_url = str(slack_info.get("image_192") or "").strip() or None

        try:
            await api_client.ensure_slack_user_registered(
                slack_id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url,
            )
        except MLAIBackendUnavailableError as exc:
            print(f"⚠️ Failed to register content-factory user {user_id}: {exc!r}")
            if cost_points == 0:
                return None
            return self._content_factory_backend_unavailable_message()
        except Exception as exc:
            print(f"⚠️ Failed to register content-factory user {user_id}: {exc!r}")
            if cost_points == 0:
                return None
            return (
                "I couldn't link your Slack account to MLAI yet, so I haven't charged anything. "
                "Please try again in a moment."
            )

        if cost_points == 0:
            return None

        try:
            balance_data = await api_client.get_balance(user_id)
        except MLAIBackendUnavailableError as exc:
            print(f"⚠️ Failed to fetch Roo points balance for {user_id}: {exc!r}")
            return self._content_factory_backend_unavailable_message()
        except Exception as exc:
            print(f"⚠️ Failed to fetch Roo points balance for {user_id}: {exc!r}")
            return (
                "I couldn't check your Roo points balance just now, so I haven't started the article yet. "
                "Please try again in a moment."
            )

        balance = int(balance_data.get("balance") or 0)
        if balance < cost_points:
            return (
                f"Creating an article costs {cost_points} Roo points, and your balance is below "
                "that amount. DM Roo `points` to view your balance privately."
            )

        return None

    def _build_content_factory_start_response(
        self,
        *,
        domain: str,
        job_id: str,
        topic: Optional[str],
        workflow: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        is_discovery = workflow == "auto_discovery" or not topic
        summary_text = (
            "Starting discovery to find the best article opportunity. I'll keep this message updated."
            if is_discovery
            else f"Starting article generation for `{topic}`. I'll keep this message updated."
        )

        return {
            "message": (
                f"Starting Content Factory for {domain}. "
                "I'll keep this message updated as the run moves forward."
            ),
            "blocks": build_live_status_blocks(
                domain,
                summary_text=summary_text,
                include_decision_stage=is_discovery,
                current_stage="preparing",
            ),
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": True,
                "content_factory_watchdog_mode": workflow,
                "content_factory_domain": domain,
                "content_factory_workflow": workflow,
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }

    def _build_publish_pr_start_response(
        self,
        *,
        domain: Optional[str],
        job_id: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        display_domain = normalize_content_factory_domain(domain) or domain or "this domain"
        return {
            "message": (
                f"Publishing the completed article bundle for {display_domain} as a draft PR. "
                "I'll keep this message updated as the run moves forward."
            ),
            "blocks": build_live_status_blocks(
                display_domain,
                summary_text=(
                    "Promoting the completed article bundle into the repo and preview flow. "
                    "I'll keep this message updated."
                ),
                include_decision_stage=False,
                current_stage="preparing",
            ),
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": True,
                "content_factory_watchdog_mode": "publish_pr",
                "content_factory_domain": display_domain,
                "content_factory_workflow": "publish_pr",
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }

    async def _resolve_publish_pr_follow_up(
        self,
        api_client,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> tuple[dict, Optional[Any]]:
        from ..clients.mlai_backend import MLAIBackendUnavailableError
        (
            requested_by_slack_user_id,
            effective_slack_user_id,
            _is_delegated,
        ) = (
            lambda identity: (
                identity.requested_by_slack_user_id,
                identity.effective_slack_user_id,
                identity.is_delegated,
            )
        )(
            resolve_content_factory_identity_context(
                requester_slack_user_id=user_id,
                requested_by_slack_user_id=params.get("requested_by_slack_user_id"),
                effective_slack_user_id=params.get("effective_slack_user_id"),
            )
        )

        requested_action = detect_content_action(text)
        if requested_action != "publish_pr":
            return params, None

        existing_job_id = str(params.get("job_id") or "").strip()
        if existing_job_id:
            params["action"] = "publish_pr"
            return params, None

        if not channel_id or not thread_ts:
            return params, (
                "I couldn't identify a completed article bundle for this publish request because the Slack thread context was missing."
            )

        try:
            delegated_backend_kwargs = self._delegated_backend_kwargs(
                requested_by_slack_user_id,
                effective_slack_user_id,
            )
            resolution = await api_client.resolve_content_thread(
                slack_user_id=effective_slack_user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                requested_action="publish_pr",
                domain=params.get("domain"),
                **delegated_backend_kwargs,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return params, (
                    "I couldn't identify a completed article bundle to publish from this thread. "
                    "Please use the original content-ready thread for the article you want to publish."
                )
            return params, f"❌ I couldn't resolve which completed article to publish from this thread: {exc}"
        except MLAIBackendUnavailableError:
            return params, self._content_factory_backend_unavailable_message()
        except Exception as exc:
            return params, f"❌ I couldn't resolve which completed article to publish from this thread: {exc}"

        resolution_type = str(resolution.get("resolution") or "").strip()
        resolved_domain = (
            normalize_content_factory_domain(resolution.get("domain"))
            or params.get("domain")
            or resolution.get("domain")
        )
        if resolution_type == "ready":
            params["action"] = "publish_pr"
            params["job_id"] = str(resolution.get("job_id") or "").strip()
            if resolved_domain:
                params["domain"] = resolved_domain
            return params, None

        if resolution_type == "in_progress":
            publish_job_id = str(resolution.get("promoted_publish_job_id") or "").strip()
            if not publish_job_id:
                return params, (
                    "I found an article in this thread that is already being promoted, but I couldn't determine the active publish run."
                )
            return params, self._build_publish_pr_start_response(
                domain=resolved_domain,
                job_id=publish_job_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

        return params, (
            "I couldn't identify a completed article bundle to publish from this thread."
        )

    def _get_connected_domain_info(
        self,
        connected_domains: List[dict],
        domain: Optional[str],
    ) -> Optional[dict]:
        """Return the connected domain record for the requested domain."""
        if not domain or not connected_domains:
            return None

        return next(
            (domain_info for domain_info in connected_domains if domain_info.get("domain") == domain),
            None,
        )

    async def _publish_content_bundle_as_pr(
        self,
        api_client,
        *,
        job_id: str,
        domain: Optional[str],
        requested_by_slack_user_id: str,
        effective_slack_user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        text: str,
        params: dict,
    ) -> Any:
        from ..clients.mlai_backend import MLAIBackendUnavailableError

        if not job_id:
            return (
                "I couldn't tell which article bundle to publish as a PR in this thread. "
                "Please ask from the original article thread after the content-ready message."
            )

        reconnect_result = await self._request_github_reconnect(
            api_client,
            user_id=effective_slack_user_id,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
            domain=domain,
            github_repo=None,
            trigger="preflight",
            pending_action="publish_pr",
            channel_id=channel_id,
            thread_ts=thread_ts,
            button_label=f"Reconnect GitHub for {domain or 'this domain'}",
            text=text,
            params=params,
            save_pending=True,
        )
        if reconnect_result is not None:
            return reconnect_result

        try:
            response = await api_client.publish_article_as_pr(
                job_id,
                effective_slack_user_id,
                **self._delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        except httpx.HTTPStatusError as exc:
            error_message = str(exc)
            try:
                error_data = exc.response.json()
            except Exception:
                error_data = {}
            if isinstance(error_data, dict):
                if error_data.get("error_code") == "AUTH_REQUIRED":
                    if is_delegated_content_factory_request(
                        requested_by_slack_user_id,
                        effective_slack_user_id,
                    ):
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    if not error_data.get("pending_intent_stored"):
                        await self._save_content_factory_pending_intent(
                            api_client,
                            requested_by_slack_user_id,
                            params,
                            text,
                            channel_id,
                            thread_ts,
                        )
                    auth_url = error_data.get("auth_url")
                    if not auth_url:
                        reconnect = await api_client.reconnect_content_factory_github(
                            slack_user_id=effective_slack_user_id,
                            domain=domain,
                            github_repo=None,
                            trigger="fallback_412",
                            pending_action="publish_pr",
                        )
                        auth_url = reconnect.get("auth_url")
                    message = error_data.get("message") or "Reconnect GitHub before I can publish that bundle as a PR."
                    if auth_url:
                        blocks = self._build_github_reconnect_blocks(
                            message,
                            auth_url,
                            button_label=f"Reconnect GitHub for {domain or 'this domain'}",
                        )
                        if channel_id:
                            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"{message} Use the button above to continue.",
                                blocks=blocks,
                            )
                        return {
                            "message": f"{message}\n\n{auth_url}",
                            "blocks": blocks,
                        }
                error_message = (
                    error_data.get("error")
                    or error_data.get("message")
                    or error_message
                )
            return f"❌ I couldn't publish that article bundle as a PR: {error_message}"
        except Exception as exc:
            return f"❌ I couldn't publish that article bundle as a PR: {exc}"

        child_job_id = str(response.get("job_id") or response.get("run_id") or "").strip()
        if not child_job_id:
            return (
                "I asked Content Factory to publish that article bundle as a PR, "
                "but it didn't return a run ID."
            )

        resolved_domain = (
            normalize_content_factory_domain(domain)
            or normalize_content_factory_domain(response.get("domain"))
            or domain
            or response.get("domain")
        )
        return self._build_publish_pr_start_response(
            domain=resolved_domain,
            job_id=child_job_id,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )

    def _resolve_content_factory_repo_name(
        self,
        integration: dict,
        connected_domains: List[dict],
        domain: Optional[str],
    ) -> tuple[Optional[str], Optional[dict]]:
        """Resolve the repo Roo should use for content-factory flows."""
        domain_info = self._get_connected_domain_info(connected_domains, domain)

        if domain:
            return (
                integration.get("domain_github_repo")
                or (domain_info or {}).get("github_repo"),
                domain_info,
            )

        return integration.get("github_repo"), domain_info

    @staticmethod
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

    @classmethod
    def _registry_target_readiness(cls, target: Any) -> dict[str, bool]:
        if not cls._is_registry_driven_publish_target(target):
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

    @classmethod
    def _registry_target_publish_ready(cls, target: Any) -> bool:
        if not cls._is_registry_driven_publish_target(target):
            return False
        readiness = cls._registry_target_readiness(target)
        return all(readiness.values())

    @staticmethod
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

    @classmethod
    def _best_registry_driven_target(cls, *sources: Any) -> Optional[dict]:
        targets: list[dict] = []
        for source in sources:
            if not source:
                continue
            if isinstance(source, list):
                candidates = source
            elif isinstance(source, dict):
                candidates = source.get("publish_targets") or source.get("targets") or []
                if cls._is_registry_driven_publish_target(source):
                    candidates = [source, *list(candidates or [])]
                else:
                    direct_target = cls._registry_target_from_article_system(source)
                    nested_target = cls._registry_target_from_article_system(source.get("article_system"))
                    synthesized = [target for target in (direct_target, nested_target) if target]
                    if synthesized:
                        candidates = [*list(candidates or []), *synthesized]
            else:
                candidates = []
            for candidate in candidates:
                if isinstance(candidate, dict) and cls._is_registry_driven_publish_target(candidate):
                    targets.append(candidate)
        if not targets:
            return None
        return next((target for target in targets if cls._registry_target_publish_ready(target)), None) or targets[0]

    @staticmethod
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

    @classmethod
    def _registry_target_issues(cls, target: Optional[dict], article_system: Optional[dict] = None) -> list[str]:
        issues: list[str] = []
        target = target or {}
        article_system = article_system or {}
        strategy = target.get("registration_strategy") if isinstance(target.get("registration_strategy"), dict) else {}
        readiness = cls._registry_target_readiness(target)
        for key, ready in readiness.items():
            if not ready:
                issues.append(f"{key.replace('_', ' ')} is not proven")

        diagnostic_sources = [
            target.get("diagnostics"),
            strategy.get("diagnostics"),
            article_system.get("diagnostics"),
            target.get("observability"),
            article_system.get("observability"),
        ]
        for source in diagnostic_sources:
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

    @classmethod
    def _registry_target_diagnostic_message(
        cls,
        *,
        domain: str,
        target: Optional[dict],
        article_system: Optional[dict] = None,
    ) -> str:
        registry_path = cls._registry_target_path(target, article_system)
        issues = cls._registry_target_issues(target, article_system)
        issue_text = ""
        if issues:
            issue_text = "\n\nBlockers:\n" + "\n".join(f"- {item}" for item in issues[:6])
        return (
            f"I found a registry-driven SEO system for *{domain}* at `{registry_path}`, "
            f"but it is not safe to patch automatically yet."
            f"{issue_text}\n\n"
            "Roo stopped before changing the repository. Use content-only delivery for now, "
            "or add/confirm a `.content-factory/target.yml` hook after the registry target is proven."
        )

    @classmethod
    def _registry_target_diagnostic_blocks(
        cls,
        *,
        domain: str,
        target: Optional[dict],
        article_system: Optional[dict] = None,
    ) -> list[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": cls._registry_target_diagnostic_message(
                        domain=domain,
                        target=target,
                        article_system=article_system,
                    ),
                },
            }
        ]
    
    async def _execute_content_factory(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        thread_history: Optional[List[dict]] = None
    ) -> str:
        """Execute the content factory generation workflow."""
        from ..clients.mlai_backend import MLAIBackendUnavailableError

        params = dict(params or {})
        params["client_request_id"] = self._get_content_factory_client_request_id(params)
        (
            requested_by_slack_user_id,
            effective_slack_user_id,
            is_delegated,
        ) = (
            lambda identity: (
                identity.requested_by_slack_user_id,
                identity.effective_slack_user_id,
                identity.is_delegated,
            )
        )(
            resolve_content_factory_identity_context(
                requester_slack_user_id=user_id,
                requested_by_slack_user_id=params.get("requested_by_slack_user_id"),
                effective_slack_user_id=params.get("effective_slack_user_id"),
            )
        )
        params["requested_by_slack_user_id"] = requested_by_slack_user_id
        params["effective_slack_user_id"] = effective_slack_user_id

        def content_factory_identity_payload(**extra: Any) -> dict[str, Any]:
            return build_content_factory_identity_payload(
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                **extra,
            )

        delegated_backend_kwargs = self._delegated_backend_kwargs(
            requested_by_slack_user_id,
            effective_slack_user_id,
        )
        
        # Get a MLAIBackendClient for API calls
        settings = get_settings()
        from roo.clients.mlai_backend import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY
        )
        domain = params.get("domain")
        org_config_cached = None
        action = params.get("action")
        params, publish_resolution = await self._resolve_publish_pr_follow_up(
            api_client,
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if publish_resolution is not None:
            return publish_resolution
        action = params.get("action")
        domain = params.get("domain")
        if action == "publish_pr":
            return await self._publish_content_bundle_as_pr(
                api_client,
                job_id=str(params.get("job_id") or "").strip(),
                domain=domain,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=text,
                params=params,
            )
        is_scan_request = self._is_explicit_scan_request(text, params)
        is_article_flow = action != "scaffold" and not is_scan_request
        requested_delivery_mode, requested_delivery_mode_confirmed = self._resolve_requested_article_delivery_mode(
            text,
            params,
        )

        # Check status of the requested GitHub integration.
        # When a domain is explicitly provided, querying the generic user-level
        # integration can return a multi-domain selection error before we ever
        # reach the domain-specific flow.
        try:
            integration = await api_client.get_integration(
                effective_slack_user_id,
                domain=domain,
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        
        # 1. New User Disclaimer & Education
        # If user has no integration AND hasn't confirmed the disclaimer yet
        if not integration and not params.get("confirmed"):
            await self._save_content_factory_pending_intent(
                api_client,
                requested_by_slack_user_id,
                params,
                text,
                channel_id,
                thread_ts,
            )

            confirm_value = json.dumps(
                build_content_factory_identity_payload(
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    text=text,
                    params=params,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
            )
            disclaimer_cost_text = (
                f"*Cost:* Articles for **{normalize_content_factory_domain(domain) or domain or 'this domain'}** are free. "
                "No Roo points will be deducted for this run.\n\n"
                if is_free_content_factory_domain(domain)
                else (
                    f"*Cost:* Creating an article costs **{CONTENT_FACTORY_ARTICLE_COST_POINTS} Roo points**. "
                    "Those points are deducted when article research/generation starts. "
                    "If something goes wrong, message Dr Sam on Slack and he can help sort out a refund.\n\n"
                )
            )
            
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ Content Factory Requirements",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Before we start, a quick heads-up! This skill works best with **Next.js & Tailwind CSS** projects "
                            "when you want a PR-based publish flow. If there's no repo connected, I can still do "
                            "**content-only** article research and writing for manual upload.\n\n"
                            f"{disclaimer_cost_text}"
                            "*How it works:*\n"
                            "1. 🔎 **Researches** your domain, competitors, and keywords\n"
                            "2. ✍️ **Writes** the article draft\n"
                            "3. 🧩 **Uses** repo context when you want code/publish delivery\n"
                            "4. 🚀 **Either** hands back content-only copy or prepares the PR flow"
                        )
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "I'm ready to proceed",
                                "emoji": True
                            },
                            "action_id": "confirm_content_factory",
                            "value": confirm_value,
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please review the requirements above.", thread_ts=thread_ts, blocks=blocks)
                return "Please review the requirements above to get started! 👆"
            return "Please confirm you have a Next.js/Tailwind project and are ready to connect GitHub."

        # Check for Expired Token or Other Errors
        if integration and integration.get("error"):
            if is_delegated:
                return self._delegated_content_factory_auth_required_message(
                    effective_slack_user_id=effective_slack_user_id,
                    domain=domain,
                )
            auth_url = integration.get("auth_url")
            error_msg = integration.get("error")
            
            if not auth_url:
                # Fallback if auth_url missing in error response
                try:
                    auth_url_resp = await api_client.get_github_auth_url(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                auth_url = auth_url_resp.get("auth_url")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ **Connection Issue**: {error_msg}\nI need you to re-connect your GitHub account to continue."
                    }
                },
                {
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
                                "text": "🚀 I've Connected - Resume",
                                "emoji": True
                            },
                            "action_id": "resume_scan",
                            "value": json.dumps(
                                build_content_factory_identity_payload(
                                    requested_by_slack_user_id=requested_by_slack_user_id,
                                    effective_slack_user_id=effective_slack_user_id,
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                )
                            ),
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return self._already_posted_response(
                    "Please re-connect your GitHub account using the button above. 🔌",
                    blocks=blocks,
                )
            return f"GitHub connection issue ({error_msg}). Please re-connect here: {auth_url}"

        if not integration:
            if is_article_flow:
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                if org_config_cached:
                    domain = domain or org_config_cached.get("domain")
                    repo_hint = org_config_cached.get("github_repo")
                    integration = {
                        "connected_domains": (
                            [{"domain": domain, "github_repo": repo_hint, "scanned": bool(org_config_cached.get("scan_summary"))}]
                            if domain
                            else []
                        ),
                        "recommended_next_action": None,
                        "last_article": None,
                        "project_scanned": bool(org_config_cached.get("scan_summary")),
                        "content_research_ready": bool(org_config_cached.get("scan_summary")),
                        "article_system": org_config_cached.get("article_system") or {},
                        "article_delivery_mode": org_config_cached.get("article_delivery_mode"),
                    }

            if not integration:
                if is_delegated:
                    return self._delegated_content_factory_auth_required_message(
                        effective_slack_user_id=effective_slack_user_id,
                        domain=domain,
                    )
                try:
                    auth_response = await api_client.get_github_auth_url(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                auth_url = auth_response.get("auth_url")

                if not auth_url:
                    return "Sorry mate, I couldn't get the authorization URL from the backend. Try again strictly?"

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "I need permission to access your GitHub to publish articles. Click the button below to connect your account."
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Connect GitHub Account",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github",
                                "style": "primary"
                            }
                        ]
                    }
                ]

                await self._save_content_factory_pending_intent(
                    api_client,
                    requested_by_slack_user_id,
                    params,
                    text,
                    channel_id,
                    thread_ts,
                )

                if channel_id:
                    post_message(channel_id, "Please connect GitHub", thread_ts=thread_ts, blocks=blocks)
                    return self._already_posted_response(
                        "I've sent a button to connect your GitHub account. 🔌",
                        blocks=blocks,
                    )
                return f"Please connect your GitHub account here: {auth_url}"

        # 2. Resolve domain from connected_domains
        connected_domains = integration.get("connected_domains", []) if integration else []

        if not domain:
            if len(connected_domains) == 0:
                # No domains connected — fall back to org config lookup
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        slack_user_id=effective_slack_user_id
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                if org_config_cached:
                    domain = org_config_cached.get("domain")
            elif len(connected_domains) == 1:
                # Single domain — use it automatically
                domain = connected_domains[0].get("domain")
            else:
                # Multiple domains — ask user to choose
                domain_list = "\n".join(
                    f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                    for d in connected_domains
                )
                msg = f"You have multiple connected codebases. Which one should I work with?\n\n{domain_list}\n\nTry: `@Roo scan <domain>` or `@Roo write an article for <domain>`"
                if channel_id:
                    post_message(channel_id, msg, thread_ts)
                return msg

        # Refresh integration with domain context once we know the target domain.
        # The generic top-level integration response may still show repo drift,
        # while the domain-specific response can correctly say "research_article"
        # or "write_article" without forcing a re-scan.
        if domain:
            try:
                domain_integration = await api_client.get_integration(
                    effective_slack_user_id,
                    domain=domain,
                )
            except MLAIBackendUnavailableError:
                return self._content_factory_backend_unavailable_message()
            if domain_integration:
                integration = domain_integration
                connected_domains = integration.get("connected_domains", connected_domains)
            elif is_article_flow and org_config_cached is None:
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()

        repo_name, domain_info = self._resolve_content_factory_repo_name(
            integration or {},
            connected_domains,
            domain,
        )

        if domain and integration and integration.get("needs_github_auth") and not is_article_flow:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="manual",
                pending_action=action or ("scan" if is_scan_request else "repo_action"),
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

        # No repo at all — prompt to connect
        if not repo_name and not is_article_flow:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action=action or ("scan" if is_scan_request else "repo_action"),
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Reconnect GitHub & Select Repo",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

        # 3. Handle explicit scaffold action
        if action == "scaffold":
            if not domain:
                return "I need a domain to scaffold the articles directory. Try: `@Roo scaffold articles for <domain>`"

            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scaffold",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

            # Check scan prerequisite
            if domain_info and not domain_info.get("scanned"):
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "prerequisite_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                "style": "primary",
                                "value": json.dumps(
                                    content_factory_identity_payload(
                                        domain=domain,
                                        channel_id=channel_id,
                                        thread_ts=thread_ts,
                                        original_intent={"action": "scaffold"},
                                    )
                                ),
                                "action_id": "prerequisite_scan"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                "value": json.dumps(
                                    content_factory_identity_payload(domain=domain)
                                ),
                                "action_id": "prerequisite_cancel"
                            }
                        ]
                    }
                ]
                if channel_id:
                    post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                return "I need to scan your codebase first before I can scaffold the articles directory."

            if channel_id:
                post_message(
                    channel_id,
                    f"📁 Creating articles directory for *{domain}*...",
                    thread_ts
                )

            try:
                result = await api_client.scaffold_articles(
                    domain=domain,
                    slack_user_id=effective_slack_user_id,
                    slack_channel_id=channel_id or "",
                    slack_thread_ts=thread_ts or "",
                    **delegated_backend_kwargs,
                )

                status_code = result.get("status_code")
                data = result.get("data", {})

                if status_code == 200:
                    pr_url = data.get("pr_url", "")
                    preview_url = data.get("preview_url", "")
                    primary_action_url = data.get("primary_action_url", "")
                    primary_action_label = data.get("primary_action_label", "")
                    detail_parts = []
                    if pr_url:
                        detail_parts.append(f"<{pr_url}|View PR>")
                    if primary_action_url:
                        detail_parts.append(f"<{primary_action_url}|{primary_action_label or 'Open Review'}>")
                    elif preview_url:
                        detail_parts.append(f"<{preview_url}|View Preview>")
                    detail_text = f" {' | '.join(detail_parts)}" if detail_parts else ""
                    return f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                elif status_code == 202:
                    return "Scaffolding is underway! I'll reply here when it's done. 🏗️"
                elif status_code == 412:
                    missing_step = data.get("missing_step", "scan")
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent={"action": "scaffold"},
                                        )
                                    ),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif status_code == 400:
                    if data.get("needs_github_auth"):
                        if is_delegated:
                            return self._delegated_content_factory_auth_required_message(
                                effective_slack_user_id=effective_slack_user_id,
                                domain=domain,
                            )
                        oauth_url = data.get("oauth_url", "")
                        return f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {oauth_url}"
                    return f"❌ Could not start scaffolding: {data.get('error', 'Unknown error')}"
                elif status_code == 404:
                    return f"❌ No configuration found for *{domain}*."
                else:
                    return f"❌ Unexpected response from backend (status {status_code})"
            except Exception as e:
                print(f"❌ Failed to trigger scaffold: {e}")
                return f"❌ Error creating articles directory: {e}"

        # 4. Check scan status
        needs_scan = False
        scan_reason = ""
        recommended_next_action = integration.get("recommended_next_action") if integration else None
        scan_completed = bool(
            (integration.get("scan_completed") if integration else False)
            or (integration.get("content_research_ready") if integration else False)
            or (domain_info and domain_info.get("scanned"))
        )

        if not is_article_flow or repo_name:
            if recommended_next_action == "scan":
                needs_scan = True
                scan_reason = "Initial scan required"
            elif domain_info:
                if not domain_info.get("scanned"):
                    needs_scan = True
                    scan_reason = "Initial scan required"
            elif integration and not integration.get("project_scanned"):
                needs_scan = True
                scan_reason = "Initial scan required"

        # Only use legacy has_updates as a fallback when the backend has not
        # already told us that a usable scan exists for this domain.
        if (
            not scan_completed
            and recommended_next_action in (None, "scan")
            and integration
            and integration.get("has_updates")
            and (not is_article_flow or repo_name)
        ):
            needs_scan = True
            scan_reason = "🔄 Updates detected in repository"

        # Explicit scan requests should confirm before re-scanning an already scanned repo.
        last_scanned = integration.get("last_scanned_at", "Never") if integration else "Never"
        if self._is_explicit_scan_request(text, params) and scan_completed and not needs_scan:
            return self._build_existing_scan_confirmation(
                domain=domain,
                repo_name=repo_name,
                last_scanned=last_scanned,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

        # Compile Status Report
        last_scanned = integration.get("last_scanned_at", "Never") if integration else "Never"
        last_article = ((integration or {}).get("last_article") or {}).get("title", "None")
        connection_state = str(
            (domain_info or {}).get("connection_state")
            or (integration or {}).get("connection_state")
            or ""
        ).strip().lower()
        needs_github_auth = bool((integration or {}).get("needs_github_auth")) or connection_state == "auth_required"

        if channel_id:
            if needs_github_auth and repo_name and domain:
                connected_msg = f"👋 G'day! Repository selected: `{repo_name}` for *{domain}*.\n\n"
            elif needs_github_auth and repo_name:
                connected_msg = f"👋 G'day! Repository selected: `{repo_name}`.\n\n"
            elif repo_name and domain:
                connected_msg = f"👋 G'day! Connected to `{repo_name}` for *{domain}*.\n\n"
            elif repo_name:
                connected_msg = f"👋 G'day! I see you're connected to `{repo_name}`.\n\n"
            else:
                connected_msg = f"👋 G'day! Working on *{domain}* in content-only mode unless you choose publish later.\n\n"
            status_msg = (
                connected_msg +
                f"📊 **Status Report:**\n"
                f"• Last scanned: {last_scanned}\n"
                f"• Last article: {last_article}\n"
            )
            if needs_scan:
                status_msg += f"\n{scan_reason}. Scanning updates now... 🕵️"
            elif needs_github_auth and repo_name:
                status_msg += "• Repository: reconnect GitHub to continue with repo-backed work"
            elif repo_name:
                status_msg += "• Repository: ✅ Up to date"
            else:
                status_msg += "• Repository: not connected (content-only is still available)"

            post_message(channel_id, status_msg, thread_ts)

        if needs_scan:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain or 'this domain'}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

            scan_result = await api_client.trigger_repo_scan(
                effective_slack_user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain,
                **delegated_backend_kwargs,
            )
            
            if scan_result.get("status") == "accepted":
                return "updates are being processed in the background! 🏃\nI'll reply here when the scan is complete."
                
            # Handle sync failures or other errors
            if scan_result.get("error"):
                error_msg = scan_result.get("message", "Unknown error")

                # Multiple domains — backend needs user to choose
                if scan_result.get("error") == "multiple_domains":
                    available = scan_result.get("available_domains", [])
                    domain_list = "\n".join(
                        f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                        for d in available
                    )
                    msg = f"{error_msg}\n\n{domain_list}\n\nTry: `@Roo scan <domain>`"
                    if channel_id:
                        post_message(channel_id, msg, thread_ts)
                    return msg

                # If backend says GitHub isn't connected for this domain
                if scan_result.get("needs_github_auth"):
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    oauth_url = scan_result.get("oauth_url")
                    domain_name = scan_result.get("domain", domain)
                    if oauth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌",
                                blocks=blocks,
                            )
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account and try again."

                # If error indicates auth failure or repo not found (404/403/401)
                if any(code in str(error_msg) for code in ["404", "401", "403", "Not Found", "Bad credentials"]):
                    # Fetch Auth URL to allow reconnect
                    try:
                        auth_response = await api_client.get_github_auth_url(
                            effective_slack_user_id,
                            domain=domain,
                        )
                    except MLAIBackendUnavailableError:
                        return self._content_factory_backend_unavailable_message()
                    auth_url = auth_response.get("auth_url")
                    
                    if auth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ **Connection Lost**: It looks like I can't access your repository anymore ({error_msg})."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Re-connect GitHub App",
                                            "emoji": True
                                        },
                                        "url": auth_url,
                                        "action_id": "connect_github",
                                        "style": "danger"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                "Please re-connect your GitHub App using the button above. 🔌",
                                blocks=blocks,
                            )
                
                return f"Had some trouble scanning your repository: {error_msg}"


            # Legacy Sync Behavior (if backend returns 200 immediately)
            # Scan succeeded - refresh integration status
            try:
                integration = await api_client.get_integration(
                    effective_slack_user_id,
                    domain=domain,
                )
            except MLAIBackendUnavailableError:
                return self._content_factory_backend_unavailable_message()
            if not integration or not integration.get("project_scanned"):
                return "Scanning is taking a bit longer than expected. Please wait for the notification! 🦘"
            
            if channel_id:
                post_message(channel_id, "✅ Repository analysis complete! Ready to write.", thread_ts)

        # 3. Validation: Check parameters (Domain/Topic)
        topic = params.get("topic")
        target_keyword = params.get("target_keyword", "")

        if not domain:
            return "I can help write that article! To get started, I just need to know the domain name (e.g., mlai.au)."

        if self._should_prompt_for_article_direction(text, params):
            return {
                "message": (
                    f"I can do that for {domain}. First, choose whether you want me to research the best article "
                    "opportunity or whether you'll give me the topic."
                ),
                "blocks": self._build_article_direction_blocks(
                    domain,
                    user_id,
                    channel_id,
                    thread_ts,
                    params["client_request_id"],
                    requested_delivery_mode,
                    requested_delivery_mode_confirmed,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                ),
            }

        article_system = (integration or {}).get("article_system") or {}
        if not article_system and domain_info:
            article_system = domain_info.get("article_system") or {}
        registry_target = self._best_registry_driven_target(integration, domain_info, article_system)
        registry_target_ready = self._registry_target_publish_ready(registry_target)

        if (
            topic
            and registry_target
            and not registry_target_ready
            and recommended_next_action in {"scaffold", "confirm_article_system"}
        ):
            message = self._registry_target_diagnostic_message(
                domain=domain,
                target=registry_target,
                article_system=article_system,
            )
            blocks = self._registry_target_diagnostic_blocks(
                domain=domain,
                target=registry_target,
                article_system=article_system,
            )
            if channel_id:
                post_message(channel_id, f"Registry target needs confirmation for {domain}", thread_ts=thread_ts, blocks=blocks)
                return self._already_posted_response(message, blocks=blocks)
            return message

        if topic and recommended_next_action == "confirm_article_system":
            original_intent = {
                "action": "write",
                "topic": topic,
                "client_request_id": params["client_request_id"],
            }
            detected_location = (
                article_system.get("directory_path")
                or article_system.get("directory_name")
                or "the detected article directory"
            )
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"I found what looks like your existing article system at *`{detected_location}`*, "
                            f"but I need confirmation before I write into it."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": "article_system_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Use Detected Structure", "emoji": True},
                            "style": "primary",
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_use_detected",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Rescan Repo", "emoji": True},
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_rescan",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_scaffold",
                        },
                    ],
                },
            ]
            if channel_id:
                post_message(channel_id, f"Confirm article system for {domain}", thread_ts=thread_ts, blocks=blocks)
            return "I found an existing article structure, but I need you to confirm whether I should use it."

        try:
            # Start generation via MLAI Backend
            # Enhance context with thread history if available
            full_context = text
            if thread_history:
                history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in thread_history[:-1]])
                full_context = f"Context from Thread:\n{history_str}\n\nCurrent Request: {text}"

            access_error = await self._validate_content_factory_paid_access(
                api_client,
                requested_by_slack_user_id,
                domain,
            )
            if access_error:
                return access_error

            effective_article_delivery_mode = (
                requested_delivery_mode
                or str((integration or {}).get("article_delivery_mode") or "").strip().lower()
                or str((org_config_cached or {}).get("article_delivery_mode") or "").strip().lower()
                or None
            )
            if effective_article_delivery_mode == "publish_code":
                reconnect_result = await self._request_github_reconnect(
                    api_client,
                    user_id=effective_slack_user_id,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    domain=domain,
                    github_repo=repo_name,
                    trigger="preflight",
                    pending_action="write_article",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    button_label=f"Reconnect GitHub for {domain}",
                    text=text,
                    params=params,
                    save_pending=True,
                )
                if reconnect_result is not None:
                    return reconnect_result

            from ..slack_client import get_user_info
            slack_info = get_user_info(requested_by_slack_user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            
            # Note: topic can be None (triggers Auto-Write / Research Mode)
            response = await api_client.trigger_article_generation(
                slack_user_id=effective_slack_user_id,
                domain=domain,
                topic=topic,
                target_keyword=target_keyword,
                context=full_context,
                delivery_mode=requested_delivery_mode,
                delivery_mode_confirmed=requested_delivery_mode_confirmed,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                client_request_id=params["client_request_id"],
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
                **delegated_backend_kwargs,
            )
            
            job_id = response.get("job_id")
            if not job_id:
                return "Failed to start generation: No job ID returned from backend."

            if str(response.get("status") or "").strip().lower() == "awaiting_delivery_mode":
                return self._build_article_delivery_mode_prompt(
                    domain=domain,
                    job_id=job_id,
                    topic=topic,
                    recommended_delivery_mode=response.get("recommended_delivery_mode"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )

            workflow = str(
                response.get("workflow")
                or ("auto_discovery" if not topic else "direct_generate")
            ).strip().lower()

            return self._build_content_factory_start_response(
                domain=domain,
                job_id=job_id,
                topic=topic,
                workflow=workflow,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )
            
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        except httpx.HTTPStatusError as e:
            print(f"Content Generation HTTP Error: {e}")
            if e.response.status_code == 412:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                error_code = error_data.get("error_code", "")
                if error_code == "AUTH_REQUIRED":
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    if not error_data.get("pending_intent_stored"):
                        await self._save_content_factory_pending_intent(
                            api_client,
                            requested_by_slack_user_id,
                            params,
                            text,
                            channel_id,
                            thread_ts,
                        )
                    auth_url = error_data.get("auth_url")
                    if not auth_url:
                        reconnect = await api_client.reconnect_content_factory_github(
                            slack_user_id=effective_slack_user_id,
                            domain=domain,
                            github_repo=repo_name,
                            trigger="fallback_412",
                            pending_action="write_article",
                        )
                        auth_url = reconnect.get("auth_url")
                    message = error_data.get("message") or f"Reconnect GitHub for {domain} before I continue."
                    if auth_url:
                        blocks = self._build_github_reconnect_blocks(
                            message,
                            auth_url,
                            button_label=f"Reconnect GitHub for {domain}",
                        )
                        if channel_id:
                            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"{message} Use the button above to continue.",
                                blocks=blocks,
                            )
                        return {
                            "message": f"{message}\n\n{auth_url}",
                            "blocks": blocks,
                        }
                    return message
                if error_code == "PUBLISH_TARGET_ACTION_REQUIRED":
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    error_article_system = error_data.get("article_system") or article_system or {}
                    error_registry_target = self._best_registry_driven_target(
                        error_data,
                        integration,
                        domain_info,
                        error_article_system,
                    )
                    if error_registry_target and not self._registry_target_publish_ready(error_registry_target):
                        message = self._registry_target_diagnostic_message(
                            domain=domain,
                            target=error_registry_target,
                            article_system=error_article_system,
                        )
                        blocks = self._registry_target_diagnostic_blocks(
                            domain=domain,
                            target=error_registry_target,
                            article_system=error_article_system,
                        )
                        if channel_id:
                            post_message(
                                channel_id,
                                f"Registry target needs confirmation for {domain}",
                                thread_ts=thread_ts,
                                blocks=blocks,
                            )
                            return self._already_posted_response(message, blocks=blocks)
                        return message
                    auth_url = None
                    if domain:
                        try:
                            auth_response = await api_client.get_github_auth_url(
                                effective_slack_user_id,
                                domain=domain,
                            )
                        except MLAIBackendUnavailableError:
                            return self._content_factory_backend_unavailable_message()
                        auth_url = auth_response.get("auth_url")

                    if channel_id and auth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"Publish mode isn't ready for *{domain}* yet.\n\n"
                                        "I need a connected GitHub repo before I can open the PR flow. "
                                        "If you want, connect GitHub now, or ask me to do this as content-only instead."
                                    ),
                                },
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": f"Connect GitHub for {domain}", "emoji": True},
                                        "url": auth_url,
                                        "action_id": "connect_github",
                                        "style": "primary",
                                    }
                                ],
                            },
                        ]
                        post_message(channel_id, f"Publish mode needs GitHub for {domain}", thread_ts=thread_ts, blocks=blocks)
                        return (
                            f"Publish mode needs GitHub for {domain}. Use the button above to connect it, "
                            "or ask me for content-only delivery."
                        )
                    if auth_url:
                        return (
                            f"Publish mode needs GitHub for {domain}. Connect it here: {auth_url}\n\n"
                            "Or ask me for content-only delivery."
                        )
                    return (
                        f"Publish mode isn't ready for {domain} yet because no GitHub repo is connected. "
                        "Ask me for content-only delivery instead, or connect GitHub and try again."
                    )
                if error_code == "ARTICLE_SYSTEM_ACTION_REQUIRED":
                    recommended_action = error_data.get("recommended_action", "scaffold")
                    article_system = error_data.get("article_system") or {}
                    resolution_source = error_data.get("article_system_resolution_source", "")
                    detected_location = (
                        article_system.get("directory_path")
                        or article_system.get("directory_name")
                        or "the detected article directory"
                    )
                    error_registry_target = self._best_registry_driven_target(
                        error_data,
                        integration,
                        domain_info,
                        article_system,
                    )
                    if error_registry_target and not self._registry_target_publish_ready(error_registry_target):
                        message = self._registry_target_diagnostic_message(
                            domain=domain,
                            target=error_registry_target,
                            article_system=article_system,
                        )
                        blocks = self._registry_target_diagnostic_blocks(
                            domain=domain,
                            target=error_registry_target,
                            article_system=article_system,
                        )
                        if channel_id:
                            post_message(
                                channel_id,
                                f"Registry target needs confirmation for {domain}",
                                thread_ts=thread_ts,
                                blocks=blocks,
                            )
                            return self._already_posted_response(message, blocks=blocks)
                        return message
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic

                    if recommended_action == "confirm_article_system":
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"I found what looks like your existing article system at *`{detected_location}`*, "
                                        f"but I need confirmation before I write into it."
                                    ),
                                },
                            },
                            {
                                "type": "actions",
                                "block_id": "article_system_actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Use Detected Structure", "emoji": True},
                                        "style": "primary",
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_use_detected",
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Rescan Repo", "emoji": True},
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_rescan",
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_scaffold",
                                    },
                                ],
                            },
                        ]
                        if channel_id:
                            post_message(channel_id, f"Confirm article system for {domain}", thread_ts=thread_ts, blocks=blocks)
                        return "I found an existing article structure, but I need you to confirm whether I should use it."

                    if article_system.get("state") in {"existing", "roo_scaffolded"}:
                        return (
                            f"I found an existing article system at `{detected_location}`, but the backend still blocked "
                            f"writing (resolved via {resolution_source or 'unknown'}). Please rescan the repo or try again."
                        )

                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"I need to set up your articles directory before I can write articles for *{domain}*.\n\n"
                                    f"This will create a PR with all the reusable components and a demo article so you can see how everything looks."
                                ),
                            },
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scaffold",
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel",
                                },
                            ],
                        },
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scaffold required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to set up your articles directory first."
                missing_step = error_data.get("missing_step", "")
                if missing_step == "scan":
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif missing_step == "scaffold":
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to set up your articles directory before I can write articles for *{domain}*.\n\nThis will create a PR with all the reusable components and a demo article so you can see how everything looks."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scaffold"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scaffold required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to set up your articles directory first."
                else:
                    return f"A prerequisite step is missing: {error_data.get('error', 'Unknown')}. Please try again."
            if e.response.status_code == 400:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                # Structured error: GitHub not connected for this domain
                if error_data.get("needs_github_auth"):
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    oauth_url = error_data.get("oauth_url")
                    domain_name = error_data.get("domain", domain)
                    if oauth_url and channel_id:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain, then try again."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                        return f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌"
                    elif oauth_url:
                        return f"GitHub isn't connected for {domain_name}. Connect here: {oauth_url}"
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account."
                # Other 400 errors - show the error message
                error_msg = error_data.get("error", str(e))
                return f"Sorry mate, I had trouble starting the article generation: {error_msg}"
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"
        except Exception as e:
            print(f"Content Generation Error: {e}")
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"

    async def _monitor_generation(
        self,
        client,  # This is now MLAIBackendClient
        job_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        slack_user_id: str
    ):
        """Monitor job progress and post updates to Slack."""
        last_progress = -1
        last_step = ""
        
        try:
            consecutive_failures = 0

            # Poll until completion
            while True:
                try:
                    status_data = await client.check_generation_status(job_id)
                    consecutive_failures = 0 # Reset on success
                    
                    state = status_data.get("status")
                    progress = status_data.get("progress", 0)
                    step = status_data.get("current_step", "unknown")
                    
                    # Update progress
                    should_update = (
                        progress >= last_progress + 20 or 
                        (step != last_step and step in ["researching", "writing", "optimizing", "publishing"])
                    )
                    
                    if should_update:
                        msg = f"📝 *Status Update*: {step.title()}... ({progress}%)"
                        try:
                            post_message(channel_id, msg, thread_ts)
                            last_progress = progress
                            last_step = step
                        except Exception as e:
                            print(f"Failed to post progress: {e}")

                    if state == "awaiting_confirmation":
                        return
                    if state == "completed":
                        break
                    elif state == "failed":
                        raise Exception(f"Job failed: {status_data.get('error', 'Unknown')}")
                        
                except Exception as loop_error:
                    # If it's the "Job failed" exception raised above, re-raise it to exit
                    if "Job failed" in str(loop_error):
                        raise loop_error
                        
                    consecutive_failures += 1
                    print(f"⚠️ Monitor polling failed ({consecutive_failures}/5): {loop_error}")
                    
                    if consecutive_failures >= 5:
                        raise Exception(f"Lost connection to backend after 5 attempts. Last error: {loop_error}")
                
                await asyncio.sleep(5.0)
            
            # Publish
            post_message(channel_id, "✨ Article generated! Publishing now...", thread_ts)
            
            publish_result = await client.publish_article(job_id, slack_user_id)
            
            preview_url = publish_result.get("preview_url")
            primary_action_url = publish_result.get("primary_action_url")
            primary_action_label = publish_result.get("primary_action_label") or "Preview"
            pr_url = publish_result.get("pr_url")
            review_url = primary_action_url or preview_url
            review_label = primary_action_label if primary_action_url else "Preview"
            
            final_msg = (
                f"🎉 *Article Published!* \n\n"
                f"👀 *{review_label}:* {review_url}\n"
                f"💻 *Pull Request:* {pr_url}\n\n"
                f"Review the content and merge the PR when you're ready!"
            )
            
            post_message(channel_id, final_msg, thread_ts)
            
        except Exception as e:
            error_msg = f"❌ Something went wrong with the article generation: {str(e)}"
            post_message(channel_id, error_msg, thread_ts)
    

    async def _execute_mlai_data_query(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        thread_history: Optional[List[dict]] = None,
        slack_team_id: Optional[str] = None,
    ) -> Any:
        """Execute curated read-only data queries through mlai-backend."""
        from roo.clients import mlai_backend as backend_module

        settings = get_settings()
        if not settings.MLAI_BACKEND_URL:
            return "Sorry mate, the data query API isn't configured. Ask the team to set MLAI_BACKEND_URL."

        client = backend_module.MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            action = self._linear_channel_issue_action(
                text,
                params,
                thread_history=thread_history,
            )
            if action:
                if not channel_id or not slack_team_id:
                    return "This Linear issue list is only available from its connected Slack channel."
                if action == "list_linear_channel_issues":
                    result = await client.list_linear_channel_issues(
                        slack_workspace_id=slack_team_id,
                        slack_channel_id=channel_id,
                        requester_slack_id=user_id,
                        limit=self._coerce_data_query_limit(params.get("limit"), default=50),
                    )
                    return {
                        "message": self._format_linear_channel_issue_list(result),
                        "data": {
                            "action": action,
                            "issue_identifiers": [
                                str(issue.get("identifier") or "")
                                for issue in (result.get("issues") or [])
                                if isinstance(issue, dict) and issue.get("identifier")
                            ],
                            "result": result,
                        },
                    }

                issue_reference = self._resolve_linear_channel_issue_reference(
                    text=text,
                    params=params,
                    thread_history=thread_history,
                )
                issue_identifier = self._linear_issue_identifier(issue_reference)
                if not issue_identifier:
                    issue_candidates, pagination_complete = (
                        await self._list_all_linear_channel_issues(
                            client,
                            slack_workspace_id=slack_team_id,
                            slack_channel_id=channel_id,
                            requester_slack_id=user_id,
                        )
                    )
                    if not pagination_complete:
                        return (
                            "I couldn't finish searching the complete Linear issue list. "
                            "Please try again or reply with the issue key."
                        )
                    resolution = self._match_linear_channel_issue(
                        issue_reference,
                        issue_candidates,
                    )
                    if resolution.get("ambiguous"):
                        return self._format_linear_issue_choices(resolution["matches"])
                    issue_identifier = str(resolution.get("identifier") or "")
                if not issue_identifier:
                    return (
                        "Which Linear issue do you mean? Reply with an issue key such as "
                        "`TECH-16`, a list number, or a distinctive part of the title."
                    )

                result = await client.get_linear_channel_issue(
                    slack_workspace_id=slack_team_id,
                    slack_channel_id=channel_id,
                    requester_slack_id=user_id,
                    issue_identifier=issue_identifier,
                    include_comments=True,
                )
                return {
                    "message": self._format_linear_channel_issue_detail(result),
                    "data": {
                        "action": action,
                        "issue_identifier": issue_identifier,
                        "result": result,
                    },
                }

            if self._data_query_catalog_requested(text, params):
                catalog = await client.get_data_catalog(user_id)
                return {
                    "message": self._format_data_catalog(catalog),
                    "data": {"action": "data_catalog", "catalog": catalog},
                }

            payload = self._build_data_query_payload(text, params, user_id)
            if not payload.get("resource"):
                return (
                    "I can query the curated data resources, but I need a more specific dataset. "
                    "Ask for the data catalog to see what is available."
                )

            result = await client.query_data(payload)
            return {
                "message": self._format_data_query_result(payload, result),
                "data": {
                    "action": "data_query",
                    "payload": payload,
                    "result": result,
                },
            }
        except backend_module.MLAIBackendUnavailableError:
            return "MLAI backend is temporarily unavailable. Please try again in a moment."
        except httpx.HTTPStatusError as exc:
            detail = self._extract_http_error_detail(exc)
            if exc.response.status_code == 403:
                return detail or "You do not have access to that data resource."
            if exc.response.status_code == 400:
                return f"The data query was rejected: {detail or 'invalid query'}"
            return detail or "The data query failed. Please try again in a moment."

    async def _list_all_linear_channel_issues(
        self,
        client: Any,
        *,
        slack_workspace_id: str,
        slack_channel_id: str,
        requester_slack_id: str,
    ) -> tuple[list[dict], bool]:
        issues: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            request = {
                "slack_workspace_id": slack_workspace_id,
                "slack_channel_id": slack_channel_id,
                "requester_slack_id": requester_slack_id,
                "limit": 100,
            }
            if cursor:
                request["after"] = cursor
            page = await client.list_linear_channel_issues(**request)
            issues.extend(
                issue
                for issue in (page.get("issues") or [])
                if isinstance(issue, dict)
            )
            page_info = (
                page.get("pageInfo")
                if isinstance(page.get("pageInfo"), dict)
                else {}
            )
            if not page_info.get("hasNextPage"):
                return issues, True
            next_cursor = str(page_info.get("endCursor") or "").strip()
            if not next_cursor or next_cursor in seen_cursors:
                return issues, False
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _linear_channel_issue_action(
        self,
        text: str,
        params: dict,
        *,
        thread_history: Optional[List[dict]] = None,
    ) -> str:
        action = str(params.get("action") or "").strip().lower()
        if action in {"list_linear_channel_issues", "get_linear_channel_issue"}:
            return action
        text_lower = str(text or "").lower()
        explicit_identifier = (
            self._linear_issue_identifier(params.get("issue_reference"))
            or self._linear_issue_identifier(params.get("issue_identifier"))
            or self._linear_issue_identifier(text_lower)
        )
        if action in {"catalog", "list_resources", "schema"} and not explicit_identifier:
            return ""
        thread_context = self._linear_channel_issue_thread_context(thread_history)
        if "linear" in text_lower and re.search(
            r"\b(?:mlai[_ -]?tech|tech)\b.*\b(?:todo|issues?|tickets?|tasks?)\b",
            text_lower,
        ):
            return "list_linear_channel_issues"
        query_resource = self._infer_data_query_resource(text_lower, params)
        if action == "query" and query_resource and query_resource != "linear_issues":
            return ""
        if action == "query" and not thread_context:
            return ""
        explicit_reference = str(
            params.get("issue_reference")
            or params.get("issue_identifier")
            or ""
        )
        if explicit_identifier or self._linear_issue_identifier(explicit_reference):
            return "get_linear_channel_issue"
        detail_requested = (
            self._linear_contextual_detail_request(text_lower)
            or bool(re.search(r"\bnumber\s+\d+\b", text_lower))
        )
        bare_list_number = bool(re.fullmatch(r"\s*#?\d{1,3}\s*", text_lower))
        if detail_requested and (
            "linear" in text_lower
            or thread_context
        ):
            return "get_linear_channel_issue"
        if bare_list_number and thread_context:
            return "get_linear_channel_issue"
        return ""

    def _linear_contextual_detail_request(self, text: Any) -> bool:
        return bool(re.search(
            r"\b(?:more\s+(?:info|information|about)|details?|descriptions?|comments?|"
            r"status|state|assignees?|owners?|ownership|owns|"
            r"who\s+(?:owns|is\s+assigned)|projects?|cycles?|priorit(?:y|ies)|"
            r"estimates?|due(?:\s+date)?|deadlines?|created(?:\s+by)?|updated|"
            r"labels?|attachments?|relations?|metadata)\b",
            str(text or ""),
            re.IGNORECASE,
        ))

    def _linear_channel_issue_thread_context(
        self,
        thread_history: Optional[List[dict]],
    ) -> bool:
        return bool(self._linear_channel_issue_response_messages(thread_history))

    def _linear_channel_issue_response_messages(
        self,
        thread_history: Optional[List[dict]],
    ) -> list[dict]:
        numbered_issue = re.compile(
            r"^\s*\d{1,3}\.\s+(?:"
            r"<https://linear\.app/[^|>]+\|[A-Z][A-Z0-9]{1,15}-\d+>"
            r"|`[A-Z][A-Z0-9]{1,15}-\d+`)\s+—",
            re.IGNORECASE | re.MULTILINE,
        )
        detail_heading = re.compile(
            r"^\s*\*(?:"
            r"<https://linear\.app/[^|>]+\|[A-Z][A-Z0-9]{1,15}-\d+>"
            r"|`[A-Z][A-Z0-9]{1,15}-\d+`)\s+—[^\n]+\*\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        return [
            message
            for message in self._roo_authored_thread_messages(thread_history)
            if numbered_issue.search(str(message.get("text") or ""))
            or detail_heading.search(str(message.get("text") or ""))
            or self._linear_channel_issue_empty_response(message.get("text"))
        ]

    def _linear_channel_issue_empty_response(self, text: Any) -> bool:
        return bool(re.fullmatch(
            r"\s*\*[^\n*]+\*\s+is empty at the moment\.\s*",
            str(text or ""),
            re.IGNORECASE,
        ))

    def _roo_authored_thread_messages(
        self,
        thread_history: Optional[List[dict]],
    ) -> list[dict]:
        if not thread_history:
            return []
        try:
            roo_user_id = str(get_bot_user_id() or "").strip()
        except Exception:
            return []
        if not roo_user_id:
            return []
        return [
            message
            for message in thread_history
            if isinstance(message, dict)
            and (message.get("is_bot") or message.get("bot_id"))
            and str(message.get("user") or "").strip() == roo_user_id
        ]

    def _resolve_linear_channel_issue_reference(
        self,
        *,
        text: str,
        params: dict,
        thread_history: Optional[List[dict]],
    ) -> str:
        explicit = str(
            params.get("issue_reference")
            or params.get("issue_identifier")
            or ""
        ).strip()
        identifier = self._linear_issue_identifier(explicit) or self._linear_issue_identifier(text)
        if identifier:
            return identifier

        ordinal_match = re.search(
            r"\b(?:number|item|issue|ticket|task)\s*#?\s*(\d{1,3})\b",
            str(text or ""),
            re.IGNORECASE,
        )
        if not ordinal_match:
            ordinal_match = re.fullmatch(
                r"\s*#?(\d{1,3})\s*",
                str(text or ""),
                re.IGNORECASE,
            )
        if ordinal_match:
            ordinal = int(ordinal_match.group(1))
            numbered_identifier = self._linear_numbered_issue_identifier_from_thread(
                thread_history,
                ordinal=ordinal,
            )
            if numbered_identifier:
                return numbered_identifier

        contextual_detail_request = self._linear_contextual_detail_request(text)
        if re.search(
            r"\b(?:it|its|that|this|the\s+issue|the\s+ticket)\b",
            str(text or ""),
            re.IGNORECASE,
        ) or (
            contextual_detail_request
            and not self._linear_issue_reference_tokens(text)
        ):
            identifiers = self._linear_issue_identifiers_from_thread(
                thread_history,
                prefer_single=True,
            )
            if len(identifiers) == 1:
                return identifiers[0]
        return explicit or str(text or "").strip()

    def _linear_issue_identifier(self, value: Any) -> str:
        match = re.search(
            r"\b[A-Z][A-Z0-9]{1,15}-\d+\b",
            str(value or ""),
            re.IGNORECASE,
        )
        return match.group(0).upper() if match else ""

    def _linear_numbered_issue_identifier_from_thread(
        self,
        thread_history: Optional[List[dict]],
        *,
        ordinal: int,
    ) -> str:
        numbered_issue = re.compile(
            r"^\s*(\d{1,3})\.\s+(?:"
            r"<https://linear\.app/[^|>]+\|([A-Z][A-Z0-9]{1,15}-\d+)>"
            r"|`([A-Z][A-Z0-9]{1,15}-\d+)`)\s+—",
            re.IGNORECASE,
        )
        for message in reversed(
            self._linear_channel_issue_response_messages(thread_history)
        ):
            if self._linear_channel_issue_empty_response(message.get("text")):
                return ""
            numbered_list_found = False
            for line in str(message.get("text") or "").splitlines():
                match = numbered_issue.match(line)
                if not match:
                    continue
                numbered_list_found = True
                if int(match.group(1)) == ordinal:
                    return str(match.group(2) or match.group(3) or "").upper()
            if numbered_list_found:
                return ""
        return ""

    def _linear_issue_identifiers_from_thread(
        self,
        thread_history: Optional[List[dict]],
        *,
        prefer_single: bool = False,
    ) -> list[str]:
        for message in reversed(
            self._linear_channel_issue_response_messages(thread_history)
        ):
            if self._linear_channel_issue_empty_response(message.get("text")):
                return []
            identifiers = []
            for match in re.findall(
                r"\b[A-Z][A-Z0-9]{1,15}-\d+\b",
                str(message.get("text") or ""),
                re.IGNORECASE,
            ):
                normalized = match.upper()
                if normalized not in identifiers:
                    identifiers.append(normalized)
            if prefer_single and identifiers:
                first_line = str(message.get("text") or "").splitlines()[0]
                heading_identifier = self._linear_issue_identifier(first_line)
                if heading_identifier:
                    return [heading_identifier]
                # A newer multi-issue list is an ambiguity boundary. Do not
                # skip past it and silently reuse an older detail response.
                if len(identifiers) > 1:
                    return []
            if identifiers and (not prefer_single or len(identifiers) == 1):
                return identifiers
        return []

    def _match_linear_channel_issue(
        self,
        reference: str,
        issues: list[dict],
    ) -> dict[str, Any]:
        reference_tokens = self._linear_issue_reference_tokens(reference)
        if not reference_tokens:
            return {}
        matches = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            title_tokens = self._linear_issue_reference_tokens(issue.get("title"))
            if reference_tokens.issubset(title_tokens):
                matches.append(issue)
        if len(matches) == 1:
            return {"identifier": matches[0].get("identifier"), "matches": matches}
        if len(matches) > 1:
            return {"ambiguous": True, "matches": matches[:8]}
        return {}

    def _linear_issue_reference_tokens(self, value: Any) -> set[str]:
        stopwords = {
            "about", "all", "comments", "description", "detail", "details",
            "give", "info", "information", "issue", "linear", "me", "more",
            "on", "one", "please", "show", "tell", "that", "the", "this",
            "ticket", "todo", "what", "whats", "who", "with", "owns",
            "assigned", "is", "status", "state", "assignee", "assignees",
            "owner", "owners", "ownership", "project", "projects", "cycle",
            "cycles", "priority", "priorities", "estimate", "estimates",
            "due", "date", "deadline", "deadlines", "created", "by",
            "updated", "label", "labels", "attachment", "attachments",
            "relation", "relations", "metadata",
        }
        cleaned = re.sub(r"\[tech_team\]", " ", str(value or ""), flags=re.IGNORECASE)
        return {
            token
            for token in re.findall(r"[a-z0-9]+", cleaned.lower())
            if len(token) > 1 and token not in stopwords
        }

    def _format_linear_issue_choices(self, matches: list[dict]) -> str:
        lines = ["I found a few matching MLAI_TECH issues. Which one do you mean?"]
        for issue in matches:
            identifier = self._slack_escape(issue.get("identifier") or "Issue")
            title = self._clean_linear_issue_title(issue.get("title"))
            lines.append(f"• `{identifier}` — {title}")
        return "\n".join(lines)

    def _format_linear_channel_issue_list(self, result: dict) -> str:
        metadata = result.get("list") if isinstance(result.get("list"), dict) else {}
        display_name = self._slack_escape(metadata.get("displayName") or "Linear issues")
        issues = [item for item in (result.get("issues") or []) if isinstance(item, dict)]
        if not issues:
            return f"*{display_name}* is empty at the moment."
        lines = [f"*{len(issues)} issues in {display_name}*"]
        for index, issue in enumerate(issues, start=1):
            identifier = self._slack_escape(issue.get("identifier") or "Issue")
            title = self._clean_linear_issue_title(issue.get("title"))
            url = str(issue.get("url") or "").strip()
            label = f"<{url}|{identifier}>" if url.startswith("https://linear.app/") else f"`{identifier}`"
            lines.append(f"{index}. {label} — {title}")
        if (result.get("pageInfo") or {}).get("hasNextPage"):
            lines.append("More issues are available in Linear.")
        lines.append(
            "Reply in this thread and mention Roo with an issue key, list number, "
            "or part of a title for full details."
        )
        return "\n".join(lines)

    def _format_linear_channel_issue_detail(self, result: dict) -> str:
        issue = result.get("issue") if isinstance(result.get("issue"), dict) else {}
        identifier = self._slack_escape(issue.get("identifier") or "Linear issue")
        title = self._clean_linear_issue_title(issue.get("title"))
        url = str(issue.get("url") or "").strip()
        heading = f"<{url}|{identifier}>" if url.startswith("https://linear.app/") else f"`{identifier}`"
        lines = [f"*{heading} — {title}*"]

        metadata = []
        state = issue.get("state") if isinstance(issue.get("state"), dict) else {}
        if state.get("name"):
            metadata.append(f"*Status:* {self._slack_escape(state['name'])}")
        assignee = issue.get("assignee") if isinstance(issue.get("assignee"), dict) else {}
        assignee_name = assignee.get("displayName") or assignee.get("name")
        metadata.append(f"*Assignee:* {self._slack_escape(assignee_name or 'Unassigned')}")
        project = issue.get("project") if isinstance(issue.get("project"), dict) else {}
        if project.get("name"):
            metadata.append(f"*Project:* {self._slack_escape(project['name'])}")
        cycle = issue.get("cycle") if isinstance(issue.get("cycle"), dict) else {}
        if cycle.get("name"):
            metadata.append(f"*Cycle:* {self._slack_escape(cycle['name'])}")
        priority = issue.get("priorityLabel")
        if priority:
            metadata.append(f"*Priority:* {self._slack_escape(priority)}")
        if issue.get("estimate") is not None:
            metadata.append(f"*Estimate:* {self._slack_escape(issue['estimate'])}")
        if issue.get("dueDate"):
            metadata.append(f"*Due:* {self._slack_escape(issue['dueDate'])}")
        if metadata:
            lines.append(" · ".join(metadata))

        provenance = []
        creator = issue.get("creator") if isinstance(issue.get("creator"), dict) else {}
        creator_name = creator.get("displayName") or creator.get("name")
        if creator_name:
            provenance.append(f"*Created by:* {self._slack_escape(creator_name)}")
        if issue.get("createdAt"):
            provenance.append(f"*Created:* {self._slack_escape(issue['createdAt'])}")
        if issue.get("updatedAt"):
            provenance.append(f"*Updated:* {self._slack_escape(issue['updatedAt'])}")
        if provenance:
            lines.append(" · ".join(provenance))

        labels = [item for item in (issue.get("labels") or []) if isinstance(item, dict)]
        if labels:
            label_names = ", ".join(
                self._slack_escape(label.get("name") or "")
                for label in labels
                if label.get("name")
            )
            if label_names:
                lines.append(f"*Labels:* {label_names}")

        description = str(issue.get("description") or "").strip()
        lines.extend(["", "*Description*", self._slack_escape(description) if description else "No description."])

        attachments = [item for item in (issue.get("attachments") or []) if isinstance(item, dict)]
        if attachments:
            lines.extend(["", f"*Attachments — {len(attachments)}*"])
            for attachment in attachments:
                attachment_title = self._slack_escape(attachment.get("title") or "Attachment")
                attachment_url = str(attachment.get("url") or "").strip()
                lines.append(
                    f"• <{attachment_url}|{attachment_title}>"
                    if attachment_url.startswith(("https://", "http://"))
                    else f"• {attachment_title}"
                )
            if issue.get("attachmentsTruncated"):
                lines.append("Additional attachments are available in Linear.")

        relations = issue.get("relations") if isinstance(issue.get("relations"), dict) else {}
        relation_edges = [
            item for item in (relations.get("edges") or []) if isinstance(item, dict)
        ]
        if relation_edges:
            lines.extend(["", f"*Relations — {len(relation_edges)}*"])
            for relation in relation_edges:
                related = relation.get("issue") if isinstance(relation.get("issue"), dict) else {}
                relation_type = self._slack_escape(relation.get("type") or "related")
                related_id = self._slack_escape(related.get("identifier") or "Issue")
                related_title = self._clean_linear_issue_title(related.get("title"))
                lines.append(f"• {relation_type}: `{related_id}` — {related_title}")
            if relations.get("truncated"):
                lines.append("Additional relations are available in Linear.")

        comments = [item for item in (result.get("comments") or []) if isinstance(item, dict)]
        lines.extend(["", f"*Comments — {len(comments)}*"])
        if not comments:
            lines.append("No comments yet.")
        for comment in comments:
            author = comment.get("user") if isinstance(comment.get("user"), dict) else {}
            behalf = comment.get("onBehalfOf") if isinstance(comment.get("onBehalfOf"), dict) else {}
            author_name = (
                behalf.get("displayName") or behalf.get("name")
                or author.get("displayName") or author.get("name") or "Unknown author"
            )
            created_at = str(comment.get("createdAt") or "").strip()
            quoted = str(comment.get("quotedText") or "").strip()
            body = str(comment.get("body") or "").strip()
            comment_lines = [f"• *{self._slack_escape(author_name)}*{f' · {created_at}' if created_at else ''}"]
            if quoted:
                comment_lines.append(f"> {self._slack_escape(quoted)}")
            comment_lines.append(self._slack_escape(body) if body else "(empty comment)")
            lines.extend(comment_lines)
        if result.get("commentsTruncated"):
            lines.append("Additional comments are available in Linear.")

        rendered = "\n".join(lines)
        if len(rendered) > 35000:
            return rendered[:34750].rstrip() + "\n\n_Response truncated; open the Linear issue for the remainder._"
        return rendered

    def _clean_linear_issue_title(self, value: Any) -> str:
        title = re.sub(r"^\s*\[TECH_TEAM\]\s*", "", str(value or ""), flags=re.IGNORECASE)
        return self._slack_escape(title.strip() or "Untitled issue")

    def _slack_escape(self, value: Any) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _data_query_catalog_requested(self, text: str, params: dict) -> bool:
        text_lower = str(text or "").lower()
        raw_catalog_request = bool(
            re.search(r'\b(?:data|database|db)\s+(?:catalog|resources?|tables?|schema)\b', text_lower)
            or re.search(r'\b(?:what|which|show|list)\b.*\b(?:tables?|resources?)\b.*\b(?:query|available|access)\b', text_lower)
        )
        if raw_catalog_request:
            return True

        action = str(params.get("action") or "").lower().strip()
        if action not in {"catalog", "list_resources", "schema"}:
            return False

        # The generic LLM extractor can mistake "how many X do we have?" for a
        # catalog request. If the raw text points at a known resource, query it.
        return not bool(self._infer_data_query_resource(text_lower, params))

    def _build_data_query_payload(self, text: str, params: dict, user_id: str) -> dict:
        text_lower = str(text or "").lower()
        operation = self._infer_data_query_operation(text_lower, params)
        resource = self._infer_data_query_resource(text_lower, params)
        payload: dict[str, Any] = {
            "requester_slack_id": user_id,
            "resource": resource,
            "operation": operation,
            "offset": self._coerce_data_query_offset(params.get("offset")),
        }

        filters = self._extract_data_query_filters(params)
        filters.extend(self._infer_data_query_filters(text_lower, resource))
        if filters:
            payload["filters"] = filters

        if operation == "aggregate":
            group_by = self._extract_string_list(params.get("group_by"))
            if group_by:
                payload["group_by"] = group_by
            limit = self._coerce_data_query_limit(params.get("limit"), default=20)
            payload["limit"] = limit
            order_by = self._extract_data_query_order_by(params.get("order_by"))
            if order_by:
                payload["order_by"] = order_by
            return payload

        if operation == "count":
            return payload

        fields = self._extract_string_list(params.get("fields")) or self._default_data_query_fields(resource)
        if fields:
            payload["fields"] = fields
        limit = self._coerce_data_query_limit(params.get("limit"), default=20)
        payload["limit"] = limit
        order_by = self._extract_data_query_order_by(params.get("order_by"))
        if order_by:
            payload["order_by"] = order_by
        return payload

    def _infer_data_query_operation(self, text_lower: str, params: dict) -> str:
        if re.search(r'\b(?:how\s+many|count|number\s+of|total\s+number)\b', text_lower):
            return "count"
        if re.search(r'\b(?:group\s+by|break\s+down|breakdown|by\s+status|by\s+state|by\s+month)\b', text_lower):
            return "aggregate"
        if re.search(r'\b(?:show|list|which|what|query|find|search|give\s+me|display|report)\b', text_lower):
            return "list"

        operation = str(params.get("operation") or "").lower().strip()
        if operation in {"list", "count", "aggregate"}:
            return operation
        return "list"

    def _infer_data_query_resource(self, text_lower: str, params: dict) -> str:
        resource_patterns = (
            ("vibe_raising_companies", (r'\bvibe\s*raising\b.*\bcompan', r'\bcompan(?:y|ies)\b.*\bvibe\s*raising\b')),
            ("vibe_raising_profiles", (r'\bvibe\s*raising\b.*\bprofiles?\b', r'\bfounder\s+profiles?\b')),
            ("monthly_update_drafts", (r'\bmonthly\s+update\s+drafts?\b', r'\bupdate\s+drafts?\b', r'\bdrafts?\s+for\s+my\s+company\b')),
            ("startup_metrics", (r'\bstartup\s+metrics?\b', r'\bmetrics?\b.*\bstartup\b')),
            ("startup_events", (r'\bstartup\s+events?\b', r'\btimeline\s+events?\b')),
            ("startup_profiles", (r'\bstartup\s+profiles?\b', r'\bcompany\s+profile\b')),
            ("startup_bindings", (r'\bstartup\s+bindings?\b', r'\buser\s+startup\s+bindings?\b')),
            ("content_factory_run_step_attempts", (r'\bcontent\s+factory\b.*\bstep\s+attempts?\b',)),
            ("content_factory_run_steps", (r'\bcontent\s+factory\b.*\brun\s+steps?\b', r'\bcontent\s+factory\b.*\bsteps?\b')),
            ("content_factory_runs", (r'\bcontent\s+factory\b.*\bruns?\b',)),
            ("content_factory_jobs", (r'\bcontent\s+factory\b.*\bjobs?\b', r'\barticle\s+jobs?\b')),
            ("written_articles", (r'\bwritten\s+articles?\b', r'\bpublished\s+articles?\b')),
            ("researched_keywords", (r'\bresearched\s+keywords?\b', r'\bseo\s+keywords?\b')),
            ("linear_project_updates", (r'\blinear\b.*\bproject\s+updates?\b',)),
            ("linear_projects", (r'\blinear\b.*\bprojects?\b',)),
            ("linear_issues", (r'\blinear\b.*\b(?:issues?|tickets?|tasks?)\b',)),
            ("gmail_attachments", (r'\bgmail\b.*\battachments?\b',)),
            ("gmail_threads", (r'\bgmail\b.*\bthreads?\b',)),
            ("gmail_messages", (r'\bgmail\b.*\bmessages?\b', r'\bemails?\b')),
            ("slack_channel_selections", (r'\bslack\b.*\bchannel\s+selections?\b',)),
            ("slack_threads", (r'\bslack\b.*\bthreads?\b',)),
            ("slack_messages", (r'\bslack\b.*\bmessages?\b',)),
            ("github_integrations", (r'\bgithub\s+integrations?\b', r'\bconnected\s+github\b')),
            ("financial_accounts", (r'\bfinancial\s+accounts?\b', r'\bbank\s+accounts?\b')),
            ("financial_records", (r'\bfinancial\s+records?\b', r'\btransactions?\b')),
            ("organizations", (r'\borganizations?\b', r'\bcompanies?\b')),
            ("coworking_bookings", (r'\bcoworking\b.*\bbookings?\b',)),
        )
        for resource, patterns in resource_patterns:
            if any(re.search(pattern, text_lower) for pattern in patterns):
                return resource

        explicit = str(params.get("resource") or params.get("table") or "").strip().lower()
        if explicit:
            return re.sub(r'[^a-z0-9_]+', '_', explicit).strip("_")
        return ""

    def _infer_data_query_filters(self, text_lower: str, resource: str) -> list[dict]:
        filters: list[dict] = []
        if resource in {"content_factory_jobs", "content_factory_runs", "content_factory_run_steps", "content_factory_run_step_attempts"}:
            if re.search(r'\b(?:failed|failure|errored|errors?)\b', text_lower):
                value = "error" if resource == "content_factory_jobs" else "failed"
                field = "status"
                operator = "eq"
                filters.append({"field": field, "operator": operator, "value": value})
            elif re.search(r'\b(?:queued|running|completed|cancelled|canceled)\b', text_lower):
                status_match = re.search(r'\b(queued|running|completed|cancelled|canceled)\b', text_lower)
                if status_match:
                    value = "cancelled" if status_match.group(1) == "canceled" else status_match.group(1)
                    filters.append({"field": "status", "operator": "eq", "value": value})
        if resource == "monthly_update_drafts":
            status_match = re.search(r'\b(draft|generated|sent|approved|failed|error)\b', text_lower)
            if status_match:
                value = "error" if status_match.group(1) in {"failed", "error"} else status_match.group(1)
                filters.append({"field": "status", "operator": "eq", "value": value})
        if resource == "vibe_raising_companies" and re.search(r'\bregistered\b', text_lower):
            filters.append({"field": "registered", "operator": "eq", "value": True})
        return filters

    def _default_data_query_fields(self, resource: str) -> list[str]:
        defaults = {
            "vibe_raising_companies": ["name", "domain", "organization_domain", "registered", "created_at"],
            "vibe_raising_profiles": ["user_email", "user_slack_id", "role", "organization_name", "updated_at"],
            "monthly_update_drafts": ["organization_id", "month", "status", "title", "groundedness_status", "updated_at"],
            "startup_profiles": ["organization_id", "stage", "organization_kind", "short_description", "updated_at"],
            "startup_bindings": ["user_email", "organization_domain", "role", "is_default_for_gmail", "updated_at"],
            "startup_metrics": ["metric_name", "value_text", "value_number", "unit", "period_month", "confidence"],
            "startup_events": ["event_type", "title", "event_date", "sentiment", "investor_importance", "confidence"],
            "content_factory_jobs": ["job_id", "domain", "status", "selected_keyword", "article_url", "pr_url", "error_message", "created_at"],
            "content_factory_runs": ["run_id", "workflow", "domain", "status", "current_step", "approval_state", "error", "updated_at"],
            "content_factory_run_steps": ["run_key", "domain", "step_key", "status", "attempts", "message", "error"],
            "content_factory_run_step_attempts": ["run_key", "domain", "attempt", "status", "message", "error", "created_at"],
            "written_articles": ["title", "slug", "category", "article_url", "primary_keyword", "published_at"],
            "researched_keywords": ["keyword", "volume", "difficulty", "intent", "tier", "opportunity_index", "status"],
            "linear_issues": ["identifier", "title", "state_name", "priority_label", "assignee_name", "team_key", "url", "updated_at_linear"],
            "linear_projects": ["name", "status_name", "health", "progress", "priority", "lead_name", "target_date", "url"],
            "linear_project_updates": ["health", "author_name", "url", "created_at_linear", "updated_at_linear"],
            "gmail_messages": ["internal_date", "subject", "from_address", "snippet", "relevance_label", "relevance_score"],
            "gmail_threads": ["gmail_thread_id", "source_message_count", "hydration_status", "extraction_status", "latest_message_internal_date"],
            "gmail_attachments": ["filename", "mime_type", "size_bytes", "extraction_status", "created_at"],
            "slack_messages": ["channel_name", "author_name", "posted_at", "thread_ts", "created_at"],
            "slack_threads": ["channel_name", "thread_ts", "source_message_count", "latest_message_at", "relevance_label"],
            "slack_channel_selections": ["channel_name", "selected", "last_synced_at", "updated_at"],
            "github_integrations": ["slack_user_id", "github_user_name", "github_repo", "project_scanned", "last_scanned_at", "updated_at"],
            "financial_accounts": ["provider", "account_label", "institution_name", "account_type", "status", "currency", "balance", "last_synced_at"],
            "financial_records": ["provider", "record_type", "amount", "direction", "status", "transaction_date", "description", "merchant_name"],
            "organizations": ["id", "name", "domain", "created_at"],
            "coworking_bookings": ["user_email", "user_slack_id", "date", "status", "points_cost"],
        }
        return list(defaults.get(resource, []))

    def _extract_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
        return result

    def _extract_data_query_filters(self, value: Any) -> list[dict]:
        if not isinstance(value, list):
            return []
        filters = []
        for item in value:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            operator = str(item.get("operator") or "").strip()
            if not field or not operator:
                continue
            filters.append({"field": field, "operator": operator, "value": item.get("value")})
        return filters

    def _extract_data_query_order_by(self, value: Any) -> list[dict]:
        if not isinstance(value, list):
            return []
        order_by = []
        for item in value:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            direction = str(item.get("direction") or "asc").strip().lower()
            if field and direction in {"asc", "desc"}:
                order_by.append({"field": field, "direction": direction})
        return order_by

    def _coerce_data_query_limit(self, value: Any, *, default: int) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(limit, 100))

    def _coerce_data_query_offset(self, value: Any) -> int:
        try:
            offset = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, offset)

    def _format_data_catalog(self, catalog: dict) -> str:
        resources = catalog.get("resources") or []
        if not resources:
            return "No data resources are currently registered."

        lines = ["Available data resources:"]
        for resource in resources[:30]:
            key = resource.get("key") or "unknown"
            operations = ", ".join(resource.get("operations") or [])
            fields = ", ".join((resource.get("fields") or [])[:8])
            lines.append(f"- `{key}` ({operations}): {fields}")
        if len(resources) > 30:
            lines.append(f"...and {len(resources) - 30} more.")
        return "\n".join(lines)

    def _format_data_query_result(self, payload: dict, result: dict) -> str:
        if result.get("message"):
            return str(result["message"])

        resource = result.get("resource") or payload.get("resource") or "resource"
        rows = result.get("rows") or []
        operation = payload.get("operation") or "list"

        if operation == "count":
            count_value = rows[0].get("count") if rows else 0
            return f"`{resource}` count: {count_value}"

        if not rows:
            return f"No matching records found for `{resource}`."

        display_rows = rows[:10]
        fields = list(display_rows[0].keys())
        table_rows = [[self._stringify_data_cell(row.get(field)) for field in fields] for row in display_rows]
        message = [
            f"`{resource}` results",
            self._data_query_table(fields, table_rows),
        ]
        returned_count = result.get("returned_count", len(rows))
        limit = result.get("limit", payload.get("limit"))
        offset = result.get("offset", payload.get("offset", 0))
        if result.get("has_more"):
            message.append(f"Showing {returned_count} rows from offset {offset}. More rows are available with a higher offset.")
        else:
            message.append(f"Showing {returned_count} row{'s' if returned_count != 1 else ''}.")
        if len(rows) > len(display_rows):
            message.append(f"Displayed first {len(display_rows)} of {len(rows)} returned rows.")
        if limit:
            message.append(f"Limit: {limit}.")
        return "\n".join(message)

    def _stringify_data_cell(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, default=str)
        else:
            rendered = str(value)
        rendered = rendered.replace("\n", " ").strip()
        if len(rendered) > 80:
            return rendered[:77] + "..."
        return rendered

    def _data_query_table(self, headers: list[str], rows: list[list[str]]) -> str:
        widths = [min(max(len(header), 3), 28) for header in headers]
        for row in rows:
            for index, cell in enumerate(row):
                widths[index] = min(max(widths[index], len(cell)), 28)

        def fit(value: str, width: int) -> str:
            if len(value) > width:
                return value[: max(0, width - 3)] + "..."
            return value.ljust(width)

        lines = [" ".join(fit(header, widths[index]) for index, header in enumerate(headers))]
        for row in rows:
            lines.append(" ".join(fit(cell, widths[index]) for index, cell in enumerate(row)))
        return "```\n" + "\n".join(lines) + "\n```"
    
    async def _execute_mlai_points(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        request_id: Optional[str] = None,
    ) -> Any:
        """Execute the MLAI Points skill."""
        import httpx

        # Get client from skill's implementation module
        # ClientClass = skill.get_client_class("MLAIBackendClient")
        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )
        ClientClass = MLAIBackendClient
        
        if ClientClass is None:
            return "Sorry mate, the Points skill isn't properly configured. Missing implementation."
        
        action = None
        try:
            settings = get_settings()
            if not settings.MLAI_BACKEND_URL:
                return "Sorry mate, the Points API isn't configured. Ask the team to set MLAI_BACKEND_URL."
            
            client = ClientClass(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
                internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY
            )

            action = self._resolve_routed_points_action(params, text)

            if not action:
                return (
                    "Not sure which points action you're after, mate — try "
                    "`balance`, `tasks`, `coworking book today`, `rewards`, or "
                    "`coworking report`. 🦘"
                )

            # Execute the appropriate action
            handle_kwargs = {
                "client": client,
                "action": action,
                "params": params,
                "text": text,
                "user_id": user_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "skill": skill,
            }
            if request_id:
                handle_kwargs["request_id"] = request_id
            return await self._handle_points_action(
                **handle_kwargs
            )
            
        except MLAIBackendUnavailableError:
            return self._points_backend_unavailable_message(action)
        except PermissionError:
            return "Sorry mate, you're not authorized to do that. Only Points Admins can perform that action. 🔒"
        except ValueError as e:
            return str(e)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                return "Sorry mate, you're not authorized to do that. Only Points Admins can perform that action. 🔒"
            elif e.response.status_code == 409:
                error_detail = self._redact_points_balance_error(
                    self._extract_http_error_detail(e)
                )
                return error_detail or (
                    "That task changed while you were editing it. Refresh it and try again."
                )
            elif e.response.status_code == 404:
                if action == "request_points":
                    error_detail = self._extract_http_error_detail(e)
                    print(
                        "❌ Points request queue failed after executor handoff: "
                        f"status=404 error={error_detail or str(e)}"
                    )
                    return self._points_request_queue_error_message()
                return "Hmm, couldn't find that. Double-check the ID or date and try again? 🤔"
            elif e.response.status_code == 400:
                # Handle bad requests (e.g. insufficient funds)
                try:
                    error_detail = self._extract_http_error_detail(e)
                    safe_detail = self._redact_points_balance_error(error_detail)

                    if self._is_points_balance_error(error_detail):
                        try:
                            balance_data = await client.get_balance(user_id)
                            current_balance = balance_data.get("balance", 0)
                            return self._deliver_personal_points_message(
                                recipient_user_id=user_id,
                                requester_user_id=user_id,
                                channel_id=channel_id,
                                thread_ts=thread_ts,
                                private_message=(
                                    f"🛑 Computer says no: {safe_detail}\n\n"
                                    f"Your current balance is **{current_balance} points**."
                                ),
                                public_message=f"🛑 Computer says no: {safe_detail}",
                                action=action or "points_error",
                            )
                        except Exception:
                            return f"🛑 Computer says no: {safe_detail}"

                    return f"🛑 {safe_detail}"
                except Exception:
                    return f"Ran into a snag with that request (400 Bad Request)."
            elif e.response.status_code >= 500:
                return self._points_backend_unavailable_message(action)
            else:
                error_detail = self._redact_points_balance_error(
                    self._extract_http_error_detail(e)
                )
                return f"Ran into a snag: {error_detail or 'The points request was rejected.'}"
        except Exception as e:
            print(f"Points skill error: exc_type={e.__class__.__name__}")
            return "Had some trouble with the points system. Try again shortly."


    def _resolve_topup_pack_id(self, text: str, params: dict) -> tuple[Optional[str], Optional[int]]:
        """Resolve a top-up pack ID; return unsupported amount when a non-pack amount is present."""
        pack_candidates = [
            params.get("pack_id"),
            params.get("pack"),
            params.get("topup_pack"),
        ]
        for candidate in pack_candidates:
            value = str(candidate or "").strip().lower()
            if not value:
                continue
            value = value.replace("-", "_")
            if value in ROO_TOPUP_PACKS:
                return value, None
            match = re.search(r"\b(\d+)\b", value)
            if match:
                amount = int(match.group(1))
                return ROO_TOPUP_PACK_BY_POINTS.get(amount), amount

        point_candidates = [
            params.get("points_amount"),
            params.get("points"),
            params.get("amount"),
        ]
        for candidate in point_candidates:
            if candidate in (None, ""):
                continue
            try:
                amount = int(candidate)
            except (TypeError, ValueError):
                continue
            return ROO_TOPUP_PACK_BY_POINTS.get(amount), amount

        text_lower = self._normalize_points_routing_text(text)
        exact_pack = re.search(r"\btopup[_\s-]*(10|20|50)\b", text_lower)
        if exact_pack:
            amount = int(exact_pack.group(1))
            return ROO_TOPUP_PACK_BY_POINTS[amount], None

        amount_match = re.search(r"\b(\d+)\b", text_lower)
        if amount_match:
            amount = int(amount_match.group(1))
            return ROO_TOPUP_PACK_BY_POINTS.get(amount), amount

        return None, None

    def _topup_pack_list_message(self) -> str:
        lines = ["Available Top-up Roo Points packs:"]
        for pack_id in ("topup_5", "topup_10", "topup_25"):
            pack = ROO_TOPUP_PACKS[pack_id]
            lines.append(f"- {pack['label']} - {pack['price']}")
        lines.append("")
        lines.append(
            "Top-up Roo Points are optional and do not count toward lifetime earned contribution."
        )
        return "\n".join(lines)

    def _trusted_topup_checkout_options(
        self,
        raw_options: Any,
        *,
        settings: Any,
    ) -> list[dict[str, Any]]:
        """Return sanitized Stripe Checkout options safe to embed in Slack."""
        allowed_hosts = set(
            getattr(settings, "roo_points_stripe_checkout_hosts", set()) or set()
        )
        trusted = []
        for raw_option in raw_options if isinstance(raw_options, list) else []:
            if not isinstance(raw_option, dict):
                continue
            checkout_url = str(raw_option.get("checkout_session_url") or "").strip()
            parsed = urlsplit(checkout_url)
            try:
                checkout_port = parsed.port
            except ValueError:
                checkout_port = -1
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.hostname.lower() not in allowed_hosts
                or parsed.username
                or parsed.password
                or checkout_port not in {None, 443}
            ):
                print(
                    "ROO_TOPUP_UNTRUSTED_CHECKOUT_URL "
                    f"pack_id={raw_option.get('pack_id')}"
                )
                continue
            try:
                points = int(raw_option.get("points_amount"))
                amount_cents = int(raw_option.get("amount_cents"))
            except (TypeError, ValueError):
                continue
            if points <= 0 or amount_cents <= 0:
                continue
            currency = str(raw_option.get("currency") or "aud").strip().lower()
            pack_id = str(raw_option.get("pack_id") or "").strip()
            if not pack_id:
                continue
            trusted.append(
                {
                    "pack_id": pack_id,
                    "points": points,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "checkout_url": checkout_url,
                    "expires_at": str(raw_option.get("expires_at") or "").strip(),
                }
            )
        return trusted

    @staticmethod
    def _topup_price_label(amount_cents: int, currency: str) -> str:
        amount = amount_cents / 100
        if currency.lower() == "aud":
            return f"A${amount:.2f}"
        return f"{currency.upper()} {amount:.2f}"

    def _topup_checkout_button_response(
        self,
        *,
        options: list[dict[str, Any]],
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        had_partial_errors: bool,
    ) -> dict:
        singular = len(options) == 1
        message = (
            "Your Top-up Roo Points checkout is ready. Use the private Stripe "
            "Checkout button below."
            if singular
            else "Choose a Top-up Roo Points pack using one of the private Stripe Checkout buttons."
        )
        if had_partial_errors:
            message += " Some checkout options are temporarily unavailable."
        message += (
            "\n\nTop-up Roo Points are optional MLAI community reward points. "
            "They are not money, have no cash value, cannot be converted to cash, "
            "and do not count toward lifetime earned contribution."
        )

        buttons = []
        for option in options:
            price = self._topup_price_label(
                option["amount_cents"],
                option["currency"],
            )
            button = {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": f"{option['points']} points · {price}",
                    "emoji": True,
                },
                "url": option["checkout_url"],
                "action_id": f"roo_topup_checkout_{option['pack_id']}",
            }
            if option["pack_id"] == "topup_10":
                button["style"] = "primary"
            buttons.append(button)

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            },
            {"type": "actions", "elements": buttons},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Stripe will ask you to accept the Roo Points terms "
                            "before payment. Checkout links expire within 24 hours."
                        ),
                    }
                ],
            },
        ]
        response_data = {
            "action": "topup_points",
            "delivery": "direct_message",
            "pack_ids": [option["pack_id"] for option in options],
            "partial": had_partial_errors,
        }
        if channel_id and not channel_id.startswith("D"):
            dm_response = send_dm(
                user_id,
                message,
                blocks=blocks,
            )
            if not dm_response or not dm_response.get("ok"):
                return {
                    "message": (
                        f"⚠️ I couldn’t open a private Slack DM for <@{user_id}>. "
                        "Please DM me `topup` and I’ll create fresh checkout buttons there."
                    ),
                    "data": {
                        **response_data,
                        "delivery_failed": True,
                    },
                    "suppress_post": False,
                }
            return {
                "message": (
                    f"🔒 I’ve sent <@{user_id}> a direct message with private "
                    "Stripe Checkout buttons. Check your DMs."
                ),
                "data": response_data,
                "suppress_post": False,
            }
        return {
            "message": message,
            "blocks": blocks,
            "data": response_data,
        }

    def _normalize_points_routing_text(self, text: str) -> str:
        """Normalize common Slack typo variants before deterministic routing."""
        text_lower = str(text or "").lower()
        replacements = {
            "coworkign": "coworking",
            "cowokrking": "coworking",
            "cowokring": "coworking",
            "co working": "coworking",
            "co-working": "coworking",
            "peopel": "people",
        }
        for typo, replacement in replacements.items():
            text_lower = text_lower.replace(typo, replacement)
        return text_lower


    def _coworking_target_mentions_present(self, text: str, params: dict) -> bool:
        """Return True when a coworking booking request contains a non-Roo target mention."""
        if re.search(r"<@[A-Z0-9]+>", str(text or ""), re.IGNORECASE):
            return True
        for raw_target in list(params.get("target_users", []) or []) + [
            params.get("target_user"),
            params.get("target_slack_id"),
        ]:
            if raw_target not in (None, ""):
                return True
        return False

    def _extract_task_identifier(self, text: str, explicit_task_id=None) -> Optional[str]:
        """Extract either a numeric task id or a ROO task code from text."""
        import re

        if explicit_task_id:
            identifier = str(explicit_task_id).strip()
            return identifier.upper() if identifier.lower().startswith("roo-") else identifier

        code_match = re.search(r'\b(ROO-\d+)\b', text, re.IGNORECASE)
        if code_match:
            return code_match.group(1).upper()

        id_match = re.search(r'(?:task|#)\s*(\d+)', text, re.IGNORECASE)
        if id_match:
            return id_match.group(1)

        bare_id_match = re.search(r'\b(\d+)\b', text)
        if bare_id_match:
            return bare_id_match.group(1)

        return None

    def _match_task_list_mode(self, text: str, params: dict) -> Optional[str]:
        """Match a task list request to a specific queue, if one is clearly requested."""
        explicit_mode = str(params.get("list_mode", "") or "").strip().lower()
        if explicit_mode in {"all", "mine", "review", "open"}:
            return explicit_mode
        if explicit_mode == "available":
            return "open"

        text_lower = " ".join(text.lower().split())
        if "tasks all" in text_lower or "all tasks" in text_lower:
            return "all"
        if any(
            phrase in text_lower
            for phrase in [
                "tasks mine",
                "my tasks",
                "my work",
                "show me my tasks",
                "what am i working on",
            ]
        ):
            return "mine"
        if any(
            phrase in text_lower
            for phrase in [
                "tasks review",
                "review tasks",
                "what needs my review",
                "what is waiting for my review",
                "tasks waiting for my review",
            ]
        ):
            return "review"
        if any(
            phrase in text_lower
            for phrase in [
                "tasks open",
                "open tasks",
                "what tasks are open",
                "what tasks are available",
                "what can i claim",
                "claimable tasks",
                "show me tasks",
                "show me the tasks",
                "give me the tasks",
                "list tasks",
            ]
        ):
            return "open"
        if text_lower == "tasks":
            return "open"
        return None


    def _resolve_task_list_mode(self, text: str, params: dict) -> str:
        """Resolve which task list variant the user asked for."""
        return self._match_task_list_mode(text, params) or "open"

    def _coerce_optional_bool(self, value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized in {"true", "yes", "on", "1", "ready", "publish", "published"}:
            return True
        if normalized in {"false", "no", "off", "0", "not ready", "draft", "unpublish"}:
            return False
        return None

    def _extract_task_edit_updates(self, params: dict, text: str = "") -> dict:
        updates: dict[str, Any] = {}
        text_lower = text.lower()
        scalar_fields = [
            "title",
            "description",
            "portfolio",
            "work_domain",
            "review_flow",
            "reviewer_slack_id",
            "fallback_reviewer_slack_id",
            "repo",
            "difficulty",
            "due_date",
            "acceptance_criteria",
            "how_to_test",
            "definition_of_done",
            "blocked_reason",
        ]
        for field in scalar_fields:
            value = params.get(field)
            if value not in (None, ""):
                updates[field] = value

        if params.get("task_title") and "title" not in updates:
            updates["title"] = params["task_title"]

        points = params.get("points")
        if points not in (None, ""):
            updates["points"] = int(points)

        estimate_minutes = params.get("estimate_minutes")
        if estimate_minutes not in (None, ""):
            updates["estimate_minutes"] = int(estimate_minutes)

        volunteer_ready = self._coerce_optional_bool(params.get("volunteer_ready"))
        if volunteer_ready is not None:
            updates["volunteer_ready"] = volunteer_ready
        elif "volunteer ready" in text_lower:
            updates["volunteer_ready"] = not any(
                phrase in text_lower
                for phrase in ["not volunteer ready", "no longer volunteer ready", "unpublish", "mark draft"]
            )

        target_user = params.get("target_user") or params.get("target_slack_id")
        if target_user not in (None, ""):
            if "fallback reviewer" in text_lower and "fallback_reviewer_slack_id" not in updates:
                updates["fallback_reviewer_slack_id"] = target_user
            elif "reviewer" in text_lower and "reviewer_slack_id" not in updates:
                updates["reviewer_slack_id"] = target_user

        return updates

    def _resolve_routed_points_action(self, params: dict, text: str) -> str:
        """Normalize the router-supplied points action; safety guards only.

        Phase 4 of the routing redesign: the action arrives validated from the
        router's enum (or a button payload) — no text-sniffing re-derivation.
        """
        action = str(params.get("action") or "").strip().lower()
        # Historical alias from older button payloads
        if action == "book":
            action = "book_coworking"
        # Safety guard (not text-sniffing): a booking that names ANOTHER
        # member is an admin check-in, whatever the router said.
        if action == "book_coworking" and self._coworking_target_mentions_present(text, params):
            action = "admin_checkin_coworking"
        return action

    def _resolve_points_admin_management_action(
        self,
        text: str,
        *,
        explicit_action: str = "",
    ) -> str:
        """Resolve privileged points-admin management intent from text or extracted action."""
        if explicit_action in [
            "promote_points_admin",
            "revoke_points_admin",
            "set_points_admin_allowance",
        ]:
            return explicit_action

        if self._is_points_admin_promotion_command(text):
            return "promote_points_admin"

        if self._is_points_admin_revocation_command(text):
            return "revoke_points_admin"

        if self._is_points_allowance_change_command(text):
            return "set_points_admin_allowance"

        return ""

    def _is_points_super_admin(self, user_id: str) -> bool:
        """Return true when the requester can manage points admins and allowances."""
        return user_id == POINTS_SUPER_ADMIN_SLACK_ID

    def _points_super_admin_denial(self) -> str:
        """Fixed denial response for restricted points super-admin actions."""
        return (
            f"Sorry mate, only <@{POINTS_SUPER_ADMIN_SLACK_ID}> can manage "
            "Points Admin access and weekly allowances. 🔒"
        )

    def _coworking_report_points_admin_denial(self) -> str:
        """Fixed denial response for coworking booking reports."""
        return (
            "Sorry mate, you'll need to be a Points Admin to generate coworking reports. 🔒"
        )

    def _points_admin_role(self, admin_details: Optional[dict]) -> str:
        if not isinstance(admin_details, dict):
            return ""
        return str(admin_details.get("role") or "").strip().lower()

    def _is_full_points_admin_details(self, admin_details: Optional[dict]) -> bool:
        return self._points_admin_role(admin_details) in FULL_POINTS_ADMIN_ROLES

    def _can_generate_coworking_report_details(self, admin_details: Optional[dict]) -> bool:
        return self._points_admin_role(admin_details) in COWORKING_REPORT_ROLES

    def _full_points_admin_denial(self, admin_details: Optional[dict], action_label: str) -> str:
        if self._points_admin_role(admin_details) == "partner":
            return (
                f"Sorry mate, partner admins can only generate coworking reports. "
                f"You need a full Points Admin role to {action_label}. 🔒"
            )
        return f"Sorry mate, you'll need to be a full Points Admin to {action_label}. 🔒"

    def _is_points_admin_promotion_command(self, text: str) -> bool:
        """Detect commands that promote a tagged user to Points Admin."""
        return bool(
            re.search(r"\b(?:promote|make)\b", text, re.IGNORECASE)
            and re.search(r"\b(?:roo\s+)?points\s+admin\b", text, re.IGNORECASE)
        )

    def _is_points_admin_revocation_command(self, text: str) -> bool:
        """Detect commands that revoke a user's Points Admin access."""
        return bool(
            re.search(r"\b(?:revoke|remove)\b", text, re.IGNORECASE)
            and re.search(r"\b(?:roo\s+)?points\s+admin\b", text, re.IGNORECASE)
        )

    def _is_points_allowance_change_command(self, text: str) -> bool:
        """Detect commands that change a tagged admin's weekly allowance."""
        text_lower = text.lower()

        return bool(
            re.search(r"\b(?:set|change|update|increase|decrease)\b", text_lower)
            and (
                re.search(r"\bweekly\s+(?:points\s+)?allowance\b", text_lower)
                or re.search(
                    r"\b(?:number\s+of\s+points|how\s+many\s+points|points)\b.*\bcan\s+give\s+out\s+weekly\b",
                    text_lower,
                )
                or re.search(r"\bcan\s+give\s+out\s+weekly\b", text_lower)
            )
        )

    def _extract_non_roo_mentions(self, text: str, bot_id: Optional[str] = None) -> list[str]:
        """Extract unique tagged Slack user IDs, excluding Roo when present."""
        target_ids: list[str] = []
        seen: set[str] = set()

        for target_id in re.findall(r"<@([A-Z0-9]+)>", text):
            if target_id == bot_id or target_id in seen:
                continue
            seen.add(target_id)
            target_ids.append(target_id)

        return target_ids

    def _extract_single_admin_target(
        self,
        text: str,
        *,
        bot_id: Optional[str],
        action_label: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract exactly one tagged target user for privileged admin actions."""
        target_ids = self._extract_non_roo_mentions(text, bot_id=bot_id)

        if not target_ids:
            return None, f"Tag exactly one user to {action_label}."
        if len(target_ids) > 1:
            return None, f"Tag exactly one user to {action_label}. I found multiple mentions there."

        return target_ids[0], None

    def _extract_weekly_allowance(self, text: str, fallback_allowance: Any = None) -> Optional[int]:
        """Extract the requested weekly allowance from params or text."""
        if fallback_allowance not in (None, "", "0"):
            try:
                return int(fallback_allowance)
            except (TypeError, ValueError):
                pass

        patterns = (
            r"(?:number\s+of\s+points|how\s+many\s+points).*?\bcan\s+give\s+out\s+weekly\s+(?:to|of)\s+(-?\d+)\b",
            r"\bcan\s+give\s+out\s+weekly\s+(?:to|of)\s+(-?\d+)\b",
            r"weekly\s+(?:points\s+)?allowance\s+(?:to|of)\s+(-?\d+)\b",
            r"allowance\s+(?:to|of)\s+(-?\d+)\b",
            r"(?<![A-Z0-9])(-?\d+)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    return None

        return None

    def _extract_points_request_reason(self, text: str, fallback_reason: str = "") -> str:
        """Extract the reason from a natural-language points request."""
        if fallback_reason:
            return fallback_reason.strip()

        patterns = (
            r"request\s+\d+\s*(?:points?|pts?)\s+for\s+(.+)",
            r"(?:i\s*(?:am|'m)\s+)?requesting\s+\d+\s*(?:points?|pts?)\s+for\s+(.+)",
            r"(?:can i|get me|i(?:'d| would)? like)\s+\d+\s*(?:points?|pts?)\s+for\s+(.+)",
            r"(?:please\s+)?(?:award|give|reward)\s+(?:me|myself|<@[A-Z0-9]+>)\s+\d+\s*(?:roo\s+)?(?:points?|pts?)\s+for\s+(.+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().rstrip(".!?")

        return ""

    def _extract_points_request_amount(self, text: str, fallback_points: Any = None) -> Optional[int]:
        """Extract the requested points amount from the text when the LLM misses it."""
        if fallback_points not in (None, "", 0, "0"):
            try:
                return int(fallback_points)
            except (TypeError, ValueError):
                pass

        match = re.search(r"(?<![a-zA-Z])(\d+)\s*(?:points?|pts?)", text, re.IGNORECASE)
        if not match:
            return None

        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _extract_http_error_detail(self, exc: httpx.HTTPStatusError) -> str:
        """Extract a compact error string from a backend HTTP error."""
        if exc.response.status_code >= 500:
            return "MLAI backend is temporarily unavailable. Please try again in a moment."

        try:
            payload = exc.response.json()
        except ValueError:
            body = exc.response.text.strip()
            body_lower = body.lower()
            content_type = exc.response.headers.get("content-type", "").lower()
            if "<!doctype html" in body_lower or "<html" in body_lower or "html" in content_type:
                return "MLAI backend returned an unexpected error response. Please try again in a moment."
            return body
        except Exception:
            return ""

        if isinstance(payload, dict):
            for key in ("error", "detail", "message"):
                value = payload.get(key)
                if value:
                    return str(value)
        if payload in (None, "", [], {}):
            return ""
        return str(payload)

    def _is_topup_balance_cap_error(self, error_detail: str) -> bool:
        detail = error_detail.lower()
        return (
            "100-point" in detail
            and "balance cap" in detail
            and ("top-up" in detail or "topup" in detail or "purchase" in detail)
        )

    def _format_points_balance_summary(
        self,
        data: dict,
        tasks_command: str = "tasks",
        admin_allowance: Optional[dict] = None,
    ) -> str:
        balance = data.get("balance", 0)
        earned = data.get("lifetime_earned", 0)
        spent = data.get("lifetime_spent", 0)
        purchased = 0
        for key in (
            "lifetime_purchased",
            "lifetime_purchased_points",
            "lifetime_points_purchased",
            "points_purchased",
            "purchased_points",
            "lifetime_topup_points",
        ):
            if data.get(key) is not None:
                purchased = data.get(key)
                break

        allowance_line = ""
        if (
            isinstance(admin_allowance, dict)
            and not admin_allowance.get("error")
            and {"allowance", "used", "remaining"}.issubset(admin_allowance)
        ):
            allowance_line = (
                "🎁 **Admin Giveaway Allowance:** "
                f"{admin_allowance['remaining']} of {admin_allowance['allowance']} points "
                f"remaining this week ({admin_allowance['used']} used; resets Monday)\n"
            )

        return (
            f"G'day mate! Here's your points summary:\n\n"
            f"💰 **Current Balance:** {balance} points\n"
            f"{allowance_line}"
            f"📈 **Lifetime Earned:** {earned} points\n"
            f"📉 **Lifetime Spent:** {spent} points\n"
            f"🛒 **Lifetime Purchased:** {purchased} points\n\n"
            f"Nice work! Check out `{tasks_command}` to earn more 🦘"
        )

    async def _get_points_admin_allowance_for_summary(
        self,
        client,
        user_id: str,
    ) -> Optional[dict]:
        """Return allowance data only when the backend confirms a full Points Admin."""
        get_admin_allowance = getattr(client, "get_admin_allowance", None)
        if not callable(get_admin_allowance):
            return None

        try:
            allowance = await get_admin_allowance(user_id)
        except Exception as exc:
            print(f"⚠️ Failed to fetch Points Admin allowance for balance summary: {exc!r}")
            return None

        if not isinstance(allowance, dict) or allowance.get("error"):
            return None
        if not {"allowance", "used", "remaining"}.issubset(allowance):
            return None
        return allowance

    @staticmethod
    def _is_points_balance_error(error_detail: str) -> bool:
        detail = str(error_detail or "").lower()
        return (
            "balance" in detail
            or ("insufficient" in detail and ("point" in detail or "fund" in detail))
            or ("not enough" in detail and "point" in detail)
            or bool(
                re.search(
                    r"\b(?:has|have|available|remaining|current)\b[^\n]*"
                    r"\d+\s*(?:roo\s+)?points?\b",
                    detail,
                )
            )
            or bool(re.search(r"\d+\s*<\s*\d+", detail))
        )

    @classmethod
    def _redact_points_balance_error(cls, error_detail: str) -> str:
        detail = str(error_detail or "").strip()
        if cls._is_points_balance_error(detail) and re.search(r"\d", detail):
            return "There are not enough Roo Points for this action."
        return detail

    def _deliver_personal_points_message(
        self,
        *,
        recipient_user_id: str,
        requester_user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        private_message: str,
        action: str,
        public_message: Optional[str] = None,
        private_ack: str = "I've sent your Roo Points details privately.",
        private_failure_ack: str = (
            "I couldn't send you a DM. DM Roo `points` to view your points privately."
        ),
    ) -> dict:
        """Deliver personal points data without a shared-channel fallback."""
        result_data = {
            "action": action,
            "delivery": "direct_message",
            "private_points": True,
        }
        requester_dm = bool(
            channel_id
            and str(channel_id).startswith("D")
            and recipient_user_id == requester_user_id
        )
        if requester_dm:
            return {
                "message": private_message,
                "data": {**result_data, "delivery": "current_direct_message"},
            }

        try:
            response = send_dm(recipient_user_id, private_message)
            dm_delivered = bool(response and response.get("ok"))
        except Exception as exc:
            print(
                "⚠️ Private Roo Points delivery failed "
                f"action={action} exc_type={exc.__class__.__name__}"
            )
            dm_delivered = False
        result_data["dm_delivered"] = dm_delivered

        if public_message is not None:
            if not dm_delivered and recipient_user_id == requester_user_id:
                self._post_private_points_ack(
                    channel_id=channel_id,
                    requester_user_id=requester_user_id,
                    thread_ts=thread_ts,
                    text=(
                        "I couldn't send your private points details. "
                        "DM Roo `points` to view them safely."
                    ),
                    action=action,
                )
            return {
                "message": public_message,
                "data": result_data,
            }

        acknowledgement = (
            private_ack
            if dm_delivered
            else private_failure_ack
        )
        result_data["ephemeral_delivered"] = self._post_private_points_ack(
            channel_id=channel_id,
            requester_user_id=requester_user_id,
            thread_ts=thread_ts,
            text=acknowledgement,
            action=action,
        )
        if not dm_delivered:
            result_data["delivery_failed"] = True

        # Personal data must never fall back to a shared Slack response, even
        # when both the DM and private acknowledgement fail.
        return {
            "message": "",
            "suppress_post": True,
            "data": result_data,
        }

    @staticmethod
    def _post_private_points_ack(
        *,
        channel_id: Optional[str],
        requester_user_id: str,
        thread_ts: Optional[str],
        text: str,
        action: str,
    ) -> bool:
        if not channel_id or str(channel_id).startswith("D") or not requester_user_id:
            return False
        try:
            response = post_ephemeral(
                channel=channel_id,
                user=requester_user_id,
                text=text,
                thread_ts=thread_ts,
            )
            return bool(response and response.get("ok"))
        except Exception as exc:
            print(
                "⚠️ Private Roo Points acknowledgement failed "
                f"action={action} exc_type={exc.__class__.__name__}"
            )
            return False

    async def _get_points_balance_summary_for_rewards(self, client, user_id: str) -> Optional[dict]:
        try:
            return await client.get_balance(user_id)
        except Exception as exc:
            print(f"⚠️ Failed to fetch Roo points balance for rewards catalog: {exc!r}")
            return None

    def _format_rewards_catalog(
        self,
        rewards: list[dict],
        balance_summary: Optional[dict] = None,
    ) -> str:
        def sort_key(reward: dict) -> tuple[int, str]:
            try:
                cost = int(reward.get("cost_points", 0) or 0)
            except (TypeError, ValueError):
                cost = 0
            return cost, str(reward.get("code", ""))

        balance_summary = balance_summary or {}
        user_balance = balance_summary.get("balance")
        if user_balance is None:
            user_balance = next(
                (
                    reward.get("user_balance")
                    for reward in rewards
                    if reward.get("user_balance") is not None
                ),
                None,
            )
        lifetime_earned = balance_summary.get("lifetime_earned")

        lines = ["🎁 **Available Roo Rewards**"]
        if user_balance is not None:
            lines.append(f"Your balance: **{user_balance} points**")
        if lifetime_earned is not None:
            lines.append(f"Lifetime earned: **{lifetime_earned} points**")
        lines.append("")

        if not rewards:
            lines.append("No redeemable rewards are available at the moment.")
        for reward in sorted(rewards, key=sort_key):
            code = str(reward.get("code", "") or "").strip()
            name = str(reward.get("name", "") or code or "Reward").strip()
            cost = reward.get("cost_points", 0)
            point_word = "point" if cost == 1 else "points"
            lines.append(f"• **{name}** (`{code}`) - {cost} {point_word}")

            details = []
            description = str(reward.get("description", "") or "").strip()
            if description:
                details.append(description)

            stock_remaining = reward.get("stock_remaining")
            if stock_remaining is not None:
                details.append(f"{stock_remaining} left")

            fulfillment = str(reward.get("fulfillment", "") or "").lower()
            if fulfillment == "auto":
                details.append("instant redemption")
            elif fulfillment == "manual":
                details.append("admin approval")

            can_afford = reward.get("can_afford")
            if can_afford is True:
                details.append("you can redeem this now")
            elif can_afford is False and user_balance is not None:
                try:
                    shortfall = max(0, int(cost) - int(user_balance))
                except (TypeError, ValueError):
                    shortfall = 0
                if shortfall:
                    details.append(f"need {shortfall} more points")

            if details:
                lines.append(f"  _{'; '.join(details)}_")

        lines.extend(
            (
                "",
                "**Other ways to use Roo Points**",
                "• SEO article generation costs 4 Roo Points.",
                "• MLAI sometimes auctions merch, cool items, or experiences for a variable Roo Points bid. Highest bidder wins.",
                "",
                "**How lifetime earned Roo Points matter**",
                "• Bounties and paid work generally go to members with the highest lifetime earned Roo Points.",
                "• To be voted into the MLAI committee, you need at least 100 lifetime earned Roo Points.",
                "",
                "Request one with `reward request <CODE>`.",
                "For coworking, `coworking book YYYY-MM-DD` is usually the quickest path.",
            )
        )
        return "\n".join(lines)

    def _points_request_queue_error_message(self) -> str:
        """User-facing fallback when Roo cannot queue a points request for emoji approval."""
        return (
            "I couldn't queue that points request for admin approval just now. "
            "Please try again in a tick or ask a Points Admin to use the existing manual award flow."
        )

    def _is_self_directed_points_award(
        self,
        text: str,
        params: dict,
        user_id: str,
        bot_id: Optional[str] = None,
    ) -> bool:
        """Detect an award command that is actually asking Roo to award the requester themselves."""
        text_lower = text.lower()
        if re.search(r"\b(?:award|give|reward)\s+(?:me|myself)\b", text_lower):
            return True

        mentioned_users = [
            mentioned_user
            for mentioned_user in re.findall(r"<@([A-Z0-9]+)>", text)
            if mentioned_user != bot_id
        ]
        if mentioned_users and all(mentioned_user == user_id for mentioned_user in mentioned_users):
            return True

        param_targets: list[str] = []
        for param_target in params.get("target_users", []) or []:
            cleaned_target = re.sub(r"[<@>]", "", str(param_target))
            if cleaned_target and cleaned_target != bot_id:
                param_targets.append(cleaned_target)

        for param_target in (params.get("target_user"), params.get("target_slack_id")):
            cleaned_target = re.sub(r"[<@>]", "", str(param_target or ""))
            if cleaned_target and cleaned_target != bot_id:
                param_targets.append(cleaned_target)

        return bool(param_targets) and all(param_target == user_id for param_target in param_targets)

    def _shift_months(self, value: date, months: int) -> date:
        """Move a date by calendar months, clamping to the target month's end."""
        target_month_index = value.month - 1 + months
        target_year = value.year + target_month_index // 12
        target_month = target_month_index % 12 + 1
        target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
        return date(target_year, target_month, target_day)

    def _resolve_coworking_report_range(self, text: str, params: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve coworking report start/end dates from presets, params, or text."""
        text_lower = text.lower()
        if re.search(r"\blast\s+week\b", text_lower):
            from ..utils import get_current_date

            today = get_current_date()
            current_sunday_start = today - timedelta(days=(today.weekday() + 1) % 7)
            start = current_sunday_start - timedelta(days=7)
            end = current_sunday_start - timedelta(days=1)
            return start.isoformat(), end.isoformat(), None

        if re.search(r"\bthis\s+week\b", text_lower):
            from ..utils import get_current_date

            today = get_current_date()
            start = today - timedelta(days=today.weekday())
            return start.isoformat(), today.isoformat(), None

        months = None
        if re.search(r"\blast\s+3\s+months?\b", text_lower):
            months = 3
        elif re.search(r"\blast\s+6\s+months?\b", text_lower):
            months = 6
        elif re.search(r"\b(?:last|past)\s+(?:1\s+)?years?\b", text_lower) or re.search(
            r"\blast\s+12\s+months?\b",
            text_lower,
        ):
            months = 12

        if months:
            from ..utils import get_current_date

            today = get_current_date()
            start = self._shift_months(today, -months) + timedelta(days=1)
            return start.isoformat(), today.isoformat(), None

        if re.search(r"\blast\s+month\b", text_lower):
            from ..utils import get_current_date

            today = get_current_date()
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            return start.isoformat(), end.isoformat(), None

        start_date = params.get("start_date") or params.get("from_date")
        end_date = params.get("end_date") or params.get("to_date")
        iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if len(iso_dates) >= 2:
            start_date, end_date = iso_dates[0], iso_dates[1]

        if not start_date or not end_date:
            return (
                None,
                None,
                "Give me a start date and end date, like `coworking report from 2026-01-01 to 2026-03-31`, "
                "or ask for `coworking report last 3 months`.",
            )

        try:
            start = date.fromisoformat(str(start_date))
            end = date.fromisoformat(str(end_date))
        except ValueError:
            return None, None, "Use ISO dates for coworking reports, like `2026-01-01`."

        range_days = (end - start).days + 1
        if range_days <= 0:
            return None, None, "The coworking report end date must be on or after the start date."
        if range_days > 366:
            return None, None, "Coworking reports are limited to 366 days. Try a shorter date range."

        return start.isoformat(), end.isoformat(), None

    def _coworking_report_flags(self, text: str) -> dict:
        """Identify optional analysis behaviors requested by the user."""
        text_lower = text.lower()
        return {
            "comparison_requested": bool(
                re.search(
                    r"\b(?:compare|compared|comparison|versus|vs\.?|prior|previous|week before|month before)\b",
                    text_lower,
                )
            ),
            "detail_requested": bool(re.search(r"\b(?:detail|detailed|breakdown|table)\b", text_lower)),
            "raw_requested": bool(re.search(r"\b(?:raw|daily|day by day|each day)\b", text_lower)),
            "busiest_requested": bool(re.search(r"\b(?:busiest|peak|highest|most used)\b", text_lower)),
            "quietest_requested": bool(re.search(r"\b(?:quietest|lowest|least used)\b", text_lower)),
            "trend_requested": bool(re.search(r"\b(?:trend|trends|pattern|patterns|changed|change)\b", text_lower)),
            "recommendations_requested": bool(
                re.search(r"\b(?:recommend|recommendation|recommendations|what should|what can we do)\b", text_lower)
            ),
        }

    def _coworking_request_needs_llm_intent(self, text: str) -> bool:
        """Return true when deterministic date parsing may need LLM help."""
        text_lower = text.lower()
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text_lower):
            return False
        if any(
            phrase in text_lower
            for phrase in [
                "this week",
                "last week",
                "last month",
                "last 3 months",
                "last 6 months",
                "last year",
                "last 12 months",
            ]
        ):
            return False
        return bool(
            "coworking" in text_lower
            and re.search(r"\b(?:from|between|since|until|during|for|compare|trend|busiest|quietest)\b", text_lower)
        )

    def _extract_json_object(self, content: str) -> dict:
        """Parse a best-effort JSON object from an LLM response."""
        content = str(content or "").strip()
        if not content:
            return {}
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _coworking_report_llm_model(self) -> str:
        try:
            return get_settings().ROUTER_MODEL
        except Exception:
            return "gpt-5.4"

    async def _extract_coworking_report_intent_with_llm(self, text: str, params: dict) -> dict:
        """Use GPT-5.4 to extract date/comparison hints for flexible report requests."""
        if not self._coworking_request_needs_llm_intent(text):
            return {}

        try:
            response = await chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Extract coworking report intent as strict JSON only. "
                            "Use YYYY-MM-DD dates when the user states exact dates. "
                            "If dates are ambiguous, omit them. Allowed keys: "
                            "start_date, end_date, comparison_start_date, comparison_end_date, "
                            "comparison_requested, focus, detail_requested, recommendations_requested."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "message": text,
                                "existing_params": params,
                                "timezone": "Australia/Melbourne",
                            }
                        ),
                    },
                ],
                model=self._coworking_report_llm_model(),
                max_tokens=320,
                reasoning_effort="low",
            )
            return self._extract_json_object(response.content)
        except Exception as exc:
            print(f"⚠️ Coworking report intent extraction failed: {exc}")
            return {}

    def _resolve_coworking_report_range_from_intent(
        self,
        text: str,
        params: dict,
        llm_intent: dict,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve a primary report range, falling back to LLM-extracted dates."""
        start_date, end_date, error = self._resolve_coworking_report_range(text, params)
        if not error:
            return start_date, end_date, None

        merged_params = {
            **params,
            "start_date": llm_intent.get("start_date") or params.get("start_date") or params.get("from_date"),
            "end_date": llm_intent.get("end_date") or params.get("end_date") or params.get("to_date"),
        }
        if not merged_params.get("start_date") or not merged_params.get("end_date"):
            return start_date, end_date, error
        return self._resolve_coworking_report_range(text, merged_params)

    def _resolve_coworking_comparison_range(
        self,
        text: str,
        params: dict,
        llm_intent: dict,
        primary_start: str,
        primary_end: str,
    ) -> Optional[dict]:
        """Resolve the comparison range for a coworking analysis request."""
        flags = self._coworking_report_flags(text)
        comparison_requested = (
            flags["comparison_requested"]
            or bool(params.get("compare") or params.get("comparison_requested"))
            or bool(llm_intent.get("comparison_requested"))
        )

        explicit_start = (
            params.get("comparison_start_date")
            or params.get("compare_start_date")
            or llm_intent.get("comparison_start_date")
        )
        explicit_end = (
            params.get("comparison_end_date")
            or params.get("compare_end_date")
            or llm_intent.get("comparison_end_date")
        )
        if explicit_start and explicit_end:
            try:
                start = date.fromisoformat(str(explicit_start))
                end = date.fromisoformat(str(explicit_end))
            except ValueError:
                return None
            if end >= start and (end - start).days + 1 <= 366:
                return {
                    "label": "Comparison range",
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                }

        if not comparison_requested:
            return None

        start = date.fromisoformat(primary_start)
        end = date.fromisoformat(primary_end)
        range_days = (end - start).days + 1
        comparison_end = start - timedelta(days=1)
        comparison_start = comparison_end - timedelta(days=range_days - 1)

        text_lower = text.lower()
        label = "Previous period"
        if re.search(r"\b(?:week prior|prior week|previous week|week before)\b", text_lower):
            label = "Week prior"
        elif re.search(r"\b(?:month prior|previous month|month before)\b", text_lower):
            label = "Month prior"

        return {
            "label": label,
            "start_date": comparison_start.isoformat(),
            "end_date": comparison_end.isoformat(),
        }

    def _format_coworking_days(self, days: list[dict], *, limit: int = 5) -> str:
        if not days:
            return "None"
        shown = [
            f"{day.get('date')} ({int(day.get('booked_users', 0))})"
            for day in days[:limit]
        ]
        if len(days) > limit:
            shown.append(f"+{len(days) - limit} more")
        return ", ".join(shown)

    def _summarize_coworking_report(self, report: dict) -> dict:
        """Build deterministic analysis metrics from backend coworking report JSON."""
        report_range = report.get("range", {})
        totals = report.get("totals", {})
        daily = [
            {
                "date": str(row.get("date") or ""),
                "booked_users": int(row.get("booked_users", 0) or 0),
            }
            for row in report.get("daily", [])
            if row.get("date")
        ]

        total_user_days = int(totals.get("booked_user_days", sum(row["booked_users"] for row in daily)) or 0)
        range_days = int(totals.get("range_days", len(daily)) or len(daily) or 0)
        active_days = int(totals.get("active_days", sum(1 for row in daily if row["booked_users"] > 0)) or 0)
        average_per_day = totals.get("average_per_day")
        if average_per_day is None:
            average_per_day = round(total_user_days / range_days, 2) if range_days else 0
        average_per_day = float(average_per_day or 0)

        if daily:
            max_users = max(row["booked_users"] for row in daily)
            min_users = min(row["booked_users"] for row in daily)
            busiest_days = [row for row in daily if row["booked_users"] == max_users and max_users > 0]
            quietest_days = [row for row in daily if row["booked_users"] == min_users]
        else:
            busiest_days = []
            quietest_days = []

        day_of_week = {}
        for row in daily:
            try:
                day = date.fromisoformat(row["date"])
            except ValueError:
                continue
            day_name = calendar.day_name[day.weekday()]
            bucket = day_of_week.setdefault(
                day_name,
                {"day": day_name, "booked_user_days": 0, "active_days": 0, "days": 0},
            )
            bucket["booked_user_days"] += row["booked_users"]
            bucket["days"] += 1
            if row["booked_users"] > 0:
                bucket["active_days"] += 1

        day_of_week_rows = []
        for day_name in calendar.day_name:
            bucket = day_of_week.get(day_name)
            if not bucket:
                continue
            day_of_week_rows.append({
                **bucket,
                "average": round(bucket["booked_user_days"] / bucket["days"], 2) if bucket["days"] else 0,
            })

        top_days = sorted(daily, key=lambda row: (-row["booked_users"], row["date"]))[:5]
        bottom_days = sorted(daily, key=lambda row: (row["booked_users"], row["date"]))[:5]

        return {
            "range": {
                "start_date": report_range.get("start_date"),
                "end_date": report_range.get("end_date"),
            },
            "booked_user_days": total_user_days,
            "unique_users": int(totals.get("unique_users", 0) or 0),
            "active_days": active_days,
            "range_days": range_days,
            "average_per_day": round(average_per_day, 2),
            "busiest_days": busiest_days,
            "quietest_days": quietest_days,
            "top_days": top_days,
            "bottom_days": bottom_days,
            "day_of_week": day_of_week_rows,
            "daily": daily,
        }

    def _percent_delta(self, current: float, previous: float) -> Optional[float]:
        if previous == 0:
            return None
        return round(((current - previous) / previous) * 100, 1)

    def _compare_coworking_summaries(self, primary: dict, comparison: dict) -> dict:
        booked_delta = primary["booked_user_days"] - comparison["booked_user_days"]
        average_delta = round(primary["average_per_day"] - comparison["average_per_day"], 2)
        active_day_delta = primary["active_days"] - comparison["active_days"]
        unique_user_delta = primary["unique_users"] - comparison["unique_users"]
        return {
            "booked_user_days_delta": booked_delta,
            "booked_user_days_percent_delta": self._percent_delta(
                primary["booked_user_days"],
                comparison["booked_user_days"],
            ),
            "average_per_day_delta": average_delta,
            "average_per_day_percent_delta": self._percent_delta(
                primary["average_per_day"],
                comparison["average_per_day"],
            ),
            "active_days_delta": active_day_delta,
            "unique_users_delta": unique_user_delta,
        }

    def _format_coworking_delta(self, delta: float, percent_delta: Optional[float]) -> str:
        if delta == 0:
            return "unchanged"
        direction = "up" if delta > 0 else "down"
        amount = abs(delta)
        amount_text = str(int(amount)) if float(amount).is_integer() else str(round(amount, 2))
        if percent_delta is None:
            return f"{direction} {amount_text} (prior was 0)"
        return f"{direction} {amount_text} ({percent_delta:+.1f}%)"

    def _format_coworking_range(self, summary: dict) -> str:
        report_range = summary.get("range", {})
        return f"{report_range.get('start_date')} to {report_range.get('end_date')}"

    def _coworking_primary_label(self, text: str) -> str:
        text_lower = text.lower()
        if re.search(r"\blast\s+week\b", text_lower):
            return "Last week"
        if re.search(r"\bthis\s+week\b", text_lower):
            return "This week"
        if re.search(r"\blast\s+month\b", text_lower):
            return "Last month"
        if re.search(r"\blast\s+3\s+months?\b", text_lower):
            return "Last 3 months"
        if re.search(r"\blast\s+6\s+months?\b", text_lower):
            return "Last 6 months"
        if re.search(r"\b(?:last|past)\s+(?:1\s+)?years?\b", text_lower) or re.search(r"\blast\s+12\s+months?\b", text_lower):
            return "Last year"
        return "Selected range"

    def _build_coworking_analysis_context(
        self,
        text: str,
        primary_report: dict,
        comparison_report: Optional[dict],
        comparison_range: Optional[dict],
    ) -> dict:
        flags = self._coworking_report_flags(text)
        primary_summary = self._summarize_coworking_report(primary_report)
        comparison_summary = self._summarize_coworking_report(comparison_report) if comparison_report else None
        comparison = (
            self._compare_coworking_summaries(primary_summary, comparison_summary)
            if comparison_summary
            else None
        )
        return {
            "question": text,
            "source": "active coworking bookings, not door check-ins",
            "flags": flags,
            "primary": {
                "label": self._coworking_primary_label(text),
                "summary": primary_summary,
                "report": primary_report,
            },
            "comparison": {
                "label": (comparison_range or {}).get("label", "Previous period"),
                "summary": comparison_summary,
                "report": comparison_report,
                "metrics": comparison,
            } if comparison_summary else None,
        }

    def _coworking_direct_answer(self, context: dict) -> str:
        primary = context["primary"]["summary"]
        primary_label = context["primary"]["label"]
        flags = context["flags"]
        comparison = context.get("comparison")

        if flags.get("busiest_requested") and primary["busiest_days"]:
            return f"{primary_label}'s busiest day was {self._format_coworking_days(primary['busiest_days'])}."
        if flags.get("quietest_requested") and primary["quietest_days"]:
            return f"{primary_label}'s quietest day was {self._format_coworking_days(primary['quietest_days'])}."
        if comparison and comparison.get("metrics"):
            metrics = comparison["metrics"]
            return (
                f"{primary_label} had {primary['booked_user_days']} booked user-days, "
                f"{self._format_coworking_delta(metrics['booked_user_days_delta'], metrics['booked_user_days_percent_delta'])} "
                f"from {comparison['label'].lower()}."
            )
        return (
            f"{primary_label} had {primary['booked_user_days']} booked user-days "
            f"across {primary['active_days']} active days."
        )

    def _coworking_table(self, headers: list[str], rows: list[list[Any]]) -> str:
        widths = [len(header) for header in headers]
        string_rows = [[str(cell) for cell in row] for row in rows]
        for row in string_rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        lines = [" ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
        for row in string_rows:
            lines.append(" ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
        return "```\n" + "\n".join(lines) + "\n```"

    def _coworking_detail_sections(self, report: dict, summary: dict, flags: dict) -> list[str]:
        range_days = summary.get("range_days", 0)
        sections = []
        daily = report.get("daily", [])
        weekly = report.get("weekly", [])
        monthly = report.get("monthly", [])

        if flags.get("raw_requested") or range_days <= 14:
            rows = [[row.get("date", ""), int(row.get("booked_users", 0) or 0)] for row in daily]
            sections.extend(["*Daily*", self._coworking_table(["Date", "Users"], rows)])
            return sections

        if range_days <= 93:
            weekly_rows = [
                [
                    row.get("week_start", ""),
                    int(row.get("booked_user_days", 0) or 0),
                    int(row.get("active_days", 0) or 0),
                ]
                for row in weekly
            ]
            monthly_rows = [
                [
                    row.get("month", ""),
                    int(row.get("booked_user_days", 0) or 0),
                    int(row.get("active_days", 0) or 0),
                ]
                for row in monthly
            ]
            sections.extend(["*Weekly*", self._coworking_table(["Week start", "User-days", "Active"], weekly_rows)])
            sections.extend(["*Monthly*", self._coworking_table(["Month", "User-days", "Active"], monthly_rows)])
            return sections

        monthly_rows = [
            [
                row.get("month", ""),
                int(row.get("booked_user_days", 0) or 0),
                int(row.get("active_days", 0) or 0),
            ]
            for row in monthly
        ]
        sections.extend(["*Monthly*", self._coworking_table(["Month", "User-days", "Active"], monthly_rows)])
        sections.extend([
            "*Highlights*",
            f"Top days: {self._format_coworking_days(summary.get('top_days', []))}",
            f"Quietest days: {self._format_coworking_days(summary.get('bottom_days', []))}",
        ])
        return sections

    def _format_coworking_analysis_fallback(self, context: dict) -> str:
        primary = context["primary"]["summary"]
        comparison = context.get("comparison")
        lines = [
            "🏢 *Coworking usage*",
            f"Range: {self._format_coworking_range(primary)}",
            "Source: Active coworking bookings (not door check-ins)",
            "",
            self._coworking_direct_answer(context),
            "",
            "*Measured facts*",
            f"• Booked user-days: {primary['booked_user_days']}",
            f"• Unique users: {primary['unique_users']}",
            f"• Active days: {primary['active_days']} of {primary['range_days']}",
            f"• Average per day: {primary['average_per_day']}",
            f"• Busiest day: {self._format_coworking_days(primary['busiest_days'])}",
        ]

        if comparison and comparison.get("summary") and comparison.get("metrics"):
            comparison_summary = comparison["summary"]
            metrics = comparison["metrics"]
            lines.extend([
                "",
                "*Comparison*",
                f"• {comparison['label']}: {self._format_coworking_range(comparison_summary)}",
                f"• Prior booked user-days: {comparison_summary['booked_user_days']}",
                f"• Change: {self._format_coworking_delta(metrics['booked_user_days_delta'], metrics['booked_user_days_percent_delta'])}",
                f"• Average/day change: {self._format_coworking_delta(metrics['average_per_day_delta'], metrics['average_per_day_percent_delta'])}",
                f"• Active-day change: {metrics['active_days_delta']:+d}",
            ])

        if context["flags"].get("recommendations_requested") or context["flags"].get("trend_requested"):
            lines.extend([
                "",
                "*Interpretation*",
                "• This is based on bookings only. Use it as a directional usage signal, not door-swipe attendance.",
            ])

        lines.extend(["", *self._coworking_detail_sections(context["primary"]["report"], primary, context["flags"])])
        return "\n".join(lines)

    async def _generate_coworking_llm_response(self, context: dict) -> Optional[str]:
        """Ask GPT-5.4 to turn bounded report data into a concise Slack answer."""
        flags = context.get("flags", {})
        if not (
            context.get("comparison")
            or flags.get("trend_requested")
            or flags.get("recommendations_requested")
        ):
            return None

        bounded_context = {
            "question": context["question"],
            "source": context["source"],
            "primary": context["primary"],
            "comparison": context.get("comparison"),
            "instructions": {
                "format": "Slack mrkdwn",
                "style": "insight first, concise, practical",
                "facts_rule": "Only use measured booking facts from this JSON for numeric claims.",
                "interpretation_rule": "Broader explanations or suggestions must be labelled Interpretation or Recommendations.",
            },
        }

        try:
            response = await chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Roo, writing an MLAI coworking usage answer for Slack. "
                            "Start with '*Coworking usage*'. State the exact range(s) and source. "
                            "Answer the user's question directly first. Keep it concise. "
                            "Use only the provided JSON for booking numbers. "
                            "Do not invent attendance, names, emails, or causes. "
                            "If you add broader advice, put it under '*Interpretation*' or '*Recommendations*'. "
                            "Do not include large raw tables."
                        ),
                    },
                    {"role": "user", "content": json.dumps(bounded_context, sort_keys=True)},
                ],
                model=self._coworking_report_llm_model(),
                max_tokens=700,
                reasoning_effort="low",
            )
            content = str(response.content or "").strip()
            return content or None
        except Exception as exc:
            print(f"⚠️ Coworking report GPT response failed: {exc}")
            return None

    async def _format_coworking_analysis_response(self, context: dict) -> str:
        llm_response = await self._generate_coworking_llm_response(context)
        if not llm_response:
            return self._format_coworking_analysis_fallback(context)

        primary = context["primary"]["summary"]
        detail_sections = self._coworking_detail_sections(context["primary"]["report"], primary, context["flags"])
        if detail_sections:
            return "\n".join([llm_response, "", *detail_sections])
        return llm_response

    def _format_coworking_report(self, report: dict) -> str:
        """Format a coworking booking report for Slack."""
        context = self._build_coworking_analysis_context("coworking report", report, None, None)
        return self._format_coworking_analysis_fallback(context)

    def _resolve_coworking_booking_date(
        self,
        params: dict,
        text: str,
        *,
        default_to_today: bool,
    ) -> Optional[str]:
        """Resolve coworking booking/check-in date from params, text, or today's default."""
        raw_date = str(params.get("date") or "").strip().strip(".,")

        if not raw_date:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(text or ""))
            if match:
                raw_date = match.group(1)

        text_lower = self._normalize_points_routing_text(text)
        if not raw_date:
            if re.search(r"\btomorrow\b", text_lower):
                raw_date = "tomorrow"
            elif re.search(r"\btoday\b", text_lower):
                raw_date = "today"

        if raw_date.lower() not in {"today", "tomorrow"} and (raw_date or not default_to_today):
            return raw_date or None

        from roo.utils import get_current_date
        today = get_current_date()
        if raw_date.lower() == "today":
            return today.isoformat()
        if raw_date.lower() == "tomorrow":
            return (today + timedelta(days=1)).isoformat()
        return today.isoformat()

    def _extract_coworking_checkin_targets(
        self,
        text: str,
        params: dict,
        *,
        bot_id: Optional[str],
    ) -> list[str]:
        """Extract target Slack IDs for admin coworking check-in."""
        target_slack_ids: list[str] = []

        for mentioned_user in re.findall(r"<@([A-Z0-9]+)>", str(text or ""), re.IGNORECASE):
            if not bot_id or mentioned_user != bot_id:
                target_slack_ids.append(mentioned_user)

        if not target_slack_ids:
            param_values: list[Any] = []
            param_values.extend(params.get("target_users", []) or [])
            param_values.extend(
                [
                    params.get("target_user"),
                    params.get("target_slack_id"),
                ]
            )
            for raw_target in param_values:
                if raw_target in (None, ""):
                    continue
                cleaned = re.sub(r"[<@>]", "", str(raw_target)).strip()
                if cleaned and (not bot_id or cleaned != bot_id):
                    target_slack_ids.append(cleaned)

        deduped: list[str] = []
        for target in target_slack_ids:
            if target not in deduped:
                deduped.append(target)
        return deduped

    def _coworking_booking_already_queued_message(
        self,
        *,
        booking_date: str,
        target_user_id: str,
        admin_checkin: bool,
    ) -> str:
        if admin_checkin:
            return (
                f"I already have a coworking check-in request for <@{target_user_id}> "
                f"on **{booking_date}** queued or in progress. I'll confirm in this thread "
                "when it completes."
            )
        return (
            f"I already have your coworking booking request for **{booking_date}** "
            "queued or in progress. I'll confirm in this thread when it completes."
        )

    def _coworking_booking_queued_for_retry_message(
        self,
        *,
        booking_date: str,
        target_user_id: str,
        admin_checkin: bool,
    ) -> str:
        if admin_checkin:
            return (
                f"I got the coworking check-in request for <@{target_user_id}> on "
                f"**{booking_date}**, but MLAI backend didn't confirm it yet. I've queued "
                "it and will keep retrying automatically. I won't double-book the same day."
            )
        return self._coworking_booking_queued_message(booking_date)

    def _format_coworking_booking_success(
        self,
        *,
        booking_date: str,
        target_user_id: str,
        cost: int,
        new_balance: Optional[int],
        admin_checkin: bool,
        discount_applied: bool = False,
        include_balance: bool = True,
    ) -> str:
        point_word = "point" if cost == 1 else "points"
        if admin_checkin:
            balance_line = (
                f"\nTheir balance: {new_balance} pts"
                if include_balance and new_balance is not None
                else ""
            )
            return (
                f"You beauty! 🎉\n\n"
                f"Checked <@{target_user_id}> in for **{booking_date}** at the coworking space.\n"
                f"Cost: {cost} {point_word}{balance_line}"
            )

        balance_line = ""
        if include_balance and new_balance is not None:
            balance_line = f" (Balance remaining: {new_balance} points)"

        message = (
            f"You beauty! 🎉\n\n"
            f"Booked you in for **{booking_date}** at the coworking space.\n"
            f"Cost: {cost} {point_word}{balance_line}\n\n"
            f"See you there, legend!"
        )

        # Nudge founders who paid the standard (undiscounted) price: submitting a
        # monthly startup update lowers the coworking cost for that month. The
        # backend tells us whether the discount already applied.
        if not discount_applied:
            message += (
                "\n\n💡 Startup founders get a discount on coworking bookings when they "
                "submit a monthly update for their startup. Submit yours here: "
                "https://mlai.au/platform/login?app=founder-tools&next=/founder-tools"
            )

        return message

    async def _format_admin_coworking_bad_request(
        self,
        *,
        client,
        target_user_id: str,
        exc: httpx.HTTPStatusError,
    ) -> str:
        error_detail = self._redact_points_balance_error(
            self._extract_http_error_detail(exc) or "Bad request"
        )
        return f"🛑 I couldn't check <@{target_user_id}> in: {error_detail}"

    async def _format_coworking_booking_rejection(
        self,
        *,
        client,
        target_user_id: str,
        requested_by_user_id: str,
        booking_date: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        admin_checkin: bool,
        exc: httpx.HTTPStatusError,
    ) -> Any:
        """Render a terminal backend rejection without calling it an outage."""
        status_code = exc.response.status_code
        fallback_by_status = {
            400: "The booking request was rejected.",
            401: "That booking isn't allowed for your account.",
            403: "That booking isn't allowed for your account.",
            404: "I couldn't find the MLAI account needed for that booking.",
        }
        if status_code in {401, 403}:
            # These statuses normally describe Roo's service credential, not a
            # member action. Do not expose backend authentication details.
            error_detail = fallback_by_status[status_code]
        else:
            error_detail = self._extract_http_error_detail(exc) or fallback_by_status.get(
                status_code,
                "The booking request was rejected.",
            )
        safe_detail = self._redact_points_balance_error(error_detail)

        if admin_checkin:
            return f"🛑 I couldn't check <@{target_user_id}> in: {safe_detail}"

        public_message = (
            f"🛑 I couldn't book you in for **{booking_date}**: {safe_detail}"
        )
        if not self._is_points_balance_error(error_detail):
            return public_message

        private_message = public_message
        try:
            balance_data = await client.get_balance(target_user_id)
            current_balance = balance_data.get("balance")
            if current_balance is not None:
                private_message += (
                    f"\n\nYour current balance is **{current_balance} Roo Points**."
                )
        except Exception as balance_exc:
            print(
                "🏢 coworking_rejection_balance_lookup_failed "
                f"exc_type={balance_exc.__class__.__name__}"
            )

        return self._deliver_personal_points_message(
            recipient_user_id=target_user_id,
            requester_user_id=requested_by_user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            private_message=private_message,
            public_message=public_message,
            action="book_coworking_error",
            private_ack=public_message,
        )

    def _format_admin_coworking_batch_success(
        self,
        *,
        booking_date: str,
        batch_result: dict,
    ) -> str:
        results = batch_result.get("results") or []
        created_count = int(batch_result.get("created_count") or 0)
        already_booked_count = int(batch_result.get("already_booked_count") or 0)

        lines = [
            "You beauty! 🎉",
            "",
            f"Checked **{len(results)}** people for coworking on **{booking_date}**.",
        ]
        if created_count or already_booked_count:
            lines.append(
                f"Created: **{created_count}** · Already booked: **{already_booked_count}**"
            )

        for result in results:
            slack_user_id = str(result.get("slack_user_id") or "").strip()
            if not slack_user_id:
                booking = result.get("booking") or {}
                slack_user_id = str(booking.get("slack_id") or booking.get("slack_user_id") or "").strip()
            if not slack_user_id:
                continue

            status = "already booked" if result.get("already_booked") else "booked"
            points_cost = result.get("points_cost")
            cost_text = f" · {points_cost} pts" if points_cost is not None else ""
            lines.append(f"• <@{slack_user_id}>: {status}{cost_text}")

        lines.extend([
            "",
            "Admin Roo Points were not charged; each target user's Roo Points were used.",
        ])
        return "\n".join(lines)

    def _format_admin_coworking_batch_bad_request(
        self,
        *,
        booking_date: str,
        exc: httpx.HTTPStatusError,
    ) -> str:
        error_detail = self._redact_points_balance_error(
            self._extract_http_error_detail(exc) or "The booking request was rejected."
        )
        per_target_errors: list[dict] = []
        try:
            payload = exc.response.json()
            if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                per_target_errors = [
                    item for item in payload["errors"] if isinstance(item, dict)
                ]
        except Exception:
            per_target_errors = []

        lines = [
            f"🛑 No bookings were created for **{booking_date}**.",
            error_detail,
        ]
        for item in per_target_errors[:10]:
            slack_user_id = str(item.get("slack_user_id") or "").strip()
            target_label = f"<@{slack_user_id}>" if slack_user_id else "Target"
            reason = self._redact_points_balance_error(
                str(
                    item.get("error")
                    or item.get("detail")
                    or item.get("message")
                    or "Rejected"
                ).strip()
            )
            lines.append(f"• {target_label}: {reason}")
        return "\n".join(lines)

    async def _book_coworking_many_for_admin(
        self,
        *,
        client,
        admin_user_id: str,
        target_user_ids: list[str],
        booking_date: str,
        channel_id: Optional[str],
    ) -> Any:
        try:
            result = await client.book_coworking_many(
                admin_slack_user_id=admin_user_id,
                target_slack_user_ids=target_user_ids,
                booking_date=booking_date,
                slack_channel_id=channel_id,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                return self._format_admin_coworking_batch_bad_request(
                    booking_date=booking_date,
                    exc=exc,
                )
            if exc.response.status_code == 403:
                return self._full_points_admin_denial(None, "check people in for coworking")
            if is_retryable_coworking_exception(exc):
                return (
                    "I couldn't confirm that multi-person coworking check-in because MLAI backend "
                    "didn't respond cleanly. Please retry the same command; existing bookings are "
                    "treated as already booked and won't be charged twice."
                )
            raise
        except Exception as exc:
            if is_retryable_coworking_exception(exc):
                return (
                    "I couldn't confirm that multi-person coworking check-in because MLAI backend "
                    "didn't respond cleanly. Please retry the same command; existing bookings are "
                    "treated as already booked and won't be charged twice."
                )
            raise

        return self._format_admin_coworking_batch_success(
            booking_date=str(result.get("date") or booking_date),
            batch_result=result,
        )

    async def _book_coworking_with_intent(
        self,
        *,
        client,
        target_user_id: str,
        requested_by_user_id: str,
        booking_date: str,
        text: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        admin_checkin: bool,
    ) -> Any:
        print(
            "🏢 coworking_booking_execute "
            f"requested_by_user_id={requested_by_user_id} target_user_id={target_user_id} "
            f"booking_date={booking_date} admin_checkin={admin_checkin}"
        )
        try:
            store = get_coworking_intent_store()
            intent = store.record_intent(
                slack_user_id=target_user_id,
                requested_by_slack_id=requested_by_user_id,
                booking_date=booking_date,
                channel_id=channel_id,
                thread_ts=thread_ts,
                request_text=text,
            )
            leased_intent = store.reserve_for_processing(
                int(intent["id"]),
                owner=f"roo-sync-{uuid4().hex}",
            )
        except Exception as exc:
            print(
                "🏢 coworking_intent_persist_failed "
                f"slack_user_id={target_user_id} requested_by={requested_by_user_id} "
                f"booking_date={booking_date} exc_type={exc.__class__.__name__} exc={exc}"
            )
            return (
                "I couldn't safely queue that coworking booking request just now, "
                "so I didn't send it to MLAI backend. Please try again in a moment."
            )

        if not leased_intent:
            return self._coworking_booking_already_queued_message(
                booking_date=booking_date,
                target_user_id=target_user_id,
                admin_checkin=admin_checkin,
            )

        try:
            result = await client.book_coworking(target_user_id, booking_date, channel_id)
            store.mark_confirmed(int(leased_intent["id"]), backend_result=result)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            if is_retryable_coworking_exception(exc):
                store.mark_retryable_failure(int(leased_intent["id"]), error=error)
                return self._coworking_booking_queued_for_retry_message(
                    booking_date=booking_date,
                    target_user_id=target_user_id,
                    admin_checkin=admin_checkin,
                )
            store.mark_blocked(int(leased_intent["id"]), error=error)
            if isinstance(exc, httpx.HTTPStatusError):
                print(
                    "🏢 coworking_booking_terminal_rejection "
                    f"status={exc.response.status_code}"
                )
                return await self._format_coworking_booking_rejection(
                    client=client,
                    target_user_id=target_user_id,
                    requested_by_user_id=requested_by_user_id,
                    booking_date=booking_date,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    admin_checkin=admin_checkin,
                    exc=exc,
                )
            raise

        cost = result.get("points_cost", 1)
        # The backend is the single source of truth for whether the
        # monthly-update discount applied; we only render the nudge off this.
        discount_applied = bool(result.get("monthly_update_discount_applied", False))
        from roo.clients.mlai_backend import MLAIBackendUnavailableError

        new_balance = None
        try:
            balance_data = await client.get_balance(target_user_id)
            new_balance = balance_data.get("balance", 0)
        except MLAIBackendUnavailableError:
            pass

        private_message = self._format_coworking_booking_success(
            booking_date=booking_date,
            target_user_id=target_user_id,
            cost=cost,
            new_balance=new_balance,
            admin_checkin=False,
            discount_applied=discount_applied,
        )
        public_message = self._format_coworking_booking_success(
            booking_date=booking_date,
            target_user_id=target_user_id,
            cost=cost,
            new_balance=None,
            admin_checkin=admin_checkin,
            discount_applied=discount_applied,
            include_balance=False,
        )
        return self._deliver_personal_points_message(
            recipient_user_id=target_user_id,
            requester_user_id=requested_by_user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            private_message=private_message,
            public_message=public_message,
            action="book_coworking",
        )

    async def _handle_points_action(
        self,
        client,
        action: str,
        params: dict,
        text: str,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        skill,
        request_id: Optional[str] = None,
    ) -> Any:
        """Handle individual points actions."""
        
        # =====================================================================
        # Member Actions
        # =====================================================================

        if action == "link_account":
            from ..slack_client import (
                SlackIdentityLookupError,
                get_verified_user_email,
            )

            try:
                email = get_verified_user_email(user_id)
            except SlackIdentityLookupError:
                private_message = (
                    "I couldn't verify an email for your Slack profile, so I didn't "
                    "change any account link. Ask an MLAI admin to check your Slack "
                    "profile email and try again."
                )
            else:
                try:
                    linked_user_id = await client.link_slack_user(user_id, email)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        private_message = (
                            "That MLAI account is already linked to another Slack "
                            "profile, so I didn't change either account. Ask an MLAI "
                            "admin to resolve the existing link."
                        )
                    else:
                        print(
                            "Slack account link failed "
                            f"status={exc.response.status_code}"
                        )
                        private_message = (
                            "I couldn't safely link your Slack and MLAI accounts right now. "
                            "Nothing was confirmed—please try `link` again in a moment."
                        )
                except Exception as exc:
                    print(
                        "Slack account link failed "
                        f"exc_type={exc.__class__.__name__}"
                    )
                    private_message = (
                        "I couldn't safely link your Slack and MLAI accounts right now. "
                        "Nothing was confirmed—please try `link` again in a moment."
                    )
                else:
                    if linked_user_id is None:
                        private_message = (
                            "I couldn't find an existing MLAI account with the same "
                            "email as your Slack profile. Sign in to MLAI with that "
                            "email, or ask an MLAI admin to update your account email, "
                            "then try `link` again."
                        )
                    else:
                        private_message = (
                            "✅ Your Slack profile is linked to your MLAI account. "
                            "You can now try `book me in` again."
                        )

            return self._deliver_personal_points_message(
                recipient_user_id=user_id,
                requester_user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                private_message=private_message,
                action="link_account",
                private_ack="I've sent your account-link result privately.",
                private_failure_ack=(
                    "I couldn't send you a DM. Open Roo's direct messages and "
                    "run `link` there to see the result privately."
                ),
            )
        
        if action == "topup_points":
            settings = get_settings()
            if not getattr(settings, "ROO_POINTS_TOPUP_ENABLED", False):
                return "Top-up checkout is not enabled yet. Ask the MLAI team to finish enabling Stripe top-ups first."

            pack_id, unsupported_amount = self._resolve_topup_pack_id(text, params)
            if getattr(settings, "ROO_POINTS_TOPUP_BUTTONS_ENABLED", False):
                purchase_from = {"source": "slack"}
                if channel_id:
                    purchase_from["slack_channel_id"] = channel_id
                if thread_ts:
                    purchase_from["slack_thread_ts"] = thread_ts
                checkout_request_id = str(
                    request_id or f"roo-topup-{uuid4().hex}"
                ).strip()
                requested_pack_ids = [pack_id] if pack_id else None
                try:
                    checkout_payload = await client.create_points_checkout_options(
                        slack_user_id=user_id,
                        checkout_request_id=checkout_request_id,
                        pack_ids=requested_pack_ids,
                        purchase_from=purchase_from,
                    )
                except httpx.HTTPStatusError as exc:
                    error_detail = self._extract_http_error_detail(exc)
                    detail = f" {error_detail}" if error_detail else ""
                    return f"I couldn't create the Stripe top-up buttons yet.{detail}"

                options = self._trusted_topup_checkout_options(
                    checkout_payload.get("options"),
                    settings=settings,
                )
                if not options:
                    return (
                        "I couldn't create trusted Stripe Checkout links for those "
                        "top-up packs. Please try again shortly."
                    )
                return self._topup_checkout_button_response(
                    options=options,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    had_partial_errors=bool(checkout_payload.get("errors")),
                )

            if not pack_id:
                if unsupported_amount is None:
                    return self._topup_pack_list_message()
                return "I can only help with these fixed top-up packs right now: 10, 20, or 50 Top-up Roo Points."

            purchase_from = {"source": "slack"}
            if channel_id:
                purchase_from["slack_channel_id"] = channel_id
            if thread_ts:
                purchase_from["slack_thread_ts"] = thread_ts

            try:
                purchase = await client.create_points_purchase(
                    slack_user_id=user_id,
                    pack_id=pack_id,
                    purchase_from=purchase_from,
            )
            except httpx.HTTPStatusError as exc:
                error_detail = self._extract_http_error_detail(exc)
                if self._is_topup_balance_cap_error(error_detail):
                    return (
                        "You've already got heaps of Roo Points, so you can't top up right now. "
                        "This top-up would put you over the 100-point spendable balance cap. "
                        "Use some points first, then try again."
                    )
                detail = f" {error_detail}" if error_detail else ""
                return f"I couldn't create that top-up checkout yet.{detail}"

            checkout_url = str(purchase.get("frontend_checkout_page_url") or "").strip()
            if not checkout_url:
                return "I created the top-up request, but the checkout link was missing. Ask the MLAI team to check the points purchase API."

            return (
                "I created your Top-up Roo Points checkout. Continue here:\n"
                f"{checkout_url}\n\n"
                "Top-up Roo Points are MLAI community reward points. They are not money, "
                "have no cash value, cannot be converted to cash, and cannot be sold or transferred. "
                "They do not count toward lifetime earned contribution."
            )

        if action == "balance":
            data = await client.get_balance(user_id)
            admin_allowance = await self._get_points_admin_allowance_for_summary(
                client,
                user_id,
            )
            message = self._format_points_balance_summary(
                data,
                tasks_command="tasks",
                admin_allowance=admin_allowance,
            )
            return self._deliver_personal_points_message(
                recipient_user_id=user_id,
                requester_user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                private_message=message,
                action="balance",
                private_ack="I've sent your Roo Points summary privately.",
            )

        elif action == "delete_flex":
            is_dm = bool(channel_id and str(channel_id).startswith("D"))
            if not channel_id or not str(channel_id).startswith(("C", "D", "G")):
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "delete_flex",
                        "preview_delivered": False,
                        "invalid_channel": True,
                    },
                }

            from ..slack_client import get_bot_user_id

            try:
                bot_user_id = get_bot_user_id()
            except Exception:
                bot_user_id = None
            target_mentions = [
                mentioned_user_id
                for mentioned_user_id in re.findall(r"<@([A-Z0-9]+)>", text)
                if mentioned_user_id != bot_user_id
            ]
            if target_mentions:
                target_rejected_text = (
                    "You can only delete your own Roo Points flex. "
                    "Use `delete my flex` without tagging another member."
                )
                if is_dm:
                    return target_rejected_text
                delivered = self._post_private_points_ack(
                    channel_id=channel_id,
                    requester_user_id=user_id,
                    thread_ts=thread_ts,
                    text=target_rejected_text,
                    action="delete_flex",
                )
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "delete_flex",
                        "preview_delivered": False,
                        "target_rejected": True,
                        "ephemeral_delivered": delivered,
                    },
                }

            settings = get_settings()
            store = get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
            lookup_failed = False
            try:
                records = (
                    store.list_shared_for_user(slack_user_id=user_id)
                    if is_dm
                    else store.list_shared(
                        slack_user_id=user_id,
                        channel_id=channel_id,
                    )
                )
            except Exception as exc:
                print(
                    "Points flex delete lookup failed "
                    f"exc_type={exc.__class__.__name__}"
                )
                records = []
                lookup_failed = True

            if not records:
                empty_text = (
                    "I couldn't safely look up your Roo Points flexes. Try again in a moment."
                    if lookup_failed
                    else (
                        "I couldn't find any active Roo Points flexes from you."
                        if is_dm
                        else "I couldn't find an active Roo Points flex from you in this channel."
                    )
                )
                if is_dm:
                    return empty_text
                delivered = self._post_private_points_ack(
                    channel_id=channel_id,
                    requester_user_id=user_id,
                    thread_ts=thread_ts,
                    text=empty_text,
                    action="delete_flex",
                )
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "delete_flex",
                        "preview_delivered": delivered,
                        "flex_count": 0,
                    },
                }

            tokens = {
                str(record["request_id"]): issue_points_flex_deletion(
                    signing_secret=settings.SLACK_SIGNING_SECRET,
                    request_id=str(record["request_id"]),
                    slack_user_id=user_id,
                    channel_id=str(record["channel_id"]),
                    interaction_channel_id=channel_id,
                )
                for record in records
            }
            preview_text = (
                "Confirm which Roo Points flex to delete."
                if len(records) > 1
                else "Confirm deletion of your Roo Points flex."
            )
            preview_blocks = build_points_flex_delete_blocks(
                records=records,
                tokens=tokens,
                include_channel=is_dm,
            )
            if is_dm:
                return {
                    "message": preview_text,
                    "blocks": preview_blocks,
                    "data": {
                        "action": "delete_flex",
                        "preview_ready": True,
                        "flex_count": len(records),
                    },
                }
            try:
                response = post_ephemeral(
                    channel=channel_id,
                    user=user_id,
                    text=preview_text,
                    thread_ts=thread_ts,
                    blocks=preview_blocks,
                )
                preview_delivered = bool(response and response.get("ok"))
            except Exception as exc:
                print(
                    "Points flex delete preview failed "
                    f"exc_type={exc.__class__.__name__}"
                )
                preview_delivered = False

            if not preview_delivered:
                try:
                    send_dm(
                        user_id,
                        "I couldn't show the flex deletion controls in that channel. "
                        "Try `@Roo delete my flex` there again in a moment.",
                    )
                except Exception as exc:
                    print(
                        "Points flex delete fallback failed "
                        f"exc_type={exc.__class__.__name__}"
                    )

            return {
                "message": "",
                "suppress_post": True,
                "data": {
                    "action": "delete_flex",
                    "preview_delivered": preview_delivered,
                    "flex_count": len(records),
                },
            }

        elif action == "flex_points":
            if not channel_id or str(channel_id).startswith("D"):
                return (
                    "Run `@Roo flex my points` in the shared channel where you "
                    "want Roo to post your lifetime-earned contribution total."
                )

            if not str(channel_id).startswith(("C", "G")):
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "flex_points",
                        "preview_delivered": False,
                        "invalid_channel": True,
                    },
                }

            if not thread_ts:
                delivered = self._post_private_points_ack(
                    channel_id=channel_id,
                    requester_user_id=user_id,
                    thread_ts=None,
                    text=(
                        "I couldn't identify the request thread, so nothing was shared. "
                        "Run `@Roo flex my points` again."
                    ),
                    action="flex_points",
                )
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "flex_points",
                        "preview_delivered": delivered,
                        "missing_thread": True,
                    },
                }

            from ..slack_client import get_bot_user_id

            try:
                bot_user_id = get_bot_user_id()
            except Exception:
                bot_user_id = None
            target_mentions = [
                mentioned_user_id
                for mentioned_user_id in re.findall(r"<@([A-Z0-9]+)>", text)
                if mentioned_user_id != bot_user_id
            ]
            if target_mentions:
                delivered = self._post_private_points_ack(
                    channel_id=channel_id,
                    requester_user_id=user_id,
                    thread_ts=thread_ts,
                    text=(
                        "You can only flex your own lifetime-earned Roo Points. "
                        "Use `@Roo flex my points` without tagging another member."
                    ),
                    action="flex_points",
                )
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "flex_points",
                        "preview_delivered": False,
                        "target_rejected": True,
                        "ephemeral_delivered": delivered,
                    },
                }

            data = await client.get_balance(user_id)
            lifetime_earned = parse_lifetime_earned(data)
            settings = get_settings()
            token, confirmation = issue_points_flex_confirmation(
                signing_secret=settings.SLACK_SIGNING_SECRET,
                slack_user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            preview_text = (
                "Confirm whether to share your lifetime-earned Roo Points "
                "in this thread."
            )
            try:
                response = post_ephemeral(
                    channel=channel_id,
                    user=user_id,
                    text=preview_text,
                    thread_ts=thread_ts,
                    blocks=build_points_flex_preview_blocks(
                        lifetime_earned=lifetime_earned,
                        token=token,
                    ),
                )
                preview_delivered = bool(response and response.get("ok"))
            except Exception as exc:
                print(
                    "Points flex preview delivery failed "
                    f"exc_type={exc.__class__.__name__}"
                )
                preview_delivered = False

            if not preview_delivered:
                try:
                    send_dm(
                        user_id,
                        "I couldn't show the sharing confirmation in that "
                        "thread. Try `@Roo flex my points` there again in a moment.",
                    )
                except Exception as exc:
                    print(
                        "Points flex preview fallback failed "
                        f"exc_type={exc.__class__.__name__}"
                    )

            return {
                "message": "",
                "suppress_post": True,
                "data": {
                    "action": "flex_points",
                    "preview_delivered": preview_delivered,
                    "confirmation_request_id": confirmation.request_id,
                },
            }
        
        elif action == "history":
            limit = params.get("limit", 10)
            entries = await client.get_history(user_id, limit)
            
            if not entries:
                message = "No transactions yet! Start earning points by claiming some tasks 💪"
            else:
                lines = ["📜 **Your Recent Transactions:**\n"]
                for entry in entries[:10]:
                    delta = entry.get("delta", 0)
                    emoji = "➕" if delta > 0 else "➖"
                    desc = entry.get("description", "")[:50]
                    lines.append(f"{emoji} {delta:+d} pts - {desc}")
                message = "\n".join(lines)
            return self._deliver_personal_points_message(
                recipient_user_id=user_id,
                requester_user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                private_message=message,
                action="history",
                private_ack="I've sent your Roo Points history privately.",
            )
        
        elif action == "request_points":
            if not channel_id:
                return "Points requests only work from Slack channels or threads."
            if channel_id and channel_id.startswith("D"):
                return (
                    "Points requests only work in a shared channel or thread so an admin can approve them with ✅. "
                    "Pop this request into a channel and I'll queue it there."
                )

            points = self._extract_points_request_amount(text, params.get("points"))
            reason = self._extract_points_request_reason(text, params.get("reason", ""))

            if points is None:
                return "How many points are you requesting? Try `request 5 points for helping at the event`."
            if points <= 0:
                return "Points requests need a positive number of points."
            if not reason:
                return "What are you requesting the points for? Try `request 5 points for helping at the event`."

            from ..slack_client import get_bot_user_id

            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            mentioned_users = [
                mentioned_user
                for mentioned_user in re.findall(r"<@([A-Z0-9]+)>", text)
                if mentioned_user != bot_id
            ]
            non_self_mentions = [mentioned_user for mentioned_user in mentioned_users if mentioned_user != user_id]
            if non_self_mentions:
                return "For now, points requests are only for yourself. Ask an admin to use the manual award flow for someone else."

            try:
                request_record = await client.create_points_request(
                    requester_slack_id=user_id,
                    target_slack_id=user_id,
                    points=points,
                    reason=reason,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                )
            except httpx.HTTPStatusError as exc:
                error_detail = self._extract_http_error_detail(exc)
                print(
                    "❌ Points request queue failed: "
                    f"status={exc.response.status_code} error={error_detail or str(exc)}"
                )
                return self._points_request_queue_error_message()
            except Exception as exc:
                print(f"❌ Points request queue failed: error={exc}")
                return self._points_request_queue_error_message()

            request_id = request_record.get("id")
            summary_text = (
                f"📝 *Points request pending*\n\n"
                f"<@{user_id}> requested *{points} points* for: {reason}\n\n"
                f"Points Admins can approve this by reacting with a green tick (✅ or ✔️) to this message."
            )
            pending_request_record = None
            post_kwargs = {
                "channel": channel_id,
                "text": summary_text,
                "thread_ts": thread_ts,
            }
            if request_id:
                pending_request_record = build_points_request_record(
                    request_id=int(request_id),
                    requester_slack_id=user_id,
                    target_slack_id=user_id,
                    points=points,
                    reason=reason,
                    slack_thread_ts=thread_ts,
                )
                post_kwargs["metadata"] = build_points_request_metadata(pending_request_record)
            summary_response = post_message(**post_kwargs)
            summary_ts = summary_response.get("ts")

            if request_id and summary_ts and pending_request_record:
                remember_points_request_summary(
                    channel_id=channel_id,
                    summary_message_ts=summary_ts,
                    record=pending_request_record,
                )
                try:
                    await client.attach_points_request_slack_summary(
                        request_id=request_id,
                        slack_channel_id=channel_id,
                        slack_thread_ts=thread_ts,
                        slack_summary_message_ts=summary_ts,
                    )
                except Exception as exc:
                    print(
                        "⚠️ Points request summary attach failed; continuing with Slack-side fallback: "
                        f"request_id={request_id} channel={channel_id} "
                        f"thread_ts={thread_ts} error={exc!r}"
                    )

                return {
                    "message": "",
                    "data": {"action": action, "request_id": request_id},
                    "suppress_post": True,
                }

            return (
                "I created the points request, but I couldn't finish wiring up emoji approval for it. "
                "Please ask a Points Admin to use the existing manual award flow for now."
            )

        elif action == "list_tasks":
            text_lower = text.lower()
            if "tasks quick" in text_lower or "quick tasks" in text_lower:
                return 'Use `tasks` or `tasks open` for claimable work. `tasks quick` is no longer supported.'

            list_mode = self._resolve_task_list_mode(text, params)
            portfolio = params.get("portfolio")

            list_kwargs = {"status": None, "portfolio": portfolio}
            if list_mode == "open":
                list_kwargs["claimable"] = True
            elif list_mode == "mine":
                list_kwargs["assigned_to_me"] = user_id
            elif list_mode == "review":
                list_kwargs["reviewer_slack_id"] = user_id
                list_kwargs["needs_review"] = True

            tasks = await client.list_tasks(**list_kwargs)
            
            if not tasks:
                empty_messages = {
                    "all": "No tasks at the moment. Check back soon! 🦘",
                    "open": "No open tasks at the moment. Check back soon! 🦘",
                    "mine": "You don't have any tasks assigned right now. 🦘",
                    "review": "Nothing is waiting for your review right now. 🦘",
                }
                return empty_messages.get(list_mode, "No tasks at the moment. Check back soon! 🦘")
            
            headings = {
                "all": "All Tasks",
                "open": "Open Tasks",
                "mine": "My Tasks",
                "review": "Tasks To Review",
            }
            heading = headings.get(list_mode, "Tasks")
            lines = [f"📋 **{heading}:**\n"]
            for task in tasks[:10]:
                tid = task.get("id")
                task_code = task.get("task_code") or f"#{tid}"
                title = task.get("title", "Untitled")[:40]
                pts = task.get("points_estimate", task.get("points", 0))
                port = task.get("portfolio", "")
                estimate_minutes = task.get("estimate_minutes")
                estimate_text = f" · ~{estimate_minutes} min" if estimate_minutes else ""
                status_text = task.get("status", "")
                status_suffix = f" · {status_text}" if list_mode in {"all", "mine", "review"} and status_text else ""
                lines.append(f"• **{task_code}** - {title} ({pts} pts){estimate_text} 📂 {port}{status_suffix}")
            
            footer_messages = {
                "all": '\nUse "tasks" to see what can be claimed right now.',
                "open": '\nKeen to help? Just say "task claim <id or code>" to get started!',
                "mine": '\nSubmit with "task submit <id or code> <description>" when you are done.',
                "review": '\nApprove or reject with "task approve <id or code>" or "task reject <id or code> <reason>".',
            }
            lines.append(footer_messages.get(list_mode, ""))
            return "\n".join(lines)
        
        elif action == "claim_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            if not task_id:
                return "Which task do you want to claim? Give me the task ID or code (e.g., \"task claim 42\" or \"task claim ROO-0042\")"

            result = await client.claim_task(task_id, user_id)
            display_id = result.get("task_code") or f"#{result.get('id', task_id)}"
            title = result.get("title", "")
            pts = result.get("points_estimate", result.get("points", 0))
            
            return f"Ripper! 🎉 You've claimed **{display_id} - {title}** ({pts} pts).\n\nWhen you're done, submit your work with \"task submit {display_id} <description>\""

        elif action == "unclaim_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            if not task_id:
                return "Which task do you want to release? Give me the task ID or code."

            result = await client.unclaim_task(task_id, user_id)
            task = result.get("task", {})
            display_id = task.get("task_code") or f"#{task.get('id', task_id)}"
            title = task.get("title", "")
            return f"Released **{display_id} - {title}** back to the volunteer queue."
        
        elif action == "submit_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            submission_text = params.get("submission_text", "")
            submission_url = params.get("submission_url")
            
            if not task_id:
                return "Which task are you submitting? Give me the task ID or code (e.g., \"task submit 42 done!\" or \"task submit ROO-0042 done!\")"
            
            if not submission_text:
                # Extract text after the task ID
                match = re.search(r'(?:task\s+)?(?:ROO-\d+|#?\d+)\s+(.+)', text, re.IGNORECASE)
                if match:
                    submission_text = match.group(1)
                else:
                    submission_text = "Submitted via Slack"
            
            result = await client.submit_task(task_id, user_id, submission_text, submission_url)
            
            display_id = result.get("task_code") or task_id
            return f"Submitted! 📬 Task {display_id} is now pending approval.\n\nA reviewer will take a look soon. Legend! 🦘"
        
        elif action == "coworking_report":
            admin_details = await client.get_admin_details(user_id)
            if not self._can_generate_coworking_report_details(admin_details):
                return self._coworking_report_points_admin_denial()

            llm_intent = await self._extract_coworking_report_intent_with_llm(text, params)
            start_date, end_date, error = self._resolve_coworking_report_range_from_intent(text, params, llm_intent)
            if error:
                return error

            report = await client.get_coworking_report(user_id, start_date, end_date)
            comparison_range = self._resolve_coworking_comparison_range(
                text,
                params,
                llm_intent,
                start_date,
                end_date,
            )
            comparison_report = None
            if comparison_range:
                comparison_report = await client.get_coworking_report(
                    user_id,
                    comparison_range["start_date"],
                    comparison_range["end_date"],
                )

            context = self._build_coworking_analysis_context(
                text,
                report,
                comparison_report,
                comparison_range,
            )
            return await self._format_coworking_analysis_response(context)

        elif action == "admin_checkin_coworking":
            from ..slack_client import get_bot_user_id
            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            target_slack_ids = self._extract_coworking_checkin_targets(
                text,
                params,
                bot_id=bot_id,
            )
            if not target_slack_ids:
                return "Who should I check in? Mention one or more users, like `check @Jasmine @Lee in today`."

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "check people in for coworking")

            booking_date = self._resolve_coworking_booking_date(
                params,
                text,
                default_to_today=True,
            )

            if len(target_slack_ids) > 1:
                return await self._book_coworking_many_for_admin(
                    client=client,
                    admin_user_id=user_id,
                    target_user_ids=target_slack_ids,
                    booking_date=booking_date,
                    channel_id=channel_id,
                )

            target_user_id = target_slack_ids[0]
            try:
                return await self._book_coworking_with_intent(
                    client=client,
                    target_user_id=target_user_id,
                    requested_by_user_id=user_id,
                    booking_date=booking_date,
                    text=text,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    admin_checkin=True,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400:
                    return await self._format_admin_coworking_bad_request(
                        client=client,
                        target_user_id=target_user_id,
                        exc=exc,
                    )
                raise

        elif action == "check_coworking":
            check_date = params.get("date")
            days = params.get("days", 7)

            availability = await client.check_coworking(check_date, days, slack_user_id=user_id)
            
            if not availability:
                return "Couldn't check availability right now. Try again in a tick?"
            
            lines = ["🏢 **Coworking Availability:**\n"]
            for slot in availability[:7]:
                date_str = slot.get("date", "")
                avail = slot.get("available_slots", 0)
                cost = slot.get("cost_points", 1)
                emoji = "✅" if avail > 0 else "❌"
                lines.append(f"{emoji} **{date_str}**: {avail} slots ({cost} pt)")
            
            lines.append("\nBook a day with \"coworking book <date>\"")
            return "\n".join(lines)

        elif action == "book_coworking":
            from ..slack_client import get_bot_user_id
            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None
            target_slack_ids = self._extract_coworking_checkin_targets(
                text,
                params,
                bot_id=bot_id,
            )
            if target_slack_ids:
                admin_details = await client.get_admin_details(user_id)
                if not self._is_full_points_admin_details(admin_details):
                    return self._full_points_admin_denial(admin_details, "check people in for coworking")

                booking_date = self._resolve_coworking_booking_date(
                    params,
                    text,
                    default_to_today=True,
                )

                if len(target_slack_ids) > 1:
                    return await self._book_coworking_many_for_admin(
                        client=client,
                        admin_user_id=user_id,
                        target_user_ids=target_slack_ids,
                        booking_date=booking_date,
                        channel_id=channel_id,
                    )

                target_user_id = target_slack_ids[0]
                try:
                    return await self._book_coworking_with_intent(
                        client=client,
                        target_user_id=target_user_id,
                        requested_by_user_id=user_id,
                        booking_date=booking_date,
                        text=text,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        admin_checkin=True,
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400:
                        return await self._format_admin_coworking_bad_request(
                            client=client,
                            target_user_id=target_user_id,
                            exc=exc,
                        )
                    raise

            booking_date = self._resolve_coworking_booking_date(
                params,
                text,
                default_to_today=True,
            )
            return await self._book_coworking_with_intent(
                client=client,
                target_user_id=user_id,
                requested_by_user_id=user_id,
                booking_date=booking_date,
                text=text,
                channel_id=channel_id,
                thread_ts=thread_ts,
                admin_checkin=False,
            )
        
        elif action == "cancel_coworking":
            booking_date = params.get("date")
            booking_id = params.get("booking_id")
            
            # Normalize date aliases
            if str(booking_date or "").lower() in {"today", "tomorrow"}:
                from roo.utils import get_current_date
                today = get_current_date()

                if booking_date.lower() == "today":
                    booking_date = today.isoformat()
                elif booking_date.lower() == "tomorrow":
                    booking_date = (today + timedelta(days=1)).isoformat()
            
            if not booking_date and not booking_id:
                match = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                if match:
                    booking_date = match.group(1)
                else:
                    return "Which booking do you want to cancel? Give me the date (e.g., \"cancel coworking 2025-12-20\")"
            
            result = await client.cancel_coworking(user_id, booking_id, booking_date)
            refunded = result.get("refunded", False)
            refund_amount = result.get("refund_amount", 0)
            
            if refunded:
                return f"No worries! Cancelled your booking. {refund_amount} point refunded to your balance. 👍"
            else:
                return f"Booking cancelled. (No refund - cancellation after cutoff)"
        
        elif action == "list_rewards":
            rewards = await client.list_rewards(user_id)
            balance_summary = await self._get_points_balance_summary_for_rewards(client, user_id)
            message = self._format_rewards_catalog(rewards, balance_summary=balance_summary)
            return self._deliver_personal_points_message(
                recipient_user_id=user_id,
                requester_user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                private_message=message,
                action="list_rewards",
                private_ack="I've sent your personalised Roo Rewards list privately.",
            )
        
        elif action == "request_reward":
            reward_code = params.get("reward_code", "").upper()
            quantity = params.get("quantity", 1)
            
            if not reward_code:
                match = re.search(r'request\s+(\w+)', text, re.IGNORECASE)
                if match:
                    reward_code = match.group(1).upper()
                else:
                    return "Which reward would you like? Give me the code (e.g., \"reward request HOTDESK_DAY\")"
            
            result = await client.request_reward(
                user_id, reward_code, quantity,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts
            )
            
            return f"Request submitted! 🎉 Your request for **{reward_code}** is pending approval.\n\nAn admin will review it shortly."
        
        # =====================================================================
        # Admin Actions
        # =====================================================================

        elif action == "promote_points_admin":
            from ..slack_client import get_bot_user_id

            if not self._is_points_super_admin(user_id):
                return self._points_super_admin_denial()

            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            target_slack_id, target_error = self._extract_single_admin_target(
                text,
                bot_id=bot_id,
                action_label="promote them to Roo Points Admin",
            )
            if target_error:
                return target_error

            await self._ensure_user_exists(target_slack_id)
            result = await client.promote_points_admin(user_id, target_slack_id)

            error_message = str(result.get("error") or result.get("detail") or "").strip()
            promoted_target = client._clean_slack_id(
                result.get("target_slack_id") or target_slack_id
            )

            if result.get("already_admin") or (
                "already" in error_message.lower() and "admin" in error_message.lower()
            ):
                return f"<@{promoted_target}> is already a Roo Points Admin."
            if error_message:
                return f"Couldn't promote <@{promoted_target}> to Roo Points Admin: {error_message}"

            return f"✅ <@{promoted_target}> is now a Roo Points Admin."

        elif action == "revoke_points_admin":
            from ..slack_client import get_bot_user_id

            if not self._is_points_super_admin(user_id):
                return self._points_super_admin_denial()

            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            target_slack_id, target_error = self._extract_single_admin_target(
                text,
                bot_id=bot_id,
                action_label="remove their Roo Points Admin access",
            )
            if target_error:
                return target_error

            result = await client.revoke_points_admin(user_id, target_slack_id)

            error_message = str(result.get("error") or result.get("detail") or "").strip()
            revoked_target = client._clean_slack_id(
                result.get("target_slack_id") or target_slack_id
            )

            if result.get("already_revoked"):
                return f"<@{revoked_target}> already doesn't have Roo Points Admin access."
            if error_message:
                if "not a points admin" in error_message.lower():
                    return f"<@{revoked_target}> isn't a Points Admin right now."
                return f"Couldn't revoke Roo Points Admin access for <@{revoked_target}>: {error_message}"

            return f"✅ Removed Roo Points Admin access from <@{revoked_target}>."

        elif action == "set_points_admin_allowance":
            from ..slack_client import get_bot_user_id

            if not self._is_points_super_admin(user_id):
                return self._points_super_admin_denial()

            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            target_slack_id, target_error = self._extract_single_admin_target(
                text,
                bot_id=bot_id,
                action_label="change their weekly points allowance",
            )
            if target_error:
                return target_error

            weekly_allowance = self._extract_weekly_allowance(
                text,
                params.get("weekly_allowance"),
            )
            if weekly_allowance is None:
                return (
                    "What weekly allowance should I set? Give me a positive number of points, "
                    "like `set <@user> weekly points allowance to 150`."
                )
            if weekly_allowance <= 0:
                return "Weekly points allowance has to be a positive number."

            result = await client.set_points_admin_weekly_allowance(
                user_id,
                target_slack_id,
                weekly_allowance,
            )

            error_message = str(result.get("error") or result.get("detail") or "").strip()
            if error_message:
                if "not a points admin" in error_message.lower():
                    return f"<@{target_slack_id}> isn't a Points Admin yet, so I can't update their allowance."
                return f"Couldn't update <@{target_slack_id}>'s weekly allowance: {error_message}"

            effective_allowance = result.get("weekly_allowance", result.get("allowance", weekly_allowance))
            updated_target = client._clean_slack_id(result.get("target_slack_id") or target_slack_id)

            return (
                f"✅ Set <@{updated_target}>'s weekly points allowance to "
                f"{effective_allowance} points."
            )
        
        elif action == "create_task":
            # 1. Parameter Aliases
            title = params.get("task_title") or params.get("title") or params.get("submission_text")
            points = params.get("points")
            description = params.get("description", "")
            admin_details = await client.get_admin_details(user_id)

            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "create tasks")
            
            # Default portfolio logic: Param > Admin's Portfolio > "events"
            portfolio = params.get("portfolio")
            if not portfolio and admin_details:
                portfolio = admin_details.get("portfolio")
            
            if not portfolio:
                portfolio = "events" # Fallback if lookup fails

            due_date = params.get("due_date")
            assigned_to = params.get("assigned_to_user_id") or params.get("target_user")
            
            # 2. Validation
            if not title:
                return "G'day! I need a task title to create the task, mate. (e.g., \"create task 'Fix docs' 5 points\")"
            
            if not points:
                return "Crikey! You need to specify how many points this task is worth."
            
            # 3. Execution
            result = await client.create_task(
                admin_slack_id=user_id,
                title=title,
                points=int(points),
                description=description,
                portfolio=portfolio,
                due_date=due_date,
                assigned_to_user_id=assigned_to,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts
            )
            
            # 4. Response Handling
            if result.get("error") == "forbidden":
                return "Sorry mate, but I can't create tasks. You need to be a Points Admin for that! If you reckon you should have access, have a chat with the committee. 🤔"
            
            task_id = result.get("id")
            task_code = result.get("task_code")
            pts = result.get("points", points)
            port = result.get("portfolio", portfolio)
            
            assigned_msg = ""
            if result.get("assigned_to_user_id"):
                assigned_msg = f" and assigned to <@{result.get('assigned_to_user_id')}>"
            elif assigned_to:
                 assigned_msg = f" and assigned to <@{client._clean_slack_id(assigned_to)}>"
            
            task_ref = f"{task_code} / #{task_id}" if task_code else f"#{task_id}"
            return f"✅ Beauty! Created task **{title}** worth **{pts} points**{assigned_msg}. Task ID: {task_ref}"

        elif action == "edit_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            if not task_id:
                return "Which task do you want to edit? Give me the task ID or code."

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "edit tasks")

            current_task = await client.get_task(task_id)
            updates = self._extract_task_edit_updates(params, text)
            if not updates:
                return (
                    "Tell me what you want to change. "
                    "You can edit: title, description, points, portfolio, work domain, review flow, reviewer, "
                    "fallback reviewer, repo, estimate minutes, difficulty, due date, volunteer ready, "
                    "acceptance criteria, how to test, definition of done, and blocked reason."
                )

            result = await client.update_task(
                task_id,
                user_id,
                updates,
                expected_updated_at=current_task["updated_at"],
            )
            display_id = result.get("task_code") or f"#{result.get('id', task_id)}"
            changed_fields = ", ".join(sorted(updates.keys()))
            return f"Updated **{display_id}**. Changed: {changed_fields}."

        elif action == "cancel_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            if not task_id:
                return "Which task do you want to cancel? Give me the task ID or code."

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "cancel tasks")

            reason = params.get("reason", "")
            result = await client.cancel_task(task_id, user_id, reason=reason)
            display_id = result.get("task_code") or f"#{result.get('id', task_id)}"
            return f"Cancelled **{display_id}**. The task history is preserved, and it is no longer available for claim."
        
        elif action == "view_rate_card":
             card = await client.get_rate_card()
             if not card:
                 return "Rate card is empty or unavailable."
             
             lines = ["📋 **Standard Point Rates:**\n"]
             for item in card:
                 name = item.get("name", "Unknown")
                 pts = item.get("points", 0)
                 desc = item.get("description", "")
                 lines.append(f"• **{name}** ({pts} pts) - {desc}")
             
             return "\n".join(lines)
        
        elif action == "approve_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))

            if not task_id:
                return "Which task are you approving? Give me the task ID or code (e.g., \"task approve 42\" or \"task approve ROO-0042\")"

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "approve tasks")

            result = await client.approve_task(task_id, user_id)
            points_awarded = result.get("points_awarded", 0)
            task = result.get("task", {})
            display_id = task.get("task_code") or f"#{task.get('id', task_id)}"
            
            return f"Approved! ✅ Task {display_id} completed. {points_awarded} points awarded. 🎉"
        
        elif action == "reject_task":
            task_id = self._extract_task_identifier(text, params.get("task_id"))
            reason = params.get("reason", "")
            
            if not task_id:
                return "Which task are you rejecting? Give me the task ID or code."

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "reject tasks")
            
            result = await client.reject_task(task_id, user_id, reason)
            task = result.get("task", {})
            display_id = task.get("task_code") or f"#{task.get('id', task_id)}"
            
            return f"Task {display_id} rejected. The volunteer can resubmit if needed."
        
        elif action in ["deduct_points", "deduct"]:
            return "Sorry mate, I can only award points, not deduct them! 🚫"
            
        elif action in ["award_points", "award"]:
            from ..slack_client import get_bot_user_id
            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None

            management_action = self._resolve_points_admin_management_action(
                text,
                explicit_action=str(params.get("action", "") or "").lower().strip(),
            )
            if management_action:
                return await self._handle_points_action(
                    client=client,
                    action=management_action,
                    params=params,
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    skill=skill,
                )

            if self._is_self_directed_points_award(text, params, user_id, bot_id=bot_id):
                return await self._handle_points_action(
                    client=client,
                    action="request_points",
                    params=params,
                    text=text,
                    user_id=user_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    skill=skill,
                )

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "award points")

            # Early allowance check for award actions (before LLM/rate card lookup)
            if action in ["award_points", "award"]:
                try:
                    allowance_status = await client.get_admin_allowance(user_id)
                    if 'error' in allowance_status:
                        return "Sorry mate, you're not authorized to award points. Only Points Admins can do that. 🔒"
                    remaining = allowance_status.get('remaining', 0)
                    if remaining <= 0:
                        weekly_allowance = allowance_status.get('allowance', 0)
                        return (
                            f"You've used your full weekly allowance ({weekly_allowance} pts). "
                            "It resets on Monday. ⏰"
                        )
                    # Store for later use in messages
                    params['_admin_remaining_allowance'] = remaining
                    params['_admin_weekly_allowance'] = allowance_status.get('allowance', 0)
                except Exception as e:
                    print(f"⚠️ Allowance pre-check failed: {e}")
                    # Continue anyway - the actual award will fail if not authorized

            points = params.get("points", 0)
            reason = params.get("reason", "Manual adjustment")

            
            # Get Roo's bot ID to filter it from target users
            # Extract ALL user mentions from the text (excluding Roo)
            all_mentions = re.findall(r'<@([A-Z0-9]+)>', text)
            target_slack_ids = [uid for uid in all_mentions if uid != bot_id]
            
            # Fallback to params if no mentions found in text
            if not target_slack_ids:
                target_users_param = params.get("target_users", [])
                target_user_param = params.get("target_user", "")
                target_slack_id_param = params.get("target_slack_id", "")
                
                if target_users_param:
                    # Clean each ID
                    for tu in target_users_param:
                        cleaned = re.sub(r'[<@>]', '', str(tu))
                        if cleaned and cleaned != bot_id:
                            target_slack_ids.append(cleaned)
                elif target_user_param:
                    cleaned = re.sub(r'[<@>]', '', str(target_user_param))
                    if cleaned and cleaned != bot_id:
                        target_slack_ids.append(cleaned)
                elif target_slack_id_param:
                    cleaned = re.sub(r'[<@>]', '', str(target_slack_id_param))
                    if cleaned and cleaned != bot_id:
                        target_slack_ids.append(cleaned)
            
            # Validate we have valid targets (not prepositions)
            invalid_words = ["for", "to", "reason", "because", "points", "award", "give", "and"]
            target_slack_ids = [uid for uid in target_slack_ids if uid.lower() not in invalid_words]
            
            if not target_slack_ids:
                return "Who should I award points to? Mention them like @user (e.g., 'award 5 points to @Jasmine')"
            
            # Extract points amount if not in params
            # Extract points amount if not in params
            if not points:
                # 1. Try Regex fallback first (in case params missed explicit points)
                pts_match = re.search(r'(?<![a-zA-Z])([+-]?\d+)\s*(?:points?|pts?)?', text, re.IGNORECASE)
                if pts_match:
                    found_val = int(pts_match.group(1))
                    has_keyword = "point" in pts_match.group(0).lower() or "pts" in pts_match.group(0).lower()
                    if has_keyword or abs(found_val) < 1000:
                        points = found_val
            
            # 2. Smart Awards Logic (Rate Card) - Only if points still missing
            if not points:
                if reason:
                    print(f"🕵️ No points specified. Checking Rate Card for '{reason}'...")
                    try:
                        rate_card = await client.get_rate_card()
                        matches = []
                        reason_lower = reason.lower()
                        
                        for item in rate_card:
                            name = item.get("name", "")
                            desc = item.get("description", "") or ""
                            # Enhanced scoring
                            score = 0
                            if reason_lower in name.lower(): score += 50
                            if reason_lower in desc.lower(): score += 30
                            
                            seq_score = SequenceMatcher(None, reason_lower, name.lower()).ratio() * 100
                            if seq_score > 60: score += seq_score
                            
                            if score > 40:
                                matches.append((score, item))
                        
                        matches.sort(key=lambda x: x[0], reverse=True)
                        
                        if matches:
                            top_match = matches[0][1]
                            top_pts = top_match.get("points")
                            top_name = top_match.get("name")
                            cleanup_target = client._clean_slack_id(target_slack_ids[0]) if target_slack_ids else "the user"
                            
                            # Include remaining allowance context if available
                            remaining_info = ""
                            if params.get('_admin_remaining_allowance'):
                                remaining_info = f" (You have {params['_admin_remaining_allowance']} pts left this week.)"
                            
                            if len(matches) == 1 or matches[0][0] > 80:
                                return f"I found a match in the Rate Card: '{top_name}' is worth {top_pts} points. Should I award {top_pts} points to <@{cleanup_target}>?{remaining_info}"
                            else:
                                options = [f"'{m[1].get('name')}' ({m[1].get('points')} pts)" for m in matches[:3]]
                                return f"That sounds like it could be {options[0]} or {options[1] if len(options)>1 else ''}. Which one is it?{remaining_info}"
                                
                    except Exception as e:
                        print(f"⚠️ Smart award lookup failed: {e}")

                return "How many points should I award? (e.g., \"award @user 5 points\")"
            
            # Validate positive points
            if points < 0:
                return "Crikey! I can only award positive points. 🚫"
            
            # Award points to each target user
            results = []
            errors = []
            for target_id in target_slack_ids:
                try:
                    # Deduplication: Link Slack ID to existing email user if needed
                    try:
                        # Check if this Slack ID is already known
                        existing_user_id = await client.get_user_by_slack_id(target_id)
                        
                        if not existing_user_id:
                            # Not found by Slack ID -> Check if we know this user by email
                            from ..slack_client import get_user_info
                            u_info = get_user_info(target_id)
                            u_email = u_info.get("email")
                            
                            if u_email:
                                linked_user_id = await client.link_slack_user(target_id, u_email)
                                if linked_user_id:
                                    print(f"🔗 Linked Slack ID {target_id} to existing user {linked_user_id} via email {u_email}")
                    except Exception as e:
                        print(f"⚠️ User linking failed (continuing to award): {e}")

                    result = await client.award_points(user_id, target_id, int(points), reason)
                    new_balance = result.get("new_balance")
                    results.append({"user": target_id})
                    balance_line = (
                        f"\nYour new balance is {new_balance} points."
                        if new_balance is not None
                        else ""
                    )
                    try:
                        send_dm(
                            target_id,
                            (
                                f"You received {int(points)} Roo Points.\n"
                                f"Reason: {reason}{balance_line}"
                            ),
                        )
                    except Exception as exc:
                        print(
                            "⚠️ Award recipient DM failed "
                            f"exc_type={exc.__class__.__name__}"
                        )
                except Exception as e:
                    errors.append(
                        {
                            "user": target_id,
                            "error": self._redact_points_balance_error(str(e)),
                        }
                    )
            
            # Build response
            emoji = "🎉" if points > 0 else "📉"
            verb = "Awarded" if points > 0 else "Deducted"
            
            if len(results) == 1 and not errors:
                r = results[0]
                return f"{emoji} {verb} {abs(points)} points to <@{r['user']}>.\n\nReason: {reason}"
            
            lines = [f"{emoji} {verb} {abs(points)} points each!\n\nReason: {reason}\n"]
            for r in results:
                lines.append(f"✅ <@{r['user']}>: points awarded")
            for e in errors:
                lines.append(f"❌ <@{e['user']}>: {e['error']}")
            
            return "\n".join(lines)
        
        else:
            # Fall back to LLM for unrecognized actions
            return await self._execute_with_llm(skill, text, params, user_id, None)

    async def _execute_github_integration(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str]
    ) -> str:
        """Execute the GitHub Integration skill."""
        from roo.clients.mlai_backend import MLAIBackendUnavailableError
        
        # Get API client for GitHub token operations
        settings = get_settings()
        from roo.clients.mlai_backend import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY
        )
        
        # 1. Check for valid integration & handle errors
        domain = params.get("domain")
        action = params.get("action")
        try:
            integration = await api_client.get_integration(user_id, domain=domain)
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()

        if action == "reconnect":
            resolved_domain = domain
            connected_domains = integration.get("connected_domains", []) if integration else []
            if not resolved_domain:
                if len(connected_domains) == 1:
                    resolved_domain = connected_domains[0].get("domain")
                elif len(connected_domains) > 1:
                    domain_list = "\n".join(
                        f"  • `{item['domain']}` → `{item.get('github_repo', 'unknown')}`"
                        for item in connected_domains
                    )
                    return (
                        "You have multiple connected codebases. Tell me which one to reconnect GitHub for:\n\n"
                        f"{domain_list}\n\nTry: `@Roo reconnect to github for <domain>`"
                    )

            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=resolved_domain,
                github_repo=None,
                trigger="manual",
                pending_action="reconnect_github",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=(
                    f"Reconnect GitHub for {resolved_domain}"
                    if resolved_domain
                    else "Reconnect GitHub"
                ),
            )
            if reconnect_result is not None:
                return reconnect_result
            if resolved_domain:
                return f"GitHub is already connected for {resolved_domain}."
            return "GitHub is already connected."
        
        # Check for Expired Token or Other Errors (Same as content-factory)
        if integration and integration.get("error"):
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Re-connect GitHub",
                include_resume=True,
                resume_action="resume_scan",
                resume_value={"domain": domain} if domain else {},
            )
            if reconnect_result is not None:
                return reconnect_result
            return f"GitHub connection issue ({integration.get('error')})."

        if not integration:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Connect GitHub Account",
            )
            if reconnect_result is not None:
                return reconnect_result
            return "GitHub is already connected."

        # If a specific domain was requested, require domain-level connectivity.
        if domain and integration.get("needs_github_auth"):
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Connect GitHub for {domain}",
            )
            if reconnect_result is not None:
                return reconnect_result

        # 2. Determine Repo Name
        # When the user names a domain explicitly, prefer the domain-resolved repo.
        connected_domains = integration.get("connected_domains", [])
        repo_name, domain_info = self._resolve_content_factory_repo_name(
            integration,
            connected_domains,
            domain,
        )
        
        if not repo_name:
             reconnect_result = await self._request_github_reconnect(
                 api_client,
                 user_id=user_id,
                 domain=domain,
                 github_repo=None,
                 trigger="manual",
                 pending_action="scan",
                 channel_id=channel_id,
                 thread_ts=thread_ts,
                 button_label="Reconnect GitHub & Select Repo",
             )
             if reconnect_result is not None:
                 return reconnect_result
             return "Please select a repository to scan."

        scan_completed = bool(
            integration.get("scan_completed")
            or integration.get("content_research_ready")
            or integration.get("project_scanned")
            or (domain_info and domain_info.get("scanned"))
        )
        if (
            self._is_explicit_scan_request(text, params)
            and scan_completed
            and integration.get("recommended_next_action") != "scan"
            and not integration.get("has_updates")
        ):
            return self._build_existing_scan_confirmation(
                domain=domain,
                repo_name=repo_name,
                last_scanned=integration.get("last_scanned_at", "Never"),
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        # 3. Trigger Scan via Backend
        if channel_id:
            scan_msg = f"🔍 Requesting scan for `{repo_name}`..."
            post_message(channel_id, scan_msg, thread_ts=thread_ts)

        try:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain or 'this domain'}",
            )
            if reconnect_result is not None:
                return reconnect_result

            # Trigger via Backend Client
            scan_result = await api_client.trigger_repo_scan(
                user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain
            )
            
            if scan_result.get("error"):
                error_msg = scan_result.get("message", "Unknown error")
                # Check if auth-related — show reconnect button
                if scan_result.get("needs_github_auth"):
                    oauth_url = scan_result.get("oauth_url")
                    if not oauth_url:
                        try:
                            auth_resp = await api_client.get_github_auth_url(user_id, domain=domain)
                            oauth_url = auth_resp.get("auth_url")
                        except MLAIBackendUnavailableError:
                            return self._content_factory_backend_unavailable_message()
                        except Exception:
                            oauth_url = None
                    if oauth_url and channel_id:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"❌ Scan failed: {error_msg}"
                                }
                            },
                            {
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
                                            build_content_factory_identity_payload(
                                                requested_by_slack_user_id=requested_by_slack_user_id,
                                                effective_slack_user_id=effective_slack_user_id,
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                            )
                                        ),
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        post_message(channel_id, f"Scan failed: {error_msg}", thread_ts=thread_ts, blocks=blocks)
                        return "Please re-connect your GitHub account using the button above. 🔌"
                return f"❌ Scan failed: {error_msg}"

            # Backend might return status: 'started' or 'queued'
            return f"✅ Scan started for `{repo_name}`! I'll let you know when the backend updates."
            
        except Exception as e:
            print(f"GitHub Integration Scan Error: {e}")
            return f"Sorry mate, I had trouble triggering the scan: {str(e)}"
