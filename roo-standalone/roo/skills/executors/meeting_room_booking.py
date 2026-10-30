"""Meeting room booking skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from ...meeting_room_booking import CONFERENCE_ROOM_SLUG, MELBOURNE_TZ, MeetingRoomInputError, backend_error_message as meeting_room_backend_error_message, booking_preview, cancellation_selection, format_interval as format_meeting_room_interval, has_date_reference as has_meeting_room_date_reference, parse_backend_timestamp, room_selection_prompt, room_slug_from_text, resolve_availability_interval as resolve_meeting_room_availability_interval, resolve_interval as resolve_meeting_room_interval, resolve_local_date as resolve_meeting_room_date, supported_active_rooms
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from typing import Optional
from datetime import datetime
from ...meeting_room_availability import format_day_availability, resolve_search_duration
from ...meeting_room_clarifications import get_meeting_room_clarification_store, public_room_choice_prompt
from ...config import get_settings
import httpx
import re
from ...slack_client import send_dm


class MeetingRoomBookingExecutor(SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_meeting_room_booking(
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            slack_team_id=kwargs.get("slack_team_id"),
            request_message_ts=kwargs.get("current_message_ts"),
        )
        return result

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
                        "exactly 1 or 2 hours."
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
            conference_room = None
            if requested_room_slug == CONFERENCE_ROOM_SLUG and action in (
                "check_room_availability", "book_meeting_room",
            ):
                # Check eligibility before asking for missing booking details.
                # The backend owns the rule and returns only 'unavailable' on denial.
                access = await client.check_meeting_room_availability(
                    user_id,
                    room_slug=CONFERENCE_ROOM_SLUG,
                    date=datetime.now(MELBOURNE_TZ).date().isoformat(),
                    target_slack_user_id=target_slack_user_id,
                )
                conference_room = access.get("room") or {}
                if conference_room.get("slug") != CONFERENCE_ROOM_SLUG:
                    raise MeetingRoomInputError(
                        "invalid_response",
                        "I could not verify that meeting room. Ask Roo to start again.",
                    )
            if action == "check_room_availability":
                rooms = (
                    [conference_room] if conference_room else
                    supported_active_rooms(await client.list_meeting_rooms())
                )
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
                    duration_half_hours = resolve_search_duration(text, params)
                    for room in selected_rooms:
                        availability_results.append(
                            await client.check_meeting_room_availability(
                                user_id,
                                room_slug=room["slug"],
                                date=local_date.isoformat(),
                                target_slack_user_id=target_slack_user_id,
                            )
                        )
                    message = format_day_availability(
                        availability_results, local_date, duration_half_hours
                    )
                    return self._deliver_meeting_room_response(
                        user_id=user_id,
                        channel_id=channel_id,
                        message=message,
                        action=action,
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
                rooms = (
                    [conference_room] if conference_room else
                    supported_active_rooms(await client.list_meeting_rooms())
                )
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


