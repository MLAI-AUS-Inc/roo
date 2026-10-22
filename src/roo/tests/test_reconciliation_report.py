import base64
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

executor_module = importlib.import_module("roo.skills.executor")
backend_module = importlib.import_module("roo.clients.mlai_backend")
slack_client_module = importlib.import_module("roo.slack_client")
SkillExecutor = executor_module.SkillExecutor


def _report(**overrides):
    report = {
        "window": {"since": "2026-06-01T00:00:00+00:00", "until": "2026-07-01T00:00:00+00:00"},
        "currency_totals": {"AUD": {"payouts": 2, "gross": 70.00, "stripe_fee": 2.75, "deposit": 67.25}},
        "payout_count": 2,
        "charge_count": 5,
        "unmatched_charge_count": 0,
        "payouts": [
            {"payout_id": "po_A", "warnings": []},
            {"payout_id": "po_B", "warnings": ["1 non-charge transaction(s) (refunds/adjustments) in this payout."]},
        ],
        "brief": {
            "filename": "reconciliation-2026-06-01-to-2026-07-01.md",
            "content_base64": base64.b64encode(b"# Stripe payout reconciliation brief\n").decode("ascii"),
            "content_type": "text/markdown",
        },
        "workbook": {
            "filename": "reconciliation-2026-06-01-to-2026-07-01.xlsx",
            "content_base64": base64.b64encode(b"PK\x03\x04fake-xlsx-bytes").decode("ascii"),
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    }
    report.update(overrides)
    return report


def _audit(**overrides):
    audit = {
        "schema_version": 1,
        "audit_version": "reconciliation-event-finance-audit-v1",
        "period_start": "2026-02-02",
        "period_end": "2026-08-02",
        "required_categories": [
            {"key": "ticket_sales", "label": "Ticket sales", "kind": "revenue"},
            {"key": "sponsorship_revenue", "label": "Sponsorship revenue", "kind": "revenue"},
            {"key": "catering_cost", "label": "Catering cost", "kind": "cost"},
            {"key": "contractor_cost", "label": "Contractor cost", "kind": "cost"},
        ],
        "events": [
            {
                "event_name": "Complete Gala",
                "start_at": "2026-06-10T10:00:00+00:00",
                "completeness_status": "complete",
                "present_categories": ["ticket_sales", "sponsorship_revenue", "catering_cost", "contractor_cost"],
                "missing_categories": [],
                "categories": {
                    "ticket_sales": {
                        "status": "present",
                        "evidence": [{"source_type": "luma_event", "source_id": "evt-1", "amount_cents": 10000}],
                    },
                    "sponsorship_revenue": {
                        "status": "present",
                        "evidence": [{"source_type": "xero_bank_transaction_line", "source_id": "bt-1:0", "amount_cents": 50000, "date": "2026-06-11", "account_name": "Sponsorships & Grants", "contact_name": "Sponsor Co"}],
                    },
                    "catering_cost": {"status": "present", "evidence": [{"source_type": "xero_bank_transaction_line", "source_id": "bt-2:0", "amount_cents": 20000, "account_name": "Catering / Food & Beverages"}]},
                    "contractor_cost": {"status": "present", "evidence": [{"source_type": "xero_bank_transaction_line", "source_id": "bt-3:0", "amount_cents": 30000, "account_name": "Contractor Expenses"}]},
                },
            },
            {
                "event_name": "Ticket Only Night",
                "start_at": "2026-07-01T10:00:00+00:00",
                "completeness_status": "incomplete",
                "present_categories": ["ticket_sales"],
                "missing_categories": ["sponsorship_revenue", "catering_cost", "contractor_cost"],
                "categories": {
                    "ticket_sales": {"status": "present", "evidence": [{"source_type": "luma_event", "source_id": "evt-2", "amount_cents": 7500}]},
                    "sponsorship_revenue": {"status": "missing", "evidence": []},
                    "catering_cost": {"status": "missing", "evidence": []},
                    "contractor_cost": {"status": "missing", "evidence": []},
                },
            },
        ],
        "summary": {
            "event_count": 2,
            "complete_count": 1,
            "incomplete_count": 1,
            "missing_counts": {
                "ticket_sales": 0,
                "sponsorship_revenue": 1,
                "catering_cost": 1,
                "contractor_cost": 1,
            },
        },
        "account_resolution_warnings": [],
        "limitations": ["Missing means no tracked evidence was found in this period."],
        "xero_writes": False,
    }
    audit.update(overrides)
    return audit


class FakeReconBackendClient:
    report = _report()
    audit = _audit()
    unavailable = False
    status_code = None
    calls = []
    operation_results = {}

    def __init__(self, *args, **kwargs):
        pass

    async def get_reconciliation_report(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"slack_user_id": slack_user_id, **kwargs})
        self._raise_if_needed("/api/v1/integrations/reconciliation/report")
        return self.report

    async def get_statement_reconciliation_readiness(self, slack_user_id, **kwargs):
        self.__class__.calls.append({
            "action": "readiness",
            "slack_user_id": slack_user_id,
            **kwargs,
        })
        self._raise_if_needed("/api/v1/integrations/reconciliation/readiness")
        return self.operation_results.get("readiness", {
            "ready_to_start": True,
            "ready_to_execute_bank_transactions": True,
            "ready_to_execute_bill_payments": True,
            "tracking_ready": True,
            "latest_statement_scan": {
                "id": 12,
                "fresh": True,
                "candidate_count": 8,
            },
            "monthly_context": {
                "run_id": "monthly-2026-07",
                "status": "completed",
            },
            "blockers": [],
            "warnings": [],
            "recommended_next_action": "Start Xero reconciliation in preview-only mode.",
        })

    def _raise_if_needed(self, endpoint):
        if self.unavailable:
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")
        if self.status_code:
            request = httpx.Request("POST", f"https://backend.test{endpoint}")
            response = httpx.Response(self.status_code, json={"error": f"backend {self.status_code}"}, request=request)
            raise httpx.HTTPStatusError("backend error", request=request, response=response)

    async def start_statement_reconciliation_run(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"action": "start", "slack_user_id": slack_user_id, **kwargs})
        self._raise_if_needed("/api/v1/integrations/reconciliation/agent-runs")
        return self.operation_results.get("start", {
            "run_id": "xero-reconciliation-123",
            "status": "queued",
            "dry_run": True,
            "deterministic_suggestion_count": 1,
            "rule_conflict_count": 0,
            "deferred_bill_count": 0,
            "agent_line_count": 1,
            "valley_dispatched": True,
        })

    async def get_statement_reconciliation_outcomes(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"action": "outcomes", "slack_user_id": slack_user_id, **kwargs})
        self._raise_if_needed("/api/v1/integrations/reconciliation/outcomes")
        return self.operation_results.get("outcomes", {
            "confirmed_reconciled_count": 3,
            "pending_human_match_count": 1,
            "rule_review_candidate_count": 1,
            "automatic_rule_creation": False,
            "recent_confirmed": [{
                "transaction_date": "2026-07-20",
                "currency": "AUD",
                "amount": "845.00",
                "description": "Contractor work for Present Studio.",
                "project_name": "[Studio] Present Studio",
            }],
            "learning_candidates": [{
                "candidate_id": "candidate-present-studio",
                "candidate_version": "version-present-studio",
                "merchant_key": "transfer to contractor one",
                "confirmed_example_count": 2,
                "eligible_for_rule_review": True,
                "eligible_for_promotion": True,
                "review_status": "pending",
                "suggested_rule": {
                    "account_code": "405",
                    "account_name": "Contractor Expenses",
                    "project_name": "[Studio] Present Studio",
                },
            }],
        })

    async def decide_statement_reconciliation_learning_candidate(
        self, slack_user_id, candidate_id, **kwargs
    ):
        self.__class__.calls.append({
            "action": "decide_candidate",
            "slack_user_id": slack_user_id,
            "candidate_id": candidate_id,
            **kwargs,
        })
        self._raise_if_needed(
            f"/api/v1/integrations/reconciliation/learning-candidates/{candidate_id}"
        )
        decision = kwargs["decision"]
        return self.operation_results.get("decide_candidate", {
            "decision": "promoted" if decision == "promote" else "rejected",
            "idempotent": False,
            "rule": {
                "id": 77,
                "name": "Learned: Contractor One",
                "status": "verified",
            } if decision == "promote" else None,
        })

    async def get_statement_reconciliation_run(self, slack_user_id, run_id, **kwargs):
        self.__class__.calls.append({"action": "status", "slack_user_id": slack_user_id, "run_id": run_id, **kwargs})
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}")
        return self.operation_results.get("status", {
            "run_id": run_id, "status": "completed", "suggestions": [{"id": 10}],
        })

    async def retry_statement_reconciliation_run(self, slack_user_id, run_id, **kwargs):
        self.__class__.calls.append({"action": "retry", "slack_user_id": slack_user_id, "run_id": run_id, **kwargs})
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}/retry")
        return self.operation_results.get("retry", {
            "run_id": run_id,
            "status": "queued",
            "valley_dispatched": True,
            "idempotent": False,
        })

    async def preview_statement_reconciliation_run(self, slack_user_id, run_id, **kwargs):
        self.__class__.calls.append({"action": "preview", "slack_user_id": slack_user_id, "run_id": run_id, **kwargs})
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}/preview")
        return self.operation_results.get("preview", {
            "run_id": run_id,
            "run_status": "completed",
            "suggestion_count": 1,
            "ready_count": 1,
            "approved_count": 0,
            "results": [{
                "suggestion": {
                    "id": 10,
                    "description": "Contractor payment for Aaron AI coding",
                    "project": {"tracking_option_name": "[Studio] Aaron AI"},
                    "routing": {
                        "source": "verified_rule",
                        "verified_rule_id": 77,
                    },
                },
                "preview": {"ready": True, "operation": "bank_transaction"},
            }],
        })

    async def approve_ready_statement_reconciliation_run(self, slack_user_id, run_id, **kwargs):
        self.__class__.calls.append({"action": "approve", "slack_user_id": slack_user_id, "run_id": run_id, **kwargs})
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}/decisions")
        return self.operation_results.get("approve", {
            "run_id": run_id, "requested_count": 2, "recorded_count": 1,
        })

    async def reject_statement_reconciliation_suggestions(
        self, slack_user_id, run_id, suggestion_ids, **kwargs
    ):
        self.__class__.calls.append({
            "action": "reject",
            "slack_user_id": slack_user_id,
            "run_id": run_id,
            "suggestion_ids": suggestion_ids,
            **kwargs,
        })
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}/decisions")
        return self.operation_results.get("reject", {
            "run_id": run_id,
            "requested_count": len(suggestion_ids),
            "recorded_count": len(suggestion_ids),
        })

    async def execute_approved_statement_reconciliation_run(self, slack_user_id, run_id, **kwargs):
        self.__class__.calls.append({"action": "execute", "slack_user_id": slack_user_id, "run_id": run_id, **kwargs})
        self._raise_if_needed(f"/api/v1/integrations/reconciliation/agent-runs/{run_id}/execute")
        return self.operation_results.get("execute", {
            "run_id": run_id, "approved_candidate_count": 2, "executed_count": 1,
            "human_reconciliation_required": True,
        })


    async def get_event_finance_audit(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"method": "audit", "slack_user_id": slack_user_id, **kwargs})
        if self.unavailable:
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")
        if self.status_code:
            request = httpx.Request("GET", "https://backend.test/api/v1/integrations/reconciliation/event-finance-audit")
            response = httpx.Response(self.status_code, json={"error": f"backend {self.status_code}"}, request=request)
            raise httpx.HTTPStatusError("backend error", request=request, response=response)
        return self.audit


