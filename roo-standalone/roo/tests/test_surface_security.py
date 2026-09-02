import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.config import Settings
from roo.config import get_settings
from roo import main as main_module
from roo.skills.loader import load_skills


BASE_SETTINGS = {
    "_env_file": None,
    "SLACK_BOT_TOKEN": "xoxb-synthetic",
    "SLACK_SIGNING_SECRET": "synthetic-signing-secret",
    "OPENAI_API_KEY": "synthetic-openai-key",
}
SERVICE_PRINCIPAL_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
DISPATCH_SECRET = "dispatch-secret-" + ("s" * 32)


def settings(**overrides):
    return Settings(**{**BASE_SETTINGS, **overrides})


def test_public_surface_preserves_reviewed_public_skills_and_has_no_private_skill():
    configured = settings()

    assert configured.ROO_SURFACE == "public"
    assert "mlai-points" in configured.enabled_skill_names
    assert "content-factory" in configured.enabled_skill_names
    assert "linear-channel-issues" in configured.enabled_skill_names
    assert not (configured.enabled_skill_names & configured.PRIVATE_SKILLS)
    assert configured.is_slack_context_allowed(
        channel_id="CANYWHERE",
        user_id="UANYONE",
        channel_type="channel",
    )


@pytest.mark.parametrize("retention_days", [0, 366])
def test_coworking_intent_retention_is_bounded(retention_days):
    with pytest.raises(
        ValidationError,
        match="COWORKING_INTENT_RETENTION_DAYS must be between 1 and 365",
    ):
        settings(COWORKING_INTENT_RETENTION_DAYS=retention_days)

    configured = settings(COWORKING_INTENT_RETENTION_DAYS=30)
    assert configured.COWORKING_INTENT_RETENTION_DAYS == 30


def test_founder_tools_link_origins_are_validated_by_environment():
    configured = settings(
        FOUNDER_ACCOUNT_LINK_ENABLED=True,
        FOUNDER_TOOLS_LINK_ORIGINS="https://mlai.au",
    )
    assert configured.founder_tools_link_origins == frozenset({"https://mlai.au"})

    local = settings(
        FOUNDER_TOOLS_LINK_ORIGINS="http://localhost:3000",
        ROO_ENVIRONMENT="development",
    )
    assert local.founder_tools_link_origins == frozenset({"http://localhost:3000"})

    with pytest.raises(ValidationError, match="localhost origins"):
        settings(
            FOUNDER_TOOLS_LINK_ORIGINS="http://localhost:3000",
            ROO_ENVIRONMENT="production",
        )
    with pytest.raises(ValidationError, match="must use HTTPS"):
        settings(FOUNDER_TOOLS_LINK_ORIGINS="http://mlai.au")

    with pytest.raises(ValidationError, match="invalid origin"):
        settings(FOUNDER_TOOLS_LINK_ORIGINS="https://mlai.au/path")

    with pytest.raises(ValidationError, match="allowed origin in production"):
        settings(
            ROO_ENVIRONMENT="production",
            FOUNDER_ACCOUNT_LINK_ENABLED=True,
            FOUNDER_TOOLS_LINK_ORIGINS="",
        )


def test_founder_link_action_is_fail_closed_until_enabled():
    disabled = settings()
    enabled = settings(FOUNDER_ACCOUNT_LINK_ENABLED=True)

    assert "mlai-points:link_founder_account" not in disabled.implicit_action_allowlist
    assert "mlai-points:link_founder_account" in enabled.implicit_action_allowlist
    assert "mlai-points:link_account" not in enabled.implicit_action_allowlist


def test_linear_channel_issue_writes_fail_closed_without_public_backend_credentials():
    assert not settings().LINEAR_CHANNEL_ISSUE_WRITES_ENABLED
    with pytest.raises(ValidationError, match="MLAI_BACKEND_URL"):
        settings(LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=True, ROO_API_KEY="roo-key")
    with pytest.raises(ValidationError, match="ROO_API_KEY"):
        settings(
            LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=True,
            MLAI_BACKEND_URL="https://backend.test",
        )
    with pytest.raises(ValidationError, match="only on Public Roo"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=True,
            MLAI_BACKEND_URL="https://backend.test",
            ROO_API_KEY="roo-key",
        )


