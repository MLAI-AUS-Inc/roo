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
