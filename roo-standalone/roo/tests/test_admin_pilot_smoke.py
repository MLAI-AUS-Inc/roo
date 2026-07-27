import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.admin_pilot_smoke import admin_pilot_signed_smoke_report
from roo.config import Settings


SERVICE_PRINCIPAL_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"


def configured_settings():
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN="xoxb-synthetic-admin",
        SLACK_SIGNING_SECRET="synthetic-admin-signing-secret",
        OPENAI_API_KEY="synthetic-openai-key",
        MLAI_BACKEND_URL="https://backend.test",
        ROO_SURFACE="admin",
        ROO_ENABLED_SKILLS="admin-brain",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
        ROO_ALLOWED_DM_USER_IDS="UADMIN123",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_ACTIONS_ENABLED=False,
        ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
    )


def approval_manifest():
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "organization_domain": "mlai.au",
        "approval_status": "approved",
        "approved_at": (now - timedelta(days=1)).isoformat(),
        "review_due_at": (now + timedelta(days=30)).isoformat(),
        "approvers": {
            "data": "Data Owner",
            "security": "Security Owner",
            "review": "Review Owner",
            "operations": "Operations Owner",
        },
        "pilot_admin_refs": ["slack:UADMIN123"],
        "allowed_slack_contexts": [
            "dm:UADMIN123",
            "channel:GADMIN123",
        ],
        "approved_providers": ["google_drive"],
        "approved_source_scopes": {
            "google_drive": ["folder:approved-root"],
        },
        "controls": {
            "data_processing_terms_approved": True,
            "retention_and_deletion_approved": True,
            "backup_restore_tested": True,
            "incident_response_runbook_approved": True,
            "freshness_latency_cost_slos_approved": True,
            "public_roo_isolation_verified": True,
        },
    }


@pytest.mark.asyncio
async def test_signed_smoke_checks_allow_deny_and_public_client_boundaries():
    calls = []

    async def probe(context):
        calls.append(context)
        if (
            context.acting_slack_user_id == "UADMIN123"
            and context.slack_channel_id
            in {"GADMIN123", "DPILOTSMOKECHECK"}
        ):
            return 200
        if context.acting_slack_user_id == "UPILOTSMOKEDENY":
            return 401
        return 403

    report = await admin_pilot_signed_smoke_report(
        configured_settings(),
        approval_manifest(),
        organization_domain="mlai.au",
        slack_team_id="TMLAI123",
        probe=probe,
    )

    assert report["ready"]
    assert report["metrics"] == {
        "expected_allow_cases": 2,
        "allowed_cases": 2,
        "expected_deny_cases": 4,
        "denied_cases": 4,
        "public_client_isolated": True,
    }
    assert len(calls) == 6
    rendered = str(report)
    assert "UADMIN123" not in rendered
    assert "GADMIN123" not in rendered
    assert "TMLAI123" not in rendered


@pytest.mark.asyncio
async def test_signed_smoke_fails_closed_on_wrong_allow_or_deny_status():
    async def probe(context):
        if context.slack_channel_id == "GADMIN123":
            return 403
        return 200

    report = await admin_pilot_signed_smoke_report(
        configured_settings(),
        approval_manifest(),
        organization_domain="mlai.au",
        slack_team_id="TMLAI123",
        probe=probe,
    )

    assert not report["ready"]
    assert "approved_signed_request_failed" in report["blockers"]
    assert "denied_signed_request_failed" in report["blockers"]


@pytest.mark.asyncio
async def test_signed_smoke_rejects_invalid_config_or_workspace_without_calls():
    calls = []

    async def probe(context):
        calls.append(context)
        return 200

    invalid_team = await admin_pilot_signed_smoke_report(
        configured_settings(),
        approval_manifest(),
        organization_domain="mlai.au",
        slack_team_id="not-a-team",
        probe=probe,
    )
    mismatched_config = await admin_pilot_signed_smoke_report(
        configured_settings(),
        approval_manifest(),
        organization_domain="other.test",
        slack_team_id="TMLAI123",
        probe=probe,
    )

    assert invalid_team["blockers"] == ["slack_team_id_invalid"]
    assert mismatched_config["blockers"] == ["admin_pilot_config_invalid"]
    assert calls == []
