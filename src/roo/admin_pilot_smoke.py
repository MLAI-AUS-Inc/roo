"""Aggregate-only signed-request smoke gate for an active Admin Roo pilot."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Mapping

import httpx

from .admin_pilot_config import (
    PUBLIC_PILOT_ADMIN_CONTEXT,
    admin_pilot_config_report,
)
from .backend_identity import (
    BackendActorContext,
    BackendIdentityError,
)
from .clients.mlai_backend import (
    MLAIBackendClient,
    MLAIBackendUnavailableError,
)


Probe = Callable[[BackendActorContext], Awaitable[int]]
_TEAM_RE = re.compile(r"^T[A-Z0-9]{1,63}$")


async def _live_probe(settings, context: BackendActorContext) -> int:
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        service_principal_key=settings.ORG_BRAIN_API_KEY,
        surface="admin",
        actor_context=context,
    )
    try:
        await client.get_org_memory_pilot_access_probe()
    except httpx.HTTPStatusError as exc:
        return int(exc.response.status_code)
    except (
        BackendIdentityError,
        MLAIBackendUnavailableError,
        httpx.HTTPError,
        ValueError,
    ):
        return 0
    return 200


def _actor_context(
    *,
    slack_team_id: str,
    actor_id: str,
    channel_id: str,
    case_number: int,
) -> BackendActorContext:
    return BackendActorContext(
        slack_team_id=slack_team_id,
        acting_slack_user_id=actor_id,
        slack_channel_id=channel_id,
        slack_thread_ts="",
        event_id=f"EvPILOTSMOKE{case_number}",
    )


async def admin_pilot_signed_smoke_report(
    settings,
    approval_manifest: Mapping[str, Any],
    *,
    organization_domain: str,
    slack_team_id: str,
    probe: Probe | None = None,
) -> dict[str, Any]:
    """Exercise approved and denied signed paths without sending a query."""

    config_report = admin_pilot_config_report(
        settings,
        approval_manifest,
        organization_domain=organization_domain,
    )
    report = {
        "schema_version": "admin-roo-pilot-signed-smoke-v1",
        "ready": False,
        "blockers": [],
        "approval_manifest_hash": config_report.get(
            "approval_manifest_hash",
            "",
        ),
        "metrics": {
            "expected_allow_cases": 0,
            "allowed_cases": 0,
            "expected_deny_cases": 0,
            "denied_cases": 0,
            "public_client_isolated": False,
        },
    }
    if not config_report["ready"]:
        report["blockers"].append("admin_pilot_config_invalid")
        return report
    if not _TEAM_RE.fullmatch(str(slack_team_id or "")):
        report["blockers"].append("slack_team_id_invalid")
        return report

    actor_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["pilot_admin_refs"]
    ]
    private_channel_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["allowed_slack_contexts"]
        if reference.startswith("channel:")
    ]
    dm_actor_ids = [
        reference.split(":", 1)[1]
        for reference in approval_manifest["allowed_slack_contexts"]
        if reference.startswith("dm:")
    ]
    public_channels_for_pilot_admins = (
        PUBLIC_PILOT_ADMIN_CONTEXT
        in approval_manifest["allowed_slack_contexts"]
    )
    probe_request = probe or (
        lambda context: _live_probe(settings, context)
    )
    case_number = 0

    async def status_for(actor_id: str, channel_id: str) -> int:
        nonlocal case_number
        case_number += 1
        return await probe_request(
            _actor_context(
                slack_team_id=slack_team_id,
                actor_id=actor_id,
                channel_id=channel_id,
                case_number=case_number,
            )
        )

    allowed_statuses = [
        await status_for(actor_id, channel_id)
        for actor_id in actor_ids
        for channel_id in private_channel_ids
    ]
    for actor_id in dm_actor_ids:
        allowed_statuses.append(
            await status_for(actor_id, "DPILOTSMOKECHECK")
        )
    if public_channels_for_pilot_admins:
        for actor_id in actor_ids:
            allowed_statuses.append(
                await status_for(actor_id, "CPILOTSMOKECHECK")
            )

    synthetic_actor_id = "UPILOTSMOKEDENY"
    while synthetic_actor_id in actor_ids:
        synthetic_actor_id += "X"
    synthetic_private_channel_id = "GPILOTSMOKEDENY"
    while synthetic_private_channel_id in private_channel_ids:
        synthetic_private_channel_id += "X"
    synthetic_public_channel_id = "CPILOTSMOKEDENY"

    denied_statuses = [
        await status_for(synthetic_actor_id, channel_id)
        for channel_id in private_channel_ids
    ]
    for _actor_id in dm_actor_ids:
        denied_statuses.append(
            await status_for(synthetic_actor_id, "DPILOTSMOKEDENY")
        )
    for actor_id in actor_ids:
        denied_statuses.append(
            await status_for(actor_id, synthetic_private_channel_id)
        )
        if not public_channels_for_pilot_admins:
            denied_statuses.append(
                await status_for(actor_id, synthetic_public_channel_id)
            )
    if public_channels_for_pilot_admins:
        denied_statuses.append(
            await status_for(
                synthetic_actor_id,
                synthetic_public_channel_id,
            )
        )

    approved_context = (
        private_channel_ids[0]
        if private_channel_ids
        else (
            "DPILOTSMOKECHECK"
            if dm_actor_ids
            else "CPILOTSMOKECHECK"
        )
    )
    approved_actor = (
        actor_ids[0]
        if private_channel_ids or public_channels_for_pilot_admins
        else dm_actor_ids[0]
    )
    public_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        service_principal_key=settings.ORG_BRAIN_API_KEY,
        surface="public",
        actor_context=_actor_context(
            slack_team_id=slack_team_id,
            actor_id=approved_actor,
            channel_id=approved_context,
            case_number=case_number + 1,
        ),
    )
    try:
        public_client.org_memory_headers("roo-pilot-smoke-public-deny")
    except BackendIdentityError:
        public_client_isolated = True
    else:
        public_client_isolated = False

    report["metrics"].update(
        {
            "expected_allow_cases": len(allowed_statuses),
            "allowed_cases": sum(
                status == 200 for status in allowed_statuses
            ),
            "expected_deny_cases": len(denied_statuses),
            "denied_cases": sum(
                status in {401, 403} for status in denied_statuses
            ),
            "public_client_isolated": public_client_isolated,
        }
    )
    if any(status != 200 for status in allowed_statuses):
        report["blockers"].append("approved_signed_request_failed")
    if any(status not in {401, 403} for status in denied_statuses):
        report["blockers"].append("denied_signed_request_failed")
    if not public_client_isolated:
        report["blockers"].append("public_client_isolation_failed")

    report["blockers"] = sorted(set(report["blockers"]))
    report["ready"] = not report["blockers"]
    return report
