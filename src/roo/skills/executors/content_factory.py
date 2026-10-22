"""Content factory skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from typing import Any, Optional, List
from ...content_factory_progress import CONTENT_FACTORY_ARTICLE_COST_POINTS, CONTENT_FACTORY_REQUEST_SOURCE, build_live_status_blocks, get_content_factory_article_cost_points, is_free_content_factory_domain, normalize_content_factory_domain
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from ..loader import Skill
import asyncio
from ...content_factory_identity import build_content_factory_identity_payload, is_delegated_content_factory_request, resolve_content_factory_identity_context
from ...content_intent import detect_content_action
from ...config import get_settings
import httpx
import json
from ...slack_client import post_message
from .publishing_support import PublishingSupport


class ContentFactoryExecutor(PublishingSupport, SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_content_factory(skill, text, params, user_id, channel_id, thread_ts, thread_history)
        return result

    def _should_prompt_for_article_direction(self, text: str, params: dict) -> bool:
        """Prompt for topic-vs-research only on generic article requests."""
        if (params.get("topic") or "").strip():
            return False

        text_lower = text.lower()
        explicit_research_phrases = (
            "research the best article",
            "discover the best article",
            "find the best article",
            "best article to write",
            "best article for me",
            "what should i write about",
            "recommend a topic",
            "suggest a topic",
            "suggest an article topic",
            "article topic idea",
            "auto write",
            "auto-write",
        )
        if any(phrase in text_lower for phrase in explicit_research_phrases):
            return False

        research_verbs = ("research", "discover", "find", "suggest", "recommend", "choose", "pick")
        research_nouns = ("topic", "keyword", "article", "blog post", "idea")
        if any(verb in text_lower for verb in research_verbs) and any(noun in text_lower for noun in research_nouns):
            return False

        return True


    def _build_article_direction_blocks(
        self,
        domain: str,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        client_request_id: str,
        delivery_mode: Optional[str] = None,
        delivery_mode_confirmed: bool = False,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> list[dict]:
        """Build the preflight prompt for generic article requests."""
        normalized_domain = normalize_content_factory_domain(domain) or domain
        cost_points = get_content_factory_article_cost_points(domain)
        cost_text = (
            f"*Cost:* Articles for *{normalized_domain}* are free. No Roo points will be deducted for this run."
            if cost_points == 0
            else f"*Cost:* Starting the article run deducts {cost_points} Roo points."
        )
        button_payload = build_content_factory_identity_payload(
            requested_by_slack_user_id=requested_by_slack_user_id or user_id,
            effective_slack_user_id=effective_slack_user_id or user_id,
            domain=domain,
            channel_id=channel_id,
            thread_ts=thread_ts,
            client_request_id=client_request_id,
        )
        if delivery_mode is not None:
            button_payload["delivery_mode"] = delivery_mode
            button_payload["delivery_mode_confirmed"] = delivery_mode_confirmed
        button_value = json.dumps(button_payload)

        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Before I start on *{domain}*, do you already have a topic in mind?\n\n"
                        "If you do, send it through and I'll still research the best keywords, title, "
                        "and talking points so the article has the best chance to rank.\n\n"
                        "If you don't, I can research the strongest article opportunity for you.\n\n"
                        f"{cost_text}"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "article_direction_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Research the Best Article for Me",
                            "emoji": True,
                        },
                        "style": "primary",
                        "value": button_value,
                        "action_id": "article_research_best",
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "I'll Give You the Topic",
                            "emoji": True,
                        },
                        "value": button_value,
                        "action_id": "article_provide_topic",
                    },
                ],
            },
        ]


    def _resolve_requested_article_delivery_mode(
        self,
        text: str,
        params: dict,
    ) -> tuple[Optional[str], bool]:
        raw_mode = str(params.get("delivery_mode") or "").strip().lower()
        if raw_mode in {"content_only", "publish_code"}:
            confirmed = params.get("delivery_mode_confirmed")
            return raw_mode, bool(True if confirmed is None else confirmed)

        lowered = text.lower()
        content_only_phrases = (
            "content-only",
            "content only",
            "just the copy",
            "manual upload",
            "copy only",
            "text only",
        )
        if any(phrase in lowered for phrase in content_only_phrases):
            return "content_only", True

        publish_phrases = (
            "publish code",
            "publish-mode",
            "publish mode",
            "open a pr",
            "create a pr",
            "draft pr",
            "push it to the repo",
            "push this to the repo",
        )
        if any(phrase in lowered for phrase in publish_phrases):
            return "publish_code", True

        return None, False


    def _build_article_delivery_mode_prompt(
        self,
        *,
        domain: str,
        job_id: str,
        topic: Optional[str],
        recommended_delivery_mode: Optional[str],
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        topic_line = f"*Topic:* {topic}\n" if topic else ""
        recommended_line = ""
        if recommended_delivery_mode == "content_only":
            recommended_line = "\n_Recommended for this domain right now: content-only._"
        elif recommended_delivery_mode == "publish_code":
            recommended_line = "\n_Recommended for this domain right now: publish via code._"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Choose how you want me to deliver the article for *{domain}*.\n\n"
                        f"{topic_line}"
                        "• *Content-only*: research and write the article package for manual upload.\n"
                        "• *Publish via code*: create the draft through the connected repo flow."
                        f"{recommended_line}"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": "article_delivery_mode_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Content Only", "emoji": True},
                        "style": "primary" if recommended_delivery_mode != "publish_code" else None,
                        "value": json.dumps(
                            build_content_factory_identity_payload(
                                requested_by_slack_user_id=requested_by_slack_user_id,
                                effective_slack_user_id=effective_slack_user_id,
                                job_id=job_id,
                                domain=domain,
                                delivery_mode="content_only",
                            )
                        ),
                        "action_id": "select_article_delivery_mode",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Publish Via Code", "emoji": True},
                        "style": "primary" if recommended_delivery_mode == "publish_code" else None,
                        "value": json.dumps(
                            build_content_factory_identity_payload(
                                requested_by_slack_user_id=requested_by_slack_user_id,
                                effective_slack_user_id=effective_slack_user_id,
                                job_id=job_id,
                                domain=domain,
                                delivery_mode="publish_code",
                            )
                        ),
                        "action_id": "select_article_delivery_mode",
                    },
                ],
            },
        ]
        for element in blocks[1]["elements"]:
            if element.get("style") is None:
                element.pop("style", None)

        return {
            "message": f"Choose the delivery mode for {domain} before I continue.",
            "blocks": blocks,
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": False,
                "content_factory_watchdog_mode": "awaiting_delivery_mode",
                "content_factory_domain": domain,
                "content_factory_workflow": "awaiting_delivery_mode",
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }


    @staticmethod
    def _delegated_backend_kwargs(
        requested_by_slack_user_id: Optional[str],
        effective_slack_user_id: Optional[str],
    ) -> dict[str, str]:
        if is_delegated_content_factory_request(
            requested_by_slack_user_id,
            effective_slack_user_id,
        ):
            return {
                "requested_by_slack_user_id": requested_by_slack_user_id,
            }
        return {}


    async def _validate_content_factory_paid_access(
        self,
        api_client: Any,
        user_id: str,
        domain: Optional[str],
    ) -> Optional[str]:
        from ...clients.mlai_backend import MLAIBackendUnavailableError
        from ...slack_client import get_user_info

        normalized_domain = normalize_content_factory_domain(domain) or "this domain"
        cost_points = get_content_factory_article_cost_points(domain)
        slack_info = get_user_info(user_id)
        email = str(slack_info.get("email") or "").strip().lower()
        if not email:
            if cost_points == 0:
                return (
                    "I need access to your real Slack email before I can start Content Factory for you. "
                    f"Once that's available, articles for {normalized_domain} are free."
                )
            return (
                "I need access to your real Slack email before I can start Content Factory for you. "
                f"Once that's available, creating an article costs {cost_points} Roo points."
            )

        real_name = str(slack_info.get("real_name") or "").strip()
        name_parts = real_name.split(" ", 1) if real_name else []
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        avatar_url = str(slack_info.get("image_192") or "").strip() or None

        try:
            await api_client.ensure_slack_user_registered(
                slack_id=user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url,
            )
        except MLAIBackendUnavailableError as exc:
            print(f"⚠️ Failed to register content-factory user {user_id}: {exc!r}")
            if cost_points == 0:
                return None
            return self._content_factory_backend_unavailable_message()
        except Exception as exc:
            print(f"⚠️ Failed to register content-factory user {user_id}: {exc!r}")
            if cost_points == 0:
                return None
            return (
                "I couldn't link your Slack account to MLAI yet, so I haven't charged anything. "
                "Please try again in a moment."
            )

        if cost_points == 0:
            return None

        try:
            balance_data = await api_client.get_balance(user_id)
        except MLAIBackendUnavailableError as exc:
            print(f"⚠️ Failed to fetch Roo points balance for {user_id}: {exc!r}")
            return self._content_factory_backend_unavailable_message()
        except Exception as exc:
            print(f"⚠️ Failed to fetch Roo points balance for {user_id}: {exc!r}")
            return (
                "I couldn't check your Roo points balance just now, so I haven't started the article yet. "
                "Please try again in a moment."
            )

        balance = int(balance_data.get("balance") or 0)
        if balance < cost_points:
            return (
                f"Creating an article costs {cost_points} Roo points, and your balance is below "
                "that amount. DM Roo `points` to view your balance privately."
            )

        return None


    def _build_content_factory_start_response(
        self,
        *,
        domain: str,
        job_id: str,
        topic: Optional[str],
        workflow: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        is_discovery = workflow == "auto_discovery" or not topic
        summary_text = (
            "Starting discovery to find the best article opportunity. I'll keep this message updated."
            if is_discovery
            else f"Starting article generation for `{topic}`. I'll keep this message updated."
        )

        return {
            "message": (
                f"Starting Content Factory for {domain}. "
                "I'll keep this message updated as the run moves forward."
            ),
            "blocks": build_live_status_blocks(
                domain,
                summary_text=summary_text,
                include_decision_stage=is_discovery,
                current_stage="preparing",
            ),
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": True,
                "content_factory_watchdog_mode": workflow,
                "content_factory_domain": domain,
                "content_factory_workflow": workflow,
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }


    def _build_publish_pr_start_response(
        self,
        *,
        domain: Optional[str],
        job_id: str,
        requested_by_slack_user_id: Optional[str] = None,
        effective_slack_user_id: Optional[str] = None,
    ) -> dict:
        display_domain = normalize_content_factory_domain(domain) or domain or "this domain"
        return {
            "message": (
                f"Publishing the completed article bundle for {display_domain} as a draft PR. "
                "I'll keep this message updated as the run moves forward."
            ),
            "blocks": build_live_status_blocks(
                display_domain,
                summary_text=(
                    "Promoting the completed article bundle into the repo and preview flow. "
                    "I'll keep this message updated."
                ),
                include_decision_stage=False,
                current_stage="preparing",
            ),
            "data": {
                "content_factory_progress_job_id": job_id,
                "content_factory_watchdog": True,
                "content_factory_watchdog_mode": "publish_pr",
                "content_factory_domain": display_domain,
                "content_factory_workflow": "publish_pr",
                "requested_by_slack_user_id": requested_by_slack_user_id,
                "effective_slack_user_id": effective_slack_user_id,
            },
        }


    async def _resolve_publish_pr_follow_up(
        self,
        api_client,
        *,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> tuple[dict, Optional[Any]]:
        from ...clients.mlai_backend import MLAIBackendUnavailableError
        (
            requested_by_slack_user_id,
            effective_slack_user_id,
            _is_delegated,
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

        requested_action = detect_content_action(text)
        if requested_action != "publish_pr":
            return params, None

        existing_job_id = str(params.get("job_id") or "").strip()
        if existing_job_id:
            params["action"] = "publish_pr"
            return params, None

        if not channel_id or not thread_ts:
            return params, (
                "I couldn't identify a completed article bundle for this publish request because the Slack thread context was missing."
            )

        try:
            delegated_backend_kwargs = self._delegated_backend_kwargs(
                requested_by_slack_user_id,
                effective_slack_user_id,
            )
            resolution = await api_client.resolve_content_thread(
                slack_user_id=effective_slack_user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                requested_action="publish_pr",
                domain=params.get("domain"),
                **delegated_backend_kwargs,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return params, (
                    "I couldn't identify a completed article bundle to publish from this thread. "
                    "Please use the original content-ready thread for the article you want to publish."
                )
            return params, f"❌ I couldn't resolve which completed article to publish from this thread: {exc}"
        except MLAIBackendUnavailableError:
            return params, self._content_factory_backend_unavailable_message()
        except Exception as exc:
            return params, f"❌ I couldn't resolve which completed article to publish from this thread: {exc}"

        resolution_type = str(resolution.get("resolution") or "").strip()
        resolved_domain = (
            normalize_content_factory_domain(resolution.get("domain"))
            or params.get("domain")
            or resolution.get("domain")
        )
        if resolution_type == "ready":
            params["action"] = "publish_pr"
            params["job_id"] = str(resolution.get("job_id") or "").strip()
            if resolved_domain:
                params["domain"] = resolved_domain
            return params, None

        if resolution_type == "in_progress":
            publish_job_id = str(resolution.get("promoted_publish_job_id") or "").strip()
            if not publish_job_id:
                return params, (
                    "I found an article in this thread that is already being promoted, but I couldn't determine the active publish run."
                )
            return params, self._build_publish_pr_start_response(
                domain=resolved_domain,
                job_id=publish_job_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

        return params, (
            "I couldn't identify a completed article bundle to publish from this thread."
        )


    async def _publish_content_bundle_as_pr(
        self,
        api_client,
        *,
        job_id: str,
        domain: Optional[str],
        requested_by_slack_user_id: str,
        effective_slack_user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        text: str,
        params: dict,
    ) -> Any:
        from ...clients.mlai_backend import MLAIBackendUnavailableError

        if not job_id:
            return (
                "I couldn't tell which article bundle to publish as a PR in this thread. "
                "Please ask from the original article thread after the content-ready message."
            )

        reconnect_result = await self._request_github_reconnect(
            api_client,
            user_id=effective_slack_user_id,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
            domain=domain,
            github_repo=None,
            trigger="preflight",
            pending_action="publish_pr",
            channel_id=channel_id,
            thread_ts=thread_ts,
            button_label=f"Reconnect GitHub for {domain or 'this domain'}",
            text=text,
            params=params,
            save_pending=True,
        )
        if reconnect_result is not None:
            return reconnect_result

        try:
            response = await api_client.publish_article_as_pr(
                job_id,
                effective_slack_user_id,
                **self._delegated_backend_kwargs(
                    requested_by_slack_user_id,
                    effective_slack_user_id,
                ),
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        except httpx.HTTPStatusError as exc:
            error_message = str(exc)
            try:
                error_data = exc.response.json()
            except Exception:
                error_data = {}
            if isinstance(error_data, dict):
                if error_data.get("error_code") == "AUTH_REQUIRED":
                    if is_delegated_content_factory_request(
                        requested_by_slack_user_id,
                        effective_slack_user_id,
                    ):
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    if not error_data.get("pending_intent_stored"):
                        await self._save_content_factory_pending_intent(
                            api_client,
                            requested_by_slack_user_id,
                            params,
                            text,
                            channel_id,
                            thread_ts,
                        )
                    auth_url = error_data.get("auth_url")
                    if not auth_url:
                        reconnect = await api_client.reconnect_content_factory_github(
                            slack_user_id=effective_slack_user_id,
                            domain=domain,
                            github_repo=None,
                            trigger="fallback_412",
                            pending_action="publish_pr",
                        )
                        auth_url = reconnect.get("auth_url")
                    message = error_data.get("message") or "Reconnect GitHub before I can publish that bundle as a PR."
                    if auth_url:
                        blocks = self._build_github_reconnect_blocks(
                            message,
                            auth_url,
                            button_label=f"Reconnect GitHub for {domain or 'this domain'}",
                        )
                        if channel_id:
                            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"{message} Use the button above to continue.",
                                blocks=blocks,
                            )
                        return {
                            "message": f"{message}\n\n{auth_url}",
                            "blocks": blocks,
                        }
                error_message = (
                    error_data.get("error")
                    or error_data.get("message")
                    or error_message
                )
            return f"❌ I couldn't publish that article bundle as a PR: {error_message}"
        except Exception as exc:
            return f"❌ I couldn't publish that article bundle as a PR: {exc}"

        child_job_id = str(response.get("job_id") or response.get("run_id") or "").strip()
        if not child_job_id:
            return (
                "I asked Content Factory to publish that article bundle as a PR, "
                "but it didn't return a run ID."
            )

        resolved_domain = (
            normalize_content_factory_domain(domain)
            or normalize_content_factory_domain(response.get("domain"))
            or domain
            or response.get("domain")
        )
        return self._build_publish_pr_start_response(
            domain=resolved_domain,
            job_id=child_job_id,
            requested_by_slack_user_id=requested_by_slack_user_id,
            effective_slack_user_id=effective_slack_user_id,
        )


    @staticmethod
    def _is_registry_driven_publish_target(target: Any) -> bool:
        if not isinstance(target, dict):
            return False
        strategy = target.get("registration_strategy")
        if not isinstance(strategy, dict):
            strategy = {}
        return (
            str(target.get("kind") or "").strip() == "registry_driven_seo"
            or str(target.get("delivery_adapter") or "").strip() == "registry_entry"
            or str(strategy.get("type") or "").strip() == "registry_entry_patch"
        )


    @classmethod
    def _registry_target_readiness(cls, target: Any) -> dict[str, bool]:
        if not cls._is_registry_driven_publish_target(target):
            return {
                "structure_ready": False,
                "mapping_ready": False,
                "routing_ready": False,
                "safety_ready": False,
            }
        strategy = target.get("registration_strategy") if isinstance(target, dict) else {}
        if not isinstance(strategy, dict):
            strategy = {}
        readiness = target.get("readiness") if isinstance(target, dict) else {}
        if not isinstance(readiness, dict):
            readiness = strategy.get("readiness") if isinstance(strategy.get("readiness"), dict) else {}
        status = str(
            (target or {}).get("registry_status")
            or (target or {}).get("status")
            or readiness.get("status")
            or ""
        ).strip()
        all_ready = status == "publish_ready" or bool(readiness.get("publish_ready"))
        return {
            key: bool(readiness.get(key) or all_ready)
            for key in ("structure_ready", "mapping_ready", "routing_ready", "safety_ready")
        }


    @classmethod
    def _registry_target_publish_ready(cls, target: Any) -> bool:
        if not cls._is_registry_driven_publish_target(target):
            return False
        readiness = cls._registry_target_readiness(target)
        return all(readiness.values())


    @staticmethod
    def _registry_target_from_article_system(article_system: Any) -> Optional[dict]:
        if not isinstance(article_system, dict):
            return None
        if str(article_system.get("system_type") or "").strip() != "registry_driven_seo":
            return None
        registry = article_system.get("registry") if isinstance(article_system.get("registry"), dict) else {}
        mutation_target = article_system.get("publish_mutation_target")
        mutation_target_path = (
            mutation_target.get("registry_path")
            or mutation_target.get("path")
            or mutation_target.get("file")
            if isinstance(mutation_target, dict)
            else mutation_target
        )
        content_source = article_system.get("content_source")
        content_source_path = (
            content_source.get("path")
            or content_source.get("registry_path")
            or content_source.get("file")
            if isinstance(content_source, dict)
            else content_source
        )
        registry_path = (
            registry.get("path")
            or mutation_target_path
            or content_source_path
            or article_system.get("directory_path")
            or article_system.get("directory_name")
        )
        return {
            "kind": "registry_driven_seo",
            "delivery_adapter": "registry_entry",
            "readiness": article_system.get("readiness") if isinstance(article_system.get("readiness"), dict) else {},
            "diagnostics": article_system.get("diagnostics") if isinstance(article_system.get("diagnostics"), dict) else {},
            "observability": article_system.get("observability") if isinstance(article_system.get("observability"), dict) else {},
            "registration_strategy": {
                "type": "registry_entry_patch",
                "registry_path": registry_path,
                "route_template": article_system.get("route_template") or "",
            },
        }


    @classmethod
    def _best_registry_driven_target(cls, *sources: Any) -> Optional[dict]:
        targets: list[dict] = []
        for source in sources:
            if not source:
                continue
            if isinstance(source, list):
                candidates = source
            elif isinstance(source, dict):
                candidates = source.get("publish_targets") or source.get("targets") or []
                if cls._is_registry_driven_publish_target(source):
                    candidates = [source, *list(candidates or [])]
                else:
                    direct_target = cls._registry_target_from_article_system(source)
                    nested_target = cls._registry_target_from_article_system(source.get("article_system"))
                    synthesized = [target for target in (direct_target, nested_target) if target]
                    if synthesized:
                        candidates = [*list(candidates or []), *synthesized]
            else:
                candidates = []
            for candidate in candidates:
                if isinstance(candidate, dict) and cls._is_registry_driven_publish_target(candidate):
                    targets.append(candidate)
        if not targets:
            return None
        return next((target for target in targets if cls._registry_target_publish_ready(target)), None) or targets[0]


    @staticmethod
    def _registry_target_path(target: Optional[dict], article_system: Optional[dict] = None) -> str:
        target = target or {}
        article_system = article_system or {}
        strategy = target.get("registration_strategy") if isinstance(target.get("registration_strategy"), dict) else {}
        registry = article_system.get("registry") if isinstance(article_system.get("registry"), dict) else {}
        return str(
            strategy.get("registry_path")
            or target.get("registry_path")
            or target.get("content_source")
            or registry.get("path")
            or article_system.get("directory_path")
            or article_system.get("directory_name")
            or "the detected registry"
        ).strip()


    @classmethod
    def _registry_target_issues(cls, target: Optional[dict], article_system: Optional[dict] = None) -> list[str]:
        issues: list[str] = []
        target = target or {}
        article_system = article_system or {}
        strategy = target.get("registration_strategy") if isinstance(target.get("registration_strategy"), dict) else {}
        readiness = cls._registry_target_readiness(target)
        for key, ready in readiness.items():
            if not ready:
                issues.append(f"{key.replace('_', ' ')} is not proven")

        diagnostic_sources = [
            target.get("diagnostics"),
            strategy.get("diagnostics"),
            article_system.get("diagnostics"),
            target.get("observability"),
            article_system.get("observability"),
        ]
        for source in diagnostic_sources:
            if not isinstance(source, dict):
                continue
            for key in ("issues", "blocking_issues", "fallback_reasons"):
                raw_items = source.get(key)
                if isinstance(raw_items, str):
                    raw_items = [raw_items]
                for item in raw_items or []:
                    text = str(item or "").strip()
                    if text and text not in issues:
                        issues.append(text)
            fallback_reason = str(source.get("fallback_reason") or "").strip()
            if fallback_reason and fallback_reason not in issues:
                issues.append(fallback_reason)
        return issues


    @classmethod
    def _registry_target_diagnostic_message(
        cls,
        *,
        domain: str,
        target: Optional[dict],
        article_system: Optional[dict] = None,
    ) -> str:
        registry_path = cls._registry_target_path(target, article_system)
        issues = cls._registry_target_issues(target, article_system)
        issue_text = ""
        if issues:
            issue_text = "\n\nBlockers:\n" + "\n".join(f"- {item}" for item in issues[:6])
        return (
            f"I found a registry-driven SEO system for *{domain}* at `{registry_path}`, "
            f"but it is not safe to patch automatically yet."
            f"{issue_text}\n\n"
            "Roo stopped before changing the repository. Use content-only delivery for now, "
            "or add/confirm a `.content-factory/target.yml` hook after the registry target is proven."
        )


    @classmethod
    def _registry_target_diagnostic_blocks(
        cls,
        *,
        domain: str,
        target: Optional[dict],
        article_system: Optional[dict] = None,
    ) -> list[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": cls._registry_target_diagnostic_message(
                        domain=domain,
                        target=target,
                        article_system=article_system,
                    ),
                },
            }
        ]


    async def _execute_content_factory(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        thread_history: Optional[List[dict]] = None
    ) -> str:
        """Execute the content factory generation workflow."""
        from ...clients.mlai_backend import MLAIBackendUnavailableError

        params = dict(params or {})
        params["client_request_id"] = self._get_content_factory_client_request_id(params)
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
        params["requested_by_slack_user_id"] = requested_by_slack_user_id
        params["effective_slack_user_id"] = effective_slack_user_id

        def content_factory_identity_payload(**extra: Any) -> dict[str, Any]:
            return build_content_factory_identity_payload(
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                **extra,
            )

        delegated_backend_kwargs = self._delegated_backend_kwargs(
            requested_by_slack_user_id,
            effective_slack_user_id,
        )
        
        # Get a MLAIBackendClient for API calls
        settings = get_settings()
        from roo.clients.mlai_backend import MLAIBackendClient
        api_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY
        )
        domain = params.get("domain")
        org_config_cached = None
        action = params.get("action")
        params, publish_resolution = await self._resolve_publish_pr_follow_up(
            api_client,
            text=text,
            params=params,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        if publish_resolution is not None:
            return publish_resolution
        action = params.get("action")
        domain = params.get("domain")
        if action == "publish_pr":
            return await self._publish_content_bundle_as_pr(
                api_client,
                job_id=str(params.get("job_id") or "").strip(),
                domain=domain,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=text,
                params=params,
            )
        is_scan_request = self._is_explicit_scan_request(text, params)
        is_article_flow = action != "scaffold" and not is_scan_request
        requested_delivery_mode, requested_delivery_mode_confirmed = self._resolve_requested_article_delivery_mode(
            text,
            params,
        )

        # Check status of the requested GitHub integration.
        # When a domain is explicitly provided, querying the generic user-level
        # integration can return a multi-domain selection error before we ever
        # reach the domain-specific flow.
        try:
            integration = await api_client.get_integration(
                effective_slack_user_id,
                domain=domain,
            )
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        
        # 1. New User Disclaimer & Education
        # If user has no integration AND hasn't confirmed the disclaimer yet
        if not integration and not params.get("confirmed"):
            await self._save_content_factory_pending_intent(
                api_client,
                requested_by_slack_user_id,
                params,
                text,
                channel_id,
                thread_ts,
            )

            confirm_value = json.dumps(
                build_content_factory_identity_payload(
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    text=text,
                    params=params,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
            )
            disclaimer_cost_text = (
                f"*Cost:* Articles for **{normalize_content_factory_domain(domain) or domain or 'this domain'}** are free. "
                "No Roo points will be deducted for this run.\n\n"
                if is_free_content_factory_domain(domain)
                else (
                    f"*Cost:* Creating an article costs **{CONTENT_FACTORY_ARTICLE_COST_POINTS} Roo points**. "
                    "Those points are deducted when article research/generation starts. "
                    "If something goes wrong, message Dr Sam on Slack and he can help sort out a refund.\n\n"
                )
            )
            
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ Content Factory Requirements",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Before we start, a quick heads-up! This skill works best with **Next.js & Tailwind CSS** projects "
                            "when you want a PR-based publish flow. If there's no repo connected, I can still do "
                            "**content-only** article research and writing for manual upload.\n\n"
                            f"{disclaimer_cost_text}"
                            "*How it works:*\n"
                            "1. 🔎 **Researches** your domain, competitors, and keywords\n"
                            "2. ✍️ **Writes** the article draft\n"
                            "3. 🧩 **Uses** repo context when you want code/publish delivery\n"
                            "4. 🚀 **Either** hands back content-only copy or prepares the PR flow"
                        )
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "I'm ready to proceed",
                                "emoji": True
                            },
                            "action_id": "confirm_content_factory",
                            "value": confirm_value,
                            "style": "primary"
                        }
                    ]
                }
            ]
            
            if channel_id:
                post_message(channel_id, "Please review the requirements above.", thread_ts=thread_ts, blocks=blocks)
                return "Please review the requirements above to get started! 👆"
            return "Please confirm you have a Next.js/Tailwind project and are ready to connect GitHub."

        # Check for Expired Token or Other Errors
        if integration and integration.get("error"):
            if is_delegated:
                return self._delegated_content_factory_auth_required_message(
                    effective_slack_user_id=effective_slack_user_id,
                    domain=domain,
                )
            auth_url = integration.get("auth_url")
            error_msg = integration.get("error")
            
            if not auth_url:
                # Fallback if auth_url missing in error response
                try:
                    auth_url_resp = await api_client.get_github_auth_url(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                auth_url = auth_url_resp.get("auth_url")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ **Connection Issue**: {error_msg}\nI need you to re-connect your GitHub account to continue."
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
                            "url": auth_url,
                            "action_id": "connect_github",
                            "style": "danger"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🚀 I've Connected - Resume",
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
            
            if channel_id:
                post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                return self._already_posted_response(
                    "Please re-connect your GitHub account using the button above. 🔌",
                    blocks=blocks,
                )
            return f"GitHub connection issue ({error_msg}). Please re-connect here: {auth_url}"

        if not integration:
            if is_article_flow:
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                if org_config_cached:
                    domain = domain or org_config_cached.get("domain")
                    repo_hint = org_config_cached.get("github_repo")
                    integration = {
                        "connected_domains": (
                            [{"domain": domain, "github_repo": repo_hint, "scanned": bool(org_config_cached.get("scan_summary"))}]
                            if domain
                            else []
                        ),
                        "recommended_next_action": None,
                        "last_article": None,
                        "project_scanned": bool(org_config_cached.get("scan_summary")),
                        "content_research_ready": bool(org_config_cached.get("scan_summary")),
                        "article_system": org_config_cached.get("article_system") or {},
                        "article_delivery_mode": org_config_cached.get("article_delivery_mode"),
                    }

            if not integration:
                if is_delegated:
                    return self._delegated_content_factory_auth_required_message(
                        effective_slack_user_id=effective_slack_user_id,
                        domain=domain,
                    )
                try:
                    auth_response = await api_client.get_github_auth_url(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                auth_url = auth_response.get("auth_url")

                if not auth_url:
                    return "Sorry mate, I couldn't get the authorization URL from the backend. Try again strictly?"

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "I need permission to access your GitHub to publish articles. Click the button below to connect your account."
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Connect GitHub Account",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github",
                                "style": "primary"
                            }
                        ]
                    }
                ]

                await self._save_content_factory_pending_intent(
                    api_client,
                    requested_by_slack_user_id,
                    params,
                    text,
                    channel_id,
                    thread_ts,
                )

                if channel_id:
                    post_message(channel_id, "Please connect GitHub", thread_ts=thread_ts, blocks=blocks)
                    return self._already_posted_response(
                        "I've sent a button to connect your GitHub account. 🔌",
                        blocks=blocks,
                    )
                return f"Please connect your GitHub account here: {auth_url}"

        # 2. Resolve domain from connected_domains
        connected_domains = integration.get("connected_domains", []) if integration else []

        if not domain:
            if len(connected_domains) == 0:
                # No domains connected — fall back to org config lookup
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        slack_user_id=effective_slack_user_id
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()
                if org_config_cached:
                    domain = org_config_cached.get("domain")
            elif len(connected_domains) == 1:
                # Single domain — use it automatically
                domain = connected_domains[0].get("domain")
            else:
                # Multiple domains — ask user to choose
                domain_list = "\n".join(
                    f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                    for d in connected_domains
                )
                msg = f"You have multiple connected codebases. Which one should I work with?\n\n{domain_list}\n\nTry: `@Roo scan <domain>` or `@Roo write an article for <domain>`"
                if channel_id:
                    post_message(channel_id, msg, thread_ts)
                return msg

        # Refresh integration with domain context once we know the target domain.
        # The generic top-level integration response may still show repo drift,
        # while the domain-specific response can correctly say "research_article"
        # or "write_article" without forcing a re-scan.
        if domain:
            try:
                domain_integration = await api_client.get_integration(
                    effective_slack_user_id,
                    domain=domain,
                )
            except MLAIBackendUnavailableError:
                return self._content_factory_backend_unavailable_message()
            if domain_integration:
                integration = domain_integration
                connected_domains = integration.get("connected_domains", connected_domains)
            elif is_article_flow and org_config_cached is None:
                try:
                    org_config_cached = await api_client.get_content_org_config(
                        effective_slack_user_id,
                        domain=domain,
                    )
                except MLAIBackendUnavailableError:
                    return self._content_factory_backend_unavailable_message()

        repo_name, domain_info = self._resolve_content_factory_repo_name(
            integration or {},
            connected_domains,
            domain,
        )

        if domain and integration and integration.get("needs_github_auth") and not is_article_flow:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="manual",
                pending_action=action or ("scan" if is_scan_request else "repo_action"),
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

        # No repo at all — prompt to connect
        if not repo_name and not is_article_flow:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=None,
                trigger="manual",
                pending_action=action or ("scan" if is_scan_request else "repo_action"),
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label="Reconnect GitHub & Select Repo",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

        # 3. Handle explicit scaffold action
        if action == "scaffold":
            if not domain:
                return "I need a domain to scaffold the articles directory. Try: `@Roo scaffold articles for <domain>`"

            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scaffold",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

            # Check scan prerequisite
            if domain_info and not domain_info.get("scanned"):
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "prerequisite_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                "style": "primary",
                                "value": json.dumps(
                                    content_factory_identity_payload(
                                        domain=domain,
                                        channel_id=channel_id,
                                        thread_ts=thread_ts,
                                        original_intent={"action": "scaffold"},
                                    )
                                ),
                                "action_id": "prerequisite_scan"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                "value": json.dumps(
                                    content_factory_identity_payload(domain=domain)
                                ),
                                "action_id": "prerequisite_cancel"
                            }
                        ]
                    }
                ]
                if channel_id:
                    post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                return "I need to scan your codebase first before I can scaffold the articles directory."

            if channel_id:
                post_message(
                    channel_id,
                    f"📁 Creating articles directory for *{domain}*...",
                    thread_ts
                )

            try:
                result = await api_client.scaffold_articles(
                    domain=domain,
                    slack_user_id=effective_slack_user_id,
                    slack_channel_id=channel_id or "",
                    slack_thread_ts=thread_ts or "",
                    **delegated_backend_kwargs,
                )

                status_code = result.get("status_code")
                data = result.get("data", {})

                if status_code == 200:
                    pr_url = data.get("pr_url", "")
                    preview_url = data.get("preview_url", "")
                    primary_action_url = data.get("primary_action_url", "")
                    primary_action_label = data.get("primary_action_label", "")
                    detail_parts = []
                    if pr_url:
                        detail_parts.append(f"<{pr_url}|View PR>")
                    if primary_action_url:
                        detail_parts.append(f"<{primary_action_url}|{primary_action_label or 'Open Review'}>")
                    elif preview_url:
                        detail_parts.append(f"<{preview_url}|View Preview>")
                    detail_text = f" {' | '.join(detail_parts)}" if detail_parts else ""
                    return f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                elif status_code == 202:
                    return "Scaffolding is underway! I'll reply here when it's done. 🏗️"
                elif status_code == 412:
                    missing_step = data.get("missing_step", "scan")
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent={"action": "scaffold"},
                                        )
                                    ),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif status_code == 400:
                    if data.get("needs_github_auth"):
                        if is_delegated:
                            return self._delegated_content_factory_auth_required_message(
                                effective_slack_user_id=effective_slack_user_id,
                                domain=domain,
                            )
                        oauth_url = data.get("oauth_url", "")
                        return f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {oauth_url}"
                    return f"❌ Could not start scaffolding: {data.get('error', 'Unknown error')}"
                elif status_code == 404:
                    return f"❌ No configuration found for *{domain}*."
                else:
                    return f"❌ Unexpected response from backend (status {status_code})"
            except Exception as e:
                print(f"❌ Failed to trigger scaffold: {e}")
                return f"❌ Error creating articles directory: {e}"

        # 4. Check scan status
        needs_scan = False
        scan_reason = ""
        recommended_next_action = integration.get("recommended_next_action") if integration else None
        scan_completed = bool(
            (integration.get("scan_completed") if integration else False)
            or (integration.get("content_research_ready") if integration else False)
            or (domain_info and domain_info.get("scanned"))
        )

        if not is_article_flow or repo_name:
            if recommended_next_action == "scan":
                needs_scan = True
                scan_reason = "Initial scan required"
            elif domain_info:
                if not domain_info.get("scanned"):
                    needs_scan = True
                    scan_reason = "Initial scan required"
            elif integration and not integration.get("project_scanned"):
                needs_scan = True
                scan_reason = "Initial scan required"

        # Only use legacy has_updates as a fallback when the backend has not
        # already told us that a usable scan exists for this domain.
        if (
            not scan_completed
            and recommended_next_action in (None, "scan")
            and integration
            and integration.get("has_updates")
            and (not is_article_flow or repo_name)
        ):
            needs_scan = True
            scan_reason = "🔄 Updates detected in repository"

        # Explicit scan requests should confirm before re-scanning an already scanned repo.
        last_scanned = integration.get("last_scanned_at", "Never") if integration else "Never"
        if self._is_explicit_scan_request(text, params) and scan_completed and not needs_scan:
            return self._build_existing_scan_confirmation(
                domain=domain,
                repo_name=repo_name,
                last_scanned=last_scanned,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )

        # Compile Status Report
        last_scanned = integration.get("last_scanned_at", "Never") if integration else "Never"
        last_article = ((integration or {}).get("last_article") or {}).get("title", "None")
        connection_state = str(
            (domain_info or {}).get("connection_state")
            or (integration or {}).get("connection_state")
            or ""
        ).strip().lower()
        needs_github_auth = bool((integration or {}).get("needs_github_auth")) or connection_state == "auth_required"

        if channel_id:
            if needs_github_auth and repo_name and domain:
                connected_msg = f"👋 G'day! Repository selected: `{repo_name}` for *{domain}*.\n\n"
            elif needs_github_auth and repo_name:
                connected_msg = f"👋 G'day! Repository selected: `{repo_name}`.\n\n"
            elif repo_name and domain:
                connected_msg = f"👋 G'day! Connected to `{repo_name}` for *{domain}*.\n\n"
            elif repo_name:
                connected_msg = f"👋 G'day! I see you're connected to `{repo_name}`.\n\n"
            else:
                connected_msg = f"👋 G'day! Working on *{domain}* in content-only mode unless you choose publish later.\n\n"
            status_msg = (
                connected_msg +
                f"📊 **Status Report:**\n"
                f"• Last scanned: {last_scanned}\n"
                f"• Last article: {last_article}\n"
            )
            if needs_scan:
                status_msg += f"\n{scan_reason}. Scanning updates now... 🕵️"
            elif needs_github_auth and repo_name:
                status_msg += "• Repository: reconnect GitHub to continue with repo-backed work"
            elif repo_name:
                status_msg += "• Repository: ✅ Up to date"
            else:
                status_msg += "• Repository: not connected (content-only is still available)"

            post_message(channel_id, status_msg, thread_ts)

        if needs_scan:
            reconnect_result = await self._request_github_reconnect(
                api_client,
                user_id=effective_slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
                domain=domain,
                github_repo=repo_name,
                trigger="preflight",
                pending_action="scan",
                channel_id=channel_id,
                thread_ts=thread_ts,
                button_label=f"Reconnect GitHub for {domain or 'this domain'}",
                text=text,
                params=params,
                save_pending=True,
            )
            if reconnect_result is not None:
                return reconnect_result

            scan_result = await api_client.trigger_repo_scan(
                effective_slack_user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                domain=domain,
                **delegated_backend_kwargs,
            )
            
            if scan_result.get("status") == "accepted":
                return "updates are being processed in the background! 🏃\nI'll reply here when the scan is complete."
                
            # Handle sync failures or other errors
            if scan_result.get("error"):
                error_msg = scan_result.get("message", "Unknown error")

                # Multiple domains — backend needs user to choose
                if scan_result.get("error") == "multiple_domains":
                    available = scan_result.get("available_domains", [])
                    domain_list = "\n".join(
                        f"  • `{d['domain']}` → `{d.get('github_repo', 'unknown')}`"
                        for d in available
                    )
                    msg = f"{error_msg}\n\n{domain_list}\n\nTry: `@Roo scan <domain>`"
                    if channel_id:
                        post_message(channel_id, msg, thread_ts)
                    return msg

                # If backend says GitHub isn't connected for this domain
                if scan_result.get("needs_github_auth"):
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    oauth_url = scan_result.get("oauth_url")
                    domain_name = scan_result.get("domain", domain)
                    if oauth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌",
                                blocks=blocks,
                            )
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account and try again."

                # If error indicates auth failure or repo not found (404/403/401)
                if any(code in str(error_msg) for code in ["404", "401", "403", "Not Found", "Bad credentials"]):
                    # Fetch Auth URL to allow reconnect
                    try:
                        auth_response = await api_client.get_github_auth_url(
                            effective_slack_user_id,
                            domain=domain,
                        )
                    except MLAIBackendUnavailableError:
                        return self._content_factory_backend_unavailable_message()
                    auth_url = auth_response.get("auth_url")
                    
                    if auth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ **Connection Lost**: It looks like I can't access your repository anymore ({error_msg})."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Re-connect GitHub App",
                                            "emoji": True
                                        },
                                        "url": auth_url,
                                        "action_id": "connect_github",
                                        "style": "danger"
                                    }
                                ]
                            }
                        ]
                        if channel_id:
                            post_message(channel_id, "Please re-connect GitHub", thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                "Please re-connect your GitHub App using the button above. 🔌",
                                blocks=blocks,
                            )
                
                return f"Had some trouble scanning your repository: {error_msg}"


            # Legacy Sync Behavior (if backend returns 200 immediately)
            # Scan succeeded - refresh integration status
            try:
                integration = await api_client.get_integration(
                    effective_slack_user_id,
                    domain=domain,
                )
            except MLAIBackendUnavailableError:
                return self._content_factory_backend_unavailable_message()
            if not integration or not integration.get("project_scanned"):
                return "Scanning is taking a bit longer than expected. Please wait for the notification! 🦘"
            
            if channel_id:
                post_message(channel_id, "✅ Repository analysis complete! Ready to write.", thread_ts)

        # 3. Validation: Check parameters (Domain/Topic)
        topic = params.get("topic")
        target_keyword = params.get("target_keyword", "")

        if not domain:
            return "I can help write that article! To get started, I just need to know the domain name (e.g., mlai.au)."

        if self._should_prompt_for_article_direction(text, params):
            return {
                "message": (
                    f"I can do that for {domain}. First, choose whether you want me to research the best article "
                    "opportunity or whether you'll give me the topic."
                ),
                "blocks": self._build_article_direction_blocks(
                    domain,
                    user_id,
                    channel_id,
                    thread_ts,
                    params["client_request_id"],
                    requested_delivery_mode,
                    requested_delivery_mode_confirmed,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                ),
            }

        article_system = (integration or {}).get("article_system") or {}
        if not article_system and domain_info:
            article_system = domain_info.get("article_system") or {}
        registry_target = self._best_registry_driven_target(integration, domain_info, article_system)
        registry_target_ready = self._registry_target_publish_ready(registry_target)

        if (
            topic
            and registry_target
            and not registry_target_ready
            and recommended_next_action in {"scaffold", "confirm_article_system"}
        ):
            message = self._registry_target_diagnostic_message(
                domain=domain,
                target=registry_target,
                article_system=article_system,
            )
            blocks = self._registry_target_diagnostic_blocks(
                domain=domain,
                target=registry_target,
                article_system=article_system,
            )
            if channel_id:
                post_message(channel_id, f"Registry target needs confirmation for {domain}", thread_ts=thread_ts, blocks=blocks)
                return self._already_posted_response(message, blocks=blocks)
            return message

        if topic and recommended_next_action == "confirm_article_system":
            original_intent = {
                "action": "write",
                "topic": topic,
                "client_request_id": params["client_request_id"],
            }
            detected_location = (
                article_system.get("directory_path")
                or article_system.get("directory_name")
                or "the detected article directory"
            )
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"I found what looks like your existing article system at *`{detected_location}`*, "
                            f"but I need confirmation before I write into it."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "block_id": "article_system_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Use Detected Structure", "emoji": True},
                            "style": "primary",
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_use_detected",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Rescan Repo", "emoji": True},
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_rescan",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                            "value": json.dumps(
                                content_factory_identity_payload(
                                    domain=domain,
                                    channel_id=channel_id,
                                    thread_ts=thread_ts,
                                    original_intent=original_intent,
                                )
                            ),
                            "action_id": "article_system_scaffold",
                        },
                    ],
                },
            ]
            if channel_id:
                post_message(channel_id, f"Confirm article system for {domain}", thread_ts=thread_ts, blocks=blocks)
            return "I found an existing article structure, but I need you to confirm whether I should use it."

        try:
            # Start generation via MLAI Backend
            # Enhance context with thread history if available
            full_context = text
            if thread_history:
                history_str = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in thread_history[:-1]])
                full_context = f"Context from Thread:\n{history_str}\n\nCurrent Request: {text}"

            access_error = await self._validate_content_factory_paid_access(
                api_client,
                requested_by_slack_user_id,
                domain,
            )
            if access_error:
                return access_error

            effective_article_delivery_mode = (
                requested_delivery_mode
                or str((integration or {}).get("article_delivery_mode") or "").strip().lower()
                or str((org_config_cached or {}).get("article_delivery_mode") or "").strip().lower()
                or None
            )
            if effective_article_delivery_mode == "publish_code":
                reconnect_result = await self._request_github_reconnect(
                    api_client,
                    user_id=effective_slack_user_id,
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                    domain=domain,
                    github_repo=repo_name,
                    trigger="preflight",
                    pending_action="write_article",
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    button_label=f"Reconnect GitHub for {domain}",
                    text=text,
                    params=params,
                    save_pending=True,
                )
                if reconnect_result is not None:
                    return reconnect_result

            from ...slack_client import get_user_info
            slack_info = get_user_info(requested_by_slack_user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            
            # Note: topic can be None (triggers Auto-Write / Research Mode)
            response = await api_client.trigger_article_generation(
                slack_user_id=effective_slack_user_id,
                domain=domain,
                topic=topic,
                target_keyword=target_keyword,
                context=full_context,
                delivery_mode=requested_delivery_mode,
                delivery_mode_confirmed=requested_delivery_mode_confirmed,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                client_request_id=params["client_request_id"],
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
                **delegated_backend_kwargs,
            )
            
            job_id = response.get("job_id")
            if not job_id:
                return "Failed to start generation: No job ID returned from backend."

            if str(response.get("status") or "").strip().lower() == "awaiting_delivery_mode":
                return self._build_article_delivery_mode_prompt(
                    domain=domain,
                    job_id=job_id,
                    topic=topic,
                    recommended_delivery_mode=response.get("recommended_delivery_mode"),
                    requested_by_slack_user_id=requested_by_slack_user_id,
                    effective_slack_user_id=effective_slack_user_id,
                )

            workflow = str(
                response.get("workflow")
                or ("auto_discovery" if not topic else "direct_generate")
            ).strip().lower()

            return self._build_content_factory_start_response(
                domain=domain,
                job_id=job_id,
                topic=topic,
                workflow=workflow,
                requested_by_slack_user_id=requested_by_slack_user_id,
                effective_slack_user_id=effective_slack_user_id,
            )
            
        except MLAIBackendUnavailableError:
            return self._content_factory_backend_unavailable_message()
        except httpx.HTTPStatusError as e:
            print(f"Content Generation HTTP Error: {e}")
            if e.response.status_code == 412:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                error_code = error_data.get("error_code", "")
                if error_code == "AUTH_REQUIRED":
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    if not error_data.get("pending_intent_stored"):
                        await self._save_content_factory_pending_intent(
                            api_client,
                            requested_by_slack_user_id,
                            params,
                            text,
                            channel_id,
                            thread_ts,
                        )
                    auth_url = error_data.get("auth_url")
                    if not auth_url:
                        reconnect = await api_client.reconnect_content_factory_github(
                            slack_user_id=effective_slack_user_id,
                            domain=domain,
                            github_repo=repo_name,
                            trigger="fallback_412",
                            pending_action="write_article",
                        )
                        auth_url = reconnect.get("auth_url")
                    message = error_data.get("message") or f"Reconnect GitHub for {domain} before I continue."
                    if auth_url:
                        blocks = self._build_github_reconnect_blocks(
                            message,
                            auth_url,
                            button_label=f"Reconnect GitHub for {domain}",
                        )
                        if channel_id:
                            post_message(channel_id, message, thread_ts=thread_ts, blocks=blocks)
                            return self._already_posted_response(
                                f"{message} Use the button above to continue.",
                                blocks=blocks,
                            )
                        return {
                            "message": f"{message}\n\n{auth_url}",
                            "blocks": blocks,
                        }
                    return message
                if error_code == "PUBLISH_TARGET_ACTION_REQUIRED":
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    error_article_system = error_data.get("article_system") or article_system or {}
                    error_registry_target = self._best_registry_driven_target(
                        error_data,
                        integration,
                        domain_info,
                        error_article_system,
                    )
                    if error_registry_target and not self._registry_target_publish_ready(error_registry_target):
                        message = self._registry_target_diagnostic_message(
                            domain=domain,
                            target=error_registry_target,
                            article_system=error_article_system,
                        )
                        blocks = self._registry_target_diagnostic_blocks(
                            domain=domain,
                            target=error_registry_target,
                            article_system=error_article_system,
                        )
                        if channel_id:
                            post_message(
                                channel_id,
                                f"Registry target needs confirmation for {domain}",
                                thread_ts=thread_ts,
                                blocks=blocks,
                            )
                            return self._already_posted_response(message, blocks=blocks)
                        return message
                    auth_url = None
                    if domain:
                        try:
                            auth_response = await api_client.get_github_auth_url(
                                effective_slack_user_id,
                                domain=domain,
                            )
                        except MLAIBackendUnavailableError:
                            return self._content_factory_backend_unavailable_message()
                        auth_url = auth_response.get("auth_url")

                    if channel_id and auth_url:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"Publish mode isn't ready for *{domain}* yet.\n\n"
                                        "I need a connected GitHub repo before I can open the PR flow. "
                                        "If you want, connect GitHub now, or ask me to do this as content-only instead."
                                    ),
                                },
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": f"Connect GitHub for {domain}", "emoji": True},
                                        "url": auth_url,
                                        "action_id": "connect_github",
                                        "style": "primary",
                                    }
                                ],
                            },
                        ]
                        post_message(channel_id, f"Publish mode needs GitHub for {domain}", thread_ts=thread_ts, blocks=blocks)
                        return (
                            f"Publish mode needs GitHub for {domain}. Use the button above to connect it, "
                            "or ask me for content-only delivery."
                        )
                    if auth_url:
                        return (
                            f"Publish mode needs GitHub for {domain}. Connect it here: {auth_url}\n\n"
                            "Or ask me for content-only delivery."
                        )
                    return (
                        f"Publish mode isn't ready for {domain} yet because no GitHub repo is connected. "
                        "Ask me for content-only delivery instead, or connect GitHub and try again."
                    )
                if error_code == "ARTICLE_SYSTEM_ACTION_REQUIRED":
                    recommended_action = error_data.get("recommended_action", "scaffold")
                    article_system = error_data.get("article_system") or {}
                    resolution_source = error_data.get("article_system_resolution_source", "")
                    detected_location = (
                        article_system.get("directory_path")
                        or article_system.get("directory_name")
                        or "the detected article directory"
                    )
                    error_registry_target = self._best_registry_driven_target(
                        error_data,
                        integration,
                        domain_info,
                        article_system,
                    )
                    if error_registry_target and not self._registry_target_publish_ready(error_registry_target):
                        message = self._registry_target_diagnostic_message(
                            domain=domain,
                            target=error_registry_target,
                            article_system=article_system,
                        )
                        blocks = self._registry_target_diagnostic_blocks(
                            domain=domain,
                            target=error_registry_target,
                            article_system=article_system,
                        )
                        if channel_id:
                            post_message(
                                channel_id,
                                f"Registry target needs confirmation for {domain}",
                                thread_ts=thread_ts,
                                blocks=blocks,
                            )
                            return self._already_posted_response(message, blocks=blocks)
                        return message
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic

                    if recommended_action == "confirm_article_system":
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"I found what looks like your existing article system at *`{detected_location}`*, "
                                        f"but I need confirmation before I write into it."
                                    ),
                                },
                            },
                            {
                                "type": "actions",
                                "block_id": "article_system_actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Use Detected Structure", "emoji": True},
                                        "style": "primary",
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_use_detected",
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Rescan Repo", "emoji": True},
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_rescan",
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                        "value": json.dumps(
                                            content_factory_identity_payload(
                                                domain=domain,
                                                channel_id=channel_id,
                                                thread_ts=thread_ts,
                                                original_intent=original_intent,
                                            )
                                        ),
                                        "action_id": "article_system_scaffold",
                                    },
                                ],
                            },
                        ]
                        if channel_id:
                            post_message(channel_id, f"Confirm article system for {domain}", thread_ts=thread_ts, blocks=blocks)
                        return "I found an existing article structure, but I need you to confirm whether I should use it."

                    if article_system.get("state") in {"existing", "roo_scaffolded"}:
                        return (
                            f"I found an existing article system at `{detected_location}`, but the backend still blocked "
                            f"writing (resolved via {resolution_source or 'unknown'}). Please rescan the repo or try again."
                        )

                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"I need to set up your articles directory before I can write articles for *{domain}*.\n\n"
                                    f"This will create a PR with all the reusable components and a demo article so you can see how everything looks."
                                ),
                            },
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scaffold",
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel",
                                },
                            ],
                        },
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scaffold required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to set up your articles directory first."
                missing_step = error_data.get("missing_step", "")
                if missing_step == "scan":
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to scan your codebase first before I can do that for *{domain}*.\n\nThis will analyse your repo's design system, generate matching article components, and create content pillars."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Scan Codebase", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scan"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scan required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to scan your codebase first."
                elif missing_step == "scaffold":
                    original_intent = {
                        "action": "write",
                        "client_request_id": params["client_request_id"],
                    }
                    if topic:
                        original_intent["topic"] = topic
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"I need to set up your articles directory before I can write articles for *{domain}*.\n\nThis will create a PR with all the reusable components and a demo article so you can see how everything looks."
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "prerequisite_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Set Up Articles Directory", "emoji": True},
                                    "style": "primary",
                                    "value": json.dumps(
                                        content_factory_identity_payload(
                                            domain=domain,
                                            channel_id=channel_id,
                                            thread_ts=thread_ts,
                                            original_intent=original_intent,
                                        )
                                    ),
                                    "action_id": "prerequisite_scaffold"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                                    "value": json.dumps(
                                        content_factory_identity_payload(domain=domain)
                                    ),
                                    "action_id": "prerequisite_cancel"
                                }
                            ]
                        }
                    ]
                    if channel_id:
                        post_message(channel_id, f"Scaffold required for {domain}", thread_ts=thread_ts, blocks=blocks)
                    return "I need to set up your articles directory first."
                else:
                    return f"A prerequisite step is missing: {error_data.get('error', 'Unknown')}. Please try again."
            if e.response.status_code == 400:
                try:
                    error_data = e.response.json()
                except Exception:
                    error_data = {}
                # Structured error: GitHub not connected for this domain
                if error_data.get("needs_github_auth"):
                    if is_delegated:
                        return self._delegated_content_factory_auth_required_message(
                            effective_slack_user_id=effective_slack_user_id,
                            domain=domain,
                        )
                    oauth_url = error_data.get("oauth_url")
                    domain_name = error_data.get("domain", domain)
                    if oauth_url and channel_id:
                        blocks = [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ GitHub isn't connected for *{domain_name}*.\nClick below to connect your GitHub repo for this domain, then try again."
                                }
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Connect GitHub for " + (domain_name or "this domain"),
                                            "emoji": True
                                        },
                                        "url": oauth_url,
                                        "action_id": "connect_github",
                                        "style": "primary"
                                    }
                                ]
                            }
                        ]
                        post_message(channel_id, f"GitHub not connected for {domain_name}", thread_ts=thread_ts, blocks=blocks)
                        return f"GitHub isn't connected for {domain_name}. Click the button above to connect, then try again! 🔌"
                    elif oauth_url:
                        return f"GitHub isn't connected for {domain_name}. Connect here: {oauth_url}"
                    return f"GitHub isn't connected for {domain_name}. Please connect your GitHub account."
                # Other 400 errors - show the error message
                error_msg = error_data.get("error", str(e))
                return f"Sorry mate, I had trouble starting the article generation: {error_msg}"
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"
        except Exception as e:
            print(f"Content Generation Error: {e}")
            return f"Sorry mate, I had trouble starting the article generation: {str(e)}"


    async def _monitor_generation(
        self,
        client,  # This is now MLAIBackendClient
        job_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        slack_user_id: str
    ):
        """Monitor job progress and post updates to Slack."""
        last_progress = -1
        last_step = ""
        
        try:
            consecutive_failures = 0

            # Poll until completion
            while True:
                try:
                    status_data = await client.check_generation_status(job_id)
                    consecutive_failures = 0 # Reset on success
                    
                    state = status_data.get("status")
                    progress = status_data.get("progress", 0)
                    step = status_data.get("current_step", "unknown")
                    
                    # Update progress
                    should_update = (
                        progress >= last_progress + 20 or 
                        (step != last_step and step in ["researching", "writing", "optimizing", "publishing"])
                    )
                    
                    if should_update:
                        msg = f"📝 *Status Update*: {step.title()}... ({progress}%)"
                        try:
                            post_message(channel_id, msg, thread_ts)
                            last_progress = progress
                            last_step = step
                        except Exception as e:
                            print(f"Failed to post progress: {e}")

                    if state == "awaiting_confirmation":
                        return
                    if state == "completed":
                        break
                    elif state == "failed":
                        raise Exception(f"Job failed: {status_data.get('error', 'Unknown')}")
                        
                except Exception as loop_error:
                    # If it's the "Job failed" exception raised above, re-raise it to exit
                    if "Job failed" in str(loop_error):
                        raise loop_error
                        
                    consecutive_failures += 1
                    print(f"⚠️ Monitor polling failed ({consecutive_failures}/5): {loop_error}")
                    
                    if consecutive_failures >= 5:
                        raise Exception(f"Lost connection to backend after 5 attempts. Last error: {loop_error}")
                
                await asyncio.sleep(5.0)
            
            # Publish
            post_message(channel_id, "✨ Article generated! Publishing now...", thread_ts)
            
            publish_result = await client.publish_article(job_id, slack_user_id)
            
            preview_url = publish_result.get("preview_url")
            primary_action_url = publish_result.get("primary_action_url")
            primary_action_label = publish_result.get("primary_action_label") or "Preview"
            pr_url = publish_result.get("pr_url")
            review_url = primary_action_url or preview_url
            review_label = primary_action_label if primary_action_url else "Preview"
            
            final_msg = (
                f"🎉 *Article Published!* \n\n"
                f"👀 *{review_label}:* {review_url}\n"
                f"💻 *Pull Request:* {pr_url}\n\n"
                f"Review the content and merge the PR when you're ready!"
            )
            
            post_message(channel_id, final_msg, thread_ts)
            
        except Exception as e:
            error_msg = f"❌ Something went wrong with the article generation: {str(e)}"
            post_message(channel_id, error_msg, thread_ts)


