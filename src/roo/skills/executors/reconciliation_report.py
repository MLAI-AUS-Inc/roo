"""Reconciliation report skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest, SkillExecutorInterface
from typing import Any, Optional
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from ..loader import Skill
import base64
import calendar
from datetime import date
from ...config import get_settings
import hashlib
import httpx
from ...slack_client import upload_file
from .backend_errors import BackendErrors


class ReconciliationReportExecutor(BackendErrors, SkillExecutorInterface):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_reconciliation_report(skill, text, params, user_id, channel_id, thread_ts)
        return result

    async def _execute_reconciliation_report(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Execute Points-Admin reporting or guarded Xero reconciliation actions."""
        action = str(params.get("action") or "generate_report").strip().lower()
        if action == "audit_event_finances":
            return await self._execute_event_finance_audit(
                params=params,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "Reconciliation needs mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )
        if action == "generate_report" and not channel_id:
            return "I need a Slack channel or DM to upload the reconciliation report."

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )

        if action != "generate_report":
            return await self._execute_statement_reconciliation_action(
                action=action,
                text=text,
                params=params,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                settings=settings,
                backend_client=backend_client,
            )

        days = self._resolve_reconciliation_days(params)
        since = self._clean_optional_iso_date(params.get("since"))
        until = self._clean_optional_iso_date(params.get("until"))

        try:
            report = await backend_client.get_reconciliation_report(
                user_id,
                days=days,
                since=since,
                until=until,
                include_workbook=True,
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for the reconciliation report right now. Try again in a tick."
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            detail = self._extract_luma_http_error_detail(e)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin (`admin`, `committee`, "
                    "or `portfolio_lead`) to run the reconciliation report. 🔒"
                )
            if status_code == 503:
                return detail or "Stripe isn't configured in mlai-backend yet. Ask the team to set `STRIPE_SECRET_KEY` there."
            if status_code == 429:
                return detail or "Stripe rate-limited the reconciliation report. Try again in a minute."
            return detail or f"mlai-backend returned HTTP {status_code} for the reconciliation report."

        from ...slack_client import upload_file

        uploaded: list = []
        brief = report.get("brief") if isinstance(report, dict) else None
        if isinstance(brief, dict) and brief.get("content_base64"):
            try:
                brief_text = base64.b64decode(brief["content_base64"]).decode("utf-8")
            except Exception:
                return "mlai-backend returned a brief I couldn't decode. Ask the team to check the reconciliation endpoint."
            brief_name = str(brief.get("filename") or "reconciliation.md")
            upload_file(
                channel=channel_id,
                content=brief_text,
                filename=brief_name,
                title="Reconciliation brief",
                thread_ts=thread_ts,
            )
            uploaded.append(brief_name)

        workbook = report.get("workbook") if isinstance(report, dict) else None
        if isinstance(workbook, dict) and workbook.get("content_base64"):
            try:
                xlsx_bytes = base64.b64decode(workbook["content_base64"])
            except Exception:
                xlsx_bytes = None
            if xlsx_bytes:
                wb_name = str(workbook.get("filename") or "reconciliation.xlsx")
                upload_file(
                    channel=channel_id,
                    content=xlsx_bytes,
                    filename=wb_name,
                    title="Reconciliation workbook",
                    thread_ts=thread_ts,
                )
                uploaded.append(wb_name)

        message = self._format_reconciliation_report(report, uploaded)
        return {
            "message": message,
            "data": {
                "action": "reconciliation_report",
                "payout_count": report.get("payout_count"),
                "charge_count": report.get("charge_count"),
                "unmatched_charge_count": report.get("unmatched_charge_count"),
                "uploaded_filenames": uploaded,
            },
        }


    async def _execute_event_finance_audit(
        self,
        *,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Audit recent events for expected revenue and cost evidence."""
        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "The event finance audit needs mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )
        if not channel_id:
            return "I need a Slack channel or DM to upload the event finance audit."

        since, until = self._resolve_event_finance_audit_period(params)
        if since > until:
            return "The audit start date needs to be on or before the end date."

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )
        try:
            audit = await backend_client.get_event_finance_audit(
                user_id,
                since=since.isoformat(),
                until=until.isoformat(),
                domain=getattr(settings, "RECONCILIATION_DOMAIN", "mlai.au"),
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for the event finance audit right now. Try again in a tick."
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            detail = self._extract_luma_http_error_detail(exc)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin (`admin`, `committee`, "
                    "or `portfolio_lead`) to run the event finance audit. 🔒"
                )
            return detail or f"mlai-backend returned HTTP {status_code} for the event finance audit."

        from ...slack_client import upload_file

        filename = f"event-finance-audit-{since.isoformat()}-to-{until.isoformat()}.md"
        upload_file(
            channel=channel_id,
            content=self._event_finance_audit_markdown(audit),
            filename=filename,
            title="Event finance completeness audit",
            thread_ts=thread_ts,
        )
        return {
            "message": self._format_event_finance_audit(audit, filename),
            "data": {
                "action": "event_finance_audit",
                "period_start": audit.get("period_start"),
                "period_end": audit.get("period_end"),
                "event_count": (audit.get("summary") or {}).get("event_count"),
                "complete_count": (audit.get("summary") or {}).get("complete_count"),
                "incomplete_count": (audit.get("summary") or {}).get("incomplete_count"),
                "uploaded_filenames": [filename],
                "xero_writes": bool(audit.get("xero_writes", False)),
            },
        }


    def _resolve_event_finance_audit_period(
        self,
        params: dict,
    ) -> tuple[date, date]:
        until_text = self._clean_optional_iso_date(params.get("until"))
        until = date.fromisoformat(until_text) if until_text else date.today()
        since_text = self._clean_optional_iso_date(params.get("since"))
        if since_text:
            return date.fromisoformat(since_text), until
        try:
            months = int(params.get("months") or 6)
        except (TypeError, ValueError):
            months = 6
        months = max(1, min(months, 24))
        month_index = until.year * 12 + until.month - 1 - months
        since_year, since_month_zero = divmod(month_index, 12)
        since_month = since_month_zero + 1
        since_day = min(until.day, calendar.monthrange(since_year, since_month)[1])
        return date(since_year, since_month, since_day), until


    @staticmethod
    def _markdown_cell(value: Any) -> str:
        return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


    def _event_finance_audit_markdown(self, audit: dict) -> str:
        summary = audit.get("summary") or {}
        requirements = audit.get("required_categories") or []
        labels = {item.get("key"): item.get("label") for item in requirements}
        lines = [
            "# Event finance completeness audit",
            "",
            f"Period: {audit.get('period_start')} to {audit.get('period_end')}",
            "",
            (
                f"Events: {summary.get('event_count', 0)}; complete: "
                f"{summary.get('complete_count', 0)}; incomplete: "
                f"{summary.get('incomplete_count', 0)}."
            ),
            "",
            "> Missing means no tracked evidence was found in the period. It does not prove the event had no such revenue or cost.",
            "",
            "| Event | Date | Ticket sales | Sponsorship | Catering | Contractors | Missing |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
        symbols = {"present": "Yes", "missing": "Missing"}
        for event in audit.get("events") or []:
            categories = event.get("categories") or {}
            start_at = str(event.get("start_at") or "")[:10] or "—"
            missing = ", ".join(
                str(labels.get(key) or key) for key in event.get("missing_categories") or []
            ) or "—"
            lines.append(
                "| "
                + " | ".join(
                    [
                        self._markdown_cell(event.get("event_name")),
                        start_at,
                        symbols.get((categories.get("ticket_sales") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("sponsorship_revenue") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("catering_cost") or {}).get("status"), "Missing"),
                        symbols.get((categories.get("contractor_cost") or {}).get("status"), "Missing"),
                        self._markdown_cell(missing),
                    ]
                )
                + " |"
            )

        lines.extend(["", "## Evidence", ""])
        for event in audit.get("events") or []:
            lines.append(f"### {event.get('event_name')}")
            lines.append("")
            for requirement in requirements:
                category = (event.get("categories") or {}).get(requirement.get("key")) or {}
                evidence = category.get("evidence") or []
                if not evidence:
                    lines.append(f"- {requirement.get('label')}: **Missing**")
                    continue
                observations = []
                for item in evidence:
                    amount = int(item.get("amount_cents") or 0) / 100
                    if item.get("source_type") == "xero_bank_transaction_line":
                        context = "; ".join(
                            value
                            for value in (
                                str(item.get("date") or ""),
                                str(item.get("account_name") or item.get("account_code") or ""),
                                str(item.get("contact_name") or ""),
                                str(item.get("description") or ""),
                                str(item.get("reference") or ""),
                            )
                            if value
                        )
                    else:
                        context = f"{item.get('source_type')} {item.get('source_id')}"
                    observations.append(f"AUD {amount:,.2f} ({context})")
                lines.append(
                    f"- {requirement.get('label')}: **Present** — "
                    + "; ".join(observations)
                )
            lines.append("")

        warnings = audit.get("account_resolution_warnings") or []
        if warnings:
            lines.extend(
                [
                    "## Account resolution warnings",
                    "",
                    "The following category accounts could not be resolved from Xero: "
                    + ", ".join(warnings),
                    "",
                ]
            )
        lines.extend(["## Limitations", ""])
        lines.extend(f"- {item}" for item in audit.get("limitations") or [])
        lines.extend(["", "This audit is read-only and made no Xero writes.", ""])
        return "\n".join(lines)


    def _format_event_finance_audit(self, audit: dict, filename: str) -> str:
        summary = audit.get("summary") or {}
        event_count = int(summary.get("event_count") or 0)
        if not event_count:
            return (
                f"*Event finance audit* — no events were found from {audit.get('period_start')} "
                f"to {audit.get('period_end')}.\n📎 Uploaded `{filename}`.\n"
                "Read-only: no Xero changes were made."
            )
        missing_counts = summary.get("missing_counts") or {}
        labels = {
            "ticket_sales": "ticket sales",
            "sponsorship_revenue": "sponsorship",
            "catering_cost": "catering",
            "contractor_cost": "contractors",
        }
        lines = [
            f"*Event finance audit* — {audit.get('period_start')} to {audit.get('period_end')}",
            (
                f"✅ {summary.get('complete_count', 0)} of {event_count} event(s) have all four "
                f"categories; ⚠️ {summary.get('incomplete_count', 0)} are missing evidence."
            ),
            "Missing across events: "
            + ", ".join(
                f"{labels[key]} {int(missing_counts.get(key) or 0)}"
                for key in labels
            )
            + ".",
        ]
        incomplete = [
            event
            for event in audit.get("events") or []
            if event.get("completeness_status") == "incomplete"
        ]
        for event in incomplete[:10]:
            missing = ", ".join(
                labels.get(key, key) for key in event.get("missing_categories") or []
            )
            lines.append(f"• {event.get('event_name')}: missing {missing}")
        if len(incomplete) > 10:
            lines.append(f"• …and {len(incomplete) - 10} more in the attachment.")
        if audit.get("account_resolution_warnings"):
            lines.append("⚠️ One or more expected Xero accounts could not be resolved; see the attachment.")
        lines.extend(
            [
                "",
                f"📎 Uploaded `{filename}` with every event and its evidence.",
                "Read-only: no Xero changes were made. “Missing” means no tracked evidence in the period, not proof the category did not exist.",
            ]
        )
        return "\n".join(lines)


    async def _execute_statement_reconciliation_action(
        self,
        *,
        action: str,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
        settings: Any,
        backend_client: Any,
    ) -> Any:
        """Run the preview → approval → execution contract for Xero writes."""
        from roo.clients.mlai_backend import MLAIBackendUnavailableError

        valid_actions = {
            "check_reconciliation_readiness",
            "start_statement_reconciliation",
            "reconciliation_outcomes",
            "decide_reconciliation_rule_candidate",
            "status_statement_reconciliation",
            "retry_statement_reconciliation",
            "preview_statement_reconciliation",
            "approve_ready_reconciliation",
            "reject_reconciliation_suggestions",
            "execute_approved_reconciliation",
        }
        if action not in valid_actions:
            return "I couldn't identify that reconciliation action. Ask me to check readiness, start, preview, approve, reject, or execute a run."

        domain = str(params.get("domain") or "mlai.au").strip().lower()
        run_id = str(params.get("run_id") or "").strip()
        if action not in {
            "check_reconciliation_readiness",
            "start_statement_reconciliation",
            "reconciliation_outcomes",
            "decide_reconciliation_rule_candidate",
        } and not run_id:
            return "Please include the reconciliation run ID so I operate on the exact preview you reviewed."

        try:
            if action == "check_reconciliation_readiness":
                result = await backend_client.get_statement_reconciliation_readiness(
                    user_id,
                    domain=domain,
                )
                return {
                    "message": self._format_statement_reconciliation_readiness(result),
                    "data": {"action": action, **result},
                }

            if action == "start_statement_reconciliation":
                agent_url = str(
                    getattr(settings, "RECONCILIATION_AGENT_URL", "") or ""
                ).strip()
                if agent_url:
                    if not channel_id:
                        return (
                            "Start this workflow in the private Roo bookkeeping "
                            "channel so I can post its review digest safely."
                        )
                    trigger = await self._trigger_reconciliation_agent_prepare(
                        settings=settings,
                        agent_url=agent_url,
                        user_id=user_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        instruction=str(params.get("instruction") or text).strip()[:4000],
                    )
                    duplicate_note = (
                        " The same request was already queued, so I did not start a duplicate."
                        if trigger.get("duplicate")
                        else ""
                    )
                    return {
                        "message": (
                            "I’ve started the full reconciliation preparation: the "
                            "dedicated treasurer mailbox will sync, new invoices and "
                            "receipts will be extracted, and every current Xero line "
                            "will be checked against the latest monthly update and "
                            "connected finance/event context. A detailed digest with "
                            "exact-preview approval buttons will appear in this private "
                            "channel. No Xero write happens until you approve a preview; "
                            "after that, Roo creates the green match and you make the "
                            "final Match/OK tick in Xero."
                            + duplicate_note
                        ),
                        "data": {"action": action, **trigger},
                    }
                line_ids = self._coerce_reconciliation_statement_line_ids(
                    params.get("statement_line_ids")
                )
                result = await backend_client.start_statement_reconciliation_run(
                    user_id,
                    domain=domain,
                    instruction=str(params.get("instruction") or text).strip()[:4000],
                    statement_line_ids=line_ids,
                )
                deterministic_count = int(
                    result.get("deterministic_suggestion_count") or 0
                )
                agent_line_count = int(result.get("agent_line_count") or 0)
                conflict_count = int(result.get("rule_conflict_count") or 0)
                deferred_bill_count = int(result.get("deferred_bill_count") or 0)
                summary_parts = [
                    f"{deterministic_count} prepared from verified rules",
                    f"{agent_line_count} sent for monthly-context analysis",
                ]
                if conflict_count:
                    summary_parts.append(f"{conflict_count} rule conflicts held for review")
                if deferred_bill_count:
                    summary_parts.append(
                        f"{deferred_bill_count} exact bill matches reserved for bill-payment analysis"
                    )
                if result.get("status") == "completed":
                    next_step = "The preview-only run is complete; ask me to preview it now."
                else:
                    next_step = (
                        "It is preview-only; ask me to preview that run once its status is completed."
                    )
                start_verb = (
                    "Reused existing reconciliation run"
                    if result.get("idempotent")
                    else "Started reconciliation run"
                )
                duplicate_note = (
                    " I did not create or dispatch a duplicate."
                    if result.get("idempotent")
                    else ""
                )
                return {
                    "message": (
                        f"{start_verb} `{result.get('run_id')}` against the fresh Xero queue. "
                        f"{'; '.join(summary_parts)}.{duplicate_note} {next_step}"
                    ),
                    "data": {"action": action, **result},
                }

            if action == "reconciliation_outcomes":
                try:
                    limit = int(params.get("limit") or 50)
                except (TypeError, ValueError):
                    limit = 50
                result = await backend_client.get_statement_reconciliation_outcomes(
                    user_id,
                    domain=domain,
                    limit=max(1, min(limit, 200)),
                )
                return {
                    "message": self._format_statement_reconciliation_outcomes(result),
                    "data": {"action": action, **result},
                }

            if action == "decide_reconciliation_rule_candidate":
                candidate_id = str(params.get("candidate_id") or "").strip()
                candidate_version = str(params.get("candidate_version") or "").strip()
                decision = str(params.get("decision") or "").strip().lower()
                reason = str(params.get("reason") or "").strip()
                if not candidate_id or not candidate_version:
                    return (
                        "Include the candidate ID and version from the reconciliation "
                        "outcomes preview."
                    )
                if decision not in {"promote", "reject"}:
                    return "Choose `promote` or `reject` for the reviewed rule candidate."
                if decision == "reject" and not reason:
                    return "Include a short reason for rejecting the rule candidate."
                result = await backend_client.decide_statement_reconciliation_learning_candidate(
                    user_id,
                    candidate_id,
                    candidate_version=candidate_version,
                    decision=decision,
                    reason=reason[:2000] or None,
                    domain=domain,
                )
                if decision == "promote":
                    rule = result.get("rule") or {}
                    return {
                        "message": (
                            f"Verified reconciliation rule #{rule.get('id')} "
                            f"`{rule.get('name')}` from candidate `{candidate_id}`"
                            + (" (already promoted)." if result.get("idempotent") else ".")
                            + " Future matching lines can now use this rule; no Xero transaction was created."
                        ),
                        "data": {"action": action, **result},
                    }
                return {
                    "message": (
                        f"Rejected rule candidate `{candidate_id}` with the supplied audit reason. "
                        "No rule or Xero transaction was created."
                    ),
                    "data": {"action": action, **result},
                }

            if action == "status_statement_reconciliation":
                result = await backend_client.get_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                count = len(result.get("suggestions") or [])
                retry_hint = (
                    " The context-analysis dispatch can be retried against this run."
                    if result.get("retry_available")
                    else ""
                )
                return {
                    "message": (
                        f"Run `{run_id}` is *{result.get('status', 'unknown')}* with {count} suggestion(s). "
                        f"Preview it before approving anything.{retry_hint}"
                    ),
                    "data": {"action": action, **result},
                }

            if action == "retry_statement_reconciliation":
                result = await backend_client.retry_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                if result.get("idempotent"):
                    message = (
                        f"Run `{run_id}` is already {result.get('status', 'queued')}; "
                        "I did not create or dispatch a duplicate."
                    )
                else:
                    message = (
                        f"Re-queued monthly-context analysis for run `{run_id}` against its original "
                        "fresh Xero scan. Existing deterministic suggestions were kept; no Xero "
                        "transaction was created."
                    )
                return {
                    "message": message,
                    "data": {"action": action, **result},
                }

            if action == "preview_statement_reconciliation":
                result = await backend_client.preview_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                return {
                    "message": self._format_statement_reconciliation_preview(result),
                    "data": {"action": action, **result},
                }

            if action == "approve_ready_reconciliation":
                result = await backend_client.approve_ready_statement_reconciliation_run(
                    user_id, run_id, domain=domain
                )
                recorded = int(result.get("recorded_count") or 0)
                requested = int(result.get("requested_count") or 0)
                blocked = max(0, requested - recorded)
                return {
                    "message": (
                        f"Approved {recorded} ready suggestion(s) in run `{run_id}`"
                        + (f"; {blocked} were not ready and remain unapproved" if blocked else "")
                        + ". No Xero transactions were created yet."
                    ),
                    "data": {"action": action, **result},
                }

            if action == "reject_reconciliation_suggestions":
                suggestion_ids = self._coerce_reconciliation_suggestion_ids(
                    params.get("suggestion_ids")
                )
                reason = str(params.get("reason") or "").strip()
                if not suggestion_ids:
                    return "Include the integer suggestion IDs you want to reject from the exact run preview."
                if not reason:
                    return "Include a short reason so the rejection is useful in the reconciliation audit trail."
                result = await backend_client.reject_statement_reconciliation_suggestions(
                    user_id,
                    run_id,
                    suggestion_ids,
                    reason=reason[:2000],
                    domain=domain,
                )
                recorded = int(result.get("recorded_count") or 0)
                return {
                    "message": (
                        f"Rejected {recorded}/{len(suggestion_ids)} selected suggestion(s) in run `{run_id}`. "
                        "No Xero transactions were created."
                    ),
                    "data": {"action": action, **result},
                }

            raw_suggestion_ids = params.get("suggestion_ids")
            suggestion_ids = self._coerce_reconciliation_suggestion_ids(raw_suggestion_ids)
            if raw_suggestion_ids not in (None, "", []) and suggestion_ids is None:
                return "suggestion_ids must contain integer IDs from the exact run preview."
            result = await backend_client.execute_approved_statement_reconciliation_run(
                user_id,
                run_id,
                domain=domain,
                suggestion_ids=suggestion_ids,
            )
            executed = int(result.get("executed_count") or 0)
            candidates = int(result.get("approved_candidate_count") or 0)
            failed = max(0, candidates - executed)
            message = (
                f"Created {executed} approved matching Xero transaction(s) from run `{run_id}`"
                + (f"; {failed} approved item(s) were safely blocked" if failed else "")
                + ". Open Xero and click the green Match/OK buttons to finish reconciliation."
            )
            return {"message": message, "data": {"action": action, **result}}
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for reconciliation right now. Try again in a tick."
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            detail = self._extract_luma_http_error_detail(exc)
            if status_code == 403:
                return "Sorry mate, you'll need to be a Points Admin to run Xero reconciliation. 🔒"
            if status_code == 409:
                if action == "decide_reconciliation_rule_candidate":
                    return detail or "The rule candidate changed or is no longer safe to promote."
                if "wait for completion" in detail.lower():
                    return detail
                return (
                    (detail + " ") if detail else ""
                ) + "Import the current Xero bank-feed queue with the Chrome backfill, then start a new run."
            if status_code == 404:
                return detail or f"I couldn't find reconciliation run `{run_id}`."
            return detail or f"mlai-backend returned HTTP {status_code} for reconciliation."


    @staticmethod
    async def _trigger_reconciliation_agent_prepare(
        *,
        settings: Any,
        agent_url: str,
        user_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        instruction: str,
    ) -> dict:
        service_key = str(
            getattr(settings, "MLAI_API_KEY", "")
            or getattr(settings, "ROO_API_KEY", "")
            or getattr(settings, "INTERNAL_API_KEY", "")
            or ""
        ).strip()
        if not service_key:
            raise ValueError(
                "The reconciliation service key is not configured on Roo."
            )
        request_seed = "|".join(
            [channel_id, str(thread_ts or ""), user_id, instruction]
        )
        request_id = "roo-" + hashlib.sha256(request_seed.encode()).hexdigest()[:40]
        timeout = float(
            getattr(settings, "RECONCILIATION_AGENT_TIMEOUT_SECONDS", 30.0)
            or 30.0
        )
        async with httpx.AsyncClient(timeout=max(5.0, min(timeout, 120.0))) as client:
            response = await client.post(
                agent_url.rstrip("/") + "/internal/reconciliation/prepare",
                headers={"Authorization": f"Bearer {service_key}"},
                json={
                    "request_id": request_id,
                    "slack_user_id": user_id,
                    "channel_id": channel_id,
                    "thread_ts": str(thread_ts or ""),
                    "instruction": instruction
                    or "Plan every outstanding Xero reconciliation.",
                },
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("accepted") is not True:
            raise ValueError("The reconciliation service did not accept the request.")
        return payload


    @staticmethod
    def _coerce_reconciliation_statement_line_ids(raw: Any) -> Optional[list[str]]:
        if raw in (None, "", []):
            return None
        values = raw if isinstance(raw, list) else str(raw).split(",")
        cleaned = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        return cleaned or None


    @staticmethod
    def _format_statement_reconciliation_readiness(result: dict) -> str:
        scan = result.get("latest_statement_scan") or {}
        monthly = result.get("monthly_context") or {}
        candidate_count = int(scan.get("candidate_count") or 0)
        start_state = "ready to analyse" if result.get("ready_to_start") else "not ready to analyse"
        bank_state = (
            "Spend/Receive Money ready"
            if result.get("ready_to_execute_bank_transactions")
            else "Spend/Receive Money not ready"
        )
        bill_state = (
            "bill payments ready"
            if result.get("ready_to_execute_bill_payments")
            else "bill payments not ready"
        )
        context_state = (
            f"monthly context `{monthly.get('run_id')}`"
            if monthly.get("run_id")
            else "no completed monthly context"
        )
        lines = [
            (
                f"*Xero reconciliation is {start_state}*: {candidate_count} current "
                f"candidate(s), {context_state}; {bank_state}; {bill_state}."
            )
        ]
        blockers = [str(item).strip() for item in result.get("blockers") or [] if str(item).strip()]
        warnings = [str(item).strip() for item in result.get("warnings") or [] if str(item).strip()]
        if blockers:
            lines.append("*Blockers:* " + " ".join(blockers))
        if warnings:
            lines.append("*Setup notes:* " + " ".join(warnings))
        next_action = str(result.get("recommended_next_action") or "").strip()
        if next_action:
            lines.append(f"*Next:* {next_action}")
        return "\n".join(lines)


    @staticmethod
    def _coerce_reconciliation_suggestion_ids(raw: Any) -> Optional[list[int]]:
        if raw in (None, "", []):
            return None
        values = raw if isinstance(raw, list) else str(raw).split(",")
        result = []
        for value in values:
            try:
                item = int(value)
            except (TypeError, ValueError):
                continue
            if item not in result:
                result.append(item)
        return result or None


    @staticmethod
    def _format_statement_reconciliation_preview(result: dict) -> str:
        run_id = result.get("run_id") or "unknown"
        run_status = result.get("run_status") or "unknown"
        ready = int(result.get("ready_count") or 0)
        total = int(result.get("suggestion_count") or 0)
        approved = int(result.get("approved_count") or 0)
        lines = [
            f"*Reconciliation preview `{run_id}`* — run {run_status}; {ready}/{total} ready, {approved} approved."
        ]
        for item in (result.get("results") or [])[:10]:
            suggestion = item.get("suggestion") or {}
            preview = item.get("preview") or {}
            suggestion_id = suggestion.get("id")
            description = str(suggestion.get("description") or "Needs description").strip()
            project = (suggestion.get("project") or {}).get("tracking_option_name")
            event = (suggestion.get("event") or {}).get("tracking_option_name")
            allocation = project or event or "no event/project"
            state = "ready" if preview.get("ready") else "blocked"
            routing = suggestion.get("routing") or {}
            route_source = routing.get("source")
            if route_source == "verified_rule":
                route = f"verified rule #{routing.get('verified_rule_id')}"
            elif route_source == "exact_xero_bill":
                route = f"exact Xero bill {routing.get('xero_bill_id')}"
            elif route_source == "monthly_context_agent":
                route = "monthly context"
            elif route_source == "rule_conflict":
                route = "rule conflict"
            else:
                route = str(route_source or "legacy/manual").replace("_", " ")
            lines.append(
                f"• #{suggestion_id} — {description[:90]} — {allocation} — {route} — {state}."
            )
        if total > 10:
            lines.append(f"• …and {total - 10} more suggestion(s).")
        if ready:
            lines.append("Review these details, then explicitly ask me to approve the ready suggestions for this run.")
        else:
            lines.append("Nothing is ready to approve yet; review the blocking errors and source context.")
        return "\n".join(lines)


    @staticmethod
    def _format_statement_reconciliation_outcomes(result: dict) -> str:
        confirmed = int(result.get("confirmed_reconciled_count") or 0)
        pending = int(result.get("pending_human_match_count") or 0)
        review_candidates = int(result.get("rule_review_candidate_count") or 0)
        lines = [
            f"*Reconciliation outcomes* — {confirmed} confirmed reconciled; "
            f"{pending} still waiting for Xero Match/OK; {review_candidates} pattern(s) ready for rule review."
        ]
        recent = result.get("recent_confirmed") or []
        for item in recent[:5]:
            allocation = item.get("project_name") or item.get("event_name") or "no event/project"
            lines.append(
                f"• {item.get('transaction_date')} — {item.get('currency', '')} {item.get('amount')} — "
                f"{item.get('description') or item.get('contact_name')} — {allocation}."
            )
        candidates = [
            item
            for item in result.get("learning_candidates") or []
            if item.get("eligible_for_promotion")
            and item.get("review_status", "pending") == "pending"
        ]
        if candidates:
            lines.append("*Patterns for admin rule review:*")
            for item in candidates[:5]:
                rule = item.get("suggested_rule") or {}
                allocation = rule.get("project_name") or rule.get("event_name") or "no event/project"
                lines.append(
                    f"• `{item.get('candidate_id')}` ({item.get('confirmed_example_count')} confirmations) — "
                    f"{item.get('merchant_key')} → {rule.get('account_code')} - "
                    f"{rule.get('account_name')}; {allocation}. "
                    f"Version `{item.get('candidate_version')}`."
                )
        lines.append(
            "No rule was created automatically. After reviewing a candidate and version, "
            "an admin can explicitly promote or reject it."
        )
        return "\n".join(lines)


    def _resolve_reconciliation_days(self, params: dict) -> int:
        raw = params.get("days")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = 30
        return max(1, min(days, 92))


    @staticmethod
    def _clean_optional_iso_date(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        try:
            date.fromisoformat(text)
        except ValueError:
            return None
        return text


    def _format_reconciliation_report(self, report: dict, uploaded: list) -> str:
        payouts = report.get("payouts") or []
        if not payouts:
            return "No Stripe payouts landed in that window — nothing to reconcile. ✅"

        totals = report.get("currency_totals") or {}
        lines = [
            f"*Luma → Stripe reconciliation* — {report.get('payout_count', len(payouts))} "
            f"payout(s), {report.get('charge_count', 0)} charge(s)."
        ]
        for ccy, t in totals.items():
            lines.append(
                f"• {ccy}: {t.get('deposit', 0):,.2f} deposited across {t.get('payouts', 0)} "
                f"payout(s) (gross {t.get('gross', 0):,.2f}, Stripe fees {t.get('stripe_fee', 0):,.2f})."
            )

        unmatched = report.get("unmatched_charge_count") or 0
        if unmatched:
            lines.append(f"⚠ {unmatched} charge(s) had no Luma event match — flagged in the brief.")
        warn_payouts = [p for p in payouts if p.get("warnings")]
        if warn_payouts:
            lines.append(
                f"⚠ {len(warn_payouts)} payout(s) have warnings (refunds/adjustments or tie-out) — see the brief."
            )

        if uploaded:
            lines.append("")
            lines.append(f"📎 Uploaded {len(uploaded)} file(s): {', '.join(uploaded)}.")
            lines.append(
                "Hand the brief to Claude Cowork to reconcile in Xero — the report only "
                "prepares; a human clicks the final confirm."
            )
        return "\n".join(lines)


