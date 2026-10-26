"""Mlai points skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from typing import Any, Optional
from .constants import COWORKING_REPORT_ROLES
from ...coworking_dates import CoworkingDateError, resolve_coworking_date
from .constants import FOUNDER_ACCOUNT_LINK_MAX_FUTURE_SECONDS
from .constants import FULL_POINTS_ADMIN_ROLES
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError, validate_coworking_booking_result
from uuid import NAMESPACE_URL, uuid4, uuid5
from ...coworking_messages import NO_FOOD_REMINDER
from .constants import POINTS_SUPER_ADMIN_SLACK_ID
from .constants import ROO_TOPUP_PACKS
from .constants import ROO_TOPUP_PACK_BY_POINTS
from difflib import SequenceMatcher
from ..loader import Skill
import asyncio
import hashlib
from ...points_awards import ChatAwardRetryableError
from ...coworking_booking_intents import build_coworking_operation_id, coworking_failure_code, deliver_coworking_booking_notification, get_coworking_intent_store, is_retryable_coworking_exception
from ...points_flex import build_points_flex_delete_blocks, build_points_flex_preview_blocks, get_points_flex_store, issue_points_flex_confirmation, issue_points_flex_deletion, parse_lifetime_earned
from ...points_request_approval import build_points_request_metadata, build_points_request_record, remember_points_request_summary
import calendar
from ...llm import chat
from datetime import date, datetime, timedelta, timezone
from ...backend_identity import get_backend_actor_context
from ...slack_client import get_bot_user_id, post_ephemeral, post_message, send_dm, upload_file
from ...config import get_settings
import httpx
import json
from urllib.parse import parse_qsl, urlsplit
import re
from ...coworking_charts import render_coworking_chart
from .backend_errors import BackendErrors
from .prompt import PromptExecutor


class MlaiPointsExecutor(BackendErrors, PromptExecutor):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
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
            verified_bridge_context=kwargs.get("verified_bridge_context"),
        )
        return result

    @staticmethod
    def _points_backend_unavailable_message(action: Optional[str] = None) -> str:
        if action in {"view_rate_card", "estimate_points"}:
            return "I couldn't load the rate card just now. Please try again in a moment."

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


    async def _ensure_user_exists(self, user_id: str) -> None:
        """
        Ensure a user exists in the mlai-backend database.
        Uses the /api/v1/users/slack-user/ endpoint which handles both
        new user creation and returning existing users.

        This prevents errors when new users interact with backend-dependent features.
        """
        from ...clients.mlai_backend import MLAIBackendClient
        from ...slack_client import get_user_info

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


    async def _execute_mlai_points(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        request_id: Optional[str] = None,
        verified_bridge_context=None,
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
            if verified_bridge_context is not None:
                handle_kwargs["verified_bridge_context"] = verified_bridge_context
            return await self._handle_points_action(
                **handle_kwargs
            )
            
        except ChatAwardRetryableError:
            raise
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


    @staticmethod
    def _url_origin_parts(raw_url: str) -> Optional[tuple[str, str, int]]:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except (TypeError, ValueError):
            return None
        hostname = str(parsed.hostname or "")
        if (
            not hostname
            or hostname.endswith(".")
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in str(raw_url)
        ):
            return None
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError:
            return None
        scheme = parsed.scheme.lower()
        if scheme == "https":
            return scheme, hostname.lower(), port or 443
        if scheme == "http":
            return scheme, hostname.lower(), port or 80
        return None


    @classmethod
    def _is_safe_founder_account_link_url(cls, link_url: str, *, settings: Any) -> bool:
        if not link_url or "\\" in link_url or any(ord(char) < 32 for char in link_url):
            return False
        try:
            parsed = urlsplit(link_url)
            port = parsed.port
            query = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except (TypeError, ValueError):
            return False
        hostname = str(parsed.hostname or "")
        if (
            not hostname
            or hostname.endswith(".")
            or parsed.username
            or parsed.password
            or parsed.path not in {"/founder-tools/link-roo", "/my-startup/link-roo"}
            or parsed.fragment
            or port is None and parsed.netloc.endswith(":")
            or len(query) != 1
            or query[0][0] != "token"
            or not query[0][1]
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", query[0][1])
        ):
            return False
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError:
            return False

        scheme = parsed.scheme.lower()
        effective_port = port or (443 if scheme == "https" else 80 if scheme == "http" else -1)
        requested_origin = (scheme, hostname.lower(), effective_port)
        allowed_origins = {
            origin
            for configured_origin in (
                getattr(settings, "founder_tools_link_origins", frozenset()) or frozenset()
            )
            if (origin := cls._url_origin_parts(str(configured_origin))) is not None
        }
        if requested_origin not in allowed_origins:
            return False

        is_localhost = requested_origin[1] in {"localhost", "127.0.0.1", "::1"}
        if is_localhost:
            return not bool(getattr(settings, "is_production", False)) and scheme == "http"
        return (
            scheme == "https"
            and effective_port == 443
        )


    @staticmethod
    def _founder_account_link_expiry_timestamp(raw_expires_at: Any) -> Optional[int]:
        try:
            expires_at = datetime.fromisoformat(
                str(raw_expires_at or "").strip().replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            return None
        expires_at = expires_at.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if (
            expires_at <= now
            or expires_at > now + timedelta(seconds=FOUNDER_ACCOUNT_LINK_MAX_FUTURE_SECONDS)
        ):
            return None
        return int(expires_at.timestamp())


    @staticmethod
    def _is_direct_message_channel(channel_id: Optional[str]) -> bool:
        return bool(re.fullmatch(r"D[A-Z0-9]+", str(channel_id or "").strip()))


    @staticmethod
    def _slack_delivery_succeeded(response: Any) -> bool:
        """Accept Slack SDK responses while rejecting malformed delivery results."""
        if response is None:
            return False
        try:
            return response.get("ok") is True
        except (AttributeError, TypeError):
            return False


    def _founder_account_link_button_response(
        self,
        *,
        link_url: str,
        expires_at_timestamp: int,
        user_id: str,
        channel_id: Optional[str],
    ) -> dict:
        message = (
            "Connect Roo to the Founder Tools account you use for Monthly Updates. "
            "Your Roo Points, bookings, and points history will stay on this Slack account."
        )
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Link Founder Tools account",
                            "emoji": True,
                        },
                        "url": link_url,
                        "action_id": "roo_link_founder_account",
                        "style": "primary",
                        "accessibility_label": (
                            "Link this Roo Slack account to your Founder Tools account"
                        ),
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "This private, one-time Founder Tools link expires "
                            f"<!date^{expires_at_timestamp}^{{date_short_pretty}} at {{time}}|at its stated expiry time>. "
                            "Only continue if you requested it."
                        ),
                    }
                ],
            },
        ]
        response_data = {
            "action": "link_founder_account",
            "delivery": "direct_message",
        }
        if not self._is_direct_message_channel(channel_id):
            try:
                dm_response = send_dm(user_id, message, blocks=blocks)
            except Exception as exc:
                print(
                    "Founder Tools account-link DM failed "
                    f"exc_type={exc.__class__.__name__}"
                )
                dm_response = None
            if not self._slack_delivery_succeeded(dm_response):
                return {
                    "message": (
                        f"I couldn't confirm a private Slack DM was delivered to <@{user_id}>. "
                        "Please DM Roo `link` and I'll create a fresh Founder Tools "
                        "account-link button there."
                    ),
                    "data": {
                        **response_data,
                        "delivery_failed": True,
                    },
                    "suppress_post": False,
                }
            return {
                "message": (
                    f"I sent <@{user_id}> a private Founder Tools account-link button. "
                    "Check your DMs."
                ),
                "data": response_data,
                "suppress_post": False,
            }
        return {
            "message": message,
            "blocks": blocks,
            "data": response_data,
        }


    @staticmethod
    def _founder_account_link_status_response(
        *,
        message: str,
        user_id: str,
        channel_id: Optional[str],
    ) -> Any:
        if MlaiPointsExecutor._is_direct_message_channel(channel_id):
            return message

        try:
            dm_response = send_dm(user_id, message)
        except Exception as exc:
            print(
                "Founder Tools account-link status DM failed "
                f"exc_type={exc.__class__.__name__}"
            )
            dm_response = None
        if not MlaiPointsExecutor._slack_delivery_succeeded(dm_response):
            return {
                "message": (
                    f"I couldn't confirm a private Slack DM was delivered to <@{user_id}>. "
                    "Please DM Roo `link` to check your Founder Tools account-link status."
                ),
                "data": {
                    "action": "link_founder_account",
                    "delivery": "direct_message",
                    "delivery_failed": True,
                },
                "suppress_post": False,
            }
        return {
            "message": (
                f"I sent <@{user_id}> a private Founder Tools account-link status. "
                "Check your DMs."
            ),
            "data": {
                "action": "link_founder_account",
                "delivery": "direct_message",
            },
            "suppress_post": False,
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


    def _score_rate_card_rows(self, query: str, rate_card) -> list[tuple[float, dict]]:
        """Score rate-card rows the same way smart awards do.

        A row scores +50 when the query is contained in its name, +30 when it
        is contained in the description, and the SequenceMatcher ratio against
        the name (0-100) when that ratio is above 60. Rows stay in their
        incoming order when scores tie.
        """
        query_lower = str(query or "").lower()
        scored: list[tuple[float, dict]] = []
        for item in rate_card:
            name = item.get("name", "")
            desc = item.get("description", "") or ""
            score = 0
            if query_lower in name.lower():
                score += 50
            if query_lower in desc.lower():
                score += 30
            seq_score = SequenceMatcher(None, query_lower, name.lower()).ratio() * 100
            if seq_score > 60:
                score += seq_score
            scored.append((score, item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return scored


    def _is_explicit_rate_card_request(self, text: str) -> bool:
        """True when the user asked to see the catalog itself."""
        normalized = " ".join(str(text or "").lower().split())
        if "standard point rates" in normalized or "all the ways to earn" in normalized:
            return True
        return bool(re.search(
            r"\b(?:show|list|view|display)\b.{0,40}\brate card\b",
            normalized,
        ))


    def _has_points_estimate_marker(self, normalized: str) -> bool:
        if re.search(r"\bestimate\b", normalized):
            return True
        if "how many roo points" in normalized:
            return True
        return bool(re.search(r"\bhow many points is .+ worth\b", normalized))


    def _substantive_task_description(self, candidate: str) -> str:
        cleaned = " ".join(str(candidate or "").split()).strip(" \t\"'`:-–—")
        if not cleaned:
            return ""
        if re.search(r"\b(?:do i have|have i got|is my balance|my balance)\b", cleaned.lower()):
            return ""
        filler = {
            "this", "that", "it", "the", "a", "an", "task", "for", "please",
            "roo", "points", "point", "pts", "worth", "new", "work", "my",
        }
        meaningful = [
            word for word in re.findall(r"[a-z0-9][a-z0-9'+-]*", cleaned.lower())
            if word not in filler and len(word) > 2
        ]
        if not meaningful:
            return ""
        return cleaned


    def _extract_points_estimate_description(self, text: str) -> str:
        """Pull the work being priced out of an estimate request."""
        raw = re.sub(r"<@[A-Z0-9]+>", " ", str(text or ""), flags=re.IGNORECASE)
        raw = re.sub(r"(?i)@roo\b", " ", raw)
        raw = raw.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        normalized = " ".join(raw.split())

        for quote in reversed(re.findall(r'"([^"]+)"', normalized)):
            substantive = self._substantive_task_description(quote)
            if substantive:
                return substantive

        worth = re.search(r"(?i)\bhow many points is (.+?) worth\b", normalized)
        if worth:
            substantive = self._substantive_task_description(worth.group(1))
            if substantive:
                return substantive

        stripped = re.sub(
            r"(?i)^(?:please\s+)?(?:"
            r"estimate(?:\s+how\s+many(?:\s+roo)?\s+points)?"
            r"|how\s+many\s+roo\s+points"
            r"|how\s+many\s+points\s+is\s+this(?:\s+task)?\s+worth"
            r")"
            r"(?:\s+for(?:\s+this)?)?"
            r"\s*[:\-–—]?\s*",
            "",
            normalized,
        ).strip()
        return self._substantive_task_description(stripped)


    def _points_estimate_task_description(self, text: str, params: Optional[dict]) -> str:
        explicit = str((params or {}).get("task_description") or "").strip()
        if explicit:
            return explicit
        return self._extract_points_estimate_description(text)


    def _is_points_estimate_request(self, text: str, params: Optional[dict] = None) -> bool:
        """Estimate wording that should not dump the rate-card catalog."""
        if self._is_explicit_rate_card_request(text):
            return False
        normalized = " ".join(str(text or "").lower().split())
        if not self._has_points_estimate_marker(normalized):
            return False
        if self._points_estimate_task_description(text, params):
            return True
        # The work can be missing; estimate_points then asks what it is.
        return bool(re.search(
            r"\bhow many points is (?:this|that|it)(?:\s+task)? worth\b",
            normalized,
        ))


    def _points_estimate_why(self, description: str, item: dict) -> str:
        name = item.get("name") or "that rate"
        points = item.get("points", 0)
        query = description.lower()
        item_name = str(item.get("name") or "").lower()
        item_desc = str(item.get("description") or "").lower()
        if query and query in item_name:
            return f"That matches the '{name}' rate, which is worth {points} points."
        if query and item_desc and query in item_desc:
            return f"That matches the '{name}' description, which is worth {points} points."
        return (
            f"'{name}' is the closest rate-card row to this work, "
            f"so {points} points is the recommendation."
        )


    def _format_points_estimate(self, description: str, card: list) -> str:
        # Use the smart-award threshold rather than price unrelated work from
        # whichever name happens to have the highest weak text similarity.
        closest = [
            item
            for score, item in self._score_rate_card_rows(description, card)
            if score > 40
        ][:3]
        if not closest:
            return (
                "I couldn't find a close rate-card match for that work. "
                "Describe it more specifically or name a comparable activity."
            )
        best = closest[0]
        points = best.get("points", 0)
        lines = [
            f"I'd recommend **{points} points** for this.",
            "",
            "Closest matches:",
        ]
        for item in closest:
            lines.append(
                f"• **{item.get('name', 'Unknown')}** ({item.get('points', 0)} pts) - "
                f"{item.get('description', '')}"
            )
        lines.append("")
        lines.append(self._points_estimate_why(description, best))
        return "\n".join(lines)


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
        # Prompt routing keeps sending estimate requests to the catalog dump.
        if action == "view_rate_card" and self._is_points_estimate_request(text, params):
            action = "estimate_points"
            params["action"] = action
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


    def _can_generate_coworking_report_in_channel(self, user_id: str, channel_id: str) -> bool:
        """Use only verified Slack scope, never parameters inferred from text."""
        settings = get_settings()
        actor = get_backend_actor_context()
        team = str(getattr(settings, "COWORKING_REPORT_SLACK_TEAM_ID", "") or "").strip()
        channel = str(getattr(settings, "COWORKING_REPORT_SLACK_CHANNEL_ID", "") or "").strip()
        return bool(
            getattr(settings, "ROO_SURFACE", "public") == "public"
            and re.fullmatch(r"T[A-Z0-9]+", team)
            and re.fullmatch(r"[CG][A-Z0-9]+", channel)
            and re.fullmatch(r"[UW][A-Z0-9]+", user_id)
            and actor
            and actor.event_id
            and actor.acting_slack_user_id == user_id
            and actor.slack_team_id == team
            and actor.slack_channel_id == channel_id == channel
        )


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
        client_msg_id: Optional[str] = None,
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
            delivery_options = {}
            if client_msg_id:
                delivery_options = {
                    "client_msg_id": client_msg_id,
                    "raise_on_error": True,
                }
            response = send_dm(
                recipient_user_id,
                private_message,
                **delivery_options,
            )
            dm_delivered = bool(response and response.get("ok"))
        except Exception as exc:
            error_response = getattr(exc, "response", None)
            try:
                duplicate_message = bool(
                    client_msg_id
                    and error_response is not None
                    and error_response.get("error") == "duplicate_message"
                )
            except (AttributeError, TypeError):
                duplicate_message = False
            if duplicate_message:
                dm_delivered = True
            else:
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
            from ...utils import get_current_date

            today = get_current_date()
            current_sunday_start = today - timedelta(days=(today.weekday() + 1) % 7)
            start = current_sunday_start - timedelta(days=7)
            end = current_sunday_start - timedelta(days=1)
            return start.isoformat(), end.isoformat(), None

        if re.search(r"\bthis\s+week\b", text_lower):
            from ...utils import get_current_date

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
            from ...utils import get_current_date

            today = get_current_date()
            start = self._shift_months(today, -months) + timedelta(days=1)
            return start.isoformat(), today.isoformat(), None

        if re.search(r"\blast\s+month\b", text_lower):
            from ...utils import get_current_date

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
        chart_requested = bool(re.search(r"\b(?:charts?|graphs?|plots?|trend\s*lines?)\b", text_lower))
        return {
            "chart_requested": chart_requested,
            "chart_disabled": bool(re.search(
                r"\b(?:text[- ]only|(?:no|without)\s+(?:a\s+)?(?:charts?|graphs?|images?))\b", text_lower,
            )),
            "comparison_requested": bool(
                re.search(
                    r"\b(?:compare|compared|comparison|versus|vs\.?|prior|previous|week before|month before)\b",
                    text_lower,
                )
            ),
            "detail_requested": bool(re.search(r"\b(?:detail|detailed|breakdown|table)\b", text_lower)),
            "raw_requested": bool(
                re.search(r"\b(?:raw|table)\b", text_lower)
                or (not chart_requested and re.search(r"\b(?:daily|day by day|each day)\b", text_lower))
            ),
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
        """Use the configured router model to extract flexible report hints."""
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


    async def _upload_coworking_chart(
        self,
        report: dict,
        *,
        channel_id: str,
        thread_ts: Optional[str],
    ) -> str:
        """Attach the aggregate chart; keep the text report usable on any failure."""
        from ...utils import get_current_date

        try:
            png = await asyncio.to_thread(render_coworking_chart, report, today=get_current_date())
        except Exception as exc:
            print(f"⚠️ Coworking chart rendering failed: error_type={exc.__class__.__name__}")
            return "I couldn't render the daily booking chart this time; the text report is still available above."

        report_range = report["range"]
        start, end = report_range["start_date"], report_range["end_date"]
        try:
            response = await asyncio.to_thread(
                upload_file,
                channel=channel_id,
                content=png,
                filename=f"coworking-usage-{start}-to-{end}.png",
                title=f"Coworking usage · {start} to {end}",
                thread_ts=thread_ts,
            )
            error = response.get("error", "") if not response.get("ok") else ""
            if response.get("ok"):
                return "📈 Daily booking chart attached. Trend: trailing 7-day average where a complete window is available."
        except Exception as exc:
            response = getattr(exc, "response", None)
            error = response.get("error", "") if response is not None else ""
            print(f"⚠️ Coworking chart upload failed: error_type={exc.__class__.__name__}")
        if error == "missing_scope":
            return "I couldn't attach the chart: Roo needs Slack's `files:write` permission. The text report is still available above."
        return "I couldn't attach the chart to Slack this time; the text report is still available above."


    def _resolve_coworking_booking_date(
        self,
        params: dict,
        text: str,
        *,
        default_to_today: bool,
    ) -> Optional[str]:
        """Normalise coworking dates using the configured local calendar."""
        from roo.utils import get_current_date

        return resolve_coworking_date(
            params.get("date"),
            text,
            today=get_current_date(),
            default_to_today=default_to_today,
        )


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
                f"on **{booking_date}** queued, processing, or completed. I won't create "
                "another booking, and Roo will deliver the confirmation when it is ready."
            )
        return (
            f"I already have your coworking booking request for **{booking_date}** "
            "queued, processing, or completed. I won't create another booking, and Roo "
            "will deliver the confirmation when it is ready."
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
        discount_applied: Optional[bool] = None,
        include_balance: bool = True,
        founder_tools_explicitly_linked: Optional[bool] = None,
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
                f"Cost: {cost} {point_word}{balance_line}\n\n"
                f"{NO_FOOD_REMINDER}"
            )

        balance_line = ""
        if include_balance and new_balance is not None:
            balance_line = f" (Balance remaining: {new_balance} points)"

        message = (
            f"You beauty! 🎉\n\n"
            f"Booked you in for **{booking_date}** at the coworking space.\n"
            f"Cost: {cost} {point_word}{balance_line}\n\n"
            f"See you there, legend!\n\n"
            f"{NO_FOOD_REMINDER}"
        )

        if discount_applied is False and cost > 0:
            message += (
                "\n\n💡 Startup founders may qualify for 4-point coworking after "
                "approving an eligible monthly update and sharing it publicly or "
                "with the MLAI community. Private approvals earn 5 Roo points and "
                "do not unlock discounts. Share yours here: "
                "https://mlai.au/platform/login?app=founder-tools&next=/founder-tools"
            )

            if founder_tools_explicitly_linked is False:
                message += (
                    "\n\nAlready submitted using a different MLAI account? Type "
                    "`@Roo link` to securely link your accounts for future eligible "
                    "bookings."
                )

        return message


    async def _deliver_coworking_booking_success(
        self,
        *,
        client,
        backend_result: dict,
        booking_date: str,
        target_user_id: str,
        requested_by_user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        admin_checkin: bool,
        client_msg_id: Optional[str] = None,
    ) -> dict:
        """Render and privately deliver the backend's complete booking result."""
        backend_result = validate_coworking_booking_result(
            backend_result,
            expected_date=booking_date,
        )
        current_status = backend_result.get("operation_booking_current_status")
        if current_status in {"cancelled", "deleted"}:
            state_text = "cancelled" if current_status == "cancelled" else "removed"
            private_message = (
                f"I recovered an earlier coworking request for **{booking_date}**. "
                f"It did commit, but that booking has since been {state_text}. "
                "I did not create a new booking or charge any Roo Points during this retry."
            )
            public_message = (
                f"I recovered the earlier coworking request for <@{target_user_id}> "
                f"on **{booking_date}**, but that booking has since been {state_text}. "
                "No new booking or Roo Points charge was created by this retry."
                if admin_checkin
                else (
                    f"I recovered your earlier coworking request for **{booking_date}**, "
                    f"but that booking has since been {state_text}. No new booking or "
                    "Roo Points charge was created by this retry."
                )
            )
            return self._deliver_personal_points_message(
                recipient_user_id=target_user_id,
                requester_user_id=requested_by_user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                private_message=private_message,
                public_message=public_message,
                action="book_coworking_replay_inactive",
                client_msg_id=client_msg_id,
            )
        cost = backend_result["points_cost"]
        discount_applied = (
            None
            if backend_result.get("pricing_state_historical_unknown") is True
            else backend_result["monthly_update_discount_applied"]
        )
        raw_explicit_link_state = backend_result.get(
            "founder_tools_explicitly_linked"
        )
        founder_tools_explicitly_linked = (
            None
            if backend_result.get("founder_tools_link_state_historical_unknown") is True
            else (
                raw_explicit_link_state
                if isinstance(raw_explicit_link_state, bool)
                else None
            )
        )

        new_balance = None
        try:
            balance_data = await client.get_balance(target_user_id)
            new_balance = balance_data.get("balance", 0)
        except Exception as exc:
            print(
                "🏢 coworking_confirmation_balance_lookup_failed "
                f"exc_type={exc.__class__.__name__}"
            )

        private_message = self._format_coworking_booking_success(
            booking_date=booking_date,
            target_user_id=target_user_id,
            cost=cost,
            new_balance=new_balance,
            admin_checkin=False,
            discount_applied=discount_applied,
            founder_tools_explicitly_linked=founder_tools_explicitly_linked,
        )
        public_message = self._format_coworking_booking_success(
            booking_date=booking_date,
            target_user_id=target_user_id,
            cost=cost,
            new_balance=None,
            admin_checkin=admin_checkin,
            discount_applied=discount_applied,
            include_balance=False,
            # Cross-account guidance belongs in the member's private DM.
            founder_tools_explicitly_linked=None,
        )
        return self._deliver_personal_points_message(
            recipient_user_id=target_user_id,
            requester_user_id=requested_by_user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            private_message=private_message,
            public_message=public_message,
            action="book_coworking",
            client_msg_id=client_msg_id,
        )


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
        client_msg_id: Optional[str] = None,
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
            client_msg_id=client_msg_id,
        )


    def _format_admin_coworking_batch_success(
        self,
        *,
        booking_date: str,
        batch_result: dict,
    ) -> str:
        target_count = int(batch_result.get("target_count") or 0)
        if batch_result.get("operation_replayed") is True:
            current_statuses = [
                str(row.get("booking", {}).get("operation_booking_current_status") or "")
                for row in batch_result.get("results", [])
                if isinstance(row, dict) and isinstance(row.get("booking"), dict)
            ]
            active_count = sum(status == "booked" for status in current_statuses)
            inactive_count = len(current_statuses) - active_count
            return (
                f"I recovered the earlier coworking batch for **{booking_date}**. "
                f"Current state: {active_count} active, {inactive_count} cancelled or removed. "
                "No new bookings or Roo Points charges were created by this retry; "
                "each member will receive the accurate current state privately."
            )
        return (
            "You beauty! 🎉\n\n"
            f"Processed **{target_count}** coworking check-ins for **{booking_date}**. "
            "Each member will receive their booking and Roo Points details privately; "
            "any failed delivery is queued for retry.\n\n"
            "Admin Roo Points were not charged."
            f"\n\n{NO_FOOD_REMINDER}"
        )


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
        thread_ts: Optional[str],
    ) -> Any:
        try:
            store = get_coworking_intent_store()
            intent = store.record_batch_intent(
                admin_slack_user_id=admin_user_id,
                target_slack_user_ids=target_user_ids,
                booking_date=booking_date,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            leased_intent = store.reserve_batch_for_processing(
                int(intent["id"]), owner=f"roo-sync-batch-{uuid4().hex}"
            )
        except Exception as exc:
            print("🏢 coworking_batch_intent_persist_failed " f"exc_type={exc.__class__.__name__}")
            return (
                "I couldn't safely queue that multi-person coworking check-in, "
                "so I didn't send it to MLAI backend. Please try again in a moment."
            )
        if not leased_intent:
            if str(intent.get("status") or "") == "batch_confirmed":
                return (
                    f"That coworking batch for **{booking_date}** was already processed. "
                    "No one was charged twice."
                )
            return (
                f"That coworking batch for **{booking_date}** is already queued or processing. "
                "Roo will retry it safely and send each member their details privately."
            )
        owner = str(leased_intent.get("locked_by") or "")
        try:
            result = await client.book_coworking_many(
                admin_slack_user_id=admin_user_id,
                target_slack_user_ids=target_user_ids,
                booking_date=booking_date,
                slack_channel_id=channel_id,
                operation_id=build_coworking_operation_id(
                    str(leased_intent["idempotency_key"])
                ),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                store.mark_batch_blocked(
                    int(leased_intent["id"]),
                    owner=owner,
                    error=coworking_failure_code(exc),
                    notification_required=False,
                )
                return self._format_admin_coworking_batch_bad_request(
                    booking_date=booking_date,
                    exc=exc,
                )
            if exc.response.status_code == 403:
                store.mark_batch_blocked(
                    int(leased_intent["id"]),
                    owner=owner,
                    error=coworking_failure_code(exc),
                    notification_required=False,
                )
                return self._full_points_admin_denial(None, "check people in for coworking")
            if is_retryable_coworking_exception(exc):
                store.mark_batch_retryable_failure(
                    int(leased_intent["id"]), owner=owner, error=coworking_failure_code(exc)
                )
                return (
                    "MLAI backend didn't confirm that multi-person coworking check-in cleanly. "
                    "I've queued the same atomic batch for automatic retry; existing bookings "
                    "won't be charged twice."
                )
            store.mark_batch_blocked(
                int(leased_intent["id"]), owner=owner, error=coworking_failure_code(exc)
            )
            raise
        except Exception as exc:
            if is_retryable_coworking_exception(exc):
                store.mark_batch_retryable_failure(
                    int(leased_intent["id"]), owner=owner, error=coworking_failure_code(exc)
                )
                return (
                    "MLAI backend didn't confirm that multi-person coworking check-in cleanly. "
                    "I've queued the same atomic batch for automatic retry; existing bookings "
                    "won't be charged twice."
                )
            store.mark_batch_blocked(
                int(leased_intent["id"]), owner=owner, error=coworking_failure_code(exc)
            )
            raise

        confirmed = store.mark_batch_confirmed(
            int(leased_intent["id"]), owner=owner, backend_result=result
        )
        if confirmed is None:
            return (
                f"That coworking batch for **{booking_date}** is already being reconciled. "
                "Roo will send each member their details privately."
            )
        for child_intent_id in confirmed["child_intent_ids"]:
            notification = store.reserve_notification(
                int(child_intent_id), owner=f"roo-sync-batch-notification-{uuid4().hex}"
            )
            if notification is not None:
                await deliver_coworking_booking_notification(
                    notification, store=store, client=client, executor=self,
                    post_public_message=False,
                )

        return self._format_admin_coworking_batch_success(
            booking_date=str(result["date"]),
            batch_result=result,
        )


    async def _book_coworking_with_intent(
        self,
        *,
        client,
        target_user_id: str,
        requested_by_user_id: str,
        booking_date: str,
        text: Optional[str] = None,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        admin_checkin: bool,
    ) -> Any:
        # Keep caller compatibility while deliberately discarding raw Slack text.
        del text
        try:
            store = get_coworking_intent_store()
            intent = store.record_intent(
                slack_user_id=target_user_id,
                requested_by_slack_id=requested_by_user_id,
                booking_date=booking_date,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            leased_intent = store.reserve_for_processing(
                int(intent["id"]),
                owner=f"roo-sync-{uuid4().hex}",
            )
        except Exception as exc:
            print(
                "🏢 coworking_intent_persist_failed "
                f"exc_type={exc.__class__.__name__}"
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

        mutation_owner = str(leased_intent.get("locked_by") or "")
        if not mutation_owner:
            return self._coworking_booking_already_queued_message(
                booking_date=booking_date,
                target_user_id=target_user_id,
                admin_checkin=admin_checkin,
            )
        try:
            result = await client.book_coworking(
                target_user_id,
                booking_date,
                channel_id,
                operation_id=build_coworking_operation_id(
                    str(leased_intent["idempotency_key"])
                ),
            )
            result = validate_coworking_booking_result(
                result,
                expected_date=booking_date,
            )
            confirmed = store.mark_confirmed(
                int(leased_intent["id"]),
                owner=mutation_owner,
                backend_result=result,
                notification_required=True,
            )
            if confirmed is None:
                return self._coworking_booking_already_queued_message(
                    booking_date=booking_date,
                    target_user_id=target_user_id,
                    admin_checkin=admin_checkin,
                )
        except Exception as exc:
            error = coworking_failure_code(exc)
            if is_retryable_coworking_exception(exc):
                updated = store.mark_retryable_failure(
                    int(leased_intent["id"]),
                    owner=mutation_owner,
                    error=error,
                )
                if updated is None:
                    return self._coworking_booking_already_queued_message(
                        booking_date=booking_date,
                        target_user_id=target_user_id,
                        admin_checkin=admin_checkin,
                    )
                return self._coworking_booking_queued_for_retry_message(
                    booking_date=booking_date,
                    target_user_id=target_user_id,
                    admin_checkin=admin_checkin,
                )
            blocked = store.mark_blocked(
                int(leased_intent["id"]),
                owner=mutation_owner,
                error=error,
                notification_required=not admin_checkin,
            )
            if blocked is None:
                return self._coworking_booking_already_queued_message(
                    booking_date=booking_date,
                    target_user_id=target_user_id,
                    admin_checkin=admin_checkin,
                )
            if isinstance(exc, httpx.HTTPStatusError):
                print(
                    "🏢 coworking_booking_terminal_rejection "
                    f"status={exc.response.status_code}"
                )
                if admin_checkin:
                    return await self._format_coworking_booking_rejection(
                        client=client,
                        target_user_id=target_user_id,
                        requested_by_user_id=requested_by_user_id,
                        booking_date=booking_date,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        admin_checkin=True,
                        exc=exc,
                    )
                notification = store.reserve_notification(
                    int(leased_intent["id"]),
                    owner=f"roo-sync-rejection-{uuid4().hex}",
                )
                if notification is None:
                    return {
                        "message": "",
                        "suppress_post": True,
                        "data": {
                            "action": "book_coworking",
                            "booking_blocked": True,
                            "notification_status": blocked.get("notification_status"),
                        },
                    }
                rejection_client_msg_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"roo:coworking-rejection:{leased_intent['id']}:{error}",
                    )
                )
                rendered_rejection = await self._format_coworking_booking_rejection(
                    client=client,
                    target_user_id=target_user_id,
                    requested_by_user_id=requested_by_user_id,
                    booking_date=booking_date,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    admin_checkin=admin_checkin,
                    exc=exc,
                    client_msg_id=rejection_client_msg_id,
                )
                rejection_message = (
                    str(rendered_rejection.get("message") or "")
                    if isinstance(rendered_rejection, dict)
                    else str(rendered_rejection or "")
                )
                notification_result = await deliver_coworking_booking_notification(
                    notification,
                    store=store,
                    client=client,
                    executor=self,
                    blocked_message=rejection_message,
                )
                return {
                    "message": "",
                    "suppress_post": True,
                    "data": {
                        "action": "book_coworking",
                        "booking_blocked": True,
                        "notification_status": notification_result.get(
                            "notification_status"
                        ),
                    },
                }
            raise

        notification = store.reserve_notification(
            int(leased_intent["id"]),
            owner=f"roo-sync-notification-{uuid4().hex}",
        )
        if notification is None:
            return {
                "message": "",
                "suppress_post": True,
                "data": {
                    "action": "book_coworking",
                    "booking_confirmed": True,
                    "notification_status": confirmed.get("notification_status"),
                },
            }

        notification_result = await deliver_coworking_booking_notification(
            notification,
            store=store,
            client=client,
            executor=self,
            post_public_message=False,
        )
        delivery = notification_result.get("delivery")
        if isinstance(delivery, dict):
            delivery_data = delivery.get("data")
            if (
                isinstance(delivery_data, dict)
                and delivery_data.get("delivery") != "current_direct_message"
            ):
                return delivery
        return {
            "message": "",
            "suppress_post": True,
            "data": {
                "action": "book_coworking",
                "booking_confirmed": True,
                "notification_status": notification_result.get(
                    "notification_status"
                ),
            },
        }


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
        verified_bridge_context=None,
    ) -> Any:
        """Handle individual points actions."""
        if action in {
            "quote_event_discount", "redeem_event_discount",
            "get_event_redemption", "list_event_redemptions",
        }:
            from ...event_discounts import handle_event_discount

            return await handle_event_discount(
                client=client, action=action, params=params, text=text,
                user_id=user_id, channel_id=channel_id, thread_ts=thread_ts,
                settings=get_settings(),
            )
        if action in {
            "book_coworking", "admin_checkin_coworking", "check_coworking",
        }:
            try:
                # Validate before creating a durable intent or calling an API.
                date_text = text
                if action == "check_coworking":
                    # Availability has a separate `days` window. Do not
                    # mistake "for the next 7 days" for a booking date.
                    date_text = re.sub(
                        r"\b(?:for\s+)?(?:the\s+)?next\s+\d+\s+days?\b",
                        "", date_text, flags=re.IGNORECASE,
                    )
                resolved_date = self._resolve_coworking_booking_date(
                    params, date_text,
                    default_to_today=action in {"book_coworking", "admin_checkin_coworking"},
                )
            except CoworkingDateError as exc:
                return str(exc)
            params = {**params, "date": resolved_date}
        
        # =====================================================================
        # Member Actions
        # =====================================================================

        if action == "link_founder_account":
            if not bool(getattr(get_settings(), "FOUNDER_ACCOUNT_LINK_ENABLED", False)):
                return (
                    "Founder Tools account linking is not available yet. "
                    "Please try again after the MLAI team enables it."
                )
            try:
                result = await client.start_founder_account_link(user_id)
            except MLAIBackendUnavailableError:
                return (
                    "I couldn't confirm whether a Founder Tools account link was "
                    "created right now. Please try `link` again; a fresh request "
                    "safely replaces any incomplete link."
                )
            except httpx.HTTPStatusError as exc:
                if 500 <= exc.response.status_code < 600:
                    return (
                        "I couldn't confirm whether a Founder Tools account link was "
                        "created right now. Please try `link` again; a fresh request "
                        "safely replaces any incomplete link."
                    )
                try:
                    error_code = str(exc.response.json().get("code") or "")
                except (AttributeError, ValueError):
                    error_code = ""
                if (
                    exc.response.status_code == 404
                    and error_code == "slack_user_not_found"
                ):
                    return (
                        "I couldn't verify your Slack account for Founder Tools linking. "
                        "Please try `link` again; if this continues, contact the MLAI team."
                    )
                if (
                    exc.response.status_code == 429
                    and error_code == "link_rate_limited"
                ):
                    try:
                        retry_after = int(
                            exc.response.json().get("retry_after_seconds") or 0
                        )
                    except (AttributeError, TypeError, ValueError):
                        retry_after = 0
                    wait_copy = (
                        f" Wait about {retry_after} seconds"
                        if retry_after > 0
                        else " Wait a few minutes"
                    )
                    return (
                        "You've requested several Founder Tools account links recently."
                        f"{wait_copy}, then try `link` again. Your latest link remains valid."
                    )
                return (
                    "I couldn't create a Founder Tools account link right now. "
                    "Please try `link` again shortly."
                )

            if not isinstance(result, dict):
                return (
                    "I couldn't create a trusted Founder Tools account link right now. "
                    "Please try `link` again shortly."
                )

            if result.get("status") == "already_linked":
                return self._founder_account_link_status_response(
                    message=(
                        "Your Roo Slack account is already linked to a Founder Tools account. "
                        "If that is the wrong account, contact the MLAI team for help; Roo "
                        "cannot relink accounts yet."
                    ),
                    user_id=user_id,
                    channel_id=channel_id,
                )

            link_url = str(result.get("link_url") or "").strip()
            expires_at_timestamp = self._founder_account_link_expiry_timestamp(
                result.get("expires_at")
            )
            settings = get_settings()
            if (
                result.get("status") != "link_required"
                or expires_at_timestamp is None
                or not self._is_safe_founder_account_link_url(
                    link_url,
                    settings=settings,
                )
            ):
                return (
                    "I couldn't create a trusted Founder Tools account link right now. "
                    "Please try `link` again shortly."
                )

            return self._founder_account_link_button_response(
                link_url=link_url,
                expires_at_timestamp=expires_at_timestamp,
                user_id=user_id,
                channel_id=channel_id,
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

            from ...slack_client import get_bot_user_id

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

            from ...slack_client import get_bot_user_id

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

            from ...slack_client import get_bot_user_id

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
            if not self._can_generate_coworking_report_in_channel(user_id, channel_id):
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
            message = await self._format_coworking_analysis_response(context)
            include_chart = self._coerce_optional_bool(params.get("include_chart"))
            if include_chart is None:
                include_chart = context["flags"]["chart_requested"]
            if context["flags"]["chart_disabled"]:
                include_chart = False
            if channel_id and include_chart:
                chart_note = await self._upload_coworking_chart(
                    report, channel_id=channel_id, thread_ts=thread_ts,
                )
                message = f"{message}\n\n{chart_note}"
            return message

        elif action == "admin_checkin_coworking":
            from ...slack_client import get_bot_user_id
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

            booking_date = params["date"]

            if len(target_slack_ids) > 1:
                return await self._book_coworking_many_for_admin(
                    client=client,
                    admin_user_id=user_id,
                    target_user_ids=target_slack_ids,
                    booking_date=booking_date,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )

            target_user_id = target_slack_ids[0]
            try:
                return await self._book_coworking_with_intent(
                    client=client,
                    target_user_id=target_user_id,
                    requested_by_user_id=user_id,
                    booking_date=booking_date,
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
            from ...slack_client import get_bot_user_id
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

                booking_date = params["date"]

                if len(target_slack_ids) > 1:
                    return await self._book_coworking_many_for_admin(
                        client=client,
                        admin_user_id=user_id,
                        target_user_ids=target_slack_ids,
                        booking_date=booking_date,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                    )

                target_user_id = target_slack_ids[0]
                try:
                    return await self._book_coworking_with_intent(
                        client=client,
                        target_user_id=target_user_id,
                        requested_by_user_id=user_id,
                        booking_date=booking_date,
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

            booking_date = params["date"]
            return await self._book_coworking_with_intent(
                client=client,
                target_user_id=user_id,
                requested_by_user_id=user_id,
                booking_date=booking_date,
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
            
            if not booking_id:
                # Resolve the user's human-friendly date to an immutable row
                # identity before mutating. This prevents a delayed replay of
                # an older date-based cancellation from cancelling a newer
                # booking for the same day.
                bookings = await client.get_my_bookings(user_id)
                matching_bookings = [
                    booking
                    for booking in bookings
                    if str(booking.get("date") or "") == str(booking_date)
                    and str(booking.get("status") or "") == "booked"
                    and booking.get("id")
                ]
                if not matching_bookings:
                    return "I couldn't find an active booking for that date."
                if len(matching_bookings) != 1:
                    return (
                        "I found more than one active booking for that date. "
                        "Please contact the tech team so we can cancel the right one safely."
                    )
                booking_id = str(matching_bookings[0]["id"])

            result = await client.cancel_coworking(
                user_id,
                booking_id=booking_id,
            )
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
            from ...slack_client import get_bot_user_id

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
            from ...slack_client import get_bot_user_id

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
            from ...slack_client import get_bot_user_id

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
                 return "No active point rates are configured."
             
             lines = ["📋 **Standard Point Rates:**\n"]
             for item in card:
                 name = item.get("name", "Unknown")
                 pts = item.get("points", 0)
                 desc = item.get("description", "")
                 lines.append(f"• **{name}** ({pts} pts) - {desc}")
             
             return "\n".join(lines)

        elif action == "estimate_points":
            description = self._points_estimate_task_description(text, params)
            if not description:
                return "What work should I estimate Roo points for?"
            card = await client.get_rate_card()
            if not card:
                return "No active point rates are configured."
            return self._format_points_estimate(description, card)
        
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
            from ...slack_client import get_bot_user_id
            try:
                bot_id = get_bot_user_id()
            except Exception:
                bot_id = None
            award_target_text = text

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

            if verified_bridge_context is None and self._is_self_directed_points_award(text, params, user_id, bot_id=bot_id):
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

            if verified_bridge_context is not None:
                from ...bridge_mentions import VerifiedBridgeContext
                from ...points_awards import award_bridge_points, bridge_award_details

                explicit_slack_recipients = re.findall(r"<@([UW][A-Z0-9]+)>", text)
                legacy_slack_targets = (
                    isinstance(verified_bridge_context, VerifiedBridgeContext)
                    and not verified_bridge_context.recipient_targets
                    and not verified_bridge_context.source_labels
                    and bot_id
                    and any(target != bot_id for target in explicit_slack_recipients)
                )
                if not legacy_slack_targets:
                    return await award_bridge_points(
                        client=client, context=verified_bridge_context,
                        text=text, params=params, admin_slack_id=user_id,
                        channel_id=channel_id, thread_ts=thread_ts,
                        bot_user_id=bot_id,
                    )
                # Older clients preserve actual Slack mentions. Keep that
                # verified source path compatible without letting model IDs
                # replace an absent native recipient (including revoked keys).
                points, reason, beneficiary_text = bridge_award_details(text, verified_bridge_context)
                if points is None:
                    return "How many points should I award? Include one amount, such as `4 points`."
                if points <= 0:
                    return "Points awards need a positive number of points."
                if not reason:
                    return "What are the points for? Include a reason after `for`."
                if not any(target != bot_id for target in re.findall(r"<@([UW][A-Z0-9]+)>", beneficiary_text)):
                    return "Who should receive the points? Select the beneficiary before the `for` reason, then send a new command."
                award_target_text = beneficiary_text
                params = {**params, "points": points, "reason": reason}
                request_id = f"mlai-chat:{verified_bridge_context.source_event_id}"

            admin_details = await client.get_admin_details(user_id)
            if not self._is_full_points_admin_details(admin_details):
                return self._full_points_admin_denial(admin_details, "award points")

            # Early allowance check for award actions (before LLM/rate card lookup)
            if action in ["award_points", "award"] and not request_id:
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
            all_mentions = re.findall(r'<@([A-Z0-9]+)>', award_target_text)
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
            
            # Extract points amount if not in params.
            if not points:
                # Slack IDs contain digits that are not point amounts.
                amount_text = re.sub(r'<@[A-Z0-9]+>', '', text)
                # 1. Try Regex fallback first (in case params missed explicit points).
                pts_match = re.search(r'(?<![a-zA-Z])([+-]?\d+)\s*(?:points?|pts?)?', amount_text, re.IGNORECASE)
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
                        matches = [
                            (score, item)
                            for score, item in self._score_rate_card_rows(reason, rate_card)
                            if score > 40
                        ]
                        
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
                                
                    except MLAIBackendUnavailableError:
                        return self._points_backend_unavailable_message("view_rate_card")
                    except Exception as e:
                        print(f"⚠️ Smart award lookup failed: {e}")

                return "How many points should I award? (e.g., \"award @user 5 points\")"
            
            # Validate positive points
            if points < 0:
                return "Crikey! I can only award positive points. 🚫"
            
            # Award points to each target user
            results = []
            errors = []
            for target_id in sorted(set(target_slack_ids)):
                try:
                    # Deduplication: Link Slack ID to existing email user if needed
                    try:
                        # Check if this Slack ID is already known
                        existing_user_id = await client.get_user_by_slack_id(target_id)
                        
                        if not existing_user_id:
                            # Not found by Slack ID -> Check if we know this user by email
                            from ...slack_client import get_user_info
                            u_info = get_user_info(target_id)
                            u_email = u_info.get("email")
                            
                            if u_email:
                                linked_user_id = await client.link_slack_user(target_id, u_email)
                                if linked_user_id:
                                    print(f"🔗 Linked Slack ID {target_id} to existing user {linked_user_id} via email {u_email}")
                    except Exception as e:
                        print(f"⚠️ User linking failed (continuing to award): {e}")

                    award_kwargs = {}
                    if request_id:
                        operation = hashlib.sha256(str(request_id).encode()).hexdigest()
                        award_kwargs["idempotency_key"] = f"roo-slack-award:{operation}:{target_id}"
                    result = await client.award_points(user_id, target_id, int(points), reason, **award_kwargs)
                    new_balance = result.get("new_balance")
                    results.append({"user": target_id})
                    balance_line = (
                        f"\nYour new balance is {new_balance} points."
                        if new_balance is not None
                        else ""
                    )
                    try:
                        if result.get("replayed"):
                            continue
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


