import asyncio
import csv
import importlib
import importlib.util
import sys
from datetime import datetime
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


def _load_luma_client_module():
    client_path = Path(__file__).resolve().parents[2] / "skills" / "luma_events" / "client.py"
    spec = importlib.util.spec_from_file_location("luma_events_client_for_tests", client_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


luma_client_module = _load_luma_client_module()
LumaEventsClient = luma_client_module.LumaEventsClient


class FakeAdminLookupClient:
    roles = {}
    unavailable = False

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def get_admin_details(self, slack_user_id):
        if self.unavailable:
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")
        role = self.roles.get(slack_user_id)
        if not role:
            return None
        return {"slack_user_id": slack_user_id, "role": role}


@pytest.mark.asyncio
async def test_luma_client_paginates_events_and_guests():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, dict(request.url.params)))
        params = dict(request.url.params)
        if request.url.path == "/v1/calendar/list-events":
            if params.get("pagination_cursor") == "page-2":
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "id": "evt-2",
                                "name": "MLAI April",
                                "start_at": "2026-04-20T08:00:00Z",
                                "end_at": "2026-04-20T10:00:00Z",
                            }
                        ],
                        "has_more": False,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "id": "evt-current",
                            "name": "Current Event",
                            "start_at": "2026-05-04T00:00:00Z",
                            "end_at": "2026-05-05T00:00:00Z",
                        },
                        {
                            "id": "evt-1",
                            "name": "MLAI May",
                            "start_at": "2026-05-03T08:00:00Z",
                            "end_at": "2026-05-03T10:00:00Z",
                        },
                    ],
                    "has_more": True,
                    "next_cursor": "page-2",
                },
            )

        if request.url.path == "/v1/event/get-guests":
            if params.get("pagination_cursor") == "guests-2":
                return httpx.Response(
                    200,
                    json={
                        "entries": [{"id": "gst-2", "user_email": "b@example.com"}],
                        "has_more": False,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "entries": [{"id": "gst-1", "user_email": "a@example.com"}],
                    "has_more": True,
                    "next_cursor": "guests-2",
                },
            )

        return httpx.Response(404)

    client = LumaEventsClient(
        api_key="test-key",
        base_url="https://luma.test",
        transport=httpx.MockTransport(handler),
    )

    events = await client.get_recent_ended_events(
        count=2,
        now=datetime.fromisoformat("2026-05-04T12:00:00+10:00"),
    )
    guests = await client.list_guests("evt-1")

    assert [event["id"] for event in events] == ["evt-1", "evt-2"]
    assert [guest["id"] for guest in guests] == ["gst-1", "gst-2"]
    assert any(
        path == "/v1/calendar/list-events" and params.get("pagination_cursor") == "page-2"
        for path, params in requests
    )
    assert any(
        path == "/v1/event/get-guests"
        and params.get("approval_status") == "approved"
        and params.get("pagination_cursor") == "guests-2"
        for path, params in requests
    )


def test_luma_csv_flattens_guest_tickets_and_registration_answers():
    client = LumaEventsClient(api_key="test-key")
    event = {
        "id": "evt-1",
        "name": "MLAI Demo Night",
        "url": "https://luma.com/mlai-demo",
        "start_at": "2026-04-28T08:00:00Z",
        "end_at": "2026-04-28T10:00:00Z",
    }
    guests = [
        {
            "id": "gst-1",
            "user_id": "usr-1",
            "user_email": "sam@example.com",
            "user_name": "Sam Donegan",
            "user_first_name": "Sam",
            "user_last_name": "Donegan",
            "phone_number": "+61000000000",
            "approval_status": "approved",
            "registered_at": "2026-04-01T00:00:00Z",
            "utm_source": "newsletter",
            "event_tickets": [
                {
                    "id": "tkt-1",
                    "name": "General Admission",
                    "checked_in_at": "2026-04-28T08:10:00Z",
                },
                {"id": "tkt-2", "name": "Workshop", "checked_in_at": None},
            ],
            "registration_answers": [
                {"label": "Company", "question_type": "company", "answer_company": "MLAI", "answer_job_title": "Founder"},
                {"label": "Interests", "question_type": "multi-select", "answer": ["AI", "Health"]},
            ],
        },
        {
            "id": "gst-2",
            "user_email": "missing-name@example.com",
            "approval_status": "approved",
            "event_tickets": [],
            "registration_answers": [{"label": "Interests", "answer": "Robotics"}],
        },
    ]

    rows = list(csv.DictReader(client.build_attendee_csv(event, guests).splitlines()))

    assert rows[0]["event_id"] == "evt-1"
    assert rows[0]["name"] == "Sam Donegan"
    assert rows[0]["ticket_count"] == "2"
    assert rows[0]["ticket_names"] == "General Admission; Workshop"
    assert rows[0]["checked_in_at"] == "2026-04-28T08:10:00Z"
    assert rows[0]["question: Company"] == "MLAI - Founder"
    assert rows[0]["question: Interests"] == "AI; Health"
    assert rows[1]["name"] == ""
    assert rows[1]["question: Interests"] == "Robotics"
    assert client.build_csv_filename(event) == "luma-mlai-2026-04-28-mlai-demo-night.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "committee", "partner"])