def _settings(**overrides):
    values = {
        "MLAI_BACKEND_URL": "https://backend.test",
        "MLAI_API_KEY": "api-key",
        "ROO_API_KEY": "roo-api-key",
        "INTERNAL_API_KEY": "internal-key",
        "RECONCILIATION_DOMAIN": "mlai.au",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _reset(monkeypatch, *, uploaded=None, settings_overrides=None):
    FakeReconBackendClient.report = _report()
    FakeReconBackendClient.audit = _audit()
    FakeReconBackendClient.unavailable = False
    FakeReconBackendClient.status_code = None
    FakeReconBackendClient.calls = []
    FakeReconBackendClient.operation_results = {}
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings(**(settings_overrides or {})))
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeReconBackendClient)
    if uploaded is not None:
        monkeypatch.setattr(
            slack_client_module,
            "upload_file",
            lambda **kwargs: uploaded.append(kwargs) or {"ok": True},
        )
    return executor


async def _run(executor, **overrides):
    kwargs = {
        "skill": SimpleNamespace(get_client_class=lambda name: None),
        "text": "reconcile the last 30 days of luma and stripe",
        "params": {},
        "user_id": "UADMIN",
        "channel_id": "C123",
        "thread_ts": "111.222",
    }
    kwargs.update(overrides)
    return await executor._execute_reconciliation_report(**kwargs)


