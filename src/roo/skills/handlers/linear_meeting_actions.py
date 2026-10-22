"""Linear meeting actions skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from typing import Any, Optional, List
from ...linear_effort_sizing import EFFORT_LABELS, assessment_metadata, assess_studio_effort_batch, is_project_issue, is_terminal_candidate
from .constants import LINEAR_MEETING_TIMEOUT_RECOVERY_OVERLAP_CHARS
from ...linear_inference import LINEAR_SKILL_MODEL, LinearContextualIssueResult, LinearDirectIssueBatch, LinearInferenceTimeoutError, LinearMeetingActionBatch, LinearProjectSourceSummary, LinearProjectUpdateResult, LinearReasoningSignals, linear_safety_identifier, run_linear_structured_inference
from .constants import LinearMeetingExtractionDeadlineError
from ...linear_meeting_sources import ParsedSource, SourceParseResult, parse_linear_meeting_sources, source_text_chunks
from difflib import SequenceMatcher
from ..loader import Skill
from .constants import _linear_task_sizing_setting
import asyncio
from datetime import date, datetime, timedelta
from ...llm import extract_text_from_image
from ...config import get_settings
import hashlib
import json
import re


class LinearMeetingActionsHandler:
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_linear_meeting_actions(
            skill,
            text,
            params,
            user_id,
            channel_id,
            thread_ts,
            thread_history,
            kwargs.get("event_files"),
            kwargs.get("current_message_ts"),
            kwargs.get("slack_context"),
        )
        return result

    async def _execute_linear_meeting_actions(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        thread_history: Optional[List[dict]] = None,
        event_files: Optional[List[dict]] = None,
        current_message_ts: Optional[str] = None,
        slack_context: Optional[dict[str, Any]] = None,
    ) -> dict:
        settings = get_settings()
        params = self._apply_linear_meeting_project_hint_prepass(text, params)
        params = self._apply_linear_meeting_owner_hint_prepass(text, params)
        params = self._apply_linear_meeting_default_assignee_prepass(text, params)
        request_context = (slack_context or {}).get("request") or {}
        if request_context:
            params = {
                **params,
                "source_local_datetime": request_context.get("local_datetime"),
                "source_timezone": request_context.get("timezone"),
                "requester_slack_id": request_context.get("user_id") or user_id,
                "requester_display_name": request_context.get("display_name"),
                "requester_email": request_context.get("email"),
            }
        action = str(params.get("action") or "create").strip().lower().replace("-", "_")
        if action == "size_project_issues":
            ClientClass = skill.get_client_class("LinearMeetingActionsClient")
            if ClientClass is None:
                return {"message": "Linear meeting actions are missing their Linear client implementation."}
            return await self._execute_linear_project_issue_sizing(
                text=text,
                params=params,
                user_id=user_id,
                client=ClientClass(),
                settings=settings,
            )
        thread_reference_request = self._is_linear_thread_reference_request(text, params)
        if thread_reference_request and self._linear_request_assigns_to_requester(text):
            params["owner_hint"] = f"<@{user_id}>"
        direct_issue_request = self._is_linear_direct_issue_request(text, params)
        source_result = await self._build_linear_meeting_source_result(
            text=text,
            params=params,
            thread_history=thread_history,
            event_files=event_files,
            settings=settings,
            current_message_ts=current_message_ts,
            exclude_current_message=thread_reference_request,
        )
        transcript = source_result.combined_text()
        # When meeting-note files (PDF/DOCX/text/image) were parsed, always extract
        # action items from those sources. Only treat the message as a one-off "direct
        # issue" command when no document was attached — otherwise a phrase like
        # "add these to linear as tasks" hijacks the command path and the attached file
        # is parsed but never used.
        has_document_sources = source_result.files_parsed > 0
        use_direct_issue_path = (
            direct_issue_request
            and not thread_reference_request
            and not has_document_sources
        )
        if len(transcript.split()) < 8 and not use_direct_issue_path:
            warning_suffix = self._format_linear_meeting_source_warnings(source_result.warnings)
            selection_mode = str(
                ((slack_context or {}).get("selection") or {}).get("mode") or ""
            )
            if thread_reference_request and selection_mode == "recent_channel":
                return {
                    "message": (
                        "I couldn't find enough preceding Slack context to identify the task. "
                        "Reply to the source message in a thread and mention me there, or check "
                        "that Roo has channel-history access."
                        + warning_suffix
                    )
                }
            return {
                "message": (
                    "Paste the meeting transcript or summary in this thread, then ask me to turn it into Linear tasks."
                    + warning_suffix
                )
            }

        ClientClass = skill.get_client_class("LinearMeetingActionsClient")
        if ClientClass is None:
            return {"message": "Linear meeting actions are missing their Linear client implementation."}

        client = ClientClass()
        try:
            teams, users, projects, labels, recent_issues = await asyncio.gather(
                client.list_teams(),
                client.list_users(),
                client.list_active_projects(),
                client.list_issue_labels(),
                client.list_recent_open_issues(),
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            return {
                "message": f"I couldn't read Linear context yet: {detail}"
            }

        projects, explicit_project_resolution = (
            await self._resolve_explicit_linear_project_context(
                client=client,
                params=params,
                projects=projects,
            )
        )
        resolution_status = str(
            (explicit_project_resolution or {}).get("status") or ""
        )
        if resolution_status in {"not_found", "ambiguous", "unavailable"}:
            return {
                "message": self._linear_explicit_project_resolution_error_message(
                    str(params.get("project_hint") or ""),
                    explicit_project_resolution or {},
                ),
                "data": {
                    "created_count": 0,
                    "review_count": 0,
                    "skipped_count": 0,
                    "project_resolution_status": resolution_status,
                },
            }

        project_update_requested = self._linear_meeting_project_update_requested(text, params)
        contextual_review_mode = False
        try:
            if use_direct_issue_path:
                candidates = await self._extract_linear_direct_issue_candidates(
                    text=text,
                    params=params,
                    users=users,
                    projects=projects,
                )
            else:
                candidates = await self._extract_linear_meeting_candidates_from_sources(
                    sources=source_result.sources,
                    params=params,
                    users=users,
                    projects=projects,
                )
            if not candidates and thread_reference_request and not project_update_requested:
                contextual_candidate = await self._extract_linear_thread_context_candidate(
                    sources=source_result.sources,
                    params=params,
                    users=users,
                    projects=projects,
                )
                if contextual_candidate:
                    candidates = [contextual_candidate]
                    contextual_review_mode = True
        except (LinearInferenceTimeoutError, LinearMeetingExtractionDeadlineError) as exc:
            print(
                "LINEAR_MEETING_EXTRACTION "
                + json.dumps(
                    {
                        "event": "aborted_before_writes",
                        "error_type": type(exc).__name__,
                        "project_hint": params.get("project_hint"),
                        "files_parsed": source_result.files_parsed,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return {
                "message": (
                    "I timed out while extracting action items from those notes, so I stopped "
                    "before creating any Linear tasks. Nothing was changed. Please try again."
                ),
                "data": {
                    "created_count": 0,
                    "review_count": 0,
                    "skipped_count": 0,
                    "timed_out": True,
                },
            }
        if not candidates and not project_update_requested:
            return {
                "message": (
                    "I couldn't find any concrete action items in those meeting notes."
                    + self._format_linear_meeting_source_warnings(source_result.warnings)
                )
            }

        request_project_match = self._resolve_linear_meeting_request_project(
            sources=source_result.sources,
            candidates=candidates,
            projects=projects,
            explicit_project_hint=params.get("project_hint"),
            channel_id=channel_id,
            channel_context=(slack_context or {}).get("channel"),
        )
        request_project = request_project_match.get("project") or {}
        request_project_hint = (
            str(request_project.get("name") or "").strip()
            if float(request_project_match.get("confidence") or 0.0) >= 0.78
            else ""
        )
        explicit_bulk_create = bool(
            has_document_sources
            and self._linear_meeting_explicit_creation_authorized(text, params)
        )

        auto_threshold = float(
            getattr(settings, "LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE", 0.85) or 0.85
        )
        uncertain_threshold = float(
            getattr(settings, "LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE", 0.65) or 0.65
        )
        contextual_auto_create_enabled = bool(
            getattr(settings, "LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED", True)
        )
        # Resolve the optional fallback assignee once ("if unsure, assign to X").
        # Used to rescue candidates whose own owner can't be confidently matched so
        # they remain assignable instead of being dropped.
        default_assignee_match: Optional[dict[str, Any]] = None
        default_assignee_hint = str(params.get("default_assignee_hint") or "").strip()
        if default_assignee_hint:
            resolved_default = self._match_linear_meeting_owner(default_assignee_hint, users)
            if resolved_default.get("user"):
                default_assignee_match = resolved_default

        created: list[dict[str, Any]] = []
        review_needed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        project_update: Optional[dict[str, Any]] = None
        project_update_error: Optional[str] = None
        base_source = {
            "workspace_id": str((slack_context or {}).get("workspace_id") or ""),
            "channel_id": channel_id,
            "channel_name": str(((slack_context or {}).get("channel") or {}).get("name") or ""),
            "thread_ts": thread_ts,
            "requester_slack_id": user_id,
            "request_message_ts": current_message_ts,
            "event_id": str(((slack_context or {}).get("request") or {}).get("event_id") or ""),
        }

        if project_update_requested:
            if source_result.files_seen and not source_result.files_parsed and len(transcript.split()) < 80:
                project_update_error = "Skipped project update because the attached file content could not be parsed."
            else:
                project_update_match = request_project_match
                project = project_update_match.get("project")
                if not project or float(project_update_match.get("confidence") or 0.0) < uncertain_threshold:
                    project_update_error = "Skipped project update because Roo could not confidently match the Linear project."
                else:
                    try:
                        update_input = await self._build_linear_meeting_project_update_input(
                            sources=source_result.sources,
                            params=params,
                            project=project,
                            candidates=candidates,
                            recent_issues=recent_issues,
                            settings=settings,
                        )
                        project_update = await client.create_project_update(**update_input)
                    except Exception as exc:
                        project_update_error = f"Could not create project update: {exc.__class__.__name__}: {exc}"

        prepared_candidates: list[dict[str, Any]] = []
        for candidate_index, raw_candidate in enumerate(candidates[:20]):
            candidate = self._normalize_linear_meeting_candidate(raw_candidate)
            if raw_candidate.get("contextual_review_only"):
                candidate["contextual_review_only"] = True
            if not candidate.get("title"):
                continue
            if is_terminal_candidate(candidate):
                skipped.append(
                    {
                        "title": candidate["title"],
                        "assignee": "Not applicable",
                        "project": candidate.get("project_hint") or "Unresolved",
                        "team": "Not applicable",
                        "source": candidate.get("source_label") or "Slack thread",
                        "evidence": candidate.get("evidence") or "",
                        "due_date": candidate.get("due_date"),
                        "confidence": candidate.get("confidence") or 0.0,
                        "reason": f"Work is already {candidate.get('work_status') or 'terminal'}",
                    }
                )
                continue

            if params.get("owner_hint") and (use_direct_issue_path or thread_reference_request):
                candidate["owner_hint"] = str(params["owner_hint"])
            candidate = self._normalize_linear_meeting_due_date(candidate, slack_context)
            evidence_message = self._resolve_linear_evidence_message(candidate, slack_context)
            source = {
                **base_source,
                "source_message_ts": str((evidence_message or {}).get("ts") or ""),
                "source_local_datetime": str(
                    (evidence_message or {}).get("local_datetime") or ""
                ),
                "source_permalink": self._linear_source_permalink(
                    channel_id,
                    str((evidence_message or {}).get("ts") or ""),
                ),
            }

            owner_match = self._match_linear_meeting_owner(candidate.get("owner_hint"), users)
            if (
                float(owner_match.get("confidence") or 0.0) < uncertain_threshold
                and default_assignee_match
                and default_assignee_match.get("user")
            ):
                owner_match = {
                    "user": default_assignee_match["user"],
                    "confidence": float(default_assignee_match.get("confidence") or 0.9),
                    "reason": "Fallback assignee",
                }
            effective_project_hint = (
                params.get("project_hint")
                or request_project_hint
                or candidate.get("project_hint")
            )
            project_match = self._match_linear_meeting_project(
                candidate,
                projects,
                owner_match.get("user"),
                effective_project_hint,
                channel_id=channel_id,
                channel_context=(slack_context or {}).get("channel"),
            )
            team_match = self._match_linear_meeting_team(
                project_match.get("project"),
                teams,
                params.get("team_hint") or candidate.get("team_hint"),
                getattr(settings, "LINEAR_DEFAULT_TEAM", None),
            )
            if use_direct_issue_path:
                owner_match, project_match, team_match = self._relax_unscoped_direct_issue_matches(
                    owner_match=owner_match,
                    project_match=project_match,
                    team_match=team_match,
                    teams=teams,
                    owner_requested=bool(
                        str(params.get("owner_hint") or params.get("owner") or "").strip()
                    ),
                    project_requested=bool(
                        str(
                            params.get("project_hint") or candidate.get("project_hint") or ""
                        ).strip()
                    ),
                    team_requested=bool(
                        str(params.get("team_hint") or candidate.get("team_hint") or "").strip()
                    ),
                )
            duplicate = self._find_linear_meeting_duplicate(
                candidate,
                recent_issues,
                project_match.get("project"),
            )
            decision, overall_confidence = self._linear_meeting_candidate_decision(
                candidate=candidate,
                owner_match=owner_match,
                project_match=project_match,
                team_match=team_match,
                duplicate=duplicate,
                auto_threshold=auto_threshold,
                uncertain_threshold=uncertain_threshold,
            )
            if candidate.get("contextual_review_only") and decision == "create":
                decision = "review"
            # A document request that explicitly asks Roo to create tasks is itself
            # write authorisation. Keep genuinely inferred discussion work and
            # contextual thread harvesting review-first.
            if not use_direct_issue_path and decision != "duplicate":
                contextual_explicit_create = bool(
                    contextual_auto_create_enabled
                    and thread_reference_request
                    and candidate.get("explicit_commitment")
                    and not candidate.get("contextual_review_only")
                    and decision == "create"
                )
                authorised_bulk_create = bool(
                    explicit_bulk_create
                    and candidate.get("explicit_commitment")
                    and not candidate.get("contextual_review_only")
                    and decision == "create"
                )
                if contextual_explicit_create or authorised_bulk_create:
                    decision = "create"
                elif owner_match.get("user") and team_match.get("team"):
                    decision = "review"
                else:
                    decision = "skip"

            display = self._build_linear_meeting_candidate_display(
                candidate,
                owner_match,
                project_match,
                team_match,
                overall_confidence,
            )

            if decision == "duplicate":
                skipped.append({
                    **display,
                    "reason": "Likely duplicate",
                    "duplicate": duplicate,
                })
                continue

            if decision == "skip":
                skip_reason = self._linear_meeting_skip_reason(
                    candidate,
                    owner_match,
                    project_match,
                    team_match,
                    uncertain_threshold,
                )
                if candidate.get("contextual_review_only"):
                    if float(owner_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Assignee unclear; mention who should own this and I can add it to Linear."
                    elif float(project_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Project unclear; mention the Linear project and I can add it."
                    elif float(team_match.get("confidence") or 0.0) < uncertain_threshold:
                        skip_reason = "Linear team unclear; mention the team and I can add it."
                elif (
                    use_direct_issue_path
                    and not team_match.get("team")
                    and not project_match.get("project")
                    and float(owner_match.get("confidence") or 0.0) >= uncertain_threshold
                    and float(project_match.get("confidence") or 0.0) >= uncertain_threshold
                ):
                    team_names = [
                        str(team.get("key") or team.get("name") or "").strip()
                        for team in teams
                        if str(team.get("key") or team.get("name") or "").strip()
                    ]
                    skip_reason = "Linear team unclear; mention the team and I can add it."
                    if team_names:
                        skip_reason = f"{skip_reason} Teams: {', '.join(team_names[:8])}."
                skipped.append({**display, "reason": skip_reason})
                continue

            candidate_key = (
                f"c{candidate_index}-"
                f"{hashlib.sha256(candidate['title'].encode('utf-8')).hexdigest()[:12]}"
            )
            idempotency_key = self._linear_meeting_idempotency_key(
                candidate=candidate,
                source=source,
                assignee_id=str((owner_match.get("user") or {}).get("id") or ""),
                project_id=str((project_match.get("project") or {}).get("id") or ""),
            )
            prepared_candidates.append(
                {
                    "candidate_key": candidate_key,
                    "idempotency_key": idempotency_key,
                    "candidate": candidate,
                    "owner_match": owner_match,
                    "project_match": project_match,
                    "team_match": team_match,
                    "source": source,
                    "decision": decision,
                    "overall_confidence": overall_confidence,
                    "display": display,
                }
            )

        sizing_shadow = await self._size_linear_studio_prepared_candidates(
            prepared_candidates=prepared_candidates,
            client=client,
            labels=labels,
            settings=settings,
            requester_slack_id=user_id,
        )
        sizing_mode = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_MODE",
                "LINEAR_STUDIO_SIZING_MODE",
                "off",
            )
        ).strip().lower()
        if sizing_mode not in {"off", "shadow", "review", "required"}:
            sizing_mode = "off"
        sizing_auto_threshold = float(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_AUTO_CREATE_MIN_CONFIDENCE",
                "LINEAR_STUDIO_SIZING_AUTO_CREATE_MIN_CONFIDENCE",
                0.75,
            )
            or 0.75
        )

        for prepared in prepared_candidates:
            candidate = prepared["candidate"]
            project = (prepared["project_match"].get("project") or {})
            team = prepared["team_match"].get("team") or {}
            display = prepared["display"]
            decision = prepared["decision"]
            project_candidate = is_project_issue(project)
            replay_issue = prepared.get("receipt_replay_issue")
            if isinstance(replay_issue, dict):
                created.append(
                    {
                        **display,
                        "issue": {**replay_issue, "idempotentReplay": True},
                    }
                )
                continue
            sizing_error = str(prepared.get("sizing_error") or "")
            if project_candidate and sizing_mode in {"review", "required"}:
                if sizing_error or not prepared.get("effort_assessment"):
                    skipped.append(
                        {
                            **display,
                            "reason": (
                                "Effort sizing could not be completed; no unlabeled issue was created. "
                                + (sizing_error or "Rerun the request after the sizing context is available.")
                            )[:700],
                        }
                    )
                    continue
                if sizing_mode == "review":
                    decision = "review"
                else:
                    assessment = prepared["effort_assessment"]
                    if (
                        float(assessment.get("confidence") or 0.0)
                        < sizing_auto_threshold
                        or not assessment.get("context_sufficient")
                    ):
                        decision = "review"

            meeting_action_ids = self._linear_compatible_label_ids(
                labels,
                label_name="meeting-action",
                team_id=str(team.get("id") or ""),
            )
            label_ids = meeting_action_ids[:1]
            if project_candidate and sizing_mode in {"review", "required"}:
                label_ids.append(str(prepared["effort_label_id"]))
            issue_input = self._build_linear_meeting_issue_input(
                candidate=candidate,
                owner_match=prepared["owner_match"],
                project_match=prepared["project_match"],
                team_match=prepared["team_match"],
                label_ids=label_ids,
                source=prepared["source"],
                sizing_metadata=prepared.get("effort_assessment"),
            )

            if decision == "create":
                try:
                    issue = await client.create_issue(**issue_input)
                    created.append({**display, "issue": issue})
                except Exception as exc:
                    review_needed.append(
                        {
                            **display,
                            "issue_input": issue_input,
                            "review_reason": (
                                f"Linear create failed: {exc.__class__.__name__}: {exc}"
                            ),
                        }
                    )
                continue

            review_needed.append(
                {
                    **display,
                    "issue_input": issue_input,
                    "review_reason": "Needs approval",
                }
            )

        review_batch: Optional[dict[str, Any]] = None
        if review_needed:
            source_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "source": base_source,
                        "items": [
                            item.get("issue_input", {}).get("idempotency_key")
                            or item.get("title")
                            for item in review_needed
                        ],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            try:
                review_batch = await client.create_action_batch(
                    requested_by_slack_user_id=user_id,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                    source_fingerprint=source_fingerprint,
                    items=[
                        {
                            "issue_input": item["issue_input"],
                            "display": {
                                key: value
                                for key, value in item.items()
                                if key not in {"issue_input", "review_reason"}
                            },
                            "reason": item.get("review_reason") or "Needs approval",
                        }
                        for item in review_needed
                    ],
                )
                persisted_by_position = {
                    int(item.get("position") or 0): item
                    for item in review_batch.get("items") or []
                    if isinstance(item, dict)
                }
                batch_id = str(review_batch.get("id") or "")
                if not batch_id:
                    raise RuntimeError("Approval storage returned no batch id")
                missing_positions = [
                    position
                    for position in range(len(review_needed))
                    if not str(
                        (persisted_by_position.get(position) or {}).get("id") or ""
                    )
                ]
                if missing_positions:
                    raise RuntimeError(
                        "Approval storage returned no item id for position(s) "
                        + ", ".join(str(position) for position in missing_positions)
                    )
                for position, item in enumerate(review_needed):
                    persisted = persisted_by_position.get(position) or {}
                    item["batch_id"] = batch_id
                    item["item_id"] = str(persisted.get("id") or "")
                    item.pop("issue_input", None)
                    item.pop("review_reason", None)
            except Exception as exc:
                staging_error = (
                    "Approval storage was unavailable; no review buttons were created. "
                    f"Retry the original request. ({exc.__class__.__name__}: {exc})"
                )
                skipped.extend(
                    {**item, "reason": staging_error}
                    for item in review_needed
                )
                review_needed = []

        message = self._format_linear_meeting_result_message(
            created,
            review_needed,
            skipped,
            project_update=project_update,
            project_update_error=project_update_error,
            contextual_review_mode=contextual_review_mode,
        )
        message += self._format_linear_meeting_source_warnings(source_result.warnings)
        blocks = (
            self._build_linear_meeting_review_blocks(
                message,
                review_needed,
                user_id,
                batch_id=str((review_batch or {}).get("id") or ""),
            )
            if review_needed
            else None
        )
        return {
            "message": message,
            "blocks": blocks,
            "data": {
                "created_count": len(created),
                "review_count": len(review_needed),
                "skipped_count": len(skipped),
                "review_batch_id": str((review_batch or {}).get("id") or "") or None,
                "effort_sizing_results": sizing_shadow,
            },
        }


    async def _execute_linear_project_issue_sizing(
        self,
        *,
        text: str,
        params: dict[str, Any],
        user_id: str,
        client: Any,
        settings: Any,
    ) -> dict[str, Any]:
        """Build a durable, no-write preview for sizing one complete project."""

        project_hint = str(params.get("project_hint") or "").strip()
        if not project_hint:
            project_hint = str(
                self._extract_linear_meeting_project_hint_from_text(text) or ""
            ).strip()
        if not project_hint:
            return {
                "message": (
                    "Tell me the exact Linear project name or slug to size. "
                    "For example: @Roo size the unsized tasks in Linear project Aaron AI."
                )
            }

        try:
            projects, users, labels = await asyncio.gather(
                client.list_active_projects(),
                client.list_users(),
                client.list_issue_labels(),
            )
        except Exception as exc:
            return {
                "message": f"I couldn't read Linear context yet: {exc.__class__.__name__}: {exc}"
            }

        project_match = self._match_linear_meeting_project(
            {"project_hint": project_hint},
            projects,
            None,
            project_hint,
        )
        project = project_match.get("project")
        if not isinstance(project, dict) or float(
            project_match.get("confidence") or 0.0
        ) < 0.95:
            return {
                "message": (
                    f"I couldn't uniquely match {project_hint!r} to an active Linear project. "
                    "Use its exact project name or slug; nothing was changed."
                )
            }
        project_id = str(project.get("id") or "")

        requester_hint = (
            str(params.get("requester_email") or "").strip()
            or str(params.get("requester_display_name") or "").strip()
            or f"<@{user_id}>"
        )
        requester_match = self._match_linear_meeting_owner(requester_hint, users)
        requester = requester_match.get("user")
        if not isinstance(requester, dict) or float(
            requester_match.get("confidence") or 0.0
        ) < 0.9:
            return {
                "message": (
                    "I couldn't map your Slack identity to a Linear user, so I couldn't "
                    "verify project-sizing access. Nothing was changed."
                )
            }

        max_issues = max(
            1,
            int(getattr(settings, "LINEAR_PROJECT_SIZING_MAX_ISSUES", 500) or 500),
        )
        try:
            inventory, update_inventory, bounded_context = await asyncio.gather(
                client.list_project_issues(project_id, max_issues=max_issues),
                client.list_project_updates(project_id),
                client.get_project_sizing_context(project_id),
            )
        except Exception as exc:
            return {
                "message": (
                    f"I couldn't build a complete sizing snapshot for {project.get('name')!r}: "
                    f"{exc.__class__.__name__}: {exc}. Nothing was changed."
                )
            }

        live_project = inventory.get("project")
        if not isinstance(live_project, dict) or str(
            live_project.get("id") or ""
        ) != project_id:
            return {
                "message": "Linear returned mismatched project inventory; nothing was changed."
            }
        if str(live_project.get("name") or "") != str(project.get("name") or ""):
            return {
                "message": "The Linear project changed while I prepared the preview; nothing was changed."
            }

        terminal_types = {
            str(value).strip().lower()
            for value in inventory.get("terminalStateTypes") or []
            if str(value).strip()
        } or {"completed", "canceled", "cancelled", "duplicate"}
        mode = str(params.get("mode") or params.get("scope") or "").strip().lower()
        normalized_request = self._normalize_match_text(text)
        replace_requested = mode in {
            "replace_existing",
            "replace",
            "rescore",
            "resize",
        } or any(
            token in normalized_request
            for token in ("replaceexisting", "rescore", "resize", "reestimate")
        )
        run_mode = "replace_existing" if replace_requested else "missing_only"

        active_issues: list[dict[str, Any]] = []
        skipped_terminal = 0
        skipped_already_sized = 0
        for issue in inventory.get("nodes") or []:
            if not isinstance(issue, dict):
                continue
            state_type = str(((issue.get("state") or {}).get("type")) or "").lower()
            if state_type in terminal_types:
                skipped_terminal += 1
                continue
            issue_labels = self._linear_connection_nodes(issue.get("labels"))
            effort_labels = [
                label
                for label in issue_labels
                if str(label.get("name") or "") in EFFORT_LABELS
            ]
            if run_mode == "missing_only" and len(effort_labels) == 1:
                skipped_already_sized += 1
                continue
            active_issues.append(issue)

        if not active_issues:
            return {
                "message": (
                    f"{live_project.get('name')!r} has no eligible active issues to size "
                    f"({skipped_already_sized} already sized; {skipped_terminal} terminal). "
                    "Nothing was changed."
                )
            }

        team_ids = {
            str(((issue.get("team") or {}).get("id")) or "")
            for issue in active_issues
        }
        if "" in team_ids:
            return {
                "message": "At least one eligible issue has no Linear team; nothing was changed."
            }
        try:
            for team_id in sorted(team_ids):
                for effort_label in EFFORT_LABELS:
                    compatible = self._linear_compatible_label_ids(
                        labels,
                        label_name=effort_label,
                        team_id=team_id,
                        exact_name=True,
                    )
                    if len(compatible) != 1:
                        raise RuntimeError(
                            f"Expected exactly one compatible label named {effort_label!r} "
                            f"for team {team_id!r}; found {len(compatible)}."
                        )
        except Exception as exc:
            return {
                "message": f"Effort-label preflight failed: {exc} Nothing was changed."
            }

        return await self._finish_linear_project_issue_sizing_preview(
            text=text,
            params=params,
            user_id=user_id,
            client=client,
            settings=settings,
            requester=requester,
            live_project=live_project,
            inventory=inventory,
            update_inventory=update_inventory,
            bounded_context=bounded_context,
            active_issues=active_issues,
            run_mode=run_mode,
            skipped_already_sized=skipped_already_sized,
            skipped_terminal=skipped_terminal,
        )


    async def _finish_linear_project_issue_sizing_preview(
        self,
        *,
        text: str,
        params: dict[str, Any],
        user_id: str,
        client: Any,
        settings: Any,
        requester: dict[str, Any],
        live_project: dict[str, Any],
        inventory: dict[str, Any],
        update_inventory: dict[str, Any],
        bounded_context: dict[str, Any],
        active_issues: list[dict[str, Any]],
        run_mode: str,
        skipped_already_sized: int,
        skipped_terminal: int,
    ) -> dict[str, Any]:
        full_project_context = dict(bounded_context)
        full_project_context["project"] = live_project
        full_project_context["projectUpdates"] = {
            "nodes": update_inventory.get("nodes") or [],
            "returned": len(update_inventory.get("nodes") or []),
            "truncated": bool(update_inventory.get("truncated")),
        }
        terminal_types = {
            str(value).strip().lower()
            for value in inventory.get("terminalStateTypes") or []
            if str(value).strip()
        } or {"completed", "canceled", "cancelled", "duplicate"}
        context_active_issues = [
            issue
            for issue in inventory.get("nodes") or []
            if isinstance(issue, dict)
            and str(((issue.get("state") or {}).get("type")) or "").lower()
            not in terminal_types
        ]
        full_project_context["activeIssues"] = {
            "nodes": context_active_issues,
            "returned": len(context_active_issues),
            "truncated": bool(inventory.get("truncated")),
        }

        sizing_candidates: list[dict[str, Any]] = []
        issues_by_id: dict[str, dict[str, Any]] = {}
        for issue in active_issues:
            issue_id = str(issue.get("id") or "")
            issues_by_id[issue_id] = issue
            description = str(issue.get("description") or "").strip()
            sizing_candidates.append(
                {
                    "candidate_key": issue_id,
                    "id": issue_id,
                    "identifier": str(issue.get("identifier") or ""),
                    "title": str(issue.get("title") or ""),
                    "description": description,
                    "work_status": "open",
                    "completed_work": "",
                    "remaining_work": description,
                    "available_artifacts": [],
                    "dependencies": [],
                    "acceptance_criteria": [],
                    "due_date": issue.get("dueDate"),
                    "assignee": issue.get("assignee") or {},
                    "team": issue.get("team") or {},
                }
            )

        context_max_chars = int(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS",
                "LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS",
                40000,
            )
        )
        batch_size = int(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_BATCH_SIZE",
                "LINEAR_STUDIO_SIZING_BATCH_SIZE",
                3,
            )
        )
        assessments, errors = await self._assess_linear_studio_candidates_resilient(
            candidates=sizing_candidates,
            project_context=full_project_context,
            context_max_chars=context_max_chars,
            batch_size=batch_size,
            safety_identifier=linear_safety_identifier(user_id),
            max_concurrency=int(
                getattr(
                    settings,
                    "LINEAR_PROJECT_SIZING_INFERENCE_CONCURRENCY",
                    3,
                )
                or 3
            ),
        )
        if errors or len(assessments) != len(sizing_candidates):
            first_error = next(
                iter(errors.values()),
                "One or more assessments were missing.",
            )
            return {
                "message": (
                    f"I couldn't size all {len(sizing_candidates)} eligible issues reliably "
                    f"({len(errors)} failed): {first_error} Nothing was changed."
                ),
                "data": {"sizing_errors": errors},
            }

        rubric_version = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_RUBRIC_VERSION",
                "LINEAR_STUDIO_SIZING_RUBRIC_VERSION",
                "project-effort-v2",
            )
        )
        proposals: list[dict[str, Any]] = []
        for candidate in sizing_candidates:
            issue_id = str(candidate["candidate_key"])
            issue = issues_by_id[issue_id]
            assessment = assessments[issue_id]
            metadata = assessment_metadata(
                assessment,
                project=live_project,
                model=LINEAR_SKILL_MODEL,
                rubric_version=rubric_version,
            )
            proposals.append(
                {
                    "issue_id": issue_id,
                    "identifier": issue.get("identifier"),
                    "title": issue.get("title"),
                    "team_id": str(((issue.get("team") or {}).get("id")) or ""),
                    "expected_updated_at": issue.get("updatedAt"),
                    "original_labels": self._linear_connection_nodes(
                        issue.get("labels")
                    ),
                    "effort_label": assessment.effort_label,
                    "rationale": assessment.rationale,
                    "sizing_metadata": metadata,
                }
            )

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "project": str(live_project.get("id") or ""),
                    "requester": user_id,
                    "mode": run_mode,
                    "snapshot": inventory.get("snapshotAt"),
                    "issues": [
                        [item["issue_id"], item["expected_updated_at"]]
                        for item in proposals
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            run = await client.create_project_sizing_run(
                project_id=str(live_project.get("id") or ""),
                payload={
                    "requested_by_slack_user_id": user_id,
                    "requested_by_linear_user_id": str(requester.get("id") or ""),
                    "project_name": str(live_project.get("name") or ""),
                    "mode": run_mode,
                    "model": LINEAR_SKILL_MODEL,
                    "rubric_version": rubric_version,
                    "source_snapshot_at": inventory.get("snapshotAt"),
                    "project_context": {
                        "projectUpdateCount": len(
                            update_inventory.get("nodes") or []
                        ),
                        "issueCount": len(inventory.get("nodes") or []),
                    },
                    "idempotency_key": fingerprint,
                    "items": proposals,
                },
            )
        except Exception as exc:
            return {
                "message": (
                    "I sized the issues but couldn't save a safe preview: "
                    f"{exc.__class__.__name__}: {exc}. Nothing was changed."
                )
            }

        run_id = str(run.get("id") or "")
        preview_lines = [
            (
                f"Effort sizing preview for *{live_project.get('name')}*: "
                f"{len(proposals)} issue(s)."
            ),
            (
                f"Mode: {run_mode} · model: {LINEAR_SKILL_MODEL} · "
                "no labels changed yet."
            ),
            "",
        ]
        for proposal in proposals[:15]:
            identifier = proposal.get("identifier") or proposal["issue_id"]
            preview_lines.append(
                f"- *{identifier}* {proposal['title']} -> "
                f"{proposal['effort_label']} — {proposal['rationale']}"
            )
        if len(proposals) > 15:
            preview_lines.append(f"- ...and {len(proposals) - 15} more issue(s).")
        preview_lines.append(
            f"Skipped {skipped_already_sized} already sized and "
            f"{skipped_terminal} terminal issue(s)."
        )
        message = "\n".join(preview_lines)
        value = json.dumps({"run_id": run_id, "requested_by": user_id})
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message[:3000]},
            },
            {
                "type": "actions",
                "block_id": f"linear_project_sizing_{run_id[:8]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Apply size labels",
                        },
                        "style": "primary",
                        "action_id": "linear_project_sizing_apply",
                        "value": value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "linear_project_sizing_cancel",
                        "value": value,
                    },
                ],
            },
        ]
        return {
            "message": message,
            "blocks": blocks,
            "data": {
                "run_id": run_id,
                "preview_count": len(proposals),
                "skipped_already_sized": skipped_already_sized,
                "skipped_terminal": skipped_terminal,
                "mode": run_mode,
            },
        }


    def _build_linear_meeting_transcript(
        self,
        text: str,
        params: dict,
        history: Optional[List[dict]] = None,
    ) -> str:
        explicit = str(params.get("transcript") or "").strip()
        parts: list[str] = []
        if explicit:
            parts.append(explicit)

        for message in history or []:
            if message.get("is_bot") or message.get("bot_id"):
                continue
            message_text = str(message.get("text") or "").strip()
            if not message_text:
                continue
            speaker = str(message.get("user") or "user").strip()
            parts.append(f"{speaker}: {message_text}")

        clean_text = str(text or "").strip()
        if clean_text and all(clean_text not in part for part in parts):
            parts.append(clean_text)

        return "\n".join(dict.fromkeys(parts)).strip()


    async def _build_linear_meeting_source_result(
        self,
        *,
        text: str,
        params: dict,
        thread_history: Optional[List[dict]],
        event_files: Optional[List[dict]],
        settings: Any,
        current_message_ts: Optional[str] = None,
        exclude_current_message: bool = False,
    ) -> SourceParseResult:
        image_parser = None
        if getattr(settings, "OPENAI_API_KEY", None):
            async def image_parser(image_bytes: bytes, mime_type: str, label: str) -> str:
                prompt = (
                    "Extract meeting transcript text or to-do/action items from this image. "
                    "Preserve names, due dates, checkboxes, bullets, and project labels. "
                    f"Return plain text only. Source label: {label}"
                )
                return await extract_text_from_image(
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    prompt=prompt,
                    model=getattr(settings, "OPENAI_VISION_MODEL", None),
                )

        return await parse_linear_meeting_sources(
            text=text,
            params=params,
            thread_history=thread_history,
            event_files=event_files,
            image_parser=image_parser,
            current_message_ts=current_message_ts,
            exclude_current_message=exclude_current_message,
        )


    def _format_linear_meeting_source_warnings(self, warnings: list[str]) -> str:
        if not warnings:
            return ""
        lines = ["", "", "Source notes:"]
        for warning in warnings[:8]:
            lines.append(f"- {warning}")
        if len(warnings) > 8:
            lines.append(f"- {len(warnings) - 8} more source note(s) omitted.")
        return "\n".join(lines)


    def _apply_linear_meeting_project_hint_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("project_hint"):
            return params
        project_hint = self._extract_linear_meeting_project_hint_from_text(text)
        if not project_hint:
            return params
        return {**params, "project_hint": project_hint}


    def _apply_linear_meeting_owner_hint_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("owner_hint") or params.get("owner"):
            return params
        owner_hint = self._extract_linear_meeting_owner_hint_from_text(text)
        if not owner_hint:
            return params
        return {**params, "owner_hint": owner_hint}


    def _apply_linear_meeting_default_assignee_prepass(
        self,
        text: str,
        params: dict,
    ) -> dict:
        if params.get("default_assignee_hint"):
            return params
        default_assignee = self._extract_linear_meeting_default_assignee_from_text(text)
        if not default_assignee:
            return params
        return {**params, "default_assignee_hint": default_assignee}


    def _extract_linear_meeting_default_assignee_from_text(self, text: str) -> Optional[str]:
        """Parse a fallback assignee like "if you're not sure who to assign to, assign to X"."""
        value = str(text or "").strip()
        person = r'(<@[A-Z0-9]+>|@?[A-Z][\w.\-]*(?:\s+[A-Z][\w.\-]*){0,3})'
        patterns = (
            rf'\bif\b[^.!?]{{0,180}}?\b(?:can(?:not|[\'’]?t)|could(?:not|[\'’]?t))\s+find\b[^.!?]{{0,180}}?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\bif\s+(?:you(?:\'re|\s+are)?\s+)?(?:not\s+sure|unsure|in\s+doubt|you\s+don\'?t\s+know)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\bif\s+(?:it\'?s\s+)?(?:unclear|unknown|ambiguous)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\b(?:otherwise|by\s+default|as\s+a\s+fallback|default(?:ing)?)\b[^.!?]*?\bassign(?:ed)?\b[^.!?]*?\bto\s+{person}',
            rf'\b(?:default|fallback)\s+assignee\s*[:\-]?\s*{person}',
        )
        for pattern in patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                return self._clean_linear_meeting_owner_hint(match.group(1))
        return None


    def _extract_linear_meeting_project_hint_from_text(self, text: str) -> Optional[str]:
        value = str(text or "").strip()
        for pattern in self._linear_meeting_project_hint_patterns():
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                project_hint = self._clean_linear_meeting_project_hint(match.group(1))
                normalized_hint = self._normalize_match_text(project_hint)
                if project_hint and normalized_hint not in {"update", "updates"} and not normalized_hint.startswith("update"):
                    return project_hint

        return None


    @staticmethod
    def _linear_meeting_project_hint_patterns() -> tuple[str, ...]:
        quoted = r'["\'“”‘’]([^"\'“”‘’]+)["\'“”‘’]'
        unquoted = (
            r'([A-Za-z0-9][A-Za-z0-9 _/&-]{1,80}?)'
            r'(?=\s+(?:to|and|assign|assigned|with|please|from|as|for|due|by)\b|[.!?,;]|$)'
        )
        return (
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?linear\s+project\s+{quoted}',
            rf'\blinear\s+project\s+{quoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?project\s+{quoted}',
            rf'\bproject\s+(?:called|named)\s+{quoted}',
            rf'\bproject\s+{quoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?linear\s+project\s+{unquoted}',
            rf'\bto\s+linear\s+project\s+{unquoted}',
            rf'\bproject\s+(?:called|named)\s+{unquoted}',
            rf'\b(?:in|into|to|under)\s+(?:the\s+)?project\s+{unquoted}',
            rf'\bproject\s+{unquoted}',
        )


    @staticmethod
    def _clean_linear_meeting_project_hint(value: str) -> Optional[str]:
        cleaned = str(value or "").strip(" \t\n\r'\"“”‘’`")
        return cleaned or None


    def _extract_linear_meeting_owner_hint_from_text(self, text: str) -> Optional[str]:
        value = str(text or "").strip()
        mention_assignment = re.search(
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\s*(<@[A-Z0-9]+>)',
            value,
            flags=re.IGNORECASE,
        )
        if mention_assignment:
            return mention_assignment.group(1)

        assignment = re.search(
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\s*'
            r'(.+?)(?=\s+(?:in|to|under)\s+(?:the\s+)?linear\s+project\b|[.!?]\s|$)',
            value,
            flags=re.IGNORECASE,
        )
        if assignment:
            return self._clean_linear_meeting_owner_hint(assignment.group(1))

        for_owner = re.search(
            r'\bfor\s+(<@[A-Z0-9]+>|@?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s*$',
            value,
        )
        if for_owner:
            return self._clean_linear_meeting_owner_hint(for_owner.group(1))
        return None


    @staticmethod
    def _clean_linear_meeting_owner_hint(value: str) -> Optional[str]:
        cleaned = str(value or "").strip(" \t\n\r'\"“”`")
        cleaned = re.sub(r'^(?:@|to\s+)', '', cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(" ,;:.!?")
        return cleaned or None


    def _is_linear_direct_issue_request(self, text: str, params: dict[str, Any]) -> bool:
        value = str(text or "").lower()
        if "linear" not in value:
            return False
        if re.search(r'\b(points?|rewards?|coworking|allowance|worth\s+\d+\s+points?)\b', value):
            return False
        if self._linear_meeting_project_update_requested(text, params):
            return False
        has_creation_intent = bool(re.search(r'\b(create|add|open|file|make)\b', value))
        has_issue_noun = bool(
            re.search(
                r'\b(?:to\s*do\s+items?|todo\s+items?|tasks?|issues?|tickets?|prds?)\b',
                value,
            )
        )
        return has_creation_intent and has_issue_noun


    def _is_linear_thread_reference_request(self, text: str, params: dict[str, Any]) -> bool:
        from ...linear_context import is_contextual_linear_reference

        normalized = str(text or "").lower()
        if "linear" not in normalized:
            return False
        if self._normalize_match_text(params.get("action")) in {"threadreference", "addthread", "addthis"}:
            return True
        if is_contextual_linear_reference(text):
            return True
        return False


    @staticmethod
    def _linear_request_assigns_to_requester(text: str) -> bool:
        value = str(text or "")
        return bool(
            re.search(r"\bfor\s+me\b", value, flags=re.IGNORECASE)
            or re.search(
                r"\bassign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to\s+me\b",
                value,
                flags=re.IGNORECASE,
            )
        )


    async def _extract_linear_thread_context_candidate(
        self,
        *,
        sources: list[ParsedSource],
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        source_excerpt = "\n\n".join(
            chunk
            for source in sources
            for chunk in source_text_chunks(source, max_chars=5000)[:1]
        )[:12000]
        if len(source_excerpt.split()) < 8:
            return None

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        prompt = f"""Draft exactly one possible Linear issue from the referenced Slack thread.

