"""Admin brain skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from ...admin_brain import ADMIN_BRAIN_ACCESS_DENIED_MESSAGE, ADMIN_BRAIN_UNAVAILABLE_MESSAGE, build_admin_brain_response
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from typing import Optional
from ...config import get_settings
import httpx


class AdminBrainHandler:
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_admin_brain(
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        return result

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


