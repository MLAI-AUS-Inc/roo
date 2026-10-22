"""Linear channel issues skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from typing import Any, Optional, List
from ...config import get_settings
import httpx
import re
from .backend_errors import BackendErrors
from .linear_read_support import LinearReadSupport


class LinearChannelIssuesHandler(BackendErrors, LinearReadSupport):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_linear_channel_issues(
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_history=(
                kwargs.get("linear_thread_history")
                if kwargs.get("linear_thread_history") is not None
                else thread_history
            ),
            slack_team_id=kwargs.get("slack_team_id"),
            request_id=kwargs.get("event_id") or kwargs.get("current_message_ts"),
        )
        return result

    async def _execute_linear_channel_issues(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_history: Optional[List[dict]],
        slack_team_id: Optional[str],
        request_id: Optional[str],
    ) -> Any:
        """Read or immediately apply a strictly typed channel-bound Linear edit."""
        from roo.clients import mlai_backend as backend_module

        settings = get_settings()
        if not settings.MLAI_BACKEND_URL:
            return "Sorry mate, the Linear integration isn't configured."
        if not channel_id or not slack_team_id:
            return "This Linear feature is only available from its connected Slack channel."
        client = backend_module.MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.ROO_API_KEY,
        )
        action = str(params.get("action") or "").strip().lower()
        lowered = str(text or "").lower()
        raw_issue_target = (
            self._linear_channel_write_target_reference(text)
            if action == "update_issue"
            else str(text or "")
        )
        issue_in_text = self._linear_issue_identifier(raw_issue_target)
        asks_for_status_catalogue = bool(
            re.search(
                r"(?:\b(?:list|show|which)\b.*\bstatus(?:es)?\b|"
                r"\bwhat\s+status(?:es)?\s+(?:are|can)\b|"
                r"\bavailable\s+status(?:es)?\b)",
                lowered,
            )
        )
        if not issue_in_text and (
            action == "list_statuses" or (not action and asks_for_status_catalogue)
        ):
            try:
                result = await client.list_linear_channel_statuses(
                    slack_workspace_id=slack_team_id,
                    slack_channel_id=channel_id,
                    requester_slack_id=user_id,
                )
                names = [
                    self._slack_escape(item.get("name"))
                    for item in result.get("statuses") or []
                    if isinstance(item, dict) and item.get("name")
                ]
                return "*Available MLAI_TECH statuses*\n" + "\n".join(f"• {name}" for name in names)
            except Exception as exc:
                return self._linear_channel_write_error(exc)

        if action == "list_issues":
            requested_status = str(params.get("status") or "").strip()
            if not requested_status:
                requested_status = self._linear_channel_issue_list_status(text)
            try:
                result = await client.list_linear_channel_issues(
                    slack_workspace_id=slack_team_id,
                    slack_channel_id=channel_id,
                    requester_slack_id=user_id,
                    limit=self._coerce_data_query_limit(params.get("limit"), default=50),
                    status=requested_status or None,
                )
                return self._format_linear_channel_issue_list(result)
            except Exception as exc:
                return self._linear_channel_write_error(exc)

        if action == "create_issue":
            create_request = self._linear_channel_create_request(text, params)
            if not create_request:
                return (
                    "I couldn't identify one explicit Linear issue creation request. "
                    "Include one exact title; for example, `create a Linear issue "
                    "titled Fix deployment alerts`. Nothing was created."
                )
            if not bool(getattr(settings, "LINEAR_CHANNEL_ISSUE_WRITES_ENABLED", False)):
                return "Linear issue editing is currently disabled. Nothing was created."
            if not request_id:
                return (
                    "This Linear issue creation is missing a Slack request identifier, "
                    "so nothing was created."
                )
            try:
                result = await client.create_linear_channel_issue(
                    slack_workspace_id=slack_team_id,
                    slack_channel_id=channel_id,
                    requester_slack_id=user_id,
                    request_id=str(request_id),
                    **create_request,
                )
            except Exception as exc:
                return self._linear_channel_write_error(exc)
            issue = result.get("issue") if isinstance(result.get("issue"), dict) else {}
            identifier = self._slack_escape(issue.get("identifier") or "New issue")
            title = self._clean_linear_issue_title(issue.get("title") or create_request["title"])
            url = str(issue.get("url") or "").strip()
            suffix = f" <{url}|Open in Linear>" if url.startswith("https://linear.app/") else ""
            return f"Created `{identifier}` — {title} in MLAI_TECH.{suffix}"

        if self._linear_channel_destructive_command(text):
            return "I can't delete, archive, restore, move teams, or edit existing Linear comments."

        write = None
        if action == "update_issue":
            write = self._linear_channel_write_request(text, params)
            if not write:
                return (
                    "I couldn't identify one explicit Linear edit. Include the field and exact new value; "
                    "for example, `move TECH-29 to In Progress`. Nothing was changed."
                )
            raw_issue_target = self._linear_channel_write_target_reference(
                text,
                operation=write["operation"],
            )
            issue_in_text = self._linear_issue_identifier(raw_issue_target)
            if not raw_issue_target:
                return (
                    "Which Linear issue do you mean? Explicitly name the issue or say `it` "
                    "in an issue thread. Nothing was changed."
                )

        routed_issue_identifier = self._linear_issue_identifier(
            params.get("issue_reference") or params.get("issue_identifier")
        )
        if (
            action == "update_issue"
            and issue_in_text
            and routed_issue_identifier
            and routed_issue_identifier != issue_in_text
        ):
            return "The issue in your message conflicts with the routed issue, so nothing was changed."
        resolution_params = params
        if action == "update_issue":
            resolution_params = {
                key: value
                for key, value in params.items()
                if key not in {"issue_reference", "issue_identifier"}
            }
        resolution_text = str(text or "")
        if action == "update_issue":
            # Only the deterministic command target may select a write target.
            # Never scan a title/comment/description value for an issue key.
            resolution_text = raw_issue_target
        issue_identifier = await self._resolve_linear_channel_issue_for_action(
            client=client,
            text=resolution_text,
            params=resolution_params,
            thread_history=thread_history,
            slack_workspace_id=slack_team_id,
            slack_channel_id=channel_id,
            requester_slack_id=user_id,
        )
        if isinstance(issue_identifier, dict):
            return issue_identifier["message"]
        if not issue_identifier:
            return "Which Linear issue do you mean? Please include a key such as `TECH-29`."
        try:
            detail = await client.get_linear_channel_issue(
                slack_workspace_id=slack_team_id,
                slack_channel_id=channel_id,
                requester_slack_id=user_id,
                issue_identifier=issue_identifier,
                include_comments=action == "get_issue",
            )
        except Exception as exc:
            return self._linear_channel_write_error(exc)
        if action != "update_issue":
            return self._format_linear_channel_issue_detail(detail)

        if not bool(getattr(settings, "LINEAR_CHANNEL_ISSUE_WRITES_ENABLED", False)):
            return "Linear issue editing is currently disabled. Nothing was changed."
        if not request_id:
            return "This Linear edit is missing a Slack request identifier, so nothing was changed."
        issue = detail.get("issue") if isinstance(detail.get("issue"), dict) else {}
        expected_updated_at = str(issue.get("updatedAt") or "").strip()
        if not expected_updated_at:
            return "Roo couldn't obtain the issue version needed for a safe edit. Nothing was changed."
        try:
            await client.write_linear_channel_issue(
                slack_workspace_id=slack_team_id,
                slack_channel_id=channel_id,
                requester_slack_id=user_id,
                issue_identifier=issue_identifier,
                operation=write["operation"],
                value=write["value"],
                expected_updated_at=expected_updated_at,
                request_id=str(request_id),
            )
        except Exception as exc:
            return self._linear_channel_write_error(exc)
        operation_label = write["operation"].replace("_", " ")
        return f"Updated `{self._slack_escape(issue_identifier)}`: {operation_label} completed in Linear."


    def _linear_channel_create_request(
        self, text: Any, params: dict
    ) -> Optional[dict[str, str]]:
        raw = str(text or "").strip()
        command = re.sub(r"^(?:<@[A-Z0-9]+>\s*)+", "", raw).strip()
        command = re.sub(
            r"^(?:(?:please|roo)\s*[,;:]?\s*)+", "", command,
            flags=re.IGNORECASE,
        ).strip()
        command = re.sub(
            r"^(?:can|could|would|will)\s+you(?:\s+please)?\s*[,;:]?\s*",
            "",
            command,
            flags=re.IGNORECASE,
        ).strip()
        if re.match(
            r"^(?:do\s+not|don['’]?t|never|cannot|can['’]?t)\b",
            command,
            re.IGNORECASE,
        ):
            return None
        if re.search(
            r"\b(?:but|actually|wait)\b[^\n]*?\b(?:do\s+not|don['’]?t|cancel|ignore|stop)\b|"
            r"\b(?:never\s+mind|cancel\s+that|ignore\s+that|stop\s+that)\b|"
            r"(?:[.!?;]\s+|\n+\s*)(?:please\s+)?(?:do\s+not|don['’]?t)\s+(?:do\s+)?that\b",
            command,
            re.IGNORECASE,
        ):
            return None
        create_pattern = (
            r"^(?:create|open|add)\s+(?:a|an|one)?\s*(?:new\s+)?"
            r"(?:linear\s+)?(?:issue|ticket|task)\b"
        )
        if len(re.findall(create_pattern, command, re.IGNORECASE)) != 1:
            return None
        if re.search(
            r"(?:\band\b|\bthen\b|[,;.!?]|\n)\s*(?:please\s+)?"
            r"(?:create|open|add)\s+(?:a|an|one)?\s*(?:new\s+)?"
            r"(?:linear\s+)?(?:issue|ticket|task)\b",
            command,
            re.IGNORECASE,
        ):
            return None
        match = re.match(create_pattern, command, re.IGNORECASE)
        if not match:
            return None
        tail = command[match.end():].strip()
        tail = re.sub(
            r"^(?:(?:in|to)\s+(?:the\s+)?MLAI[_ -]?TECH(?:\s+team)?\s*)",
            "",
            tail,
            flags=re.IGNORECASE,
        ).strip()
        title_match = re.match(
            r"^(?:called|titled|named|for|about|:)\s*(.+)$",
            tail,
            re.IGNORECASE,
        )
        if not title_match:
            return None
        value_text = title_match.group(1).strip()
        status = ""
        status_match = re.search(
            r"\s+(?:with|in)\s+(?:the\s+)?status\s+(.+)$",
            value_text,
            re.IGNORECASE,
        )
        if status_match:
            status = status_match.group(1).strip()
            value_text = value_text[:status_match.start()].rstrip()
        description = ""
        description_match = re.search(
            r"\s+with\s+(?:the\s+)?description\s+(.+)$",
            value_text,
            re.IGNORECASE,
        )
        if description_match:
            description = description_match.group(1).strip()
            value_text = value_text[:description_match.start()].rstrip()
        title = value_text.strip(" \t\"'")
        if not title or len(title) > 255 or len(description) > 10000:
            return None
        for explicit_field in (title, description, status):
            if re.search(
                r"(?:\band\b|\bthen\b|[,;.!?]|\n)\s*(?:please\s+)?"
                r"(?:delete|archive|trash|restore)\s+(?:issue\s+)?"
                r"(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\b",
                explicit_field,
                re.IGNORECASE,
            ):
                return None
            if self._linear_channel_has_additional_write_command(
                explicit_field, outer_operation="create_issue"
            ):
                return None
        routed_values = {
            "title": str(params.get("title") or "").strip(),
            "description": str(params.get("description") or "").strip(),
            "status": str(params.get("status") or "").strip(),
        }
        explicit_values = {"title": title, "description": description, "status": status}
        for field, routed_value in routed_values.items():
            if routed_value and routed_value.casefold() != explicit_values[field].casefold():
                return None
        result = {"title": title}
        if description:
            result["description"] = description
        if status:
            result["status"] = status
        return result


    def _linear_channel_issue_list_status(self, text: Any) -> str:
        value = str(text or "").strip()
        patterns = (
            r"\b(?:issues?|tickets?|tasks?)\s+(?:with\s+(?:the\s+)?status\s+|whose\s+status\s+is\s+)([^,;?.]+)",
            r"\b(?:issues?|tickets?|tasks?)\s+in\s+([^,;?.]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if not match:
                continue
            status = re.sub(
                r"\s+(?:right\s+now|at\s+the\s+moment|currently|please)\s*$",
                "",
                match.group(1),
                flags=re.IGNORECASE,
            ).strip(" \t\"'")
            if status and not re.search(r"\bMLAI[_ -]?TECH\b", status, re.IGNORECASE):
                return status
        if re.search(r"\ball\s+(?:MLAI[_ -]?TECH\s+)?(?:issues?|tickets?|tasks?)\b", value, re.IGNORECASE):
            return "all"
        return ""


    async def _resolve_linear_channel_issue_for_action(
        self, *, client: Any, text: str, params: dict, thread_history: Optional[List[dict]],
        slack_workspace_id: str, slack_channel_id: str, requester_slack_id: str,
    ) -> Any:
        reference = self._resolve_linear_channel_issue_reference(
            text=text, params=params, thread_history=thread_history
        )
        identifier = self._linear_issue_identifier(reference)
        if identifier:
            return identifier
        if re.fullmatch(
            r"\s*(?:it|this|that)\s*",
            str(reference or ""),
            re.IGNORECASE,
        ):
            return {
                "message": (
                    "Which Linear issue do you mean? Reply in a recognized issue thread "
                    "or include an issue key such as `TECH-29`. Nothing was changed."
                )
            }
        issues, complete = await self._list_all_linear_channel_issues(
            client,
            slack_workspace_id=slack_workspace_id,
            slack_channel_id=slack_channel_id,
            requester_slack_id=requester_slack_id,
            status="all",
        )
        if not complete:
            return {"message": "I couldn't safely search the complete Linear issue list. Nothing was changed."}
        resolution = self._match_linear_channel_issue(reference, issues)
        if resolution.get("ambiguous"):
            return {"message": self._format_linear_issue_choices(resolution["matches"])}
        return str(resolution.get("identifier") or "")


    def _linear_channel_write_request(self, text: str, params: dict) -> Optional[dict[str, Any]]:
        raw = str(text or "").strip()
        field = str(params.get("field") or "").strip().lower().replace("-", "_")
        mode = str(params.get("mode") or "").strip().lower()
        value = params.get("value")
        operation_by_field = {
            "comment": "add_comment",
            "title": "set_title",
            "priority": "set_priority",
            "estimate": "set_estimate",
            "due_date": "set_due_date",
            "assignee": "set_assignee",
            "project": "set_project",
            "cycle": "set_cycle",
            "status": "set_status",
            "duplicate": "mark_duplicate",
        }
        allowed_routed_operations: Optional[set[str]] = None
        routed_operation = operation_by_field.get(field)
        if routed_operation:
            allowed_routed_operations = {routed_operation}
        elif field == "description":
            if not mode:
                allowed_routed_operations = {
                    "append_description",
                    "replace_description",
                }
            elif mode == "append":
                allowed_routed_operations = {"append_description"}
            elif mode in {"replace", "set"}:
                allowed_routed_operations = {"replace_description"}
            else:
                return None
        elif field == "label":
            if not mode:
                allowed_routed_operations = {"add_label", "remove_label"}
            elif mode in {"add", "set"}:
                allowed_routed_operations = {"add_label"}
            elif mode == "remove":
                allowed_routed_operations = {"remove_label"}
            else:
                return None
        value_patterns = [
            ("set_status", r"\b(?:move|set)\s+(?:issue\s+)?(?:[A-Z][A-Z0-9]+-\d+|it|this|that)(?:\s+status)?\s+to\s+(.+)$"),
            ("set_status", r"\b(?:set|change|update)\s+(?:the\s+)?status(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("add_comment", r"\b(?:add|post|leave)\s+(?:a\s+)?comment(?:\s+(?:to|on)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*(?:saying|that says|:)\s*(.+)$"),
            ("add_comment", r"\bcomment\s+on\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\s*:\s*(.+)$"),
            ("set_title", r"\b(?:set|change|update|rename)\s+(?:the\s+)?title(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("append_description", r"\bappend\s+(.+?)\s+to\s+(?:the\s+)?description\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\s*$"),
            ("append_description", r"\bappend\s+(?:to\s+)?(?:the\s+)?description(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*(?:with|:|to)\s*(.+)$"),
            ("replace_description", r"\b(?:set|replace|update)\s+(?:the\s+)?description(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+(?:to|with)\s+(.+)$"),
            ("set_priority", r"\b(?:set|change|update)\s+(?:the\s+)?priority(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_estimate", r"\b(?:set|change|update)\s+(?:the\s+)?estimate(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_estimate", r"\b(?:clear|remove)\s+(?:the\s+)?estimate(?:\s+(?:of|from)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*$"),
            ("set_due_date", r"\b(?:set|change|update)\s+(?:the\s+)?due\s+date(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_due_date", r"\b(?:clear|remove)\s+(?:the\s+)?due\s+date(?:\s+(?:of|from)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*$"),
            ("set_assignee", r"\bassign\s+(?:issue\s+)?(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\s+to\s+(.+)$"),
            ("set_assignee", r"\b(?:set|change|update)\s+(?:the\s+)?assignee(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_assignee", r"\b(?:clear|remove)\s+(?:the\s+)?assignee(?:\s+(?:of|from)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*$"),
            ("add_label", r"\badd\s+(?:the\s+)?label\s+(.+?)\s+to\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\s*$"),
            ("remove_label", r"\bremove\s+(?:the\s+)?label\s+(.+?)\s+from\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that)\s*$"),
            ("set_project", r"\b(?:set|change|update)\s+(?:the\s+)?project(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_project", r"\b(?:clear|remove)\s+(?:the\s+)?project(?:\s+(?:of|from)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*$"),
            ("set_cycle", r"\b(?:set|change|update)\s+(?:the\s+)?cycle(?:\s+of\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s+to\s+(.+)$"),
            ("set_cycle", r"\b(?:clear|remove)\s+(?:the\s+)?cycle(?:\s+(?:of|from)\s+(?:[A-Z][A-Z0-9]+-\d+|it|this|that))?\s*$"),
            ("mark_duplicate", r"\b(?:mark|set)\b.*?\bduplicate\s+of\s+([A-Z][A-Z0-9]+-\d+)\b"),
        ]
        def is_outer_command(match: re.Match[str]) -> bool:
            command_start = match.start()
            command_prefix = raw[:command_start].strip()
            command_prefix = re.sub(
                r"^(?:<@[A-Z0-9]+>\s*)+", "", command_prefix
            ).strip()
            if command_prefix and not re.fullmatch(
                r"(?:(?:please|roo)\s*[,;:]?\s*)?"
                r"(?:(?:can|could|would|will)\s+you(?:\s+please)?\s*[,;:]?\s*|"
                r"please\s*[,;:]?\s*|roo\s*[,;:]?\s*)",
                command_prefix,
                re.IGNORECASE,
            ):
                return False
            return not any(
                negation.start() < command_start
                for negation in re.finditer(
                    r"\b(?:do\s+not|don['’]?t|cannot|can['’]?t|shouldn['’]?t|"
                    r"mustn['’]?t|won['’]?t|never)\b",
                    raw,
                    re.IGNORECASE,
                )
            )

        matches = [
            (operation, match)
            for operation, pattern in value_patterns
            if (match := re.search(pattern, raw, re.IGNORECASE))
            and is_outer_command(match)
        ]
        detected_operations = {operation for operation, _match in matches}
        if len(detected_operations) != 1:
            return None
        detected_operation = next(iter(detected_operations))
        command_start = min(
            match.start()
            for operation, match in matches
            if operation == detected_operation
        )
        command_prefix = raw[:command_start].strip()
        command_prefix = re.sub(r"^(?:<@[A-Z0-9]+>\s*)+", "", command_prefix).strip()
        if command_prefix and not re.fullmatch(
            r"(?:(?:please|roo)\s*[,;:]?\s*)?"
            r"(?:(?:can|could|would|will)\s+you(?:\s+please)?\s*[,;:]?\s*|"
            r"please\s*[,;:]?\s*|roo\s*[,;:]?\s*)",
            command_prefix,
            re.IGNORECASE,
        ):
            return None
        if any(
            negation.start() < command_start
            for negation in re.finditer(
                r"\b(?:do\s+not|don['’]?t|cannot|can['’]?t|shouldn['’]?t|"
                r"mustn['’]?t|won['’]?t|never)\b",
                raw,
                re.IGNORECASE,
            )
        ):
            return None
        if (
            allowed_routed_operations is not None
            and detected_operation not in allowed_routed_operations
        ):
            return None
        match = next(match for operation, match in matches if operation == detected_operation)
        if re.search(
            r"\b(?:but|actually|wait)\b[^\n]*?\b(?:do\s+not|don['’]?t|cancel|ignore|stop)\b|"
            r"\b(?:never\s+mind|cancel\s+that|ignore\s+that|stop\s+that)\b|"
            r"(?:[.!?;]\s+|\n+\s*)(?:please\s+)?(?:do\s+not|don['’]?t)\s+(?:do\s+)?that\b",
            raw[match.start():],
            re.IGNORECASE,
        ):
            return None
        captured = next((group for group in match.groups() if group), "clear")
        captured_value = captured.strip()
        remaining_text = raw[match.end():]
        if self._linear_channel_has_additional_write_command(
            f"{captured_value}{remaining_text}",
            outer_operation=detected_operation,
        ):
            return None
        if value is not None and str(value).strip().casefold() != captured_value.casefold():
            return None
        return {"operation": detected_operation, "value": captured_value}


    def _linear_channel_has_additional_write_command(
        self,
        value_and_tail: Any,
        *,
        outer_operation: str,
    ) -> bool:
        """Reject conjunction-separated extra commands outside opaque values."""

        target = r"(?:[A-Z][A-Z0-9]+-\d+|it|this|that)"
        command_patterns = {
            "set_status": rf"(?:(?:move|set)\s+(?:issue\s+)?{target}(?:\s+status)?\s+to|(?:set|change|update)\s+(?:the\s+)?status\s+of\s+{target}\s+to)\b",
            "add_comment": rf"(?:(?:add|post|leave)\s+(?:a\s+)?comment\s+(?:to|on)\s+{target}\s*(?:saying|that\s+says|:)|comment\s+on\s+{target}\s*:)",
            "set_title": rf"(?:set|change|update|rename)\s+(?:the\s+)?title\s+of\s+{target}\s+to\b",
            "append_description": rf"append\b.+\bdescription\s+of\s+{target}\b|append\s+(?:to\s+)?(?:the\s+)?description\s+of\s+{target}\b",
            "replace_description": rf"(?:set|replace|update)\s+(?:the\s+)?description\s+of\s+{target}\b",
            "set_priority": rf"(?:set|change|update)\s+(?:the\s+)?priority\s+of\s+{target}\s+to\b",
            "set_estimate": rf"(?:set|change|update|clear|remove)\s+(?:the\s+)?estimate(?:\s+(?:of|from)\s+{target})\b",
            "set_due_date": rf"(?:set|change|update|clear|remove)\s+(?:the\s+)?due\s+date(?:\s+(?:of|from)\s+{target})\b",
            "set_assignee": rf"assign\s+(?:issue\s+)?{target}\s+to\b|(?:set|change|update|clear|remove)\s+(?:the\s+)?assignee(?:\s+(?:of|from)\s+{target})\b",
            "add_label": rf"add\s+(?:the\s+)?label\b.+\s+to\s+{target}\b",
            "remove_label": rf"remove\s+(?:the\s+)?label\b.+\s+from\s+{target}\b",
            "set_project": rf"(?:set|change|update|clear|remove)\s+(?:the\s+)?project(?:\s+(?:of|from)\s+{target})\b",
            "set_cycle": rf"(?:set|change|update|clear|remove)\s+(?:the\s+)?cycle(?:\s+(?:of|from)\s+{target})\b",
            "mark_duplicate": rf"(?:mark|set)\s+(?:issue\s+)?{target}\s+as\s+(?:a\s+)?duplicate\s+of\b",
        }
        unsupported_command_pattern = (
            rf"(?:delete|archive|trash|restore)\s+(?:issue\s+)?{target}\b|"
            rf"move\s+(?:issue\s+)?{target}\s+to\s+(?:another|the)\s+team\b|"
            r"(?:edit|delete|remove)\s+(?:the\s+|an?\s+)?(?:existing\s+)?comment\b"
        )
        text = str(value_and_tail or "")
        for separator in re.finditer(
            r"(?:(?:\band\b|\bthen\b|[;,.!?])\s+|\n+\s*)",
            text,
            re.IGNORECASE,
        ):
            candidate = text[separator.end():].strip()
            candidate = re.sub(r"^(?:please\s+)", "", candidate, flags=re.IGNORECASE)
            if re.match(unsupported_command_pattern, candidate, re.IGNORECASE):
                return True
            for operation, pattern in command_patterns.items():
                if not re.match(pattern, candidate, re.IGNORECASE):
                    continue
                return True
        return False


    def _linear_channel_write_target_reference(
        self,
        text: Any,
        *,
        operation: Optional[str] = None,
    ) -> str:
        """Extract only the issue target clause, never an edit's new value."""

        raw = str(text or "")
        target = r"(?P<target>[A-Z][A-Z0-9]{1,15}-\d+|it|this|that)"
        patterns = [
            ("set_status", rf"\b(?:move|set)\s+(?:issue\s+)?{target}(?:\s+status)?\s+to\b"),
            ("set_status", rf"\b(?:set|change|update)\s+(?:the\s+)?status\s+of\s+{target}\s+to\b"),
            ("add_comment", rf"\b(?:add|post|leave)\s+(?:a\s+)?comment\s+(?:to|on)\s+{target}\b"),
            ("add_comment", rf"\bcomment\s+on\s+{target}\b"),
            ("set_title", rf"\b(?:set|change|update|rename)\s+(?:the\s+)?title\s+of\s+{target}\s+to\b"),
            ("append_description", rf"\bappend\s+.+\s+to\s+(?:the\s+)?description\s+of\s+{target}\s*$"),
            ("append_description", rf"\bappend\s+(?:to\s+)?(?:the\s+)?description\s+of\s+{target}\b"),
            ("replace_description", rf"\b(?:set|replace|update)\s+(?:the\s+)?description\s+of\s+{target}\b"),
            ("set_priority", rf"\b(?:set|change|update)\s+(?:the\s+)?priority\s+of\s+{target}\s+to\b"),
            ("set_estimate", rf"\b(?:set|change|update)\s+(?:the\s+)?estimate\s+of\s+{target}\s+to\b"),
            ("set_estimate", rf"\b(?:clear|remove)\s+(?:the\s+)?estimate\s+(?:of|from)\s+{target}\b"),
            ("set_due_date", rf"\b(?:set|change|update)\s+(?:the\s+)?due\s+date\s+of\s+{target}\s+to\b"),
            ("set_due_date", rf"\b(?:clear|remove)\s+(?:the\s+)?due\s+date\s+(?:of|from)\s+{target}\b"),
            ("set_assignee", rf"\bassign\s+(?:issue\s+)?{target}\s+to\b"),
            ("set_assignee", rf"\b(?:set|change|update)\s+(?:the\s+)?assignee\s+of\s+{target}\s+to\b"),
            ("set_assignee", rf"\b(?:clear|remove)\s+(?:the\s+)?assignee\s+(?:of|from)\s+{target}\b"),
            ("add_label", rf"\badd\s+(?:the\s+)?label\b.*?\s+to\s+{target}\s*$"),
            ("remove_label", rf"\bremove\s+(?:the\s+)?label\b.*?\s+from\s+{target}\s*$"),
            ("set_project", rf"\b(?:set|change|update)\s+(?:the\s+)?project\s+of\s+{target}\s+to\b"),
            ("set_project", rf"\b(?:clear|remove)\s+(?:the\s+)?project\s+(?:of|from)\s+{target}\b"),
            ("set_cycle", rf"\b(?:set|change|update)\s+(?:the\s+)?cycle\s+of\s+{target}\s+to\b"),
            ("set_cycle", rf"\b(?:clear|remove)\s+(?:the\s+)?cycle\s+(?:of|from)\s+{target}\b"),
            ("mark_duplicate", rf"\b(?:mark|set)\s+(?:issue\s+)?{target}\s+as\s+(?:a\s+)?duplicate\s+of\b"),
        ]
        for pattern_operation, pattern in patterns:
            if operation and pattern_operation != operation:
                continue
            match = re.search(pattern, raw, re.IGNORECASE)
            if match:
                return str(match.group("target") or "").strip()
        return ""


    def _linear_channel_destructive_command(self, text: Any) -> bool:
        """Detect unsupported destructive commands without scanning edit values."""

        command = str(text or "").strip()
        command = re.sub(r"^(?:<@[A-Z0-9]+>\s*)+", "", command).strip()
        command = re.sub(
            r"^(?:(?:please|roo)\s*[,;:]?\s*)+", "", command,
            flags=re.IGNORECASE,
        ).strip()
        command = re.sub(
            r"^(?:can|could|would|will)\s+you(?:\s+please)?\s*[,;:]?\s*",
            "",
            command,
            flags=re.IGNORECASE,
        ).strip()
        return bool(re.match(
            r"^(?:delete|archive|trash|restore)\b|"
            r"^move\s+(?:it|the\s+issue|[A-Z]+-\d+)\s+to\s+(?:another|the)\s+team\b|"
            r"^edit\s+(?:an?\s+)?existing\s+comment\b",
            command,
            re.IGNORECASE,
        ))


    def _linear_channel_write_error(self, exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            detail = self._extract_http_error_detail(exc)
            if exc.response.status_code == 409:
                return detail or "That Linear issue changed. Review it and retry your edit."
            return detail or "The Linear request was rejected. Nothing else was attempted."
        return f"I couldn't safely complete that Linear request: {str(exc) or exc.__class__.__name__}"