@pytest.mark.asyncio
async def test_success_uploads_brief_and_workbook(monkeypatch):
    uploaded = []
    executor = _reset(monkeypatch, uploaded=uploaded)

    result = await _run(executor)

    # default 30-day window sent to backend
    assert FakeReconBackendClient.calls[0]["days"] == 30
    assert FakeReconBackendClient.calls[0]["include_workbook"] is True
    # two files uploaded: brief as text, workbook as bytes
    assert [u["filename"] for u in uploaded] == [
        "reconciliation-2026-06-01-to-2026-07-01.md",
        "reconciliation-2026-06-01-to-2026-07-01.xlsx",
    ]
    assert isinstance(uploaded[0]["content"], str)
    assert uploaded[0]["content"].startswith("# Stripe payout reconciliation brief")
    assert isinstance(uploaded[1]["content"], (bytes, bytearray))
    assert uploaded[1]["content"][:2] == b"PK"
    assert all(u["thread_ts"] == "111.222" and u["channel"] == "C123" for u in uploaded)
    # summary message
    assert "Luma → Stripe reconciliation" in result["message"]
    assert "AUD: 67.25 deposited" in result["message"]
    assert "Uploaded 2 file(s)" in result["message"]
    assert result["data"]["action"] == "reconciliation_report"


