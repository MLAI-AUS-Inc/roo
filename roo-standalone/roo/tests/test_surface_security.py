import asyncio
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


def settings(**overrides):
    return Settings(**{**BASE_SETTINGS, **overrides})


def test_public_surface_preserves_reviewed_public_skills_and_has_no_private_skill():
    configured = settings()

    assert configured.ROO_SURFACE == "public"
    assert "mlai-points" in configured.enabled_skill_names
    assert "content-factory" in configured.enabled_skill_names
    assert not (configured.enabled_skill_names & configured.PRIVATE_SKILLS)
    assert configured.is_slack_context_allowed(
        channel_id="CANYWHERE",
        user_id="UANYONE",
        channel_type="channel",
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
