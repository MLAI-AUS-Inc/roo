import base64
import importlib
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

    def __init__(self, *args, **kwargs):
        pass

    async def get_reconciliation_report(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"slack_user_id": slack_user_id, **kwargs})
        if self.unavailable:
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")
        if self.status_code:
            request = httpx.Request("GET", "https://backend.test/api/v1/integrations/reconciliation/report")
            response = httpx.Response(self.status_code, json={"error": f"backend {self.status_code}"}, request=request)
            raise httpx.HTTPStatusError("backend error", request=request, response=response)
        return self.report

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