def test_victor_ai_skill_is_disabled_by_default_and_uses_channel_name_only():
    configured = settings()
    assert "victor-ai-applications" not in configured.enabled_skill_names

    configured = settings(
        VICTOR_AI_SKILL_ENABLED=True,
        MLAI_BACKEND_URL="https://backend.test",
        VICTOR_AI_ROO_SIGNING_SECRET="s" * 48,
    )
    assert "victor-ai-applications" in configured.enabled_skill_names
    assert configured.is_victor_ai_context_allowed(
        channel_name="exp-victor-ai",
    )
    assert not configured.is_victor_ai_context_allowed(
        channel_name="general",
    )


def test_victor_ai_skill_fails_closed_for_disabled_or_invalid_configuration():
    with pytest.raises(ValidationError, match="cannot be enabled"):
        settings(ROO_ENABLED_SKILLS="victor-ai-applications")

    with pytest.raises(ValidationError, match="SLACK_CHANNEL_NAME is invalid"):
        settings(
            VICTOR_AI_SKILL_ENABLED=True,
            MLAI_BACKEND_URL="https://backend.test",
            VICTOR_AI_ROO_SIGNING_SECRET="s" * 48,
            VICTOR_AI_SLACK_CHANNEL_NAME="not a slack channel",
        )

    with pytest.raises(ValidationError, match="only on Public Roo"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_ENABLED_SKILLS="victor-ai-applications",
            VICTOR_AI_SKILL_ENABLED=True,
            MLAI_BACKEND_URL="https://backend.test",
            VICTOR_AI_ROO_SIGNING_SECRET="s" * 48,
        )


def test_boost_post_moderation_is_disabled_by_default_and_fails_closed():
    configured = settings()
    assert not configured.BOOST_POST_MODERATION_ENABLED
    assert not configured.BOOST_POST_AUTO_DELETE_ENABLED

    with pytest.raises(ValidationError, match="requires BOOST_LINK_LOVE_CHANNEL_ID"):
        settings(
            BOOST_POST_MODERATION_ENABLED=True,
            BOOST_POST_ENFORCEMENT_CUTOFF_TS="1800000000.000000",
            MLAI_BACKEND_URL="https://backend.test",
        )

    with pytest.raises(ValidationError, match="requires SLACK_MODERATOR_USER_TOKEN"):
        settings(
            BOOST_POST_MODERATION_ENABLED=True,
            BOOST_POST_AUTO_DELETE_ENABLED=True,
            BOOST_LINK_LOVE_CHANNEL_ID="CBOOST123",
            BOOST_POST_ENFORCEMENT_CUTOFF_TS="1800000000.000000",
            MLAI_BACKEND_URL="https://backend.test",
        )

    configured = settings(
        BOOST_POST_MODERATION_ENABLED=True,
        BOOST_POST_AUTO_DELETE_ENABLED=True,
        BOOST_LINK_LOVE_CHANNEL_ID="CBOOST123",
        BOOST_POST_ENFORCEMENT_CUTOFF_TS="1800000000.000000",
        MLAI_BACKEND_URL="https://backend.test",
        SLACK_MODERATOR_USER_TOKEN="xoxp-synthetic",
        SLACK_MODERATOR_USER_ID="UADMIN123",
        SLACK_MODERATOR_TEAM_ID="TTEAM123",
    )
    assert configured.BOOST_POST_AUTO_DELETE_ENABLED


@pytest.mark.parametrize(
    "overrides",
    (
        {"ORG_BRAIN_ENABLED": True},
        {"ORG_BRAIN_API_KEY": "private-key"},
        {"ROO_ENABLED_SKILLS": "mlai-points admin-brain"},
    ),
)
def test_public_surface_rejects_every_private_brain_configuration(overrides):
    with pytest.raises(ValidationError, match="Public Roo"):
        settings(**overrides)


