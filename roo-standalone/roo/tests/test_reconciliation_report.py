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


class FakeReconBackendClient:
    report = _report()
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


def _settings(**overrides):
    values = {
        "MLAI_BACKEND_URL": "https://backend.test",
        "MLAI_API_KEY": "api-key",
        "ROO_API_KEY": "roo-api-key",
        "INTERNAL_API_KEY": "internal-key",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _reset(monkeypatch, *, uploaded=None, settings_overrides=None):
    FakeReconBackendClient.report = _report()
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
