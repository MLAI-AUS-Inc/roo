"""Aggregate-only live probe for the content-free unified Admin route."""

from __future__ import annotations

from typing import Awaitable, Callable

import httpx

from .admin_pilot_config import admin_pilot_config_report
from .backend_identity import BackendActorContext
from .clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError


Probe = Callable[[BackendActorContext], Awaitable[tuple[int, bool]]]


async def _live_probe(settings, router_token, context):
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        service_principal_key=router_token,
        surface="gateway",
        actor_context=context,
    )
    try:
        payload = await client.get_admin_routing_eligibility()
    except httpx.HTTPStatusError as exc:
        return int(exc.response.status_code), False
    except (MLAIBackendUnavailableError, httpx.HTTPError, ValueError):
        return 0, False
    return 200, bool(payload.get("admin_brain_eligible"))


async def unified_admin_route_smoke_report(
    settings,
    approval_manifest,
    *,
    organization_domain: str,
    slack_team_id: str,
    router_token: str,
    probe: Probe | None = None,
):
    """Prove allowed and denied route decisions without retrieving memory."""

    config_report = admin_pilot_config_report(
        settings,
        approval_manifest,
        organization_domain=organization_domain,
    )
    report = {
        "schema_version": "roo-unified-admin-route-smoke-v1",
        "ready": False,
        "blockers": [],
        "approval_manifest_hash": config_report.get("approval_manifest_hash", ""),
        "metrics": {
            "expected_allow_cases": 0,
            "eligible_cases": 0,
            "expected_deny_cases": 0,
            "denied_cases": 0,
        },
    }
    if not config_report["ready"]:
        report["blockers"].append("admin_pilot_config_invalid")
        return report
    if not str(router_token or "").startswith("mlai_sp_"):
        report["blockers"].append("router_credential_invalid")
        return report

    actor_ids = [value.split(":", 1)[1] for value in approval_manifest["pilot_admin_refs"]]
    contexts = []
    for value in approval_manifest["allowed_slack_contexts"]:
        kind, identifier = value.split(":", 1)
        if kind == "channel":
            contexts.append(identifier)
        elif kind == "dm":
            contexts.append("DPILOTROUTECHECK")

    case_number = 0
    live_probe = probe or (
        lambda context: _live_probe(settings, router_token, context)
    )

    async def decision(actor_id, channel_id):
        nonlocal case_number
        case_number += 1
        return await live_probe(
            BackendActorContext(
                slack_team_id=slack_team_id,
                acting_slack_user_id=actor_id,
                slack_channel_id=channel_id,
                slack_thread_ts="",
                event_id=f"EvROUTESMOKE{case_number}",
            )
        )

    allowed = [
        await decision(actor_id, context)
        for actor_id in actor_ids
        for context in contexts
    ]
    synthetic_actor = "UROUTESMOKEDENY"
    while synthetic_actor in actor_ids:
        synthetic_actor += "X"
    denied = []
    if contexts:
        denied.append(await decision(synthetic_actor, contexts[0]))
    for actor_id in actor_ids:
        denied.append(await decision(actor_id, "GROUTESMOKEDENY"))
        denied.append(await decision(actor_id, "CROUTESMOKEDENY"))

    report["metrics"] = {
        "expected_allow_cases": len(allowed),
        "eligible_cases": sum(status == 200 and eligible for status, eligible in allowed),
        "expected_deny_cases": len(denied),
        "denied_cases": sum(
            status in {401, 403} or (status == 200 and not eligible)
            for status, eligible in denied
        ),
    }
    if report["metrics"]["eligible_cases"] != len(allowed):
        report["blockers"].append("approved_route_failed")
    if report["metrics"]["denied_cases"] != len(denied):
        report["blockers"].append("denied_route_failed")
    report["blockers"] = sorted(set(report["blockers"]))
    report["ready"] = not report["blockers"]
    return report