This is not a formal meeting transcript. The user asked Roo to add the thread context to Linear, so infer the likely follow-up item from the prior conversation only.

Rules:
- Return null if the thread is too vague to turn into one useful Linear issue.
- Do not create a task called "add this to Linear" or similar.
- For a discussion or decision, write the issue as a concrete follow-up such as "Decide ..." or "Confirm ...".
- Infer the owner from direct address, mentions, and conversational responsibility. Prefer an exact Slack mention token like <@U123> when present.
- Use the explicit project hint if present.

Project hint: {params.get("project_hint") or "none"}
Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Referenced Slack thread:
{source_excerpt}

Return one structured issue, or null when the thread is too vague."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Draft one review-only Linear issue from Slack context. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearContextualIssueResult,
                signals=LinearReasoningSignals(
                    stage="contextual_issue",
                    source_chars=len(source_excerpt),
                    source_count=max(1, len(sources)),
                    explicit_project=bool(params.get("project_hint")),
                    explicit_owner=bool(
                        params.get("owner_hint") or params.get("owner")
                    ),
                    ambiguity=not bool(
                        params.get("project_hint")
                        and (params.get("owner_hint") or params.get("owner"))
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
        except ValueError:
            return None
        if inference.value.issue is None:
            return None
        issue = inference.value.issue.model_dump(mode="json")
        candidate = self._normalize_linear_meeting_candidate(issue)
        if not candidate.get("title"):
            return None
        if params.get("project_hint") and not candidate.get("project_hint"):
            candidate["project_hint"] = str(params["project_hint"])
        candidate["contextual_review_only"] = True
        candidate["source_label"] = candidate.get("source_label") or "Slack thread"
        candidate["priority"] = candidate.get("priority", 3)
        return candidate


    async def _extract_linear_direct_issue_candidates(
        self,
        *,
        text: str,
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        project_hint = str(params.get("project_hint") or "").strip()
        if not project_hint:
            project_hint = self._infer_linear_direct_project_hint_from_known_projects(text, projects)
            if project_hint:
                params = {**params, "project_hint": project_hint}
        task_body = self._extract_linear_direct_issue_body(text, params)
        if not task_body:
            return []

        owner_hint = str(params.get("owner_hint") or params.get("owner") or "").strip()
        fallback = {
            "title": self._linear_direct_issue_title_from_body(task_body),
            "description": task_body,
            "work_status": "open",
            "completed_work": "",
            "remaining_work": task_body,
            "available_artifacts": [],
            "dependencies": [],
            "acceptance_criteria": [],
            "owner_hint": owner_hint,
            "project_hint": project_hint,
            "evidence": text[:700],
            "source_label": "Slack command",
            "evidence_message_ts": "",
            "explicit_commitment": True,
            "confidence": 0.96,
        }

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        prompt = f"""Clean up this explicit Slack command into Linear issue fields.

The user is directly asking Roo to create Linear issue(s). Do not ignore the command as a meta-instruction.
Use the parsed task body as the work to create, not "create a Linear task" itself.

Parsed project hint: {project_hint or "none"}
Parsed assignee hint: {owner_hint or "none"}
Parsed task body: {task_body}

Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Slack command:
{text[:12000]}

Return the structured issue list. Preserve the parsed project and assignee hints."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Convert explicit Slack commands into Linear issue fields. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearDirectIssueBatch,
                signals=LinearReasoningSignals(
                    stage="direct_issue",
                    source_chars=len(text),
                    explicit_project=bool(project_hint),
                    explicit_owner=bool(owner_hint),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
            issues = [
                issue.model_dump(mode="json")
                for issue in inference.value.issues
            ]
        except Exception:
            issues = []

        candidates: list[dict[str, Any]] = []
        for issue in issues or []:
            if not isinstance(issue, dict):
                continue
            candidate = {
                **fallback,
                **issue,
                # Keep the parsed command's assignee and project. A model-invented
                # hint that nobody asked for becomes "Assignee unclear" and drops
                # an explicit create.
                "owner_hint": owner_hint,
                "project_hint": project_hint,
                "source_label": issue.get("source_label") or "Slack command",
                "evidence": issue.get("evidence") or text[:700],
                "confidence": issue.get("confidence", 0.96),
            }
            normalized = self._normalize_linear_meeting_candidate(candidate)
            if normalized.get("title"):
                candidates.append(normalized)
        return candidates or [fallback]


    def _infer_linear_direct_project_hint_from_known_projects(
        self,
        text: str,
        projects: list[dict[str, Any]],
    ) -> str:
        normalized_text = self._normalize_match_text(text)
        matches: list[tuple[int, str]] = []
        for project in projects:
            name = str(project.get("name") or "").strip()
            slug = str(project.get("slugId") or "").strip()
            for value in (name, slug):
                normalized_value = self._normalize_match_text(value)
                if normalized_value and normalized_value in normalized_text:
                    matches.append((len(normalized_value), name or slug))
                    break
        if not matches:
            return ""
        matches.sort(reverse=True)
        return matches[0][1]


    def _extract_linear_direct_issue_body(self, text: str, params: dict[str, Any]) -> str:
        value = str(text or "").strip()
        value = re.sub(r'<@[A-Z0-9]+>', ' ', value)
        value = re.sub(r'\s+', ' ', value).strip()

        assignment_pattern = (
            r'\b(?:assign(?:ed)?(?:\s+(?:it|this|task|issue))?\s+to|assignee\s*:|owner\s*:)\b.*$'
        )
        without_assignment = re.sub(assignment_pattern, '', value, flags=re.IGNORECASE).strip(" ,.;:")

        project_match = None
        for pattern in self._linear_meeting_project_hint_patterns():
            match = re.search(pattern, without_assignment, flags=re.IGNORECASE)
            if match:
                project_match = match
                break

        if project_match:
            task_body = without_assignment[project_match.end():].strip()
            if not task_body:
                task_body = without_assignment[:project_match.start()].strip()
        else:
            task_body = without_assignment

        project_hint = str(params.get("project_hint") or "").strip()
        if project_hint:
            hint_pattern = re.escape(project_hint)
            direct_project_match = re.search(
                rf'\b(?:in|into|to|under)\s+{hint_pattern}\b',
                task_body,
                flags=re.IGNORECASE,
            )
            if direct_project_match:
                after_project = task_body[direct_project_match.end():].strip()
                before_project = task_body[:direct_project_match.start()].strip()
                task_body = after_project or before_project

        task_body = re.sub(
            r'^(?:please\s+)?(?:create|add|open|file|make)\s+(?:a|an|the)?\s*'
            r'(?:linear\s+)?(?:(?:to\s*do|todo)\s+items?|tasks?|issues?|tickets?)\s*',
            '',
            task_body,
            flags=re.IGNORECASE,
        )
        owner_hint = str(params.get("owner_hint") or params.get("owner") or "").strip()
        if owner_hint:
            task_body = re.sub(
                rf'^\s*for\s+{re.escape(owner_hint)}\s*$',
                '',
                task_body,
                flags=re.IGNORECASE,
            )
            task_body = re.sub(
                rf'\s+\bfor\s+{re.escape(owner_hint)}\s*$',
                '',
                task_body,
                flags=re.IGNORECASE,
            )
        task_body = re.sub(r'^(?:in|into|to|under|for|about|that|where|:|-)\s+', '', task_body, flags=re.IGNORECASE)
        task_body = re.sub(r'\s+', ' ', task_body).strip(" ,.;:-")
        return task_body


    @staticmethod
    def _linear_direct_issue_title_from_body(task_body: str) -> str:
        title = str(task_body or "").strip()
        title = re.sub(r'^(?:to\s+)+', '', title, flags=re.IGNORECASE).strip()
        if not title:
            return "Create Linear task"
        return title[:1].upper() + title[1:]


    async def _extract_linear_meeting_candidates_from_sources(
        self,
        *,
        sources: list[ParsedSource],
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        settings = get_settings()
        extraction_concurrency = max(
            1,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_CONCURRENCY",
                        3,
                    )
                ),
                6,
            ),
        )
        extraction_timeout_seconds = max(
            120.0,
            float(
                getattr(
                    settings,
                    "LINEAR_MEETING_EXTRACTION_TOTAL_TIMEOUT_SECONDS",
                    360.0,
                )
            ),
        )
        chunk_max_chars = max(
            2_000,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_CHUNK_MAX_CHARS",
                        8_000,
                    )
                ),
                12_000,
            ),
        )
        recovery_max_chars = max(
            1_000,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_RECOVERY_MAX_CHARS",
                        4_000,
                    )
                ),
                chunk_max_chars - 1,
            ),
        )
        recovery_depth_limit = max(
            0,
            min(
                int(
                    getattr(
                        settings,
                        "LINEAR_MEETING_EXTRACTION_RECOVERY_DEPTH",
                        2,
                    )
                ),
                3,
            ),
        )
        source_chunks = [
            (source, chunk)
            for source in sources
            for chunk in source_text_chunks(source, max_chars=chunk_max_chars)
        ]
        if not source_chunks:
            return []

        semaphore = asyncio.Semaphore(extraction_concurrency)
        batch_chunk_count = len(source_chunks)

        async def extract_chunk_with_recovery(
            source: ParsedSource,
            chunk: str,
            *,
            batch_chunk_index: int,
            recovery_depth: int = 0,
        ) -> list[dict[str, Any]]:
            try:
                return await self._extract_linear_meeting_candidates(
                    transcript=chunk,
                    params=params,
                    users=users,
                    projects=projects,
                    source_label=source.label,
                    # This model request contains one source chunk. The total
                    # batch size is logged separately and must not inflate
                    # reasoning effort for every individual request.
                    source_count=1,
                    batch_chunk_index=batch_chunk_index,
                    batch_chunk_count=batch_chunk_count,
                    recovery_depth=recovery_depth,
                )
            except LinearInferenceTimeoutError as exc:
                if recovery_depth >= recovery_depth_limit:
                    raise
                recovery_chunks = self._linear_meeting_timeout_recovery_chunks(
                    source=source,
                    chunk=chunk,
                    recovery_depth=recovery_depth,
                    recovery_max_chars=recovery_max_chars,
                )
                print(
                    "LINEAR_MEETING_EXTRACTION "
                    + json.dumps(
                        {
                            "event": "chunk_timeout_recovery",
                            "batch_chunk_index": batch_chunk_index,
                            "batch_chunk_count": batch_chunk_count,
                            "recovery_depth": recovery_depth + 1,
                            "retry_chunk_count": len(recovery_chunks),
                            "source_chars": len(chunk),
                            "reasoning_effort": exc.decision.effort,
                            "timeout_seconds": exc.decision.timeout_seconds,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                recovered_candidates: list[dict[str, Any]] = []
                # Keep the worker slot until its failed chunk has recovered. This
                # prevents a retry from sitting behind the entire initial queue and
                # makes the total deadline predictable for long PDFs.
                for recovery_chunk in recovery_chunks:
                    recovered_candidates.extend(
                        await extract_chunk_with_recovery(
                            source,
                            recovery_chunk,
                            batch_chunk_index=batch_chunk_index,
                            recovery_depth=recovery_depth + 1,
                        )
                    )
                return self._dedupe_linear_meeting_candidates(recovered_candidates)

        async def extract_chunk(
            source: ParsedSource,
            chunk: str,
            *,
            batch_chunk_index: int,
        ) -> list[dict[str, Any]]:
            async with semaphore:
                return await extract_chunk_with_recovery(
                    source,
                    chunk,
                    batch_chunk_index=batch_chunk_index,
                )

        async def extract_all_chunks() -> list[list[dict[str, Any]]]:
            tasks = [
                asyncio.create_task(
                    extract_chunk(
                        source,
                        chunk,
                        batch_chunk_index=index,
                    )
                )
                for index, (source, chunk) in enumerate(source_chunks, start=1)
            ]
            try:
                return await asyncio.gather(*tasks)
            except BaseException:
                # asyncio.gather does not cancel siblings when one raises. Stop
                # queued/in-flight calls so a terminal chunk failure fails fast and
                # no model work outlives the fail-closed extraction request.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        try:
            extracted_batches = await asyncio.wait_for(
                extract_all_chunks(),
                timeout=extraction_timeout_seconds,
            )
        except LinearInferenceTimeoutError:
            raise
        except asyncio.TimeoutError as exc:
            raise LinearMeetingExtractionDeadlineError(
                "Meeting-action extraction exceeded its "
                f"{extraction_timeout_seconds:g} second runtime budget."
            ) from exc

        candidates: list[dict[str, Any]] = []
        for (source, _), extracted in zip(source_chunks, extracted_batches):
            for item in extracted:
                item.setdefault("source_label", source.label)
                candidates.append(item)
        return self._dedupe_linear_meeting_candidates(candidates)


    @staticmethod
    def _linear_meeting_timeout_recovery_chunks(
        *,
        source: ParsedSource,
        chunk: str,
        recovery_depth: int,
        recovery_max_chars: int,
    ) -> list[str]:
        source_header = f"Source: {source.label}\n"
        source_text = (
            chunk[len(source_header):]
            if chunk.startswith(source_header)
            else chunk
        )
        recovery_source = ParsedSource(
            label=source.label,
            text=source_text,
            kind=source.kind,
            metadata=source.metadata,
        )
        return source_text_chunks(
            recovery_source,
            max_chars=max(1_000, recovery_max_chars // (2**recovery_depth)),
            hard_split_overlap_chars=min(
                LINEAR_MEETING_TIMEOUT_RECOVERY_OVERLAP_CHARS,
                max(1_000, recovery_max_chars // (2**recovery_depth)) - 1,
            ),
        ) or [chunk]


    async def _extract_linear_meeting_candidates(
        self,
        *,
        transcript: str,
        params: dict,
        users: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        source_label: Optional[str] = None,
        source_count: int = 1,
        batch_chunk_index: int = 1,
        batch_chunk_count: int = 1,
        recovery_depth: int = 0,
    ) -> list[dict[str, Any]]:
        from ...utils import get_current_date

        project_names = ", ".join(
            str(project.get("name") or project.get("slugId") or "")
            for project in projects[:40]
            if project.get("name") or project.get("slugId")
        )
        user_names = ", ".join(
            str(user.get("displayName") or user.get("name") or user.get("email") or "")
            for user in users[:80]
            if user.get("displayName") or user.get("name") or user.get("email")
        )
        source_local_datetime = str(params.get("source_local_datetime") or "").strip()
        source_date = source_local_datetime[:10] if source_local_datetime else get_current_date().isoformat()
        prompt = f"""Extract concrete action items from the Slack conversation or meeting notes.

Source-local date: {source_date}
Source-local datetime: {source_local_datetime or "unknown"}
Source timezone: {params.get("source_timezone") or "unknown"}
Source label: {source_label or "Slack thread"}
Project hint: {params.get("project_hint") or "none"}
Team hint: {params.get("team_hint") or "none"}
Default assignee if the owner is unclear: {params.get("default_assignee_hint") or "none"}
Requester: {params.get("requester_display_name") or "unknown"} (<@{params.get("requester_slack_id") or "unknown"}>, {params.get("requester_email") or "email unavailable"})

Known Linear projects: {project_names or "none loaded"}
Known Linear users: {user_names or "none loaded"}

Meeting notes:
{transcript[:12000]}

Only include actionable work someone explicitly agreed, promised, or was asked to do. Preserve completed/cancelled/duplicate status when the source says the work is terminal so Roo can suppress creation. Separate completed work from the work remaining. Set explicit_commitment=false when you had to reframe a discussion into possible work. Do not include decisions, FYIs, vague follow-ups, or the user's instruction to create Linear tasks/project updates.
Resolve relative dates such as EOW from the source-local date above. In this Australian workspace, numeric dates are day/month unless the source makes another format explicit.
If an action item has no clear owner and a default assignee is given above, set its owner_hint to that default assignee.
Return the structured action_items list."""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract structured meeting action items. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearMeetingActionBatch,
                signals=LinearReasoningSignals(
                    stage="meeting_actions",
                    source_chars=len(transcript),
                    source_count=max(1, source_count),
                    batch_chunk_index=max(1, batch_chunk_index),
                    batch_chunk_count=max(1, batch_chunk_count),
                    recovery_depth=max(0, recovery_depth),
                    explicit_project=bool(params.get("project_hint")),
                    explicit_owner=bool(
                        params.get("owner_hint")
                        or params.get("default_assignee_hint")
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
        except ValueError:
            return []
        return [
            item.model_dump(mode="json")
            for item in inference.value.action_items
        ]


    def _dedupe_linear_meeting_candidates(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            title = self._normalize_match_text(candidate.get("title") or candidate.get("task"))
            owner = self._normalize_match_text(candidate.get("owner_hint") or candidate.get("owner"))
            if not title or self._is_linear_meeting_meta_candidate(candidate):
                continue
            key = self._find_similar_linear_meeting_candidate_key(deduped, candidate) or f"{title}:{owner}"
            if key not in deduped:
                deduped[key] = dict(candidate)
                continue

            self._merge_linear_meeting_candidate(deduped[key], candidate)
        return list(deduped.values())


    def _find_similar_linear_meeting_candidate_key(
        self,
        deduped: dict[str, dict[str, Any]],
        candidate: dict[str, Any],
    ) -> Optional[str]:
        title = str(candidate.get("title") or candidate.get("task") or "")
        normalized_title = self._normalize_match_text(title)
        owner = self._normalize_match_text(candidate.get("owner_hint") or candidate.get("owner"))
        candidate_tokens = self._linear_meeting_candidate_tokens(title)
        for key, existing in deduped.items():
            existing_title = str(existing.get("title") or existing.get("task") or "")
            normalized_existing_title = self._normalize_match_text(existing_title)
            if normalized_title == normalized_existing_title:
                return key

            existing_owner = self._normalize_match_text(existing.get("owner_hint") or existing.get("owner"))
            existing_tokens = self._linear_meeting_candidate_tokens(existing_title)
            if not candidate_tokens or not existing_tokens:
                continue

            overlap = len(candidate_tokens & existing_tokens) / min(len(candidate_tokens), len(existing_tokens))
            similarity = SequenceMatcher(None, normalized_title, normalized_existing_title).ratio()
            owners_match = not owner or not existing_owner or owner == existing_owner
            if overlap >= 0.86 or (owners_match and (overlap >= 0.74 or (overlap >= 0.68 and similarity >= 0.68))):
                return key
            if owners_match and self._linear_meeting_candidates_share_outcome(
                candidate,
                existing,
                candidate_tokens=candidate_tokens,
                existing_tokens=existing_tokens,
            ):
                return key
        return None


    def _linear_meeting_candidates_share_outcome(
        self,
        candidate: dict[str, Any],
        existing: dict[str, Any],
        *,
        candidate_tokens: set[str],
        existing_tokens: set[str],
    ) -> bool:
        """Merge chunk-level paraphrases that describe the same deliverable.

        The family check prevents account setup from being merged into an Apollo
        comparison merely because both titles mention the same providers.
        """

        candidate_family = self._linear_meeting_action_family(
            candidate.get("title") or candidate.get("task")
        )
        existing_family = self._linear_meeting_action_family(
            existing.get("title") or existing.get("task")
        )
        if not candidate_family or candidate_family != existing_family:
            return False
        candidate_project = self._normalize_match_text(
            candidate.get("project_hint") or candidate.get("project")
        )
        existing_project = self._normalize_match_text(
            existing.get("project_hint") or existing.get("project")
        )
        if candidate_project and existing_project and candidate_project != existing_project:
            return False
        shared = candidate_tokens & existing_tokens
        if len(shared) < 2:
            return False
        coverage = len(shared) / min(len(candidate_tokens), len(existing_tokens))
        return coverage >= 0.4


    @staticmethod
    def _linear_meeting_action_family(value: Any) -> str:
        text = str(value or "").lower()
        families = (
            ("evaluation", r"\b(?:compare|evaluate|assess|test|benchmark|validate)\b"),
            ("implementation", r"\b(?:build|implement|develop|integrate|prototype)\b"),
            ("documentation", r"\b(?:document|write|draft|specify)\b|\brequirements?\b"),
            ("account_setup", r"\b(?:account|register|registration|sign\s*up|access)\b"),
            ("outreach", r"\b(?:recruit|secure|contact|outreach|invite)\b"),
            ("scheduling", r"\b(?:schedule|coordinate|book|calendar)\b"),
        )
        for family, pattern in families:
            if re.search(pattern, text):
                return family
        return ""


    def _merge_linear_meeting_candidate(self, existing: dict[str, Any], candidate: dict[str, Any]) -> None:
        try:
            existing_confidence = float(existing.get("confidence", 0.0))
            candidate_confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            existing_confidence = candidate_confidence = 0.0
        if candidate_confidence > existing_confidence:
            existing["confidence"] = candidate.get("confidence")
            for field in (
                "title",
                "description",
                "work_status",
                "completed_work",
                "remaining_work",
                "available_artifacts",
                "dependencies",
                "acceptance_criteria",
                "owner_hint",
                "project_hint",
                "team_hint",
                "due_date",
                "priority",
            ):
                if candidate.get(field):
                    existing[field] = candidate.get(field)

        labels = {
            str(existing.get("source_label") or "").strip(),
            str(candidate.get("source_label") or "").strip(),
        }
        labels.discard("")
        if labels:
            existing["source_label"] = ", ".join(sorted(labels))

        existing_evidence = str(existing.get("evidence") or "").strip()
        candidate_evidence = str(candidate.get("evidence") or "").strip()
        if candidate_evidence and candidate_evidence not in existing_evidence:
            existing["evidence"] = (
                f"{existing_evidence}\n{candidate_evidence}".strip()
                if existing_evidence
                else candidate_evidence
            )


    def _is_linear_meeting_meta_candidate(self, candidate: dict[str, Any]) -> bool:
        title = self._normalize_match_text(candidate.get("title") or candidate.get("task"))
        description = self._normalize_match_text(candidate.get("description") or "")
        combined = f"{title} {description}"
        if "linear" in combined and (
            "projectupdate" in combined
            or "createlineartask" in combined
            or "extract" in combined
            or "todo" in combined
        ):
            return True
        return title in {"postprojectupdate", "extractmeetingtodos", "createlineartasks"}


    def _linear_meeting_candidate_tokens(self, text: str) -> set[str]:
        replacements = {
            "builders": "talent",
            "builder": "talent",
            "people": "talent",
            "professionals": "talent",
            "technical": "tech",
            "pool": "supply",
            "pools": "supply",
            "model": "service",
        }
        stop_words = {
            "a", "an", "and", "as", "by", "for", "from", "in", "into", "of", "on",
            "or", "the", "to", "with", "will", "meeting", "linear", "task", "tasks",
            "action", "actions", "item", "items", "create", "build", "start", "run",
            "make", "prepare", "draft", "update", "rework", "revise", "add", "share",
            "recruit", "building", "publish", "initial", "vetted",
        }
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
            token = replacements.get(token, token)
            token = self._linear_meeting_singular_token(token)
            if len(token) < 3 or token in stop_words:
                continue
            tokens.add(token)
        return tokens


    def _linear_meeting_project_tokens(self, text: str) -> set[str]:
        stop_words = {
            "a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "the",
            "to", "with", "project", "update", "meeting", "notes", "note", "call",
            "pdf", "docx", "slack", "linear",
        }
        tokens: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
            token = self._linear_meeting_singular_token(token)
            if len(token) < 3 or token in stop_words:
                continue
            tokens.add(token)
        return tokens


    @staticmethod
    def _linear_meeting_singular_token(token: str) -> str:
        if len(token) > 4 and token.endswith("ies"):
            return f"{token[:-3]}y"
        if len(token) > 4 and token.endswith("s"):
            return token[:-1]
        return token


    def _linear_meeting_project_update_requested(self, text: str, params: dict[str, Any]) -> bool:
        action = self._normalize_match_text(params.get("action"))
        if action in {"projectupdate", "updateproject", "projectstatus"}:
            return True
        request_text = re.sub(
            r"\s+",
            " ",
            str(text or "").lower().replace("’", "'"),
        ).strip()
        negative_patterns = (
            (
                r"\b(?:do\s+not|don't|dont|never)\s+"
                r"(?:write|create|add|post|make|include|generate|send)\s+"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
            (
                r"\b(?:no|not|without)\s+"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
            (
                r"\b(?:skip|omit|avoid)\s+"
                r"(?:(?:writing|creating|adding|posting|making|including|"
                r"generating|sending)\s+)?"
                r"(?:(?:a|the|any)\s+)?project\s+updates?\b"
            ),
        )
        if any(re.search(pattern, request_text) for pattern in negative_patterns):
            return False
        normalized_text = self._normalize_match_text(text)
        if "projectupdate" not in normalized_text:
            return False
        return any(
            token in normalized_text
            for token in (
                "linear",
                "meeting",
                "transcript",
                "summary",
                "notes",
                "file",
                "pdf",
                "docx",
                "document",
                "image",
            )
        )


    def _linear_meeting_explicit_creation_authorized(
        self,
        text: str,
        params: dict[str, Any],
    ) -> bool:
        """Return true when the requester clearly authorised task creation.

        This is intentionally narrower than merely mentioning Linear or asking Roo
        to inspect a transcript. Preview/extract-only language never grants writes.
        """

        action = self._normalize_match_text(params.get("action"))
        if action in {"extract", "preview", "review", "draft"}:
            return False
        normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        if re.search(
            r"\b(?:only\s+)?(?:extract|preview|review|draft|show|list)\b[^.!?]{0,80}"
            r"\b(?:tasks?|issues?|tickets?|to-?dos?|action\s+items?)\b",
            normalized,
        ) and not re.search(
            r"\b(?:create|add|write|send|sync|put|file|open)\b[^.!?]{0,100}"
            r"\b(?:tasks?|issues?|tickets?|to-?dos?|action\s+items?)\b",
            normalized,
        ):
            return False
        if action in {"create", "write", "sync", "add"}:
            return True
        has_write_verb = bool(
            re.search(r"\b(?:create|add|write|send|sync|put|file|open)\b", normalized)
        )
        has_task_noun = bool(
            re.search(r"\b(?:tasks?|issues?|tickets?|to-?dos?|todos?|action\s+items?)\b", normalized)
        )
        return has_write_verb and has_task_noun and "linear" in normalized


    def _resolve_linear_meeting_request_project(
        self,
        *,
        sources: list[ParsedSource],
        candidates: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        explicit_project_hint: Optional[str],
        channel_id: Optional[str],
        channel_context: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve one canonical destination shared by updates and task creation."""

        if explicit_project_hint:
            return self._match_linear_meeting_project(
                {"project_hint": explicit_project_hint},
                projects,
                owner_user=None,
                explicit_project_hint=explicit_project_hint,
                channel_id=channel_id,
                channel_context=channel_context,
            )

        source_match = self._match_linear_meeting_project_from_sources(
            sources,
            projects,
            None,
        )
        source_project = source_match.get("project") or {}
        if source_project and float(source_match.get("confidence") or 0.0) >= 0.86:
            return source_match

        matches_by_project: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            hint = str(candidate.get("project_hint") or candidate.get("project") or "").strip()
            if not hint:
                continue
            match = self._match_linear_meeting_project(
                candidate,
                projects,
                owner_user=None,
                explicit_project_hint=hint,
            )
            project = match.get("project") or {}
            project_id = str(project.get("id") or "").strip()
            if project_id and float(match.get("confidence") or 0.0) >= 0.78:
                matches_by_project.setdefault(project_id, []).append(match)

        if matches_by_project:
            ranked = sorted(
                matches_by_project.values(),
                key=lambda values: (len(values), max(float(value.get("confidence") or 0.0) for value in values)),
                reverse=True,
            )
            best = ranked[0]
            second_count = len(ranked[1]) if len(ranked) > 1 else 0
            if len(best) > second_count and len(best) >= max(1, len(candidates) // 2):
                strongest = max(best, key=lambda value: float(value.get("confidence") or 0.0))
                return {
                    **strongest,
                    "confidence": min(
                        0.96,
                        max(float(strongest.get("confidence") or 0.0), 0.9),
                    ),
                    "reason": "Matched project by action-item consensus",
                }

        if source_project:
            return source_match
        return self._match_linear_meeting_project(
            {},
            projects,
            owner_user=None,
            channel_id=channel_id,
            channel_context=channel_context,
        )


    def _match_linear_meeting_project_from_sources(
        self,
        sources: list[ParsedSource],
        projects: list[dict[str, Any]],
        explicit_project_hint: Optional[str],
    ) -> dict[str, Any]:
        if explicit_project_hint:
            return self._match_linear_meeting_project(
                {"project_hint": explicit_project_hint},
                projects,
                owner_user=None,
                explicit_project_hint=explicit_project_hint,
            )

        source_label_text = " ".join(source.label for source in sources if source.label)
        source_text = " ".join(
            f"{source.label} {source.text[:4000]}"
            for source in sources
            if source.text or source.label
        )
        normalized_source = self._normalize_match_text(source_text)
        source_tokens = self._linear_meeting_project_tokens(source_text)
        source_label_tokens = self._linear_meeting_project_tokens(source_label_text)
        best_project = None
        best_score = 0.0
        for project in projects:
            project_name = str(project.get("name") or "")
            core_project_name = self._linear_project_core_name(project_name)
            project_text = " ".join(
                str(value or "")
                for value in (core_project_name, project.get("slugId"))
                if value
            )
            project_tokens = self._linear_meeting_project_tokens(project_text)
            if project_tokens:
                label_overlap = len(project_tokens & source_label_tokens) / len(project_tokens)
                source_overlap = len(project_tokens & source_tokens) / len(project_tokens)
                token_score = max(label_overlap, source_overlap)
                if token_score >= 0.67:
                    score = 0.78 + (0.14 * token_score)
                    if label_overlap >= 0.67:
                        score += 0.04
                    if score > best_score:
                        best_project = project
                        best_score = min(score, 0.94)

            for value in (core_project_name, project.get("name"), project.get("slugId")):
                normalized_project = self._normalize_match_text(value)
                if not normalized_project:
                    continue
                if normalized_project in normalized_source:
                    return {
                        "project": project,
                        "confidence": 0.9,
                        "reason": "Matched project from meeting source",
                    }
                score = SequenceMatcher(None, normalized_project, normalized_source[: max(len(normalized_project) * 3, 80)]).ratio()
                if score > best_score:
                    best_project = project
                    best_score = score
        if best_project and best_score >= 0.65:
            return {
                "project": best_project,
                "confidence": min(best_score, 0.94),
                "reason": "Matched project by source similarity",
            }
        return {"project": None, "confidence": 0.0, "reason": "No project match"}


    @staticmethod
    def _linear_project_core_name(value: Any) -> str:
        """Remove organisational namespace prefixes such as ``[Studio]``."""

        return re.sub(r"^(?:\s*\[[^\]]+\]\s*)+", "", str(value or "")).strip()


    async def _build_linear_meeting_project_update_input(
        self,
        *,
        sources: list[ParsedSource],
        params: dict[str, Any],
        project: dict[str, Any],
        candidates: list[dict[str, Any]],
        recent_issues: Optional[list[dict[str, Any]]] = None,
        settings: Any,
    ) -> dict[str, Any]:
        project_name = str(project.get("name") or "the project")
        source_summary = await self._summarize_linear_project_update_sources(
            sources=sources,
            settings=settings,
            safety_identifier=linear_safety_identifier(
                params.get("requester_slack_id")
            ),
        )
        previous_update = self._format_linear_project_previous_update(project.get("lastUpdate"))
        raw_last_update = project.get("lastUpdate")
        last_update_health = (
            str(raw_last_update.get("health") or "")
            if isinstance(raw_last_update, dict)
            else ""
        )
        project_issue_context = self._format_linear_project_recent_issues(
            project,
            recent_issues or [],
        )
        candidate_lines = "\n".join(
            f"- {candidate.get('title') or candidate.get('task')} ({candidate.get('owner_hint') or 'owner unclear'})"
            for candidate in candidates[:15]
            if candidate.get("title") or candidate.get("task")
        )
        prompt = f"""Write a concise Linear project update for {project_name}.

Use the latest Linear project update as the baseline, and the meeting notes as the primary source for what changed since then.

Use this structure in the body:
- Summary
- Work done since last update
- Decisions made
- Risks / open questions, if any
- Next steps

Keep the update concise. Include only work done, decisions, risks, and next steps supported by the notes or Linear context. Do not invent dates, completion, blockers, or commitments.
End the body with exactly: _Generated by Roo from Slack meeting notes._

Latest Linear project update:
{previous_update or "No previous project update available."}

Relevant recent Linear issues:
{project_issue_context or "No recent project issues available."}

Meeting note summaries:
{source_summary or "No meeting notes available."}

Extracted action candidates:
{candidate_lines or "none"}"""
        try:
            inference = await run_linear_structured_inference(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write concise Linear project updates from meeting notes. "
                            "Return only the structured result."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format=LinearProjectUpdateResult,
                signals=LinearReasoningSignals(
                    stage="project_update_compose",
                    source_chars=sum(
                        len(value)
                        for value in (
                            source_summary,
                            previous_update,
                            project_issue_context,
                            candidate_lines,
                        )
                    ),
                    source_count=max(1, len(sources)),
                    candidate_count=max(1, len(candidates)),
                    explicit_project=True,
                    explicit_owner=all(
                        bool(candidate.get("owner_hint"))
                        for candidate in candidates
                    ),
                    ambiguity=any(
                        not candidate.get("owner_hint")
                        for candidate in candidates
                    ),
                    conflicting_context=bool(
                        last_update_health == "onTrack"
                        and re.search(
                            r"\b(?:at risk|off track|blocked|delayed|delay|risk)\b",
                            source_summary,
                            flags=re.IGNORECASE,
                        )
                    ),
                ),
                safety_identifier=linear_safety_identifier(
                    params.get("requester_slack_id")
                ),
            )
            body = inference.value.body.strip()
            health = inference.value.health
        except Exception:
            body = ""
            health = "onTrack"

        if not body:
            body = self._fallback_linear_meeting_project_update_body(project_name, candidates)
        body = self._ensure_linear_project_update_footer(body)
        return {
            "project_id": str(project.get("id") or ""),
            "body": body,
            "health": health,
        }


    async def _summarize_linear_project_update_sources(
        self,
        *,
        sources: list[ParsedSource],
        settings: Any,
        safety_identifier: Optional[str] = None,
    ) -> str:
        chunks: list[tuple[str, str]] = []
        for source in sources:
            for chunk in source_text_chunks(source, max_chars=8000):
                chunks.append((source.label, chunk))

        if not chunks:
            return ""

        summaries: list[str] = []
        for index, (label, chunk) in enumerate(chunks, start=1):
            prompt = f"""Summarize this meeting-notes chunk for a Linear project update.

Populate the structured work_done, decisions, risks_open_questions, and next_steps lists.

Only include facts supported by the chunk. Omit empty headings.

Chunk {index} source: {label}
{chunk[:8000]}"""
            try:
                inference = await run_linear_structured_inference(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Summarize meeting notes for concise project updates. "
                                "Return only the structured result."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    response_format=LinearProjectSourceSummary,
                    signals=LinearReasoningSignals(
                        stage="project_update_summary",
                        source_chars=len(chunk),
                        source_count=max(1, len(chunks)),
                        explicit_project=True,
                        explicit_owner=True,
                    ),
                    safety_identifier=safety_identifier,
                )
                summary = inference.value.to_markdown().strip()
            except Exception:
                summary = ""
            if summary:
                summaries.append(f"### Source chunk {index}: {label}\n{summary[:3000]}")
        return "\n\n".join(summaries)


    def _format_linear_project_previous_update(self, last_update: Any) -> str:
        if not isinstance(last_update, dict) or not last_update:
            return ""
        author = last_update.get("user") or {}
        author_label = (
            author.get("displayName")
            or author.get("name")
            or author.get("email")
            or "unknown"
        )
        parts = [
            f"- Created: {last_update.get('createdAt') or 'unknown'}",
            f"- Health: {last_update.get('health') or 'unknown'}",
            f"- Author: {author_label}",
        ]
        if last_update.get("url"):
            parts.append(f"- URL: {last_update['url']}")
        body = str(last_update.get("body") or "").strip()
        if body:
            parts.extend(["", body[:4000]])
        return "\n".join(parts).strip()


    def _format_linear_project_recent_issues(
        self,
        project: dict[str, Any],
        recent_issues: list[dict[str, Any]],
    ) -> str:
        project_id = str(project.get("id") or "")
        if not project_id:
            return ""
        lines: list[str] = []
        for issue in recent_issues:
            issue_project_id = str(((issue.get("project") or {}).get("id")) or "")
            if issue_project_id != project_id:
                continue
            assignee = issue.get("assignee") or {}
            assignee_label = (
                assignee.get("displayName")
                or assignee.get("name")
                or assignee.get("email")
                or "unassigned"
            )
            state = (issue.get("state") or {}).get("name") or "unknown"
            identifier = issue.get("identifier") or issue.get("id") or "issue"
            lines.append(f"- {identifier}: {issue.get('title') or 'Untitled'} [{state}, {assignee_label}]")
            if len(lines) >= 20:
                break
        return "\n".join(lines)


    @staticmethod
    def _ensure_linear_project_update_footer(body: str) -> str:
        footer = "_Generated by Roo from Slack meeting notes._"
        cleaned = str(body or "").strip()
        if footer.lower() in cleaned.lower():
            return cleaned
        return f"{cleaned}\n\n{footer}".strip()


    def _fallback_linear_meeting_project_update_body(
        self,
        project_name: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        lines = [
            f"## {project_name} meeting update",
            "",
            "Roo generated this update from meeting notes.",
            "",
            "### Action items",
        ]
        action_lines = [
            f"- {candidate.get('title') or candidate.get('task')}"
            for candidate in candidates[:12]
            if candidate.get("title") or candidate.get("task")
        ]
        lines.extend(action_lines or ["- No concrete action items were extracted."])
        return "\n".join(lines).strip()


    def _normalize_linear_meeting_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        title = str(candidate.get("title") or candidate.get("task") or "").strip()
        description = str(candidate.get("description") or candidate.get("details") or "").strip()
        work_status = str(
            candidate.get("work_status") or candidate.get("status") or "open"
        ).strip().lower()
        completed_work = str(candidate.get("completed_work") or "").strip()
        remaining_work = str(
            candidate.get("remaining_work") or description or title
        ).strip()

        def normalized_string_list(value: Any) -> list[str]:
            if isinstance(value, str):
                values = re.split(r"[\n,;]+", value)
            elif isinstance(value, (list, tuple, set)):
                values = list(value)
            else:
                values = []
            return [
                str(item).strip()[:500]
                for item in values
                if str(item).strip()
            ][:20]

        available_artifacts = normalized_string_list(
            candidate.get("available_artifacts") or candidate.get("artifacts")
        )
        dependencies = normalized_string_list(candidate.get("dependencies"))
        acceptance_criteria = normalized_string_list(
            candidate.get("acceptance_criteria")
        )
        owner_hint = str(candidate.get("owner_hint") or candidate.get("owner") or "").strip()
        project_hint = str(candidate.get("project_hint") or candidate.get("project") or "").strip()
        team_hint = str(candidate.get("team_hint") or candidate.get("team") or "").strip()
        due_expression = str(candidate.get("due_expression") or candidate.get("due") or "").strip()
        due_date = candidate.get("due_date") or candidate.get("dueDate")
        due_date = str(due_date).strip() if due_date else None
        if due_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
            due_date = None
        try:
            priority = int(candidate.get("priority", 3))
        except (TypeError, ValueError):
            priority = 3
        priority = min(max(priority, 0), 4)
        try:
            confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        evidence = str(candidate.get("evidence") or candidate.get("quote") or "").strip()
        evidence_message_ts = str(
            candidate.get("evidence_message_ts") or candidate.get("evidenceMessageTs") or ""
        ).strip()
        explicit_commitment_value = candidate.get("explicit_commitment", False)
        if isinstance(explicit_commitment_value, str):
            explicit_commitment = explicit_commitment_value.strip().lower() in {
                "1",
                "true",
                "yes",
            }
        else:
            explicit_commitment = bool(explicit_commitment_value)
        source_label = str(candidate.get("source_label") or candidate.get("source") or "").strip()
        return {
            "title": title[:180],
            "description": description,
            "work_status": work_status[:40],
            "completed_work": completed_work[:3000],
            "remaining_work": remaining_work[:3000],
            "available_artifacts": available_artifacts,
            "dependencies": dependencies,
            "acceptance_criteria": acceptance_criteria,
            "owner_hint": owner_hint,
            "project_hint": project_hint,
            "team_hint": team_hint,
            "due_expression": due_expression[:120],
            "due_date": due_date,
            "priority": priority,
            "evidence": evidence[:700],
            "evidence_message_ts": evidence_message_ts[:40],
            "explicit_commitment": explicit_commitment,
            "source_label": source_label[:300],
            "confidence": confidence,
        }


    def _normalize_linear_meeting_due_date(
        self,
        candidate: dict[str, Any],
        slack_context: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve common relative due expressions from the evidence timestamp."""
        expression = str(candidate.get("due_expression") or "").strip()
        evidence = str(candidate.get("evidence") or "")
        if not expression:
            relative_match = re.search(
                r"\b(EOW|end of (?:the )?week|today|tomorrow)\b",
                evidence,
                flags=re.IGNORECASE,
            )
            expression = relative_match.group(1) if relative_match else ""
        if not expression:
            return candidate

        normalized_expression = expression.lower().strip()
        supported_relative_expressions = {
            "eow",
            "end of week",
            "end of the week",
            "today",
            "tomorrow",
        }
        if normalized_expression not in supported_relative_expressions:
            return candidate

        evidence_message = self._resolve_linear_evidence_message(candidate, slack_context)
        local_value = str((evidence_message or {}).get("local_datetime") or "")
        if not local_value:
            local_value = str(((slack_context or {}).get("request") or {}).get("local_datetime") or "")
        try:
            base_date = datetime.fromisoformat(local_value).date()
        except (TypeError, ValueError):
            return candidate

        due: Optional[date] = None
        if normalized_expression in {"eow", "end of week", "end of the week"}:
            days_until_friday = 4 - base_date.weekday()
            if days_until_friday < 0:
                days_until_friday += 7
            due = base_date + timedelta(days=days_until_friday)
        elif normalized_expression == "today":
            due = base_date
        elif normalized_expression == "tomorrow":
            due = base_date + timedelta(days=1)

        if due:
            return {**candidate, "due_expression": expression, "due_date": due.isoformat()}
        return candidate


    def _resolve_linear_evidence_message(
        self,
        candidate: dict[str, Any],
        slack_context: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        messages = [
            message
            for message in ((slack_context or {}).get("messages") or [])
            if isinstance(message, dict) and not message.get("is_bot")
        ]
        if not messages:
            return None

        evidence_ts = str(candidate.get("evidence_message_ts") or "").strip()
        if evidence_ts:
            for message in messages:
                if str(message.get("ts") or "") == evidence_ts:
                    return message

        evidence = self._normalize_linear_evidence_text(candidate.get("evidence"))
        if evidence:
            for message in reversed(messages):
                message_text = self._normalize_linear_evidence_text(message.get("text"))
                if evidence in message_text or message_text in evidence:
                    return message

        request_ts = str(((slack_context or {}).get("request") or {}).get("message_ts") or "")
        prior_messages = [
            message for message in messages if str(message.get("ts") or "") != request_ts
        ]
        return prior_messages[-1] if prior_messages else messages[-1]


    @staticmethod
    def _normalize_linear_evidence_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip(" .,:;!?\"'“”")


    @staticmethod
    def _linear_source_permalink(
        channel_id: Optional[str],
        message_ts: str,
    ) -> Optional[str]:
        if not channel_id or not message_ts:
            return None
        try:
            from ...slack_client import get_message_permalink

            return get_message_permalink(channel_id, message_ts)
        except Exception:
            return None


    def _match_linear_meeting_owner(
        self,
        owner_hint: Optional[str],
        users: list[dict[str, Any]],
    ) -> dict[str, Any]:
        hint = str(owner_hint or "").strip()
        if not hint:
            return {"user": None, "confidence": 0.0, "reason": "No owner hint"}

        emails = set(re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", hint.lower()))
        mention_match = re.search(r"<@([A-Z0-9]+)>", hint)
        if mention_match:
            try:
                from ...slack_client import get_user_info

                slack_info = get_user_info(mention_match.group(1))
                slack_email = str(slack_info.get("email") or "").strip().lower()
                if slack_email:
                    emails.add(slack_email)
            except Exception:
                pass

        for user in users:
            email = str(user.get("email") or "").strip().lower()
            if email and email in emails:
                return {"user": user, "confidence": 0.98, "reason": "Matched owner by email"}

        normalized_hint = self._normalize_match_text(hint)
        plain_matches = self._find_unique_linear_user_name_matches(hint, users)
        if len(plain_matches) == 1:
            return {
                "user": plain_matches[0],
                "confidence": 0.94,
                "reason": "Matched owner by unique name",
            }
        if len(plain_matches) > 1:
            return {"user": None, "confidence": 0.0, "reason": "Ambiguous owner hint"}

        best_user = None
        best_score = 0.0
        best_reason = "No user match"
        for user in users:
            names = [
                user.get("displayName"),
                user.get("name"),
                user.get("email"),
            ]
            for name in names:
                normalized_name = self._normalize_match_text(name)
                if not normalized_name:
                    continue
                if normalized_hint == normalized_name:
                    return {"user": user, "confidence": 0.92, "reason": "Matched owner by name"}
                if normalized_name in normalized_hint or normalized_hint in normalized_name:
                    score = 0.86
                else:
                    score = SequenceMatcher(None, normalized_hint, normalized_name).ratio()
                if score > best_score:
                    best_user = user
                    best_score = score
                    best_reason = "Matched owner by name similarity"

        if best_user and best_score >= 0.82:
            return {"user": best_user, "confidence": min(best_score, 0.84), "reason": best_reason}
        return {"user": None, "confidence": 0.0, "reason": "No user match"}


    def _find_unique_linear_user_name_matches(
        self,
        owner_hint: str,
        users: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        hint_tokens = self._linear_person_name_tokens(owner_hint)
        if not hint_tokens:
            return []
        matches: list[dict[str, Any]] = []
        for user in users:
            user_tokens = self._linear_user_name_tokens(user)
            if not user_tokens:
                continue
            if hint_tokens & user_tokens:
                matches.append(user)
        return self._dedupe_linear_users_by_id(matches)


    def _linear_user_name_tokens(self, user: dict[str, Any]) -> set[str]:
        values = [user.get("displayName"), user.get("name"), user.get("email")]
        tokens: set[str] = set()
        for value in values:
            tokens.update(self._linear_person_name_tokens(str(value or "").split("@", 1)[0]))
        return tokens


    @staticmethod
    def _linear_person_name_tokens(value: str) -> set[str]:
        stop_words = {"assign", "assigned", "owner", "assignee", "to", "for", "it", "this", "task", "issue"}
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 3 and token not in stop_words
        }
        return tokens


    @staticmethod
    def _dedupe_linear_users_by_id(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for user in users:
            key = str(user.get("id") or user.get("email") or user.get("name") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(user)
        return deduped


    async def _resolve_explicit_linear_project_context(
        self,
        *,
        client: Any,
        params: dict[str, Any],
        projects: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        hint = str(params.get("project_hint") or "").strip()
        if not hint:
            return projects, None
        context_project_count = len(projects)

        snapshot_match = self._match_linear_meeting_project(
            {},
            projects,
            None,
            hint,
        )
        snapshot_confidence = float(snapshot_match.get("confidence") or 0.0)
        if snapshot_match.get("project") and snapshot_confidence >= 0.95:
            payload = {
                "status": "matched_snapshot",
                "project": snapshot_match["project"],
                "confidence": snapshot_confidence,
                "reason": snapshot_match.get("reason"),
            }
            self._log_linear_project_resolution(
                hint=hint,
                active_project_count=context_project_count,
                payload=payload,
            )
            return projects, payload

        resolver = getattr(client, "resolve_project", None)
        if not callable(resolver):
            return projects, None

        try:
            payload = await resolver(hint)
        except Exception as exc:
            if snapshot_match.get("project") and snapshot_confidence >= 0.78:
                payload = {
                    "status": "matched_snapshot",
                    "project": snapshot_match["project"],
                    "confidence": snapshot_confidence,
                    "reason": snapshot_match.get("reason"),
                    "lookupError": exc.__class__.__name__,
                }
                self._log_linear_project_resolution(
                    hint=hint,
                    active_project_count=context_project_count,
                    payload=payload,
                )
                return projects, payload
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": exc.__class__.__name__,
                "lookupDetail": str(exc).strip(),
            }
            self._log_linear_project_resolution(
                hint=hint,
                active_project_count=context_project_count,
                payload=payload,
            )
            return projects, payload

        if not isinstance(payload, dict):
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": "Invalid project resolver response",
            }
        status = str(payload.get("status") or "")
        resolved_project = payload.get("project")
        resolved_confidence = float(payload.get("confidence") or 0.0)
        if (
            status == "matched"
            and isinstance(resolved_project, dict)
            and resolved_project.get("id")
            and resolved_confidence >= 0.82
        ):
            canonical_name = str(resolved_project.get("name") or "").strip()
            if canonical_name:
                params["project_hint"] = canonical_name
            projects = self._dedupe_linear_projects_by_id(
                [resolved_project, *projects]
            )
        elif status not in {"not_found", "ambiguous"}:
            payload = {
                "status": "unavailable",
                "project": None,
                "confidence": 0.0,
                "reason": "Invalid project resolver response",
            }
        self._log_linear_project_resolution(
            hint=hint,
            active_project_count=context_project_count,
            payload=payload,
        )
        return projects, payload


    @staticmethod
    def _log_linear_project_resolution(
        *,
        hint: str,
        active_project_count: int,
        payload: dict[str, Any],
    ) -> None:
        project = payload.get("project")
        project = project if isinstance(project, dict) else {}
        print(
            "LINEAR_PROJECT_RESOLUTION "
            + json.dumps(
                {
                    "event": "explicit_project_lookup",
                    "hint": hint,
                    "active_project_count": active_project_count,
                    "status": payload.get("status"),
                    "confidence": payload.get("confidence"),
                    "reason": payload.get("reason"),
                    "matched_project_id": project.get("id"),
                    "matched_project_name": project.get("name"),
                    "is_inactive": payload.get("isInactive"),
                    "candidate_count": payload.get("candidateCount"),
                    "lookup_error": payload.get("lookupError"),
                    "lookup_detail": payload.get("lookupDetail"),
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


    @staticmethod
    def _linear_explicit_project_resolution_error_message(
        hint: str,
        payload: dict[str, Any],
    ) -> str:
        status = str(payload.get("status") or "")
        if status == "ambiguous":
            candidate_names = [
                str(candidate.get("name") or "").strip()
                for candidate in payload.get("candidates") or []
                if isinstance(candidate, dict) and candidate.get("name")
            ]
            choices = f" Matches: {', '.join(candidate_names)}." if candidate_names else ""
            return (
                f"I found multiple Linear projects matching {hint!r}.{choices} "
                "Please use the exact project title. Nothing was changed."
            )
        if status == "not_found":
            return (
                f"I couldn't find a Linear project matching {hint!r} in the full "
                "workspace. Check the exact title or Roo's access. Nothing was changed."
            )
        lookup_detail = str(payload.get("lookupDetail") or "").strip()
        if "linear_team_access_incomplete" in lookup_detail or (
            "incomplete workspace access" in lookup_detail.lower()
        ):
            return (
                "Roo's Linear connection cannot access every required workspace team. "
                "A Linear admin needs to replace or re-authorize its read/write API key "
                "for all teams. Nothing was changed."
            )
        return (
            f"I couldn't verify the Linear project {hint!r} because the full project "
            "lookup is temporarily unavailable. Nothing was changed. Please try again."
        )


    def _match_linear_meeting_project(
        self,
        candidate: dict[str, Any],
        projects: list[dict[str, Any]],
        owner_user: Optional[dict[str, Any]],
        explicit_project_hint: Optional[str] = None,
        *,
        channel_id: Optional[str] = None,
        channel_context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        hint = str(explicit_project_hint or candidate.get("project_hint") or "").strip()
        scored_by_project: dict[str, tuple[float, dict[str, Any], str]] = {}

        def record(project: dict[str, Any], score: float, reason: str) -> None:
            key = str(project.get("id") or project.get("name") or project.get("slugId") or "")
            if not key:
                return
            existing = scored_by_project.get(key)
            if existing is None or score > existing[0]:
                scored_by_project[key] = (score, project, reason)

        if hint:
            normalized_hint = self._normalize_match_text(hint)
            normalized_core_hint = self._normalize_match_text(
                self._linear_project_core_name(hint)
            )
            for project in projects:
                project_name = project.get("name")
                project_slug = project.get("slugId")
                normalized_project_name = self._normalize_match_text(project_name)
                if normalized_project_name and normalized_hint == normalized_project_name:
                    record(project, 0.98, "Matched project by exact name")
                normalized_core_project_name = self._normalize_match_text(
                    self._linear_project_core_name(project_name)
                )
                if (
                    normalized_core_hint
                    and normalized_core_project_name
                    and normalized_core_hint == normalized_core_project_name
                ):
                    record(project, 0.97, "Matched project by namespace-independent name")
                normalized_slug = self._normalize_match_text(project_slug)
                if normalized_slug and normalized_hint == normalized_slug:
                    record(project, 0.96, "Matched project by exact slug")
                for value in (
                    project_name,
                    self._linear_project_core_name(project_name),
                    project_slug,
                ):
                    normalized_value = self._normalize_match_text(value)
                    if not normalized_value:
                        continue
                    if normalized_value in normalized_hint or normalized_hint in normalized_value:
                        record(project, 0.88, "Matched project by name similarity")
                    else:
                        similarity = SequenceMatcher(None, normalized_hint, normalized_value).ratio()
                        if similarity >= 0.78:
                            record(
                                project,
                                min(similarity, 0.86),
                                "Matched project by name similarity",
                            )

                semantic_score = self._linear_project_semantic_score(hint, project)
                if semantic_score >= 0.78:
                    record(project, semantic_score, "Matched project by Linear context")

        if channel_id:
            for project in projects:
                if str(project.get("slackChannelId") or "").strip() == str(channel_id):
                    record(project, 0.97, "Matched project's linked Slack channel")

        if not hint and channel_context:
            channel_hint = " ".join(
                str(channel_context.get(key) or "")
                for key in ("name", "topic", "purpose")
            ).strip()
            if channel_hint:
                for project in projects:
                    channel_score = self._linear_project_semantic_score(channel_hint, project)
                    if channel_score >= 0.82:
                        record(
                            project,
                            min(channel_score, 0.86),
                            "Matched project from Slack channel context",
                        )

        ranked = sorted(scored_by_project.values(), key=lambda item: item[0], reverse=True)
        if ranked:
            best_score, best_project, best_reason = ranked[0]
            second_score = ranked[1][0] if len(ranked) > 1 else 0.0
            if second_score >= 0.78 and best_score - second_score < 0.05:
                return {"project": None, "confidence": 0.0, "reason": "Ambiguous project hint"}
            if best_score >= 0.78:
                return {
                    "project": best_project,
                    "confidence": best_score,
                    "reason": best_reason,
                }

        owner_id = str((owner_user or {}).get("id") or "")
        owner_email = str((owner_user or {}).get("email") or "").strip().lower()
        member_projects = []
        if owner_id or owner_email:
            for project in projects:
                if project.get("membersSource") == "team_fallback":
                    continue
                members = self._linear_connection_nodes(project.get("members"))
                lead = project.get("lead")
                participants = members + ([lead] if isinstance(lead, dict) else [])
                for member in participants:
                    if str(member.get("id") or "") == owner_id or (
                        owner_email
                        and str(member.get("email") or "").strip().lower() == owner_email
                    ):
                        member_projects.append(project)
                        break
        if len(member_projects) == 1:
            return {
                "project": member_projects[0],
                "confidence": 0.7,
                "reason": "Only active project found for owner",
            }
        return {"project": None, "confidence": 0.0, "reason": "No project match"}


    def _linear_project_semantic_score(
        self,
        hint: str,
        project: dict[str, Any],
    ) -> float:
        hint_tokens = self._linear_project_match_tokens(hint)
        if not hint_tokens:
            return 0.0
        recent_issue_titles = " ".join(
            str(issue.get("title") or "")
            for issue in (project.get("recentIssues") or [])[:20]
            if isinstance(issue, dict)
        )
        context = " ".join(
            [
                str(project.get("name") or ""),
                str(project.get("slugId") or ""),
                str(project.get("description") or ""),
                str(project.get("content") or ""),
                str((project.get("lastUpdate") or {}).get("body") or ""),
                recent_issue_titles,
            ]
        )
        context_tokens = self._linear_project_match_tokens(context)
        if not context_tokens:
            return 0.0
        coverage = len(hint_tokens & context_tokens) / len(hint_tokens)
        normalized_hint = self._normalize_match_text(hint)
        normalized_context = self._normalize_match_text(context)
        if len(normalized_hint) >= 6 and normalized_hint in normalized_context:
            return 0.92
        if coverage == 1.0 and len(hint_tokens) >= 2:
            return 0.9
        if coverage >= 0.75:
            return 0.84
        return 0.0


    @staticmethod
    def _linear_project_match_tokens(value: Any) -> set[str]:
        stop_words = {
            "about", "active", "from", "into", "linear", "project", "slack",
            "task", "team", "that", "the", "this", "with",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 3 and token not in stop_words
        }


    @staticmethod
    def _dedupe_linear_projects_by_id(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for project in projects:
            key = str(project.get("id") or project.get("name") or project.get("slugId") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(project)
        return deduped


    def _match_linear_meeting_team(
        self,
        project: Optional[dict[str, Any]],
        teams: list[dict[str, Any]],
        team_hint: Optional[str],
        default_team: Optional[str],
    ) -> dict[str, Any]:
        if project:
            project_teams = self._linear_connection_nodes(project.get("teams"))
            if project_teams:
                accessible_project_teams: list[dict[str, Any]] = []
                accessible_by_id = {
                    str(team.get("id") or "").strip(): team
                    for team in teams
                    if str(team.get("id") or "").strip()
                }
                for project_team in project_teams:
                    project_team_id = str(project_team.get("id") or "").strip()
                    accessible_team = accessible_by_id.get(project_team_id)
                    if accessible_team:
                        accessible_project_teams.append(accessible_team)

                if not accessible_project_teams:
                    return {
                        "team": None,
                        "confidence": 0.0,
                        "reason": (
                            "Roo's Linear API key cannot access the matched project's team"
                        ),
                    }

                hint = str(team_hint or "").strip()
                if hint:
                    hinted_team = self._find_linear_team_by_hint(
                        accessible_project_teams,
                        hint,
                    )
                    if hinted_team:
                        return {
                            "team": hinted_team,
                            "confidence": 0.97,
                            "reason": "Using hinted team from matched project",
                        }
                return {
                    "team": accessible_project_teams[0],
                    "confidence": 0.96,
                    "reason": "Using matched project's team",
                }

        hint = str(team_hint or "").strip()
        if hint:
            match = self._find_linear_team_by_hint(teams, hint)
            if match:
                return {"team": match, "confidence": 0.9, "reason": "Matched team by hint"}

        if default_team:
            match = self._find_linear_team_by_hint(teams, default_team)
            if match:
                return {
                    "team": match,
                    "confidence": 0.72,
                    "reason": "Using configured default team",
                }
        return {"team": None, "confidence": 0.0, "reason": "No team match"}


    def _find_linear_team_by_hint(
        self,
        teams: list[dict[str, Any]],
        hint: str,
    ) -> Optional[dict[str, Any]]:
        normalized_hint = self._normalize_match_text(hint)
        for team in teams:
            values = [team.get("id"), team.get("key"), team.get("name")]
            if any(self._normalize_match_text(value) == normalized_hint for value in values):
                return team
        for team in teams:
            normalized_values = [
                self._normalize_match_text(value)
                for value in (team.get("key"), team.get("name"))
            ]
            if any(value and (value in normalized_hint or normalized_hint in value) for value in normalized_values):
                return team
        return None


    def _find_linear_meeting_duplicate(
        self,
        candidate: dict[str, Any],
        issues: list[dict[str, Any]],
        project: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        title = self._normalize_match_text(candidate.get("title"))
        if not title:
            return None
        project_id = str((project or {}).get("id") or "")
        for issue in issues:
            if project_id:
                issue_project_id = str(((issue.get("project") or {}).get("id")) or "")
                if issue_project_id and issue_project_id != project_id:
                    continue
            issue_title = self._normalize_match_text(issue.get("title"))
            if not issue_title:
                continue
            if title == issue_title or title in issue_title or issue_title in title:
                return issue
            title_tokens = self._linear_meeting_duplicate_tokens(candidate.get("title"))
            issue_tokens = self._linear_meeting_duplicate_tokens(issue.get("title"))
            if title_tokens and issue_tokens:
                overlap = len(title_tokens & issue_tokens) / min(len(title_tokens), len(issue_tokens))
                if overlap >= 0.75:
                    return issue
            if SequenceMatcher(None, title, issue_title).ratio() >= 0.88:
                return issue
        return None


    def _relax_unscoped_direct_issue_matches(
        self,
        *,
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        teams: list[dict[str, Any]],
        owner_requested: bool,
        project_requested: bool,
        team_requested: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Allow an explicit Linear command to omit the assignee and project.

        A name the user never gave is not an unclear match. A hint that was
        given and could not be matched still fails closed. The issue still
        needs a team: the configured default, or the only accessible team.
        """
        if not owner_requested and not owner_match.get("user"):
            owner_match = {
                "user": None,
                "confidence": 1.0,
                "reason": "No assignee requested",
            }
        if not project_requested and not project_match.get("project"):
            project_match = {
                "project": None,
                "confidence": 1.0,
                "reason": "No project requested",
            }
        # A project inferred from Slack context still owns its team boundary.
        # A failed explicit team hint must not become an unrelated fallback.
        if not project_requested and not project_match.get("project") and not team_requested:
            if (
                team_match.get("team")
                and team_match.get("reason") == "Using configured default team"
            ):
                team_match = {
                    **team_match,
                    "confidence": 0.9,
                    "reason": "Using configured default team for a direct Linear command",
                }
            elif not team_match.get("team") and len(teams) == 1:
                team_match = {
                    "team": teams[0],
                    "confidence": 0.9,
                    "reason": "Only accessible Linear team",
                }
        return owner_match, project_match, team_match


    def _linear_meeting_candidate_decision(
        self,
        *,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        duplicate: Optional[dict[str, Any]],
        auto_threshold: float,
        uncertain_threshold: float,
    ) -> tuple[str, float]:
        if duplicate:
            return "duplicate", 1.0
        confidence_parts = [
            float(candidate.get("confidence") or 0.0),
            float(owner_match.get("confidence") or 0.0),
            float(project_match.get("confidence") or 0.0),
            float(team_match.get("confidence") or 0.0),
        ]
        overall = min(confidence_parts)
        if overall >= auto_threshold:
            return "create", overall
        if overall >= uncertain_threshold:
            return "review", overall
        return "skip", overall


    def _linear_meeting_skip_reason(
        self,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        uncertain_threshold: float,
    ) -> str:
        if float(candidate.get("confidence") or 0.0) < uncertain_threshold:
            return "Candidate confidence too low"
        if float(owner_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(owner_match.get("reason") or "")
            if "ambiguous" in reason.lower():
                return "Assignee unclear: multiple Linear users matched"
            return "Assignee unclear"
        if float(project_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(project_match.get("reason") or "")
            if "ambiguous" in reason.lower():
                return "Project unclear: multiple Linear projects matched"
            return "Project unclear"
        if float(team_match.get("confidence") or 0.0) < uncertain_threshold:
            reason = str(team_match.get("reason") or "").strip()
            if "api key cannot access" in reason.lower():
                return reason
            return "Team unclear"
        return "Low confidence mapping"


    async def _assess_linear_studio_candidates_resilient(
        self,
        *,
        candidates: list[dict[str, Any]],
        project_context: dict[str, Any],
        context_max_chars: int,
        batch_size: int,
        safety_identifier: str,
        max_concurrency: int = 1,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Size bounded batches and isolate a failed batch to single candidates."""

        assessments: dict[str, Any] = {}
        errors: dict[str, str] = {}
        batch_size = max(1, min(int(batch_size or 3), 10))
        chunks = [
            candidates[index : index + batch_size]
            for index in range(0, len(candidates), batch_size)
        ]
        semaphore = asyncio.Semaphore(max(1, min(int(max_concurrency or 1), 4)))

        async def assess_chunk(
            chunk_index: int,
            chunk: list[dict[str, Any]],
        ) -> tuple[dict[str, Any], dict[str, str]]:
            chunk_assessments: dict[str, Any] = {}
            chunk_errors: dict[str, str] = {}
            async with semaphore:
                try:
                    chunk_assessments.update(
                        await assess_studio_effort_batch(
                            candidates=chunk,
                            project_context=project_context,
                            context_max_chars=context_max_chars,
                            safety_identifier=safety_identifier,
                            batch_chunk_index=chunk_index,
                            batch_chunk_count=len(chunks),
                        )
                    )
                    return chunk_assessments, chunk_errors
                except Exception as batch_exc:
                    if len(chunk) == 1:
                        candidate_key = str(chunk[0]["candidate_key"])
                        chunk_errors[candidate_key] = (
                            f"{batch_exc.__class__.__name__}: {batch_exc}"
                        )
                        return chunk_assessments, chunk_errors

                for candidate in chunk:
                    candidate_key = str(candidate["candidate_key"])
                    try:
                        chunk_assessments.update(
                            await assess_studio_effort_batch(
                                candidates=[candidate],
                                project_context=project_context,
                                context_max_chars=context_max_chars,
                                safety_identifier=safety_identifier,
                                batch_chunk_index=chunk_index,
                                batch_chunk_count=len(chunks),
                                recovery_depth=1,
                            )
                        )
                    except Exception as candidate_exc:
                        chunk_errors[candidate_key] = (
                            f"{candidate_exc.__class__.__name__}: {candidate_exc}"
                        )
                return chunk_assessments, chunk_errors

        results = await asyncio.gather(
            *[
                assess_chunk(chunk_index, chunk)
                for chunk_index, chunk in enumerate(chunks, start=1)
            ]
        )
        for chunk_assessments, chunk_errors in results:
            assessments.update(chunk_assessments)
            errors.update(chunk_errors)
        return assessments, errors


    async def _size_linear_studio_prepared_candidates(
        self,
        *,
        prepared_candidates: list[dict[str, Any]],
        client: Any,
        labels: list[dict[str, Any]],
        settings: Any,
        requester_slack_id: str,
    ) -> list[dict[str, Any]]:
        mode = str(
            _linear_task_sizing_setting(
                settings,
                "LINEAR_TASK_SIZING_MODE",
                "LINEAR_STUDIO_SIZING_MODE",
                "off",
            )
        ).strip().lower()
        if mode not in {"off", "shadow", "review", "required"}:
            mode = "off"
        if mode == "off":
            return []

        project_candidates = [
            prepared
            for prepared in prepared_candidates
            if is_project_issue(prepared["project_match"].get("project") or {})
        ]
        receipt_reader = getattr(client, "get_issue_receipt", None)
        if callable(receipt_reader) and project_candidates:
            async def read_receipt(prepared: dict[str, Any]) -> dict[str, Any]:
                try:
                    return await receipt_reader(prepared["idempotency_key"])
                except Exception:
                    return {}

            receipts = await asyncio.gather(
                *[read_receipt(prepared) for prepared in project_candidates]
            )
            for prepared, receipt in zip(project_candidates, receipts):
                status = str(receipt.get("status") or "")
                issue = receipt.get("issue")
                if status == "completed" and isinstance(issue, dict):
                    prepared["receipt_replay_issue"] = issue
                    sizing = issue.get("sizingMetadata") or receipt.get("sizingMetadata")
                    if isinstance(sizing, dict):
                        prepared["display"]["effort_label"] = (
                            sizing.get("effort_label")
                            or sizing.get("effortLabel")
                            or ""
                        )
                        prepared["display"]["effort_rationale"] = sizing.get(
                            "rationale"
                        ) or ""
                elif status == "failed" and isinstance(
                    receipt.get("sizingMetadata"),
                    dict,
                ):
                    prepared["receipt_sizing_metadata"] = dict(
                        receipt["sizingMetadata"]
                    )

        groups: dict[str, list[dict[str, Any]]] = {}
        for prepared in project_candidates:
            project = prepared["project_match"].get("project") or {}
            project_id = str(project.get("id") or "")
            if project_id and not prepared.get("receipt_replay_issue"):
                groups.setdefault(project_id, []).append(prepared)

        shadow_results: list[dict[str, Any]] = []
        for project_id, group in groups.items():
            try:
                project_context = await client.get_project_sizing_context(project_id)
                live_project = project_context.get("project")
                if not isinstance(live_project, dict) or str(
                    live_project.get("id") or ""
                ) != project_id:
                    raise RuntimeError("Linear returned mismatched project sizing context.")
                if not is_project_issue(live_project):
                    raise RuntimeError("Linear returned project context without an ID.")
                registry = project_context.get("effortLabelRegistry")
                registry_labels = (
                    registry.get("nodes")
                    if isinstance(registry, dict)
                    and isinstance(registry.get("nodes"), list)
                    else labels
                )
            except Exception as exc:
                detail = f"{exc.__class__.__name__}: {exc}"
                shadow_results.append(
                    {
                        "project_id": project_id,
                        "error": detail,
                    }
                )
                if mode in {"review", "required"}:
                    for prepared in group:
                        prepared["sizing_error"] = detail
                        prepared["studio_sizing_error"] = detail
                continue

            sizing_candidates: list[dict[str, Any]] = []
            for prepared in group:
                if prepared.get("receipt_sizing_metadata"):
                    continue
                candidate = prepared["candidate"]
                source = prepared["source"]
                owner = prepared["owner_match"].get("user") or {}
                team = prepared["team_match"].get("team") or {}
                sizing_candidates.append(
                    {
                        "candidate_key": prepared["candidate_key"],
                        "title": candidate.get("title"),
                        "description": candidate.get("description"),
                        "work_status": candidate.get("work_status"),
                        "completed_work": candidate.get("completed_work"),
                        "remaining_work": candidate.get("remaining_work"),
                        "available_artifacts": candidate.get("available_artifacts"),
                        "dependencies": candidate.get("dependencies"),
                        "acceptance_criteria": candidate.get("acceptance_criteria"),
                        "evidence": candidate.get("evidence"),
                        "source_label": candidate.get("source_label"),
                        "source_permalink": source.get("source_permalink"),
                        "due_date": candidate.get("due_date"),
                        "assignee": {
                            "id": owner.get("id"),
                            "name": owner.get("displayName")
                            or owner.get("name")
                            or owner.get("email"),
                        },
                        "team": {
                            "id": team.get("id"),
                            "key": team.get("key"),
                            "name": team.get("name"),
                        },
                    }
                )
            context_max_chars = int(
                _linear_task_sizing_setting(
                    settings,
                    "LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS",
                    "LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS",
                    40000,
                )
            )
            assessments, sizing_errors = (
                await self._assess_linear_studio_candidates_resilient(
                    candidates=sizing_candidates,
                    project_context=project_context,
                    context_max_chars=context_max_chars,
                    batch_size=int(
                        _linear_task_sizing_setting(
                            settings,
                            "LINEAR_TASK_SIZING_BATCH_SIZE",
                            "LINEAR_STUDIO_SIZING_BATCH_SIZE",
                            3,
                        )
                    ),
                    safety_identifier=linear_safety_identifier(
                        requester_slack_id
                    ),
                )
                if sizing_candidates
                else ({}, {})
            )
            for prepared in group:
                try:
                    stored_metadata = prepared.get("receipt_sizing_metadata")
                    if isinstance(stored_metadata, dict):
                        metadata = stored_metadata
                        effort_label = str(
                            metadata.get("effort_label")
                            or metadata.get("effortLabel")
                            or ""
                        )
                        rationale = str(metadata.get("rationale") or "")
                        confidence = float(metadata.get("confidence") or 0.0)
                        context_sufficient = bool(
                            metadata.get("context_sufficient")
                            if "context_sufficient" in metadata
                            else metadata.get("contextSufficient", False)
                        )
                    else:
                        candidate_key = str(prepared["candidate_key"])
                        assessment = assessments.get(candidate_key)
                        if assessment is None:
                            raise RuntimeError(
                                sizing_errors.get(
                                    candidate_key,
                                    "Effort sizing returned no assessment.",
                                )
                            )
                        metadata = assessment_metadata(
                            assessment,
                            project=live_project,
                            model=LINEAR_SKILL_MODEL,
                            rubric_version=str(
                                _linear_task_sizing_setting(
                                    settings,
                                    "LINEAR_TASK_SIZING_RUBRIC_VERSION",
                                    "LINEAR_STUDIO_SIZING_RUBRIC_VERSION",
                                    "project-effort-v2",
                                )
                            ),
                        )
                        effort_label = assessment.effort_label
                        rationale = assessment.rationale
                        confidence = assessment.confidence
                        context_sufficient = assessment.context_sufficient
                    team = prepared["team_match"].get("team") or {}
                    compatible_label_ids = self._linear_compatible_label_ids(
                        registry_labels,
                        label_name=effort_label,
                        team_id=str(team.get("id") or ""),
                        exact_name=True,
                    )
                    if len(compatible_label_ids) != 1:
                        raise RuntimeError(
                            f"Expected exactly one compatible Linear label named "
                            f"{effort_label!r}; found {len(compatible_label_ids)}."
                        )
                    shadow_results.append(
                        {
                            "candidate_key": prepared["candidate_key"],
                            "project": str(live_project.get("name") or ""),
                            "effort_label": effort_label,
                            "rationale": rationale,
                            "confidence": confidence,
                            "context_sufficient": context_sufficient,
                            "reused_receipt_assessment": bool(stored_metadata),
                        }
                    )
                    if mode == "shadow":
                        continue
                    prepared["effort_assessment"] = metadata
                    prepared["effort_label_id"] = compatible_label_ids[0]
                    prepared["display"]["effort_label"] = effort_label
                    prepared["display"]["effort_rationale"] = rationale
                except Exception as exc:
                    detail = f"{exc.__class__.__name__}: {exc}"
                    shadow_results.append(
                        {
                            "candidate_key": prepared["candidate_key"],
                            "project_id": project_id,
                            "error": detail,
                        }
                    )
                    if mode in {"review", "required"}:
                        prepared["sizing_error"] = detail
                        prepared["studio_sizing_error"] = detail
        return shadow_results


    def _linear_compatible_label_ids(
        self,
        labels: list[dict[str, Any]],
        *,
        label_name: str,
        team_id: str,
        exact_name: bool = False,
    ) -> list[str]:
        selected: list[dict[str, Any]] = []
        for label in labels:
            if not isinstance(label, dict) or label.get("archivedAt"):
                continue
            name_matches = (
                str(label.get("name") or "") == label_name
                if exact_name
                else self._normalize_match_text(label.get("name"))
                == self._normalize_match_text(label_name)
            )
            if not name_matches:
                continue
            label_team = label.get("team")
            label_team_id = (
                str(label_team.get("id") or "")
                if isinstance(label_team, dict)
                else ""
            )
            if label_team_id and label_team_id != str(team_id or ""):
                continue
            selected.append(label)
        team_scoped = [
            label
            for label in selected
            if isinstance(label.get("team"), dict)
            and str((label.get("team") or {}).get("id") or "") == str(team_id or "")
        ]
        preferred = team_scoped or [
            label
            for label in selected
            if not isinstance(label.get("team"), dict)
            or not str((label.get("team") or {}).get("id") or "")
        ]
        return [str(label.get("id") or "") for label in preferred if label.get("id")]


    def _build_linear_meeting_issue_input(
        self,
        *,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        label_ids: list[str],
        source: dict[str, Any],
        sizing_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        team = team_match.get("team") or {}
        owner = owner_match.get("user") or {}
        project = project_match.get("project") or {}
        issue_input = {
            "title": candidate["title"],
            "team_id": str(team.get("id") or ""),
            "description": self._build_linear_meeting_issue_description(
                candidate,
                source,
                sizing_metadata=sizing_metadata,
            ),
            "assignee_id": str(owner.get("id") or "") or None,
            "project_id": str(project.get("id") or "") or None,
            "priority": candidate.get("priority", 3),
            "due_date": candidate.get("due_date"),
            "label_ids": label_ids,
        }
        if sizing_metadata:
            issue_input["sizing_metadata"] = sizing_metadata
        issue_input["idempotency_key"] = self._linear_meeting_idempotency_key(
            candidate=candidate,
            source=source,
            assignee_id=issue_input.get("assignee_id"),
            project_id=issue_input.get("project_id"),
        )
        return issue_input


    def _linear_meeting_idempotency_key(
        self,
        *,
        candidate: dict[str, Any],
        source: dict[str, Any],
        assignee_id: Optional[str],
        project_id: Optional[str],
    ) -> str:
        source_message_ts = (
            source.get("source_message_ts")
            or source.get("thread_ts")
            or source.get("request_message_ts")
            or ""
        )
        raw = "|".join(
            [
                str(source.get("workspace_id") or ""),
                str(source.get("channel_id") or ""),
                str(source_message_ts),
                self._normalize_match_text(candidate.get("title")),
                str(assignee_id or ""),
                str(project_id or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


    def _build_linear_meeting_issue_description(
        self,
        candidate: dict[str, Any],
        source: dict[str, Any],
        *,
        sizing_metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        lines = [
            "### Meeting action",
            candidate.get("remaining_work")
            or candidate.get("description")
            or candidate.get("title")
            or "",
            "",
        ]
        if candidate.get("completed_work"):
            lines.extend(
                [
                    "### Work already completed",
                    candidate["completed_work"],
                    "",
                ]
            )
        if candidate.get("available_artifacts"):
            lines.extend(
                [
                    "### Available artifacts",
                    *[f"- {item}" for item in candidate["available_artifacts"]],
                    "",
                ]
            )
        if candidate.get("dependencies"):
            lines.extend(
                [
                    "### Dependencies",
                    *[f"- {item}" for item in candidate["dependencies"]],
                    "",
                ]
            )
        if candidate.get("acceptance_criteria"):
            lines.extend(
                [
                    "### Acceptance criteria",
                    *[f"- {item}" for item in candidate["acceptance_criteria"]],
                    "",
                ]
            )
        if sizing_metadata:
            effort_label = (
                sizing_metadata.get("effort_label")
                or sizing_metadata.get("effortLabel")
                or ""
            )
            rationale = str(sizing_metadata.get("rationale") or "")
            lines.extend(
                [
                    "### Effort estimate",
                    f"**{effort_label}** — {rationale}",
                    "",
                ]
            )
        if candidate.get("evidence"):
            lines.extend([
                "### Evidence",
                f"> {candidate['evidence']}",
                "",
            ])
        source_lines = []
        if candidate.get("source_label"):
            source_lines.append(f"- Source document: `{candidate['source_label']}`")
        if source.get("channel_id"):
            channel_label = source.get("channel_name") or source["channel_id"]
            source_lines.append(f"- Slack channel: `{channel_label}` (`{source['channel_id']}`)")
        if source.get("thread_ts"):
            source_lines.append(f"- Slack thread: `{source['thread_ts']}`")
        if source.get("source_message_ts"):
            source_lines.append(f"- Evidence message: `{source['source_message_ts']}`")
        if source.get("source_permalink"):
            source_lines.append(f"- [Open source message in Slack]({source['source_permalink']})")
        if source.get("requester_slack_id"):
            source_lines.append(f"- Requested by: `<@{source['requester_slack_id']}>`")
        if source_lines:
            lines.extend(["### Source", *source_lines, ""])
        lines.append("_Generated by Roo from Slack context._")
        return "\n".join(line for line in lines if line is not None).strip()


    def _build_linear_meeting_candidate_display(
        self,
        candidate: dict[str, Any],
        owner_match: dict[str, Any],
        project_match: dict[str, Any],
        team_match: dict[str, Any],
        confidence: float,
    ) -> dict[str, Any]:
        owner = owner_match.get("user") or {}
        project = project_match.get("project") or {}
        team = team_match.get("team") or {}
        assignee = owner.get("displayName") or owner.get("name") or owner.get("email")
        if not assignee:
            assignee = (
                "Unassigned"
                if owner_match.get("reason") == "No assignee requested"
                else "Unresolved"
            )
        project_name = project.get("name")
        if not project_name:
            project_name = (
                "No project"
                if project_match.get("reason") == "No project requested"
                else "Unresolved"
            )
        return {
            "title": candidate.get("title") or "Untitled action",
            "assignee": assignee,
            "project": project_name,
            "team": team.get("key") or team.get("name") or "Unresolved",
            "source": candidate.get("source_label") or "Slack thread",
            "evidence": candidate.get("evidence") or "",
            "due_date": candidate.get("due_date"),
            "confidence": confidence,
            "owner_reason": owner_match.get("reason"),
            "project_reason": project_match.get("reason"),
            "team_reason": team_match.get("reason"),
        }


    def _format_linear_meeting_result_message(
        self,
        created: list[dict[str, Any]],
        review_needed: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        *,
        project_update: Optional[dict[str, Any]] = None,
        project_update_error: Optional[str] = None,
        contextual_review_mode: bool = False,
    ) -> str:
        lines: list[str] = []
        if project_update:
            project_update_label = (
                ((project_update.get("project") or {}).get("name"))
                or project_update.get("id")
                or "Linear project update"
            )
            if project_update.get("url"):
                lines.append(f"Created Linear project update: <{project_update['url']}|{project_update_label}>")
            else:
                lines.append(f"Created Linear project update: {project_update_label}")
        elif project_update_error:
            lines.append(project_update_error)

        if contextual_review_mode and review_needed:
            if lines:
                lines.append("")
            lines.append("I found a possible Linear issue from this thread. Please review before I create it.")

        if created:
            if lines:
                lines.append("")
            lines.append(f"Created {len(created)} Linear issue{'s' if len(created) != 1 else ''}:")
            for item in created[:10]:
                issue = item.get("issue") or {}
                issue_label = issue.get("identifier") or issue.get("title") or item["title"]
                if issue.get("url"):
                    detail = f"{item['assignee']} · {item['project']}"
                    if item.get("due_date"):
                        detail += f" · due {item['due_date']}"
                    if item.get("effort_label"):
                        detail += f" · {item['effort_label']}"
                    replay = " · already existed" if issue.get("idempotentReplay") else ""
                    lines.append(
                        f"- <{issue['url']}|{issue_label}> - {item['title']} "
                        f"({detail}{replay})"
                    )
                else:
                    lines.append(f"- {issue_label} - {item['title']}")
        if review_needed:
            if lines:
                lines.append("")
            lines.append(f"{len(review_needed)} action item{'s' if len(review_needed) != 1 else ''} need approval:")
            for item in review_needed[:20]:
                review_line = (
                    f"- {item['title']} -> {item['project']} / {item['assignee']} "
                    f"({item['confidence']:.0%})"
                )
                if item.get("effort_label"):
                    review_line += f" · {item['effort_label']}"
                lines.append(review_line)
        if skipped:
            if lines:
                lines.append("")
            lines.append(f"Skipped {len(skipped)} item{'s' if len(skipped) != 1 else ''}:")
            for item in skipped[:8]:
                duplicate = item.get("duplicate") or {}
                mapping = f"project: {item['project']}; assignee: {item['assignee']}"
                if duplicate.get("url"):
                    lines.append(f"- {item['title']} - {item['reason']} ({mapping}; <{duplicate['url']}|existing issue>)")
                else:
                    lines.append(f"- {item['title']} - {item['reason']} ({mapping})")
        return "\n".join(lines) if lines else "No Linear issues were created from those meeting notes."


    def _build_linear_meeting_review_blocks(
        self,
        message: str,
        review_needed: list[dict[str, Any]],
        user_id: str,
        *,
        batch_id: str,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message[:3000]}}
        ]
        batch_value = json.dumps(
            {"batch_id": batch_id, "requested_by": user_id}
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"linear_meeting_batch_{batch_id[:8]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve all"},
                        "style": "primary",
                        "action_id": "linear_meeting_approve_all",
                        "value": batch_value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject all"},
                        "style": "danger",
                        "action_id": "linear_meeting_reject_all",
                        "value": batch_value,
                    },
                ],
            }
        )
        for item in review_needed[:20]:
            item_id = str(item.get("item_id") or "")
            summary = (
                f"*{item['title']}*\n"
                f"Project: `{item['project']}` | Assignee: `{item['assignee']}` | "
                f"Confidence: `{item['confidence']:.0%}`"
            )
            if item.get("effort_label"):
                summary += f" | Effort: `{item['effort_label']}`"
            value = json.dumps(
                {
                    "batch_id": batch_id,
                    "item_ids": [item_id],
                    "requested_by": user_id,
                }
            )
            blocks.extend([
                {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2500]}},
                {
                    "type": "actions",
                    "block_id": f"linear_meeting_{item_id[:8]}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "linear_meeting_approve",
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "action_id": "linear_meeting_reject",
                            "value": value,
                        },
                    ],
                },
            ])
        return blocks


    def _linear_connection_nodes(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            nodes = value.get("nodes") or []
            return [node for node in nodes if isinstance(node, dict)]
        if isinstance(value, list):
            return [node for node in value if isinstance(node, dict)]
        return []


    def _normalize_match_text(self, value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


    def _linear_meeting_duplicate_tokens(self, value: Any) -> set[str]:
        aliases = {
            "doc": "documentation",
            "docs": "documentation",
        }
        tokens = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            tokens.add(aliases.get(token, token))
        return tokens


