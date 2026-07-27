from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import quote

import httpx

from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError


LINEAR_MEETING_CONTEXT_ENDPOINT = "/api/v1/integrations/linear/meeting-context"
LINEAR_MEETING_ISSUES_ENDPOINT = "/api/v1/integrations/linear/issues"
LINEAR_MEETING_PROJECT_UPDATES_ENDPOINT = "/api/v1/integrations/linear/project-updates"


class LinearIssueCreationInProgressError(RuntimeError):
    pass


class LinearMeetingActionsClient:
    """Backend-backed client for Roo's Linear meeting-actions skill."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        internal_api_key: Optional[str] = None,
    ):
        self.backend = MLAIBackendClient(
            base_url=base_url,
            api_key=api_key,
            internal_api_key=internal_api_key,
        )
        self._context_task: Optional[asyncio.Task[dict[str, Any]]] = None

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        try:
            response = await self.backend._request(
                method,
                endpoint,
                json=payload,
                timeout=timeout,
                circuit_breaker=True,
                use_admin_headers=True,
            )
        except MLAIBackendUnavailableError:
            raise
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        if response.status_code == 404 and allow_not_found:
            try:
                data = response.json()
            except ValueError:
                data = {}
            return data if isinstance(data, dict) else {"status": "not_found"}
        if response.status_code >= 400:
            message = self._backend_error_message(response)
            print(
                "⚠️ Linear meeting backend error "
                f"status={response.status_code} endpoint={endpoint} "
                f"body={self._backend_response_body(response)}"
            )
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            if (
                response.status_code == 409
                and isinstance(error_payload, dict)
                and error_payload.get("code") == "linear_issue_creation_in_progress"
            ):
                raise LinearIssueCreationInProgressError(message)
            raise RuntimeError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("mlai-backend returned invalid JSON for Linear meeting actions.") from exc
        return data if isinstance(data, dict) else {}

    async def _meeting_context(self) -> dict[str, Any]:
        if self._context_task is None:
            self._context_task = asyncio.create_task(
                self._request_json("GET", LINEAR_MEETING_CONTEXT_ENDPOINT)
            )
        return await self._context_task

    async def list_teams(self, limit: int = 100) -> list[dict[str, Any]]:
        context = await self._meeting_context()
        return _dict_list(context.get("teams"))

    async def list_users(self, limit: int = 250) -> list[dict[str, Any]]:
        context = await self._meeting_context()
        return _dict_list(context.get("users"))

    async def list_active_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        context = await self._meeting_context()
        return _dict_list(context.get("projects"))

    async def list_issue_labels(self, limit: int = 100) -> list[dict[str, Any]]:
        context = await self._meeting_context()
        return _dict_list(context.get("labels"))

    async def list_recent_open_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        context = await self._meeting_context()
        return _dict_list(context.get("recentIssues") or context.get("recent_issues"))

    async def get_project_sizing_context(self, project_id: str) -> dict[str, Any]:
        encoded_project_id = quote(str(project_id or "").strip(), safe="")
        if not encoded_project_id:
            raise ValueError("project_id is required")
        return await self._request_json(
            "GET",
            f"/api/v1/integrations/linear/projects/{encoded_project_id}/sizing-context",
            timeout=45.0,
        )

    async def get_issue_receipt(self, idempotency_key: str) -> dict[str, Any]:
        encoded_key = quote(str(idempotency_key or "").strip(), safe="")
        if not encoded_key:
            raise ValueError("idempotency_key is required")
        return await self._request_json(
            "GET",
            f"/api/v1/integrations/linear/issues/receipts/{encoded_key}",
            timeout=15.0,
            allow_not_found=True,
        )

    async def create_issue(
        self,
        *,
        title: str,
        team_id: str,
        description: Optional[str] = None,
        assignee_id: Optional[str] = None,
        project_id: Optional[str] = None,
        priority: Optional[int] = None,
        due_date: Optional[str] = None,
        label_ids: Optional[list[str]] = None,
        idempotency_key: Optional[str] = None,
        sizing_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "team_id": team_id,
        }
        if description:
            payload["description"] = description
        if assignee_id:
            payload["assignee_id"] = assignee_id
        if project_id:
            payload["project_id"] = project_id
        if priority is not None:
            payload["priority"] = priority
        if due_date:
            payload["due_date"] = due_date
        if label_ids:
            payload["label_ids"] = label_ids
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if sizing_metadata:
            payload["sizing_metadata"] = sizing_metadata

        try:
            return await self._request_json(
                "POST",
                LINEAR_MEETING_ISSUES_ENDPOINT,
                payload=payload,
                timeout=45.0,
            )
        except LinearIssueCreationInProgressError:
            if not idempotency_key:
                raise
            for delay in (0.25, 0.75, 1.5):
                await asyncio.sleep(delay)
                receipt = await self.get_issue_receipt(idempotency_key)
                status = str(receipt.get("status") or "")
                if status == "completed":
                    issue = receipt.get("issue")
                    if isinstance(issue, dict):
                        return {**issue, "idempotentReplay": True}
                if status == "failed":
                    return await self._request_json(
                        "POST",
                        LINEAR_MEETING_ISSUES_ENDPOINT,
                        payload=payload,
                        timeout=45.0,
                    )
            raise RuntimeError(
                "An identical Linear issue creation is still in progress; no duplicate was created."
            )

    async def create_project_update(
        self,
        *,
        project_id: str,
        body: str,
        health: str = "onTrack",
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            LINEAR_MEETING_PROJECT_UPDATES_ENDPOINT,
            payload={
                "project_id": project_id,
                "body": body,
                "health": health,
            },
            timeout=30.0,
        )

    @staticmethod
    def _backend_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("message") or payload.get("error")
            code = payload.get("code")
            operation = payload.get("operation")
            suffix_parts = [str(value) for value in (code, operation) if value]
            if detail and suffix_parts:
                return f"{detail} ({'; '.join(suffix_parts)})"
            if detail:
                return str(detail)
        return f"mlai-backend Linear meeting actions request failed with HTTP {response.status_code}."

    @staticmethod
    def _backend_response_body(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            text = str(getattr(response, "text", "") or "").strip()
            return text[:1000]
        return repr(payload)[:1000]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
