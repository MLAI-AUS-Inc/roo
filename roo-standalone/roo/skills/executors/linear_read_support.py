"""Linear read support skill execution."""
from __future__ import annotations
from typing import Any, Optional, List
from ...slack_client import get_bot_user_id
import re


class LinearReadSupport:
    async def _list_all_linear_channel_issues(
        self,
        client: Any,
        *,
        slack_workspace_id: str,
        slack_channel_id: str,
        requester_slack_id: str,
        status: Optional[str] = None,
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
            if status:
                request["status"] = status
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
        write_confirmation = re.compile(
            r"^\s*Updated\s+`[A-Z][A-Z0-9]{1,15}-\d+`:\s+"
            r"[a-z_ ]+\s+completed\s+in\s+Linear\.\s*$",
            re.IGNORECASE,
        )
        create_confirmation = re.compile(
            r"^\s*Created\s+`[A-Z][A-Z0-9]{1,15}-\d+`\s+—\s+.+?\s+"
            r"in\s+MLAI_TECH\.(?:\s+<https://linear\.app/[^>]+\|Open in Linear>)?\s*$",
            re.IGNORECASE,
        )
        ambiguous_choices = re.compile(
            r"^\s*I found a few matching MLAI_TECH issues\. Which one do you mean\?\s*$"
            r"[\s\S]*^\s*[•*-]\s+`[A-Z][A-Z0-9]{1,15}-\d+`\s+—",
            re.IGNORECASE | re.MULTILINE,
        )
        return [
            message
            for message in self._roo_authored_thread_messages(thread_history)
            if numbered_issue.search(str(message.get("text") or ""))
            or detail_heading.search(str(message.get("text") or ""))
            or write_confirmation.search(str(message.get("text") or ""))
            or create_confirmation.search(str(message.get("text") or ""))
            or ambiguous_choices.search(str(message.get("text") or ""))
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
        identifier = self._linear_issue_identifier(text) or self._linear_issue_identifier(explicit)
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


    def _coerce_data_query_limit(self, value: Any, *, default: int) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(limit, 100))