def test_public_surface_accepts_only_route_scoped_unified_admin_configuration():
    configured = settings(
        MLAI_BACKEND_URL="https://backend.test",
        ROO_UNIFIED_ADMIN_ROUTING_ENABLED=True,
        ORG_BRAIN_ROUTER_API_KEY=SERVICE_PRINCIPAL_TOKEN,
        ROO_ADMIN_INTERNAL_URL="http://roo-admin:8000",
        ROO_ADMIN_DISPATCH_SECRET=DISPATCH_SECRET,
    )

    assert configured.ROO_SURFACE == "public"
    assert configured.ROO_UNIFIED_ADMIN_ROUTING_ENABLED is True
    assert configured.ORG_BRAIN_API_KEY is None
    assert "admin-brain" not in configured.enabled_skill_names

    with pytest.raises(ValidationError, match="require ROO_UNIFIED"):
        settings(ORG_BRAIN_ROUTER_API_KEY=SERVICE_PRINCIPAL_TOKEN)


def test_internal_admin_worker_has_no_slack_or_public_runtime_credentials():
    configured = Settings(
        _env_file=None,
        SLACK_BOT_TOKEN=None,
        SLACK_SIGNING_SECRET=None,
        OPENAI_API_KEY=None,
        ROO_ENVIRONMENT="production",
        ROO_SURFACE="admin",
        FOUNDER_TOOLS_LINK_ORIGINS="",
        ROO_ADMIN_INTERNAL_ONLY=True,
        ROO_ENABLED_SKILLS="admin-brain",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
        MLAI_BACKEND_URL="https://backend.test",
        ROO_ADMIN_DISPATCH_SECRET=DISPATCH_SECRET,
    )

    from roo.config import validate_runtime_security

    validate_runtime_security(configured)
    assert configured.SLACK_BOT_TOKEN is None
    assert configured.OPENAI_API_KEY is None
    assert configured.allowed_channel_ids == frozenset()

    with pytest.raises(ValidationError, match="must not receive Slack"):
        Settings(
            _env_file=None,
            SLACK_SIGNING_SECRET=None,
            OPENAI_API_KEY=None,
            ROO_SURFACE="admin",
            ROO_ADMIN_INTERNAL_ONLY=True,
            ROO_ENABLED_SKILLS="admin-brain",
            ORG_BRAIN_ENABLED=True,
            ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
            ROO_ADMIN_DISPATCH_SECRET=DISPATCH_SECRET,
            SLACK_BOT_TOKEN="xoxb-forbidden",
        )


def test_admin_surface_requires_an_explicit_slack_context_allowlist():
    with pytest.raises(ValidationError, match="requires ROO_ALLOWED_CHANNEL_IDS"):
        settings(ROO_SURFACE="admin")


def test_admin_development_surface_starts_with_no_skills_or_brain_access():
    configured = settings(
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
        ROO_ALLOWED_DM_USER_IDS="UADMIN123",
    )

    assert configured.enabled_skill_names == frozenset()
    assert not configured.ORG_BRAIN_ENABLED
    assert configured.ROO_CONTEXTUAL_SHADOW_MODE is False
    assert configured.is_slack_context_allowed(
        channel_id="GADMIN123",
        user_id="UOTHER123",
        channel_type="group",
    )
    assert configured.is_slack_context_allowed(
        channel_id="DYNAMIC123",
        user_id="UADMIN123",
        channel_type="im",
    )
    assert not configured.is_slack_context_allowed(
        channel_id="CPUBLIC123",
        user_id="UOTHER123",
        channel_type="channel",
    )


def test_admin_surface_rejects_public_and_direct_message_channel_ids():
    for channel_id in ("CPUBLIC123", "DPRIVATE123"):
        with pytest.raises(ValidationError, match="allowlists contain invalid"):
            settings(
                ROO_SURFACE="admin",
                ROO_ALLOWED_CHANNEL_IDS=channel_id,
            )


def test_admin_surface_rejects_contextual_shadow_mode():
    with pytest.raises(ValidationError, match="cannot enable contextual shadow mode"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_CONTEXTUAL_SHADOW_MODE=True,
        )


def test_admin_brain_requires_scoped_key_and_explicit_skill():
    with pytest.raises(ValidationError, match="ORG_BRAIN_API_KEY"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ORG_BRAIN_ENABLED=True,
            ROO_ENABLED_SKILLS="admin-brain",
        )

    with pytest.raises(ValidationError, match="requires admin-brain"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ORG_BRAIN_ENABLED=True,
            ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
            ROO_ENABLED_SKILLS="tone-of-voice",
        )

    with pytest.raises(ValidationError, match="scoped service-principal"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ORG_BRAIN_ENABLED=True,
            ORG_BRAIN_API_KEY="legacy-shared-api-key",
            ROO_ENABLED_SKILLS="admin-brain",
        )


