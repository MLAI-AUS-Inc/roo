from __future__ import annotations

from typing import Any, Optional

import httpx


LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearMeetingActionsClient:
    """Small GraphQL client for the Linear meeting-actions Roo skill."""

    def __init__(self, api_key: str, base_url: str = LINEAR_GRAPHQL_URL):
        if not api_key:
            raise ValueError("LINEAR_API_KEY not configured")
        self.api_key = api_key
        self.base_url = base_url

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    async def _graphql(
        self,
        query: str,
        variables: Optional[dict[str, Any]] = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.base_url,
                json={"query": query, "variables": variables or {}},
                headers=self.headers,
                timeout=timeout,
            )
            response.raise_for_status()

        data = response.json()
        errors = data.get("errors")
        if errors:
            messages = "; ".join(str(error.get("message") or error) for error in errors)
            raise RuntimeError(f"Linear GraphQL error: {messages}")
        return data.get("data") or {}

    async def list_teams(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        query LinearTeams($first: Int!) {
          teams(first: $first) {
            nodes {
              id
              key
              name
            }
          }
        }
        """
        data = await self._graphql(query, {"first": limit})
        return _nodes(data, "teams")

    async def list_users(self, limit: int = 250) -> list[dict[str, Any]]:
        query = """
        query LinearUsers($first: Int!) {
          users(first: $first) {
            nodes {
              id
              name
              displayName
              email
              active
            }
          }
        }
        """
        data = await self._graphql(query, {"first": limit})
        users = _nodes(data, "users")
        return [user for user in users if user.get("active") is not False]

    async def list_active_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        query LinearProjects($first: Int!) {
          projects(first: $first) {
            nodes {
              id
              name
              slugId
              url
              state
              lead {
                id
                name
                displayName
                email
              }
              teams {
                nodes {
                  id
                  key
                  name
                }
              }
              members {
                nodes {
                  id
                  name
                  displayName
                  email
                }
              }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": limit})
        projects = _nodes(data, "projects")
        inactive_states = {"completed", "canceled", "cancelled", "archived"}
        return [
            project
            for project in projects
            if str(project.get("state") or "").lower() not in inactive_states
        ]

    async def list_issue_labels(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        query LinearIssueLabels($first: Int!) {
          issueLabels(first: $first) {
            nodes {
              id
              name
            }
          }
        }
        """
        data = await self._graphql(query, {"first": limit})
        return _nodes(data, "issueLabels")

    async def list_recent_open_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        query = """
        query LinearRecentIssues($first: Int!) {
          issues(first: $first) {
            nodes {
              id
              identifier
              title
              url
              state {
                name
                type
              }
              project {
                id
                name
              }
              assignee {
                id
                name
                displayName
                email
              }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": limit})
        issues = _nodes(data, "issues")
        closed_types = {"completed", "canceled", "cancelled"}
        return [
            issue
            for issue in issues
            if str((issue.get("state") or {}).get("type") or "").lower() not in closed_types
        ]

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
    ) -> dict[str, Any]:
        input_data: dict[str, Any] = {
            "title": title,
            "teamId": team_id,
        }
        if description:
            input_data["description"] = description
        if assignee_id:
            input_data["assigneeId"] = assignee_id
        if project_id:
            input_data["projectId"] = project_id
        if priority is not None:
            input_data["priority"] = priority
        if due_date:
            input_data["dueDate"] = due_date
        if label_ids:
            input_data["labelIds"] = label_ids

        mutation = """
        mutation CreateLinearMeetingIssue($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue {
              id
              identifier
              title
              url
            }
          }
        }
        """
        data = await self._graphql(mutation, {"input": input_data})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise RuntimeError("Linear issueCreate returned success=false")
        return result.get("issue") or {}


def _nodes(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key) or {}
    nodes = value.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]
