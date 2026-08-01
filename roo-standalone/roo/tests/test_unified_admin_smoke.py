import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.config import Settings
from roo.unified_admin_smoke import unified_admin_route_smoke_report


SERVICE_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"


def _settings():
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN=None,
        SLACK_SIGNING_SECRET=None,
        OPENAI_API_KEY=None,
        MLAI_BACKEND_URL="https://backend.test",
        ROO_SURFACE="admin",
        ROO_ADMIN_INTERNAL_ONLY=True,
        ROO_ENABLED_SKILLS="admin-brain",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_API_KEY=SERVICE_TOKEN,
        ROO_ADMIN_DISPATCH_SECRET="dispatch-secret-" + ("s" * 32),
    )


def _manifest():
    now = datetime.now(timezone.utc)
    return {
        "organization_domain": "mlai.au",
        "approval_status": "approved",
        "review_due_at": (now + timedelta(days=30)).isoformat(),
        "pilot_admin_refs": ["slack:UADMIN123"],
        "allowed_slack_contexts": ["dm:UADMIN123", "channel:GADMIN123"],
    }


@pytest.mark.asyncio
async def test_route_smoke_accepts_only_eligible_private_cases():
    calls = []

    async def probe(context):
        calls.append(context)
        if context.acting_slack_user_id != "UADMIN123":
            return 401, False
        if context.slack_channel_id in {"GADMIN123", "DPILOTROUTECHECK"}:
            return 200, True
        return 200, False

    report = await unified_admin_route_smoke_report(
        _settings(),
        _manifest(),
        organization_domain="mlai.au",
        slack_team_id="TMLAI123",
        router_token=SERVICE_TOKEN,
        probe=probe,
    )

    assert report["ready"]
    assert report["metrics"] == {
        "expected_allow_cases": 2,
        "eligible_cases": 2,
        "expected_deny_cases": 3,
        "denied_cases": 3,
    }
    assert len(calls) == 5
    assert "UADMIN123" not in str(report)


@pytest.mark.asyncio
async def test_route_smoke_fails_if_denied_case_becomes_eligible():
    async def probe(context):
        return 200, True

    report = await unified_admin_route_smoke_report(
        _settings(),
        _manifest(),
        organization_domain="mlai.au",
        slack_team_id="TMLAI123",
        router_token=SERVICE_TOKEN,
        probe=probe,
    )

    assert not report["ready"]
    assert report["blockers"] == ["denied_route_failed"]