def test_admin_actions_require_explicit_flag_skill_and_brain_access():
    with pytest.raises(ValidationError, match="cannot be enabled"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_ENABLED_SKILLS="admin-actions",
        )

    with pytest.raises(ValidationError, match="require ORG_BRAIN_ENABLED"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_ENABLED_SKILLS="admin-actions",
            ORG_BRAIN_ACTIONS_ENABLED=True,
        )

    with pytest.raises(ValidationError, match="requires admin-actions"):
        settings(
            ROO_SURFACE="admin",
            ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
            ROO_ENABLED_SKILLS="admin-brain",
            ORG_BRAIN_ENABLED=True,
            ORG_BRAIN_ACTIONS_ENABLED=True,
            ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
        )

    configured = settings(
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
        ROO_ENABLED_SKILLS="admin-brain admin-actions",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_ACTIONS_ENABLED=True,
        ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
    )
    assert {"admin-brain", "admin-actions"}.issubset(
        configured.enabled_skill_names
    )


def _write_skill(root: Path, directory_name: str, skill_name: str, client_body: str = ""):
    directory = root / directory_name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_name}\n"
        f"description: Synthetic {skill_name}\n"
        "---\n"
        "Synthetic test instructions.\n",
        encoding="utf-8",
    )
    if client_body:
        (directory / "client.py").write_text(client_body, encoding="utf-8")


def test_disallowed_skill_client_is_not_imported(tmp_path):
    marker = tmp_path / "private-client-imported"
    _write_skill(tmp_path, "public", "public-skill")
    _write_skill(
        tmp_path,
        "private",
        "admin-brain",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
    )

    loaded = load_skills(tmp_path, allowed_names={"public-skill"})

    assert [skill.name for skill in loaded] == ["public-skill"]
    assert not marker.exists()


@pytest.mark.parametrize(
    "path",
    (
        "/api/sim-patient",
        "/api/diagnosis-check",
        "/api/callbacks/content-factory",
    ),
)
def test_admin_surface_hides_public_only_http_capabilities(path):
    configured = settings(
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
    )
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    try:
        response = TestClient(main_module.app).post(path, json={})
    finally:
        main_module.app.dependency_overrides.clear()

    assert response.status_code == 404


def test_admin_readiness_reports_the_enforced_runtime_shape(monkeypatch):
    configured = settings(
        ROO_SURFACE="admin",
        ROO_ALLOWED_CHANNEL_IDS="GADMIN123",
        ROO_ENABLED_SKILLS="admin-brain",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_ACTIONS_ENABLED=False,
        ORG_BRAIN_API_KEY=SERVICE_PRINCIPAL_TOKEN,
        ROO_CONTEXTUAL_SHADOW_MODE=False,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(
        main_module.app.state,
        "startup_complete",
        True,
        raising=False,
    )

    payload = asyncio.run(main_module.readiness_check())

    assert payload["surface"] == "admin"
    assert payload["enabled_skills"] == ["admin-brain"]
    assert payload["org_brain_enabled"] is True
    assert payload["org_brain_actions_enabled"] is False
    assert payload["contextual_shadow_mode"] is False


def test_public_readiness_degrades_with_required_retry_worker(monkeypatch):
    configured = settings(ROO_SURFACE="public")
    monkeypatch.setattr(main_module, "get_settings", lambda: configured)
    monkeypatch.setattr(main_module.app.state, "startup_complete", True, raising=False)
    monkeypatch.setattr(
        main_module.app.state,
        "coworking_retry_health",
        {
            "status": "degraded",
            "consecutive_failures": 3,
            "last_error_type": "OperationalError",
        },
        raising=False,
    )

    response = asyncio.run(main_module.readiness_check())
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload == {
        "status": "not_ready",
        "service": "roo",
        "component": "coworking_booking_retry_worker",
        "component_status": "degraded",
        "consecutive_failures": 3,
    }
