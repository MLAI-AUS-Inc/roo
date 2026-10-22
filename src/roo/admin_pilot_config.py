"""Content-free validation of an Admin Roo deployment against pilot approval."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


_ACTOR_RE = re.compile(r"^slack:[UW][A-Z0-9]{1,63}$")
PUBLIC_PILOT_ADMIN_CONTEXT = "public_channels:pilot_admins"
_CONTEXT_RE = re.compile(
    r"^(?:dm:[UW][A-Z0-9]{1,63}|channel:G[A-Z0-9]{1,63}|"
    r"public_channels:pilot_admins)$"
)


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        manifest,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def admin_pilot_config_report(
    settings,
    approval_manifest: Mapping[str, Any],
    *,
    organization_domain: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare effective Roo settings with an exact pilot manifest."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[str] = []
    manifest = (
        approval_manifest
        if isinstance(approval_manifest, Mapping)
        else {}
    )

    if getattr(settings, "ROO_SURFACE", "") != "admin":
        blockers.append("roo_surface_not_admin")
    internal_worker = bool(getattr(settings, "ROO_ADMIN_INTERNAL_ONLY", False))
    if not internal_worker:
        blockers.append("admin_worker_not_internal_only")
    if not bool(getattr(settings, "ORG_BRAIN_ENABLED", False)):
        blockers.append("admin_brain_not_enabled")
    if bool(getattr(settings, "ORG_BRAIN_ACTIONS_ENABLED", False)):
        blockers.append("admin_actions_must_remain_disabled")
    if bool(getattr(settings, "ROO_CONTEXTUAL_SHADOW_MODE", False)):
        blockers.append("admin_shadow_mode_must_remain_disabled")
    if set(getattr(settings, "enabled_skill_names", ())) != {"admin-brain"}:
        blockers.append("admin_skill_allowlist_not_exact")

    if manifest.get("approval_status") != "approved":
        blockers.append("approval_not_current")
    review_due_at = _timestamp(manifest.get("review_due_at"))
    if review_due_at is None or review_due_at <= now:
        blockers.append("approval_not_current")
    if (
        str(manifest.get("organization_domain") or "").strip().casefold()
        != str(organization_domain or "").strip().casefold()
    ):
        blockers.append("approval_organization_mismatch")

    actor_refs = manifest.get("pilot_admin_refs")
    contexts = manifest.get("allowed_slack_contexts")
    if (
        not isinstance(actor_refs, list)
        or not 1 <= len(actor_refs) <= 3
        or len(set(actor_refs)) != len(actor_refs)
        or any(
            not isinstance(value, str) or not _ACTOR_RE.fullmatch(value)
            for value in actor_refs
        )
    ):
        blockers.append("approval_pilot_actors_invalid")
        actor_refs = []
    if (
        not isinstance(contexts, list)
        or not contexts
        or len(set(contexts)) != len(contexts)
        or any(
            not isinstance(value, str) or not _CONTEXT_RE.fullmatch(value)
            for value in contexts
        )
    ):
        blockers.append("approval_private_contexts_invalid")
        contexts = []

    approved_actor_ids = {
        value.split(":", 1)[1]
        for value in actor_refs
        if isinstance(value, str) and ":" in value
    }
    approved_channels = {
        value.split(":", 1)[1]
        for value in contexts
        if isinstance(value, str) and value.startswith("channel:")
    }
    approved_dm_users = {
        value.split(":", 1)[1]
        for value in contexts
        if isinstance(value, str) and value.startswith("dm:")
    }
    if not approved_dm_users.issubset(approved_actor_ids):
        blockers.append("approval_dm_actor_mismatch")
    # The backend pilot manifest is authoritative for the single-app design.
    # The internal worker has no Slack ingress and therefore carries no local
    # Slack actor/channel allowlist to drift from that backend-owned policy.
    if not internal_worker:
        if set(getattr(settings, "allowed_channel_ids", ())) != approved_channels:
            blockers.append("roo_channel_allowlist_mismatch")
        if set(getattr(settings, "allowed_dm_user_ids", ())) != approved_dm_users:
            blockers.append("roo_dm_allowlist_mismatch")

    blockers = sorted(set(blockers))
    return {
        "schema_version": "admin-roo-pilot-config-v1",
        "ready": not blockers,
        "blockers": blockers,
        "approval_manifest_hash": (
            _manifest_hash(manifest) if manifest else ""
        ),
        "approved_actor_count": len(approved_actor_ids),
        "approved_channel_count": len(approved_channels),
        "approved_dm_actor_count": len(approved_dm_users),
    }
