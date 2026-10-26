"""Publishing support skill execution."""
from __future__ import annotations
from typing import Any, Optional, List
from ...content_factory_progress import CONTENT_FACTORY_REQUEST_SOURCE, normalize_content_factory_domain
from ...clients.mlai_backend import MLAIBackendUnavailableError
from ...content_factory_identity import build_content_factory_identity_payload, is_delegated_content_factory_request, resolve_content_factory_identity_context
import httpx
from ...content_intent import is_explicit_scan_request
import json
from ...slack_client import post_message
from uuid import uuid4


class PublishingSupport:
    def _is_explicit_scan_request(self, text: str, params: dict) -> bool:
        """Return True when the user is explicitly asking Roo to scan a repo/codebase."""
        return is_explicit_scan_request(text, params.get("action"))


    def _build_existing_scan_confirmation(
        self,
        *,
        domain: Optional[str],
        repo_name: Optional[str],
        last_scanned: Optional[str],
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Prompt the user before triggering a fresh scan when one already exists."""
        target = f"*{domain}*" if domain else f"`{repo_name or 'this repository'}`"
        repo_line = f"\n• Repository: `{repo_name}`" if repo_name else ""
        last_scanned_display = last_scanned or "Unknown"

        return {
            "message": (
                f"I already have a scan for {domain or repo_name or 'this codebase'}. "
                f"Last scanned: {last_scanned_display}. "
                "Do you want me to run another scan anyway?"
            ),
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"I've already scanned {target}.{repo_line}\n"
                            f"• Last scanned: {last_scanned_display}\n\n"
                            "Do you want me to run another scan anyway?"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": "repeat_scan_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Scan Again",
                                "emoji": True,
                            },
                            "style": "primary",
                            "value": json.dumps(
                                build_content_factory_identity_payload(
                                    requested_by_slack_user_id=requested_by_slack_user_id or user_id,
                                    effective_slack_user_id=effective_slack_user_id or user_id,
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    rescan=True,
                                )
                            ),
                            "action_id": "prerequisite_scan",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Keep Existing Scan",
                                "emoji": True,
                            },
                            "value": json.dumps(
                                build_content_factory_identity_payload(
                                    requested_by_slack_user_id=requested_by_slack_user_id or user_id,
                                    effective_slack_user_id=effective_slack_user_id or user_id,
                                    domain=domain,
                                )
                            ),
                            "action_id": "prerequisite_cancel",
                        },
                    ],
                },
            ],
        }


    def _build_github_reconnect_blocks(
        self,
        message: str,
        auth_url: str,
        *,
        button_label: str,
        include_resume: bool = False,
        resume_action: str = "resume_scan",
        resume_value: Optional[dict] = None,
    ) -> list[dict]:
        elements = [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": button_label,
                    "emoji": True,
                },
                "url": auth_url,
                "action_id": "connect_github",
                "style": "primary",
            }
        ]
        if include_resume:
            elements.append(
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "I've Connected - Retry",
                        "emoji": True,
                    },
                    "action_id": resume_action,
                    "value": json.dumps(resume_value or {}),
                    "style": "primary",
                }
            )
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message,
                },
            },
            {
                "type": "actions",
                "elements": elements,
            },
        ]


    @staticmethod
    def _content_factory_backend_unavailable_message() -> str:
        return (
            "I couldn't reach MLAI backend just now, so I haven't started the Content Factory flow. "
            "Please try again in a moment."
        )


    @staticmethod
    def _already_posted_response(message: str, *, blocks: Optional[list] = None) -> dict:
        response = {
            "message": message,
            "suppress_post": True,
        }
        if blocks is not None:
            response["blocks"] = blocks
        return response


    @staticmethod
    def _delegated_content_factory_auth_required_message(
        *,
        effective_slack_user_id: Optional[str],
        domain: Optional[str],
    ) -> str:
        target = f"<@{effective_slack_user_id}>" if effective_slack_user_id else "that user"
        domain_label = normalize_content_factory_domain(domain) or domain or "this domain"
        return (
            f"GitHub auth for {target} isn't available for {domain_label}. "
            "Ask them to reconnect GitHub, then retry the delegated run."
        )


    async def _request_github_reconnect(
        self,
        api_client,
        *,
        user_id: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
        domain: Optional[str],
        github_repo: Optional[str],
        trigger: str,
        pending_action: Optional[str],
        channel_id: Optional[str],
        thread_ts: Optional[str],
        button_label: str,
        text: Optional[str] = None,
        params: Optional[dict] = None,
        save_pending: bool = False,
        include_resume: bool = False,
        resume_action: str = "resume_scan",
        resume_value: Optional[dict] = None,
    ) -> Optional[Any]:
        from ...clients.mlai_backend import MLAIBackendUnavailableError

        delegated_effective_slack_user_id = str(
            effective_slack_user_id or user_id or ""
        ).strip()
        delegated_requested_by_slack_user_id = str(
            requested_by_slack_user_id or user_id or ""
        ).strip()
        is_delegated = is_delegated_content_factory_request(
            delegated_requested_by_slack_user_id,
            delegated_effective_slack_user_id,
        )

        try:
            reconnect = await api_client.reconnect_content_factory_github(
                slack_user_id=user_id,
                domain=domain,
                github_repo=github_repo,
                trigger=trigger,
                pending_action=pending_action,
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        if reconnect.get("status") == "already_connected":
            return None

        if reconnect.get("status") != "auth_started":
            return None

        if is_delegated:
            return self._delegated_content_factory_auth_required_message(
                effective_slack_user_id=delegated_effective_slack_user_id,
                domain=domain,
            )

        if save_pending and text is not None and params is not None:
            await self._save_content_factory_pending_intent(
                api_client,
                user_id,
                params,
                text,
                channel_id,
                thread_ts,
            )

        auth_url = reconnect.get("auth_url")
        message = reconnect.get("message") or "Reconnect GitHub before I continue."
        if not auth_url:
            return message

        resolved_resume_value = None
        if include_resume:
            resolved_resume_value = build_content_factory_identity_payload(
                requested_by_slack_user_id=delegated_requested_by_slack_user_id,
                effective_slack_user_id=delegated_effective_slack_user_id,
                **(resume_value or {}),
            )

        blocks = self._build_github_reconnect_blocks(
            message,
            auth_url,
            button_label=button_label,
            include_resume=include_resume,
            resume_action=resume_action,
            resume_value=resolved_resume_value,
        )
        short_message = (
            f"{message} Use the button above to continue."
            if channel_id
            else f"{message}\n\n{auth_url}"
        )
        if channel_id:
            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
            return self._already_posted_response(short_message, blocks=blocks)
        return {
            "message": short_message,
            "blocks": blocks,
        }


    async def _save_content_factory_pending_intent(
        self,
        api_client: Any,
        user_id: str,
        params: dict,
        text: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        ) -> None:
        """Persist the original content request so GitHub auth/repo selection can resume it."""
        from ...clients.mlai_backend import MLAIBackendUnavailableError

        params = dict(params or {})
        client_request_id = self._get_content_factory_client_request_id(params)
        params["client_request_id"] = client_request_id
        domain = normalize_content_factory_domain(params.get("domain"))
        action = str(params.get("action") or "").strip().lower()
        (
            requested_by_slack_user_id,
            effective_slack_user_id,
            is_delegated,
        ) = (
            lambda identity: (
                identity.requested_by_slack_user_id,
                identity.effective_slack_user_id,
                identity.is_delegated,
            )
        )(
            resolve_content_factory_identity_context(
                requester_slack_user_id=user_id,
                requested_by_slack_user_id=params.get("requested_by_slack_user_id"),
                effective_slack_user_id=params.get("effective_slack_user_id"),
            )
        )

        if is_delegated:
            return

        backend_intent: dict[str, Any] = {
            "type": "roo_content_factory",
            "skill": "content-factory",
            "action": action or None,
            "params": params,
            "text": text,
            "channel": channel_id,
            "ts": thread_ts,
        }
        if action == "write" and domain:
            backend_intent = {
                "type": "write_article",
                "article_request": {
                    "domain": domain,
                    "topic": params.get("topic"),
                    "target_keyword": params.get("target_keyword"),
                    "context": params.get("context"),
                    "delivery_mode": params.get("delivery_mode"),
                    "delivery_mode_confirmed": params.get("delivery_mode_confirmed"),
                    "slack_channel_id": channel_id,
                    "slack_thread_ts": thread_ts,
                    "slack_root_message_ts": thread_ts,
                    "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                    "client_request_id": client_request_id,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
                "resume_context": {
                    "text": text,
                    "params": params,
                    "channel": channel_id,
                    "ts": thread_ts,
                },
            }

        if action == "write" and domain:
            from ... import main as main_module

            main_module._remember_pending_intent(
                requested_by_slack_user_id,
                domain,
                effective_slack_user_id=effective_slack_user_id,
                intent_data={
                    "action": "write",
                    "topic": params.get("topic"),
                    "target_keyword": params.get("target_keyword"),
                    "context": params.get("context"),
                    "delivery_mode": params.get("delivery_mode"),
                    "delivery_mode_confirmed": params.get("delivery_mode_confirmed"),
                    "client_request_id": client_request_id,
                    "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
                    "text": text,
                    "params": params,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                    "effective_slack_user_id": effective_slack_user_id,
                },
                channel_id=channel_id,
                thread_ts=thread_ts,
                wait_for="scan_complete",
                clear_job_id=True,
            )

        try:
            await api_client.save_pending_intent(requested_by_slack_user_id, backend_intent)
        except MLAIBackendUnavailableError as exc:
            print(
                "⚠️ Failed to persist content-factory pending intent to mlai-backend: "
                f"{exc!r}"
            )
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            print(
                "⚠️ Failed to persist content-factory pending intent to mlai-backend: "
                f"{detail}"
            )


    @staticmethod
    def _get_content_factory_client_request_id(params: dict) -> str:
        existing = str(params.get("client_request_id") or "").strip()
        if existing:
            return existing
        return f"content-factory-{uuid4().hex}"


    def _get_connected_domain_info(
        self,
        connected_domains: List[dict],
        domain: Optional[str],
    ) -> Optional[dict]:
        """Return the connected domain record for the requested domain."""
        if not domain or not connected_domains:
            return None

        return next(
            (domain_info for domain_info in connected_domains if domain_info.get("domain") == domain),
            None,
        )


    def _resolve_content_factory_repo_name(
        self,
        integration: dict,
        connected_domains: List[dict],
        domain: Optional[str],
    ) -> tuple[Optional[str], Optional[dict]]:
        """Resolve the repo Roo should use for content-factory flows."""
        domain_info = self._get_connected_domain_info(connected_domains, domain)

        if domain:
            return (
                integration.get("domain_github_repo")
                or (domain_info or {}).get("github_repo"),
                domain_info,
            )

        return integration.get("github_repo"), domain_info