@pytest.mark.asyncio
async def test_days_param_is_clamped(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    await _run(executor, params={"days": 500})
    assert FakeReconBackendClient.calls[0]["days"] == 92


@pytest.mark.asyncio
async def test_since_passed_and_invalid_until_dropped(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    await _run(executor, params={"since": "2026-06-01", "until": "not-a-date"})
    call = FakeReconBackendClient.calls[0]
    assert call["since"] == "2026-06-01"
    assert call["until"] is None


@pytest.mark.asyncio
async def test_non_admin_gets_points_admin_message(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    FakeReconBackendClient.status_code = 403
    result = await _run(executor)
    assert "Points Admin" in result


@pytest.mark.asyncio
async def test_backend_unavailable(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    FakeReconBackendClient.unavailable = True
    result = await _run(executor)
    assert "mlai-backend" in result


@pytest.mark.asyncio
async def test_missing_backend_url(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[], settings_overrides={"MLAI_BACKEND_URL": ""})
    result = await _run(executor)
    assert "MLAI_BACKEND_URL" in result


@pytest.mark.asyncio
async def test_requires_channel(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    result = await _run(executor, channel_id=None)
    assert "channel" in result.lower()


@pytest.mark.asyncio
async def test_empty_payouts_message(monkeypatch):
    uploaded = []
    executor = _reset(monkeypatch, uploaded=uploaded)
    FakeReconBackendClient.report = _report(
        payouts=[], payout_count=0, charge_count=0, currency_totals={}, brief=None, workbook=None
    )
    result = await _run(executor)
    assert "nothing to reconcile" in result["message"]
    assert uploaded == []


@pytest.mark.asyncio
async def test_rate_limited(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    FakeReconBackendClient.status_code = 429
    result = await _run(executor)
    assert "rate-limited" in result.lower() or "429" in result


@pytest.mark.asyncio
async def test_start_statement_reconciliation_is_preview_only(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        text="analyse these transactions for the Aaron AI project",
        params={
            "action": "start_statement_reconciliation",
            "statement_line_ids": ["line-1", "line-2"],
        },
        channel_id=None,
    )

    assert "Started reconciliation run `xero-reconciliation-123`" in result["message"]
    assert "preview-only" in result["message"]
    assert "1 prepared from verified rules" in result["message"]
    assert "1 sent for monthly-context analysis" in result["message"]
    assert FakeReconBackendClient.calls[0]["statement_line_ids"] == ["line-1", "line-2"]
    assert FakeReconBackendClient.calls[0]["instruction"].startswith("analyse these transactions")


@pytest.mark.asyncio
async def test_start_uses_treasurer_agent_when_configured(monkeypatch):
    executor = _reset(
        monkeypatch,
        settings_overrides={
            "RECONCILIATION_AGENT_URL": "https://roo.mlai.au",
            "RECONCILIATION_AGENT_TIMEOUT_SECONDS": 30,
        },
    )
    captured = {}

    async def fake_trigger(**kwargs):
        captured.update(kwargs)
        return {
            "accepted": True,
            "request_id": "roo-request-123",
            "status": "queued",
            "xero_writes": False,
        }

    monkeypatch.setattr(
        executor,
        "_trigger_reconciliation_agent_prepare",
        fake_trigger,
    )

    result = await _run(
        executor,
        text=(
            "Use the monthly updates and treasurer email to plan every "
            "outstanding Xero transaction."
        ),
        params={"action": "start_statement_reconciliation"},
    )

    assert "dedicated treasurer mailbox will sync" in result["message"]
    assert "exact-preview approval buttons" in result["message"]
    assert "final Match/OK tick" in result["message"]
    assert result["data"]["xero_writes"] is False
    assert captured["agent_url"] == "https://roo.mlai.au"
    assert captured["user_id"] == "UADMIN"
    assert captured["channel_id"] == "C123"
    assert FakeReconBackendClient.calls == []


@pytest.mark.asyncio
async def test_treasurer_agent_trigger_is_authenticated_and_idempotency_scoped(monkeypatch):
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(
            202,
            request=request,
            json={
                "accepted": True,
                "request_id": "roo-backend-id",
                "status": "queued",
                "xero_writes": False,
            },
        )

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(executor_module.httpx, "AsyncClient", fake_async_client)
    settings = _settings(
        MLAI_API_KEY="shared-service-key",
        RECONCILIATION_AGENT_TIMEOUT_SECONDS=30,
    )

    result = await SkillExecutor._trigger_reconciliation_agent_prepare(
        settings=settings,
        agent_url="https://roo.mlai.au/",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="1700000000.000001",
        instruction="Plan every outstanding line.",
    )

    request = captured["request"]
    assert request.url == (
        "https://roo.mlai.au/internal/reconciliation/prepare"
    )
    assert request.headers["Authorization"] == "Bearer shared-service-key"
    body = json.loads(request.content)
    assert body["slack_user_id"] == "UADMIN"
    assert body["channel_id"] == "C123"
    assert body["instruction"] == "Plan every outstanding line."
    assert body["request_id"].startswith("roo-")
    assert result["xero_writes"] is False


@pytest.mark.asyncio
async def test_check_reconciliation_readiness_explains_safe_next_action(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={"action": "check_reconciliation_readiness", "domain": "mlai.au"},
        channel_id=None,
    )

    assert "ready to analyse" in result["message"]
    assert "8 current candidate(s)" in result["message"]
    assert "monthly context `monthly-2026-07`" in result["message"]
    assert "Spend/Receive Money ready" in result["message"]
    assert "bill payments ready" in result["message"]
    assert "Start Xero reconciliation in preview-only mode" in result["message"]
    assert FakeReconBackendClient.calls[0] == {
        "action": "readiness",
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
    }


@pytest.mark.asyncio
async def test_repeated_start_reports_reused_run_without_duplicate(monkeypatch):
    executor = _reset(monkeypatch)
    FakeReconBackendClient.operation_results["start"] = {
        "run_id": "xero-reconciliation-existing",
        "status": "queued",
        "dry_run": True,
        "deterministic_suggestion_count": 1,
        "rule_conflict_count": 0,
        "deferred_bill_count": 0,
        "agent_line_count": 1,
        "valley_dispatched": False,
        "idempotent": True,
    }

    result = await _run(
        executor,
        params={"action": "start_statement_reconciliation"},
        channel_id=None,
    )

    assert "Reused existing reconciliation run" in result["message"]
    assert "did not create or dispatch a duplicate" in result["message"]


@pytest.mark.asyncio
async def test_start_completed_deterministic_run_can_be_previewed_immediately(monkeypatch):
    executor = _reset(monkeypatch)
    FakeReconBackendClient.operation_results["start"] = {
        "run_id": "xero-reconciliation-rules",
        "status": "completed",
        "dry_run": True,
        "deterministic_suggestion_count": 3,
        "rule_conflict_count": 0,
        "deferred_bill_count": 0,
        "agent_line_count": 0,
        "valley_dispatched": False,
    }

    result = await _run(
        executor,
        params={"action": "start_statement_reconciliation"},
        channel_id=None,
    )

    assert "3 prepared from verified rules" in result["message"]
    assert "0 sent for monthly-context analysis" in result["message"]
    assert "ask me to preview it now" in result["message"]


@pytest.mark.asyncio
async def test_outcomes_reports_confirmed_matches_without_creating_rules(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={"action": "reconciliation_outcomes", "limit": 25},
        channel_id=None,
    )

    assert "3 confirmed reconciled" in result["message"]
    assert "1 still waiting for Xero Match/OK" in result["message"]
    assert "[Studio] Present Studio" in result["message"]
    assert "candidate-present-studio" in result["message"]
    assert "version-present-studio" in result["message"]
    assert "No rule was created automatically" in result["message"]
    assert FakeReconBackendClient.calls[0] == {
        "action": "outcomes",
        "slack_user_id": "UADMIN",
        "domain": "mlai.au",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_admin_can_explicitly_promote_reviewed_rule_candidate(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={
            "action": "decide_reconciliation_rule_candidate",
            "candidate_id": "candidate-present-studio",
            "candidate_version": "version-present-studio",
            "decision": "promote",
        },
        channel_id=None,
    )

    assert "Verified reconciliation rule #77" in result["message"]
    assert "no Xero transaction was created" in result["message"]
    assert FakeReconBackendClient.calls[0] == {
        "action": "decide_candidate",
        "slack_user_id": "UADMIN",
        "candidate_id": "candidate-present-studio",
        "candidate_version": "version-present-studio",
        "decision": "promote",
        "reason": None,
        "domain": "mlai.au",
    }


@pytest.mark.asyncio
async def test_reject_rule_candidate_requires_a_reason(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={
            "action": "decide_reconciliation_rule_candidate",
            "candidate_id": "candidate-present-studio",
            "candidate_version": "version-present-studio",
            "decision": "reject",
        },
        channel_id=None,
    )

    assert "short reason" in result
    assert FakeReconBackendClient.calls == []


@pytest.mark.asyncio
async def test_preview_shows_project_and_requires_separate_approval(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={"action": "preview_statement_reconciliation", "run_id": "xero-reconciliation-123"},
        channel_id=None,
    )

    assert "1/1 ready, 0 approved" in result["message"]
    assert "Contractor payment for Aaron AI coding" in result["message"]
    assert "[Studio] Aaron AI" in result["message"]
    assert "verified rule #77" in result["message"]
    assert "explicitly ask me to approve" in result["message"]
    assert FakeReconBackendClient.calls[0]["action"] == "preview"


@pytest.mark.asyncio
async def test_status_exposes_retry_for_failed_context_dispatch(monkeypatch):
    executor = _reset(monkeypatch)
    FakeReconBackendClient.operation_results["status"] = {
        "run_id": "xero-reconciliation-123",
        "status": "queued",
        "retry_available": True,
        "suggestions": [{"id": 10}],
    }

    result = await _run(
        executor,
        params={
            "action": "status_statement_reconciliation",
            "run_id": "xero-reconciliation-123",
        },
        channel_id=None,
    )

    assert "can be retried" in result["message"]


@pytest.mark.asyncio
async def test_retry_reuses_run_without_xero_write(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={
            "action": "retry_statement_reconciliation",
            "run_id": "xero-reconciliation-123",
        },
        channel_id=None,
    )

    assert "Re-queued monthly-context analysis" in result["message"]
    assert "Existing deterministic suggestions were kept" in result["message"]
    assert "no Xero transaction was created" in result["message"]
    assert FakeReconBackendClient.calls[0]["action"] == "retry"


@pytest.mark.asyncio
async def test_retry_is_idempotent_while_run_is_already_queued(monkeypatch):
    executor = _reset(monkeypatch)
    FakeReconBackendClient.operation_results["retry"] = {
        "run_id": "xero-reconciliation-123",
        "status": "queued",
        "valley_dispatched": False,
        "idempotent": True,
    }

    result = await _run(
        executor,
        params={
            "action": "retry_statement_reconciliation",
            "run_id": "xero-reconciliation-123",
        },
        channel_id=None,
    )

    assert "did not create or dispatch a duplicate" in result["message"]


@pytest.mark.asyncio
async def test_approve_records_ready_but_does_not_execute(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={"action": "approve_ready_reconciliation", "run_id": "xero-reconciliation-123"},
        channel_id=None,
    )

    assert "Approved 1 ready suggestion" in result["message"]
    assert "1 were not ready" in result["message"]
    assert "No Xero transactions were created yet" in result["message"]
    assert [call["action"] for call in FakeReconBackendClient.calls] == ["approve"]


@pytest.mark.asyncio
async def test_execute_only_approved_and_keeps_human_match_step(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={
            "action": "execute_approved_reconciliation",
            "run_id": "xero-reconciliation-123",
            "suggestion_ids": [10, 11],
        },
        channel_id=None,
    )

    assert "Created 1 approved matching Xero transaction" in result["message"]
    assert "1 approved item(s) were safely blocked" in result["message"]
    assert "green Match/OK" in result["message"]
    assert FakeReconBackendClient.calls[0]["suggestion_ids"] == [10, 11]


@pytest.mark.asyncio
async def test_reject_selected_suggestions_records_reason_without_xero_write(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={
            "action": "reject_reconciliation_suggestions",
            "run_id": "xero-reconciliation-123",
            "suggestion_ids": [10],
            "reason": "This belongs to Present Studio.",
        },
        channel_id=None,
    )

    assert "Rejected 1/1 selected suggestion" in result["message"]
    assert "No Xero transactions were created" in result["message"]
    assert FakeReconBackendClient.calls[0] == {
        "action": "reject",
        "slack_user_id": "UADMIN",
        "run_id": "xero-reconciliation-123",
        "suggestion_ids": [10],
        "reason": "This belongs to Present Studio.",
        "domain": "mlai.au",
    }


@pytest.mark.asyncio
async def test_statement_reconciliation_requires_exact_run_id(monkeypatch):
    executor = _reset(monkeypatch)

    result = await _run(
        executor,
        params={"action": "execute_approved_reconciliation"},
        channel_id=None,
    )

    assert "exact preview" in result
    assert FakeReconBackendClient.calls == []


@pytest.mark.asyncio
async def test_stale_scan_tells_admin_to_run_chrome_backfill(monkeypatch):
    executor = _reset(monkeypatch)
    FakeReconBackendClient.status_code = 409

    result = await _run(
        executor,
        params={"action": "start_statement_reconciliation"},
        channel_id=None,
    )

    assert "Chrome backfill" in result


@pytest.mark.asyncio
async def test_event_finance_audit_uses_six_calendar_months_and_uploads_markdown(monkeypatch):
    uploaded = []
    executor = _reset(monkeypatch, uploaded=uploaded)

    result = await _run(
        executor,
        text="audit all events in the last 6 months for ticket sales, sponsorship, catering and contractors",
        params={"action": "audit_event_finances", "months": 6, "until": "2026-08-02"},
    )

    call = FakeReconBackendClient.calls[0]
    assert call == {
        "method": "audit",
        "slack_user_id": "UADMIN",
        "since": "2026-02-02",
        "until": "2026-08-02",
        "domain": "mlai.au",
    }
    assert len(uploaded) == 1
    assert uploaded[0]["filename"] == "event-finance-audit-2026-02-02-to-2026-08-02.md"
    assert "| Complete Gala | 2026-06-10 | Yes | Yes | Yes | Yes |" in uploaded[0]["content"]
    assert "Ticket Only Night: missing sponsorship, catering, contractors" in result["message"]
    assert "Read-only" in result["message"]
    assert result["data"]["action"] == "event_finance_audit"
    assert result["data"]["xero_writes"] is False


@pytest.mark.asyncio
async def test_event_finance_audit_preserves_explicit_since(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])

    await _run(
        executor,
        params={
            "action": "audit_event_finances",
            "since": "2026-03-01",
            "until": "2026-08-02",
        },
    )

    assert FakeReconBackendClient.calls[0]["since"] == "2026-03-01"


@pytest.mark.asyncio
async def test_event_finance_audit_non_admin_message(monkeypatch):
    executor = _reset(monkeypatch, uploaded=[])
    FakeReconBackendClient.status_code = 403

    result = await _run(
        executor,
        params={"action": "audit_event_finances", "until": "2026-08-02"},
    )

    assert "Points Admin" in result
