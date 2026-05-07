import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

main_module = importlib.import_module("roo.main")


class RecordingAsyncClient:
    def __init__(self, *, status_code=200, json_data=None):
        self.status_code = status_code
        self.json_data = {} if json_data is None else json_data
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, json=None, headers=None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "json": json,
                "headers": headers,
            }
        )
        return httpx.Response(
            self.status_code,
            request=httpx.Request("POST", url),
            json=self.json_data,
        )


class FakeAdminClient:
    def __init__(self, *, is_admin):
        self._is_admin = is_admin
        self.calls = []

    async def is_admin(self, slack_user_id):
        self.calls.append(slack_user_id)
        return self._is_admin


@pytest.mark.asyncio
async def test_jobs_scheduler_trigger_uses_x_api_key(monkeypatch):
    recorder = RecordingAsyncClient(
        json_data={
            "run_id": "2026-05-05-deadbeef",
            "status": "queued",
            "status_url": "/api/v1/jobs/runs/2026-05-05-deadbeef",
        }
    )
    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda *args, **kwargs: recorder)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            JOBS_API_URL="https://api.mlai.au/api/v1",
            JOBS_TRIGGER_TOKEN="jobs-trigger-secret",
            JOBS_COLLECT_LIVE=True,
            JOBS_POST_TO_SLACK=True,
            JOBS_POST_TO_NOTION=False,
            JOBS_MAX_PAGES=2,
            JOBS_PER_KEYWORD_LIMIT=7,
        ),
    )

    result = await main_module._trigger_jobs_daily_run()

    assert result is True
    assert recorder.calls == [
        {
            "method": "POST",
            "url": "https://api.mlai.au/api/v1/jobs/daily-run",
            "json": {
                "collect_live": True,
                "post_to_slack": True,
                "post_to_notion": False,
                "max_pages": 2,
                "per_keyword_limit": 7,
            },
            "headers": {"X-API-Key": "jobs-trigger-secret"},
        }
    ]


@pytest.mark.asyncio
async def test_handle_mention_manual_jobs_trigger_for_admin(monkeypatch):
    posts = []
    backend_client = FakeAdminClient(is_admin=True)

    async def fake_trigger():
        return {
            "run_id": "2026-05-07-be9541ac",
            "status": "queued",
            "status_url": "/api/v1/jobs/runs/2026-05-07-be9541ac",
            "full_list_url": "https://api.mlai.au/api/v1/jobs/daily/2026-05-07",
        }

    monkeypatch.setattr(main_module, "_make_mlai_backend_client", lambda: backend_client)
    monkeypatch.setattr(main_module, "_trigger_jobs_daily_run_request", fake_trigger)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            JOBS_API_URL="https://api.mlai.au/api/v1",
            JOBS_POST_TO_SLACK=True,
            JOBS_POST_TO_NOTION=False,
        ),
    )
    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: posts.append(kwargs))
    monkeypatch.setattr(main_module, "get_agent", lambda: pytest.fail("agent should not be called"))

    await main_module._handle_mention(
        {
            "user": "UADMIN",
            "text": "run the daily jobs scrape now",
            "channel": "C123",
            "ts": "1234.5678",
        }
    )

    assert backend_client.calls == ["UADMIN"]
    assert len(posts) == 1
    assert posts[0]["channel"] == "C123"
    assert posts[0]["thread_ts"] == "1234.5678"
    assert "Queued the daily jobs run." in posts[0]["text"]
    assert "`2026-05-07-be9541ac`" in posts[0]["text"]
    assert "This usually takes a few minutes." in posts[0]["text"]
    assert "final jobs roundup separately" in posts[0]["text"]
    assert "Open run status" not in posts[0]["text"]


@pytest.mark.asyncio
async def test_handle_mention_manual_jobs_trigger_denies_non_admin(monkeypatch):
    posts = []
    backend_client = FakeAdminClient(is_admin=False)

    monkeypatch.setattr(main_module, "_make_mlai_backend_client", lambda: backend_client)
    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: posts.append(kwargs))
    monkeypatch.setattr(main_module, "get_agent", lambda: pytest.fail("agent should not be called"))

    await main_module._handle_mention(
        {
            "user": "UNOTADMIN",
            "text": "post today's AI and startup jobs now",
            "channel": "C999",
            "ts": "9999.0001",
        }
    )

    assert backend_client.calls == ["UNOTADMIN"]
    assert posts == [
        {
            "channel": "C999",
            "thread_ts": "9999.0001",
            "text": "Sorry mate, only Points Admins can run the daily jobs scrape manually.",
        }
    ]
