"""Github integration skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from typing import Optional
from ..loader import Skill
from ...content_factory_identity import build_content_factory_identity_payload
from ...config import get_settings
import json
from ...slack_client import post_message
from .publishing_support import PublishingSupport


class GithubIntegrationHandler(PublishingSupport):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_github_integration(skill, text, params, user_id, channel_id, thread_ts)
        return result

    async def _execute_github_integration(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str]
    ) -> str:
        """Execute the GitHub Integration skill."""
        from roo.clients.mlai_backend import MLAIBackendUnavailableError
        
        # Get API client for GitHub token operations
        settings = get_settings()
        from roo.clients.mlai_backend import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY
        )
        
        # 1. Check for valid integration & handle errors
        domain = params.get("domain")
        action = params.get("action")
        try:
            integration = await api_client.get_integration(user_id, domain=domain)
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()

        if action == "reconnect":
            resolved_domain = domain
            connected_domains = integration.get("connected_domains", []) if integration else []
            if not resolved_domain:
                if len(connected_domains) == 1:
                    resolved_domain = connected_domains[0].get("domain")
                elif len(connected_domains) > 1:
                    domain_list = "\n".join(
                        f"  • `{item['domain']}` → `{item.get('github_repo', 'unknown')}`"
                        for item in connected_domains
                    )
                    return (
                        "You have multiple connected codebases. Tell me which one to reconnect GitHub for:\n\n"
                        f"{domain_list}\n\nTry: `@Roo reconnect to github for <domain>`"
                    )

            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=resolved_domain,
                github_repo=None,
                trigger="manual",
                pending_action="reconnect_github",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=(
                    f"Reconnect GitHub for {resolved_domain}"
                    if resolved_domain
                    else "Reconnect GitHub"
                ),
            )
            if reconnect_result is not None:
                return reconnect_result
            if resolved_domain:
                return f"GitHub is already connected for {resolved_domain}."
            return "GitHub is already connected."
        
        # Check for Expired Token or Other Errors (Same as content-factory)
        if integration and integration.get("error"):
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Re-connect GitHub",
                include_resume=True,
                resume_action="resume_scan",
                resume_value={"domain": domain} if domain else {},
            )
            if reconnect_result is not None:
                return reconnect_result
            return f"GitHub connection issue ({integration.get('error')})."

        if not integration:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Connect GitHub Account",
            )
            if reconnect_result is not None:
                return reconnect_result
            return "GitHub is already connected."

        # If a specific domain was requested, require domain-level connectivity.
        if domain and integration.get("needs_github_auth"):
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Connect GitHub for {domain}",
            )
            if reconnect_result is not None:
                return reconnect_result

        # 2. Determine Repo Name
        # When the user names a domain explicitly, prefer the domain-resolved repo.
        connected_domains = integration.get("connected_domains", [])
        repo_name, domain_info = self._resolve_content_factory_repo_name(
            integration,
            connected_domains,
            domain,
        )
        
        if not repo_name:
             reconnect_result = await self._request_github_reconnect(
                 api_client,
                 user_id=user_id,
                 domain=domain,
                 github_repo=None,
                 trigger="manual",
                 pending_action="scan",
                 channel_id=channel_id,
                 thread_ts=thread_ts,
                 button_label="Reconnect GitHub & Select Repo",
             )
             if reconnect_result is not None:
                 return reconnect_result
             return "Please select a repository to scan."

        scan_completed = bool(
            integration.get("scan_completed")
            or integration.get("content_research_ready")
            or integration.get("project_scanned")
            or (domain_info and domain_info.get("scanned"))
        )
        if (
            self._is_explicit_scan_request(text, params)
            and scan_completed
            and integration.get("recommended_next_action") != "scan"
            and not integration.get("has_updates")
        ):
            return self._build_existing_scan_confirmation(
                domain=domain,
                repo_name=repo_name,
                last_scanned=integration.get("last_scanned_at", "Never"),
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        # 3. Trigger Scan via Backend
        if channel_id:
            scan_msg = f"🔍 Requesting scan for `{repo_name}`..."
            post_message(channel_id, scan_msg, thread_ts=thread_ts)

        try:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain or 'this domain'}",
            )
            if reconnect_result is not None:
                return reconnect_result

            # Trigger via Backend Client
            scan_result = await api_client.trigger_repo_scan(
                user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain
            )
            
            if scan_result.get("error"):
                error_msg = scan_result.get("message", "Unknown error")
                # Check if auth-related — show reconnect button
                if scan_result.get("needs_github_auth"):
                    oauth_url = scan_result.get("oauth_url")
                    if not oauth_url:
                        try:
                            auth_resp = await api_client.get_github_auth_url(user_id, domain=domain)
                            oauth_url = auth_resp.get("auth_url")
                        except MLAIBackendUnavailableError:
                            return self._content_factory_backend_unavailable_message()
                        except Exception:
                            oauth_url = None
                    if oauth_url and channel_id:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"❌ Scan failed: {error_msg}"
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Re-connect GitHub",
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "danger"
                                    },
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "I've Connected - Resume",
                                            "emoji": True
                                        },
                                        "action_id": "resume_scan",
                                        "value": json.dumps(
                                            build_content_factory_identity_payload(
                                                requested_by_slack_user_id=requested_by_slack_user_id,
                                                effective_slack_user_id=effective_slack_user_id,
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                            )
                                        ),
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        post_message(channel_id, f"Scan failed: {error_msg}", thread_ts=thread_ts, blocks=blocks)
                        return "Please re-connect your GitHub account using the button above. 🔌"
                return f"❌ Scan failed: {error_msg}"

            # Backend might return status: 'started' or 'queued'
            return f"✅ Scan started for `{repo_name}`! I'll let you know when the backend updates."
            
        except Exception as e:
            print(f"GitHub Integration Scan Error: {e}")
            return f"Sorry mate, I had trouble triggering the scan: {str(e)}"


