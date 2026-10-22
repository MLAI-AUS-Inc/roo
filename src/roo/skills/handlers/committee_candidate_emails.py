"""Committee candidate emails skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from typing import Optional
from ...config import get_settings
import httpx
from ...slack_client import send_dm


class CommitteeCandidateEmailsHandler:
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_committee_candidate_emails(
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
        )
        return result

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


