from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import quote

import httpx
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError

LINEAR_MEETING_CONTEXT_ENDPOINT = "/api/v1/integrations/linear/meeting-context"
LINEAR_PROJECT_RESOLVE_ENDPOINT = "/api/v1/integrations/linear/projects/resolve"
LINEAR_MEETING_ISSUES_ENDPOINT = "/api/v1/integrations/linear/issues"
LINEAR_MEETING_PROJECT_UPDATES_ENDPOINT = "/api/v1/integrations/linear/project-updates"
LINEAR_MEETING_ACTION_BATCHES_ENDPOINT = "/api/v1/integrations/linear/action-batches"


class LinearIssueCreationInProgressError(RuntimeError):
    pass


class LinearMeetingTeamAccessError(RuntimeError):
    """Raised when the backend credential cannot see every required Linear team."""


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
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        try:
            response = await self.backend._request(
                method,
                endpoint,
                params=params,
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
            if (
                response.status_code == 503
                and isinstance(error_payload, dict)
                and error_payload.get("code") == "linear_team_access_incomplete"
            ):
                raise LinearMeetingTeamAccessError(message)
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

    async def resolve_project(self, query: str) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is required")
        return await self._request_json(
            "GET",
            LINEAR_PROJECT_RESOLVE_ENDPOINT,
            params={"query": query},
            timeout=45.0,
        )

    async def get_project_sizing_context(self, project_id: str) -> dict[str, Any]:
        encoded_project_id = quote(str(project_id or "").strip(), safe="")
        if not encoded_project_id:
            raise ValueError("project_id is required")
        return await self._request_json(
            "GET",
            f"/api/v1/integrations/linear/projects/{encoded_project_id}/sizing-context",
            timeout=45.0,
        )

    async def list_project_issues(
        self,
        project_id: str,
        *,
        page_size: int = 50,
        max_issues: int = 500,
    ) -> dict[str, Any]:
        encoded_project_id = quote(str(project_id or "").strip(), safe="")
        if not encoded_project_id:
            raise ValueError("project_id is required")
        nodes: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        project: dict[str, Any] = {}
        snapshot_at = ""
        terminal_types: list[str] = []
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": max(1, min(int(page_size), 100))}
            if cursor:
                params["after"] = cursor
            page = await self._request_json(
                "GET",
                f"/api/v1/integrations/linear/projects/{encoded_project_id}/issues",
                params=params,
                timeout=45.0,
            )
            if not project and isinstance(page.get("project"), dict):
                project = dict(page["project"])
            if not snapshot_at:
                snapshot_at = str(page.get("snapshotAt") or "")
            if not terminal_types and isinstance(page.get("terminalStateTypes"), list):
                terminal_types = [str(value) for value in page["terminalStateTypes"]]
            nodes.extend(_dict_list(page.get("nodes")))
            if len(nodes) > max(1, int(max_issues)):
                raise RuntimeError(
                    f"Project has more than the configured {max_issues} issue safety limit; nothing was changed."
                )
            page_info = page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor = str(page_info.get("endCursor") or "").strip()
            if not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("Linear issue pagination did not advance safely.")
            if len(nodes) >= max(1, int(max_issues)):
                raise RuntimeError(
                    f"Project has more than the configured {max_issues} issue safety limit; nothing was changed."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return {
            "project": project,
            "nodes": nodes,
            "snapshotAt": snapshot_at,
            "terminalStateTypes": terminal_types,
            "truncated": False,
        }

    async def list_project_updates(
        self,
        project_id: str,
        *,
        page_size: int = 25,
        max_updates: int = 500,
    ) -> dict[str, Any]:
        encoded_project_id = quote(str(project_id or "").strip(), safe="")
        if not encoded_project_id:
            raise ValueError("project_id is required")
        nodes: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        project: dict[str, Any] = {}
        snapshot_at = ""
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": max(1, min(int(page_size), 100))}
            if cursor:
                params["after"] = cursor
            page = await self._request_json(
                "GET",
                f"/api/v1/integrations/linear/projects/{encoded_project_id}/updates",
                params=params,
                timeout=45.0,
            )
            if not project and isinstance(page.get("project"), dict):
                project = dict(page["project"])
            if not snapshot_at:
                snapshot_at = str(page.get("snapshotAt") or "")
            nodes.extend(_dict_list(page.get("nodes")))
            if len(nodes) > max(1, int(max_updates)):
                raise RuntimeError(
                    f"Project has more than the configured {max_updates} update safety limit; nothing was changed."
                )
            page_info = page.get("pageInfo") if isinstance(page.get("pageInfo"), dict) else {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor = str(page_info.get("endCursor") or "").strip()
            if not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("Linear project-update pagination did not advance safely.")
            if len(nodes) >= max(1, int(max_updates)):
                raise RuntimeError(
                    f"Project has more than the configured {max_updates} update safety limit; nothing was changed."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return {
            "project": project,
            "nodes": nodes,
            "snapshotAt": snapshot_at,
            "truncated": False,
        }

    async def create_project_sizing_run(
        self,
        *,
        project_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_project_id = quote(str(project_id or "").strip(), safe="")
        if not encoded_project_id:
            raise ValueError("project_id is required")
        return await self._request_json(
            "POST",
            f"/api/v1/integrations/linear/projects/{encoded_project_id}/sizing-runs",
            payload=payload,
            timeout=60.0,
        )

    async def get_project_sizing_run(self, run_id: str) -> dict[str, Any]:
        encoded_run_id = quote(str(run_id or "").strip(), safe="")
        if not encoded_run_id:
            raise ValueError("run_id is required")
        return await self._request_json(
            "GET",
            f"/api/v1/integrations/linear/project-sizing-runs/{encoded_run_id}",
            timeout=30.0,
        )

    async def apply_project_sizing_run(
        self,
        run_id: str,
        *,
        requested_by_slack_user_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        encoded_run_id = quote(str(run_id or "").strip(), safe="")
        if not encoded_run_id:
            raise ValueError("run_id is required")
        return await self._request_json(
            "POST",
            f"/api/v1/integrations/linear/project-sizing-runs/{encoded_run_id}/apply",
            payload={
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "limit": limit,
            },
            timeout=60.0,
        )

    async def cancel_project_sizing_run(
        self,
        run_id: str,
        *,
        requested_by_slack_user_id: str,
    ) -> dict[str, Any]:
        encoded_run_id = quote(str(run_id or "").strip(), safe="")
        if not encoded_run_id:
            raise ValueError("run_id is required")
        return await self._request_json(
            "POST",
            f"/api/v1/integrations/linear/project-sizing-runs/{encoded_run_id}/cancel",
            payload={"requested_by_slack_user_id": requested_by_slack_user_id},
            timeout=30.0,
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

    async def create_action_batch(
        self,
        *,
        requested_by_slack_user_id: str,
        items: list[dict[str, Any]],
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        source_fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested_by_slack_user_id": requested_by_slack_user_id,
            "items": items,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
        if source_fingerprint:
            payload["source_fingerprint"] = source_fingerprint
        return await self._request_json(
            "POST",
            LINEAR_MEETING_ACTION_BATCHES_ENDPOINT,
            payload=payload,
            timeout=30.0,
        )

    async def get_action_batch(self, batch_id: str) -> dict[str, Any]:
        encoded_batch_id = quote(str(batch_id or "").strip(), safe="")
        if not encoded_batch_id:
            raise ValueError("batch_id is required")
        return await self._request_json(
            "GET",
            f"{LINEAR_MEETING_ACTION_BATCHES_ENDPOINT}/{encoded_batch_id}",
            timeout=20.0,
        )

    async def decide_action_batch(
        self,
        *,
        batch_id: str,
        requested_by_slack_user_id: str,
        decision: str,
        item_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        encoded_batch_id = quote(str(batch_id or "").strip(), safe="")
        if not encoded_batch_id:
            raise ValueError("batch_id is required")
        payload: dict[str, Any] = {
            "requested_by_slack_user_id": requested_by_slack_user_id,
            "decision": decision,
        }
        if item_ids is not None:
            payload["item_ids"] = item_ids
        return await self._request_json(
            "POST",
            f"{LINEAR_MEETING_ACTION_BATCHES_ENDPOINT}/{encoded_batch_id}/decisions",
            payload=payload,
            timeout=120.0,
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
