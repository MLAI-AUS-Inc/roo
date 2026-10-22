"""Admin actions skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from ...backend_identity import BackendIdentityError, get_backend_actor_context
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from typing import Optional
from ...admin_brain import build_admin_action_list_response, build_admin_action_response
from ...config import get_settings
import hashlib
import httpx
import json


class AdminActionsHandler:
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_admin_actions(
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        return result

    async def _execute_admin_actions(
        self,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> dict:
        """Create or review only backend-allowlisted controlled actions."""

        settings = get_settings()
        if (
            settings.ROO_SURFACE != "admin"
            or not settings.ORG_BRAIN_ENABLED
            or not settings.ORG_BRAIN_ACTIONS_ENABLED
            or not settings.ORG_BRAIN_API_KEY
        ):
            return {
                "message": "Controlled Admin Roo actions are not enabled.",
                "data": {"admin_action": True, "enabled": False},
            }

        action = str(params.get("action") or "").strip()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            service_principal_key=settings.ORG_BRAIN_API_KEY,
            surface=settings.ROO_SURFACE,
            actor_context=get_backend_actor_context(),
        )
        timeout = float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS)
        try:
            if action == "list_pending":
                payload = await client.list_org_memory_actions(
                    limit=50,
                    timeout=timeout,
                )
                return build_admin_action_list_response(payload)

            if action == "show_action":
                proposal_id = str(params.get("proposal_id") or "").strip()
                if not proposal_id:
                    return {
                        "message": (
                            "Give me the controlled-action proposal UUID to open."
                        ),
                        "data": {
                            "admin_action": True,
                            "action": action,
                        },
                    }
                proposal = await client.get_org_memory_action(
                    proposal_id,
                    timeout=timeout,
                )
                return build_admin_action_response(proposal)

            action_type, input_payload, missing = self._admin_action_payload(
                action=action,
                params=params,
                channel_id=channel_id,
            )
            if missing:
                return {
                    "message": (
                        "I haven't created a proposal. Please provide: "
                        + ", ".join(f"`{value}`" for value in missing)
                        + "."
                    ),
                    "data": {"admin_action": True, "action": action},
                }
            if not action_type:
                return {
                    "message": (
                        "Ask me to list pending actions, open a proposal UUID, "
                        "create a local Gmail/Slack/Notion draft, or propose a "
                        "scoped Linear issue create/update."
                    ),
                    "data": {
                        "admin_action": True,
                        "action": action or "help",
                    },
                }

            actor = get_backend_actor_context()
            idempotency_material = {
                "event_id": getattr(actor, "event_id", ""),
                "action_type": action_type,
                "configuration_id": str(
                    params.get("configuration_id") or ""
                ),
                "input_payload": input_payload,
            }
            proposal_key = "roo-proposal-" + hashlib.sha256(
                json.dumps(
                    idempotency_material,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            proposal = await client.create_org_memory_action(
                action_type=action_type,
                input_payload=input_payload,
                configuration_id=(
                    str(params.get("configuration_id") or "").strip()
                    or None
                ),
                idempotency_key=proposal_key,
                timeout=timeout,
            )
            if not proposal.get("requires_approval"):
                proposal = await client.execute_org_memory_action(
                    str(proposal.get("id") or ""),
                    idempotency_key=f"roo-draft-execute-{proposal['id']}",
                    timeout=timeout,
                )
            return build_admin_action_response(proposal)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            try:
                detail = str(
                    exc.response.json().get("detail") or ""
                ).strip()
            except (ValueError, AttributeError):
                detail = ""
            print(
                "ADMIN_ACTION_REQUEST_FAILED "
                f"status={status_code} action={action} user_id={user_id} "
                f"channel_id={channel_id}"
            )
            if status_code in {401, 403, 404}:
                message = (
                    "I can't access that controlled action in this context."
                )
            elif status_code == 400 and detail:
                message = f"I haven't changed anything. {detail}"
            else:
                message = (
                    "The controlled-action gateway is unavailable; "
                    "nothing was changed."
                )
            return {
                "message": message,
                "data": {"admin_action": True, "action": action},
            }
        except (
            BackendIdentityError,
            MLAIBackendUnavailableError,
            ValueError,
        ) as exc:
            print(
                "ADMIN_ACTION_UNAVAILABLE "
                f"error={exc.__class__.__name__} action={action} "
                f"channel_id={channel_id}"
            )
            return {
                "message": (
                    "The controlled-action gateway is unavailable; "
                    "nothing was changed."
                ),
                "data": {"admin_action": True, "action": action},
            }
        except Exception as exc:
            print(
                "ADMIN_ACTION_FAILED "
                f"error={exc.__class__.__name__} action={action} "
                f"channel_id={channel_id}"
            )
            return {
                "message": (
                    "The controlled-action request failed closed; "
                    "nothing was changed."
                ),
                "data": {"admin_action": True, "action": action},
            }


    @staticmethod
    def _admin_action_payload(
        *,
        action: str,
        params: dict,
        channel_id: Optional[str],
    ) -> tuple[Optional[str], dict, list[str]]:
        mapping = {
            "draft_gmail": "draft_gmail",
            "draft_slack_post": "draft_slack_post",
            "draft_notion_update": "draft_notion_update",
            "create_linear_issue": "create_linear_issue",
            "update_linear_issue": "update_linear_issue",
        }
        action_type = mapping.get(action)
        if not action_type:
            return None, {}, []

        if action == "draft_gmail":
            raw_recipients = params.get("to") or []
            recipients = (
                [
                    value.strip()
                    for value in raw_recipients.split(",")
                    if value.strip()
                ]
                if isinstance(raw_recipients, str)
                else [
                    str(value).strip()
                    for value in raw_recipients
                    if str(value).strip()
                ]
            )
            payload = {
                "to": recipients,
                "subject": str(params.get("subject") or "").strip(),
                "body": str(params.get("body") or "").strip(),
            }
            missing = [
                name
                for name, present in (
                    ("to", bool(payload["to"])),
                    ("subject", bool(payload["subject"])),
                    ("body", bool(payload["body"])),
                )
                if not present
            ]
            return action_type, payload, missing

        if action == "draft_slack_post":
            payload = {
                "channel_id": str(
                    params.get("channel_id") or channel_id or ""
                ).strip(),
                "text": str(params.get("text") or "").strip(),
            }
            if params.get("thread_ts"):
                payload["thread_ts"] = str(params["thread_ts"]).strip()
            missing = [
                name
                for name in ("channel_id", "text")
                if not payload.get(name)
            ]
            return action_type, payload, missing

        if action == "draft_notion_update":
            payload = {
                "page_id": str(params.get("page_id") or "").strip(),
                "title": str(params.get("title") or "").strip(),
                "body": str(params.get("body") or "").strip(),
            }
            missing = [name for name in payload if not payload[name]]
            return action_type, payload, missing

        payload = {
            key: str(params.get(key) or "").strip()
            for key in (
                "team_id",
                "project_id",
                "title",
                "description",
                "assignee_id",
                "due_date",
                "state_id",
            )
            if params.get(key) not in (None, "")
        }
        if params.get("priority") not in (None, ""):
            payload["priority"] = params["priority"]
        if params.get("label_ids"):
            raw_labels = params["label_ids"]
            payload["label_ids"] = (
                [
                    value.strip()
                    for value in str(raw_labels).split(",")
                    if value.strip()
                ]
                if isinstance(raw_labels, str)
                else [
                    str(value).strip()
                    for value in raw_labels
                    if str(value).strip()
                ]
            )
        if action == "update_linear_issue":
            payload["issue_id"] = str(
                params.get("issue_id") or ""
            ).strip()
        required = ["configuration_id", "project_id"]
        if action == "create_linear_issue":
            required.extend(("team_id", "title"))
        else:
            required.append("issue_id")
        missing = [
            name
            for name in required
            if not (
                params.get(name)
                if name == "configuration_id"
                else payload.get(name)
            )
        ]
        if action == "update_linear_issue":
            mutable = {
                "team_id",
                "title",
                "description",
                "assignee_id",
                "priority",
                "due_date",
                "label_ids",
                "state_id",
            }
            if not any(
                payload.get(name) not in (None, "", [])
                for name in mutable
            ):
                missing.append("at least one changed Linear field")
        return action_type, payload, missing


