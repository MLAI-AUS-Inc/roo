import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.admin_pilot_config import admin_pilot_config_report
from roo.config import Settings


SERVICE_PRINCIPAL_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"


def configured_settings(**overrides):
    values = {
        "_env_file": None,
        "SLACK_BOT_TOKEN": "xoxb-synthetic-admin",
        "SLACK_SIGNING_SECRET": "synthetic-admin-signing-secret",
        "OPENAI_API_KEY": "synthetic-openai-key",
        "ROO_SURFACE": "admin",
        "ROO_ENABLED_SKILLS": "admin-brain",
        "ROO_ALLOWED_CHANNEL_IDS": "GADMIN123",
        "ROO_ALLOWED_DM_USER_IDS": "UADMIN123",
        "ORG_BRAIN_ENABLED": True,
        "ORG_BRAIN_ACTIONS_ENABLED": False,
        "ORG_BRAIN_API_KEY": SERVICE_PRINCIPAL_TOKEN,
    }
    return Settings(**{**values, **overrides})


def approval_manifest(now):
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


def test_exact_private_binding_is_ready_and_content_free():
    now = datetime.now(timezone.utc)
    report = admin_pilot_config_report(
        configured_settings(),
        approval_manifest(now),
        organization_domain="mlai.au",
        now=now,
    )

    assert report["ready"]
    assert report["blockers"] == []
    assert "UADMIN123" not in str(report)
    assert "GADMIN123" not in str(report)


def test_allowlist_mismatch_and_actions_fail_closed():
    now = datetime.now(timezone.utc)
    report = admin_pilot_config_report(
        configured_settings(
            ROO_ALLOWED_CHANNEL_IDS="GOTHER123",
            ORG_BRAIN_ACTIONS_ENABLED=True,
            ROO_ENABLED_SKILLS="admin-brain admin-actions",
        ),
        approval_manifest(now),
        organization_domain="mlai.au",
        now=now,
    )

    assert not report["ready"]
    assert "admin_actions_must_remain_disabled" in report["blockers"]
    assert "admin_skill_allowlist_not_exact" in report["blockers"]
    assert "roo_channel_allowlist_mismatch" in report["blockers"]


def test_shadow_mode_fails_the_production_config_gate():
    now = datetime.now(timezone.utc)
    configured = configured_settings()
    object.__setattr__(configured, "ROO_CONTEXTUAL_SHADOW_MODE", True)

    report = admin_pilot_config_report(
        configured,
        approval_manifest(now),
        organization_domain="mlai.au",
        now=now,
    )

    assert not report["ready"]
    assert "admin_shadow_mode_must_remain_disabled" in report["blockers"]


def test_public_channel_approval_and_expiry_fail_closed():
    now = datetime.now(timezone.utc)
    manifest = approval_manifest(now)
    manifest["allowed_slack_contexts"] = ["channel:CPUBLIC123"]
    manifest["review_due_at"] = (now - timedelta(seconds=1)).isoformat()

    report = admin_pilot_config_report(
        configured_settings(
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_ALLOWED_DM_USER_IDS="",
        ),
        manifest,
        organization_domain="mlai.au",
        now=now,
    )

    assert not report["ready"]
    assert "approval_not_current" in report["blockers"]
    assert "approval_private_contexts_invalid" in report["blockers"]
