"""Mlai data query skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from typing import Any, Optional, List
from ..loader import Skill
from ...config import get_settings
import httpx
import json
import re
from .backend_errors import BackendErrors
from .linear_read_support import LinearReadSupport


class MlaiDataQueryExecutor(BackendErrors, LinearReadSupport, SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
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
        return result

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


    def _linear_channel_issue_thread_context(
        self,
        thread_history: Optional[List[dict]],
    ) -> bool:
        return bool(self._linear_channel_issue_response_messages(thread_history))


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