async def test_luma_executor_allows_luma_export_roles(monkeypatch, role):
    uploaded = []

    class FakeLumaClient:
        def __init__(self, api_key, base_url):
            self.api_key = api_key
            self.base_url = base_url

        async def get_recent_ended_events(self, count):
            return [{"id": "evt-1", "name": "One"}]

        async def list_guests(self, event_id, approval_status):
            return [{"id": "gst-1"}]

        def build_attendee_csv(self, event, guests):
            return "event_id,guest_id\nevt-1,gst-1\n"

        def build_csv_filename(self, event):
            return "evt-1.csv"

    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {"UADMIN": role}
    FakeAdminLookupClient.unavailable = False
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)
    monkeypatch.setattr(
        slack_client_module,
        "upload_file",
        lambda **kwargs: uploaded.append(kwargs) or {"ok": True},
    )

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: FakeLumaClient),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result["data"]["action"] == "export_attendees"
    assert uploaded[0]["filename"] == "evt-1.csv"


@pytest.mark.asyncio
async def test_luma_executor_denies_missing_points_admin_record(monkeypatch):
    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {}
    FakeAdminLookupClient.unavailable = False
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UOTHER",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "admin`, `committee`, or `partner` role" in result


@pytest.mark.asyncio
async def test_luma_executor_denies_portfolio_lead(monkeypatch):
    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {"UPORTFOLIO": "portfolio_lead"}
    FakeAdminLookupClient.unavailable = False
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UPORTFOLIO",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "admin`, `committee`, or `partner` role" in result


@pytest.mark.asyncio
async def test_luma_executor_reports_missing_backend_url(monkeypatch):
    executor = SkillExecutor()
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )

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
async def test_luma_executor_reports_unavailable_admin_lookup(monkeypatch):
    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {"UADMIN": "admin"}
    FakeAdminLookupClient.unavailable = True
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "couldn't verify your Points Admin role" in result


@pytest.mark.asyncio
async def test_luma_executor_reports_missing_api_key(monkeypatch):
    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {"UADMIN": "admin"}
    FakeAdminLookupClient.unavailable = False
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: None),
        text="luma attendees",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "LUMA_API_KEY" in result


@pytest.mark.asyncio
async def test_luma_executor_uploads_csvs(monkeypatch):
    uploaded = []

    class FakeLumaClient:
        def __init__(self, api_key, base_url):
            self.api_key = api_key
            self.base_url = base_url

        async def get_recent_ended_events(self, count):
            assert count == 2
            return [
                {"id": "evt-1", "name": "One"},
                {"id": "evt-2", "name": "Two"},
            ]

        async def list_guests(self, event_id, approval_status):
            assert approval_status == "approved"
            return [{"id": f"gst-{event_id}"}]

        def build_attendee_csv(self, event, guests):
            return f"event_id,guest_id\n{event['id']},{guests[0]['id']}\n"

        def build_csv_filename(self, event):
            return f"{event['id']}.csv"

    executor = SkillExecutor()
    FakeAdminLookupClient.roles = {"UADMIN": "admin"}
    FakeAdminLookupClient.unavailable = False
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
            LUMA_API_KEY="test-key",
            LUMA_BASE_URL="https://luma.test",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeAdminLookupClient)
    monkeypatch.setattr(
        slack_client_module,
        "upload_file",
        lambda **kwargs: uploaded.append(kwargs) or {"ok": True},
    )

    result = await executor._execute_luma_events(
        skill=SimpleNamespace(get_client_class=lambda name: FakeLumaClient),
        text="give me CSVs for the past 2 MLAI events",
        params={},
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result["data"]["action"] == "export_attendees"
    assert len(uploaded) == 2
    assert uploaded[0]["channel"] == "C123"
    assert uploaded[0]["filename"] == "evt-1.csv"
    assert uploaded[0]["thread_ts"] == "111.222"
    assert "Uploaded 2 Luma attendee CSV files" in result["message"]
