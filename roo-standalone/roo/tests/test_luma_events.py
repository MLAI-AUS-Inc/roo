import base64
import importlib
import sys
from datetime import date as real_date
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


class FakeLumaBackendClient:
    report = {
        "events": [
            {
                "event_id": "evt-1",
                "event_name": "MLAI Demo Night",
                "start_at": "2026-04-29T08:00:00Z",
                "end_at": "2026-04-29T10:00:00Z",
                "approval_status": "approved",
                "guest_count": 42,
                "checked_in_count": 31,
            }
        ],
        "total_guest_count": 42,
    }
    unavailable = False
    status_code = None
    calls = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def get_luma_attendee_report(self, slack_user_id, **kwargs):
        self.__class__.calls.append({"slack_user_id": slack_user_id, **kwargs})
        if self.unavailable:
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")
        if self.status_code:
            request = httpx.Request("GET", "https://backend.test/api/v1/integrations/luma/attendee-report")
            response = httpx.Response(
                self.status_code,
                json={"error": f"backend {self.status_code}"},
                request=request,
            )
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


def _reset_fake_backend():
    FakeLumaBackendClient.report = {
        "events": [
            {
                "event_id": "evt-1",
                "event_name": "MLAI Demo Night",
                "start_at": "2026-04-29T08:00:00Z",
                "end_at": "2026-04-29T10:00:00Z",
                "approval_status": "approved",
                "guest_count": 42,
                "checked_in_count": 31,
            }
        ],
        "total_guest_count": 42,
    }
    FakeLumaBackendClient.unavailable = False
    FakeLumaBackendClient.status_code = None
    FakeLumaBackendClient.calls = []


def _freeze_today(monkeypatch):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 4)

    monkeypatch.setattr(executor_module, "date", FrozenDate)


@pytest.mark.asyncio
async def test_luma_executor_summary_prompt_calls_backend_without_csv(monkeypatch):
    _reset_fake_backend()
    _freeze_today(monkeypatch)
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="give me a report for how many people registered for the april 29 event",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result["data"]["action"] == "luma_attendee_report"
    assert FakeLumaBackendClient.calls == [
        {
            "slack_user_id": "UADMIN",
            "event_count": 1,
            "event_date": "2026-04-29",
            "approval_status": "approved",
            "include_csv": False,
        }
    ]
    assert "Total approved guests: 42" in result["message"]
    assert "MLAI Demo Night" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_date"),
    [
        ("how many people registered for the april 29 event", "2026-04-29"),
        ("how many people registered for the 29 april event", "2026-04-29"),
        ("how many people registered for the 2026-04-29 event", "2026-04-29"),
    ],
)
async def test_luma_executor_parses_event_dates(monkeypatch, text, expected_date):
    _reset_fake_backend()
    _freeze_today(monkeypatch)
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)

    await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text=text,
        params={"event_count": 1},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert FakeLumaBackendClient.calls[0]["event_date"] == expected_date


@pytest.mark.asyncio
async def test_luma_executor_csv_prompt_uploads_backend_csv(monkeypatch):
    _reset_fake_backend()
    uploaded = []
    csv_content = "event_id,email\nevt-1,ada@example.com\n"
    FakeLumaBackendClient.report = {
        "events": [
            {
                "event_id": "evt-1",
                "event_name": "MLAI Demo Night",
                "start_at": "2026-04-29T08:00:00Z",
                "approval_status": "approved",
                "guest_count": 1,
                "checked_in_count": 1,
                "csv": {
                    "filename": "luma-mlai-2026-04-29-demo.csv",
                    "content_base64": base64.b64encode(csv_content.encode("utf-8")).decode("ascii"),
                    "content_type": "text/csv",
                },
            }
        ],
        "total_guest_count": 1,
    }
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)
    monkeypatch.setattr(
        slack_client_module,
        "upload_file",
        lambda **kwargs: uploaded.append(kwargs) or {"ok": True},
    )

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="give me CSVs for the past 1 MLAI event",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert FakeLumaBackendClient.calls[0]["include_csv"] is True
    assert uploaded == [
        {
            "channel": "C123",
            "content": csv_content,
            "filename": "luma-mlai-2026-04-29-demo.csv",
            "title": "MLAI Demo Night attendees",
            "thread_ts": "111.222",
        }
    ]
    assert "Uploaded 1 CSV file" in result["message"]


@pytest.mark.asyncio
async def test_luma_executor_report_prompt_does_not_upload(monkeypatch):
    _reset_fake_backend()
    uploaded = []
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)
    monkeypatch.setattr(slack_client_module, "upload_file", lambda **kwargs: uploaded.append(kwargs))

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="how many people registered for the latest MLAI event",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert FakeLumaBackendClient.calls[0]["include_csv"] is False
    assert uploaded == []
    assert "Uploaded" not in result["message"]


@pytest.mark.asyncio
async def test_luma_executor_reports_missing_backend_url(monkeypatch):
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings(MLAI_BACKEND_URL=""))

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "MLAI_BACKEND_URL" in result


@pytest.mark.asyncio
async def test_luma_executor_reports_backend_unavailable(monkeypatch):
    _reset_fake_backend()
    FakeLumaBackendClient.unavailable = True
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "mlai-backend" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (403, "Points Admin"),
        (503, "backend 503"),
        (429, "backend 429"),
    ],
)
async def test_luma_executor_reports_backend_http_errors(monkeypatch, status_code, expected):
    _reset_fake_backend()
    FakeLumaBackendClient.status_code = status_code
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert expected in result


@pytest.mark.asyncio
async def test_luma_executor_empty_event_response(monkeypatch):
    _reset_fake_backend()
    _freeze_today(monkeypatch)
    FakeLumaBackendClient.report = {"events": [], "total_guest_count": 0}
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeLumaBackendClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="how many people registered for the april 29 event",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "couldn't find an ended Luma event on 2026-04-29" in result
