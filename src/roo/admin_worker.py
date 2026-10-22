"""Private, internal-only Admin Brain worker for the single @Roo Slack app."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .admin_brain import ADMIN_BRAIN_FEEDBACK_ACTIONS
from .admin_dispatch import (
    AdminDispatchError,
    actor_context_from_dispatch,
    verify_and_claim_admin_dispatch,
)
from .backend_identity import use_backend_actor_context
from .clients.mlai_backend import MLAIBackendClient
from .config import get_settings, validate_runtime_security
from .skills.executor import SkillExecutor


def _settings_for_worker():
    settings = get_settings()
    if settings.ROO_SURFACE != "admin" or not settings.ROO_ADMIN_INTERNAL_ONLY:
        raise RuntimeError("Admin worker requires the internal Admin surface")
    if not settings.ORG_BRAIN_ENABLED or settings.ORG_BRAIN_ACTIONS_ENABLED:
        raise RuntimeError("Admin worker requires live read-only Admin Brain")
    if settings.enabled_skill_names != frozenset({"admin-brain"}):
        raise RuntimeError("Admin worker may load only admin-brain")
    validate_runtime_security(settings)
    return settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    _settings_for_worker()
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False


app = FastAPI(
    title="Roo Admin Worker",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


async def _verified_dispatch(request: Request, *, expected_kind: str):
    settings = _settings_for_worker()
    try:
        envelope = verify_and_claim_admin_dispatch(
            secret=settings.ROO_ADMIN_DISPATCH_SECRET,
            signature=request.headers.get("X-Roo-Dispatch-Signature", ""),
            raw_body=await request.body(),
            receipt_db_path=settings.ROO_ADMIN_DISPATCH_RECEIPTS_DB_PATH,
            max_age_seconds=settings.ROO_ADMIN_DISPATCH_MAX_AGE_SECONDS,
            receipt_ttl_seconds=settings.ROO_ADMIN_DISPATCH_RECEIPT_TTL_SECONDS,
        )
    except AdminDispatchError as exc:
        raise HTTPException(status_code=403, detail="unauthorized") from exc
    if envelope.get("kind") != expected_kind:
        raise HTTPException(status_code=400, detail="dispatch kind mismatch")
    return settings, envelope, actor_context_from_dispatch(envelope)


@app.get("/healthz/ready")
async def readiness():
    if not getattr(app.state, "ready", False):
        return JSONResponse({"status": "not_ready"}, status_code=503)
    settings = _settings_for_worker()
    return {
        "status": "ok",
        "service": "roo-admin-worker",
        "surface": "admin",
        "internal_only": True,
        "enabled_skills": sorted(settings.enabled_skill_names),
        "org_brain_enabled": settings.ORG_BRAIN_ENABLED,
        "org_brain_actions_enabled": settings.ORG_BRAIN_ACTIONS_ENABLED,
    }


@app.post("/internal/admin/query")
async def query(request: Request):
    settings, envelope, context = await _verified_dispatch(
        request,
        expected_kind="query",
    )
    payload = envelope["payload"]
    text = str(payload.get("text") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="query is invalid")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="query parameters are invalid")
    executor = SkillExecutor()
    with use_backend_actor_context(context):
        result = await executor._execute_admin_brain(
            text=text,
            params=params,
            user_id=context.acting_slack_user_id,
            channel_id=context.slack_channel_id,
            thread_ts=context.slack_thread_ts or None,
        )
    return {
        "result": result,
        "destination": {
            "channel_id": context.slack_channel_id,
            "thread_ts": context.slack_thread_ts,
            "requester_user_id": context.acting_slack_user_id,
        },
    }


@app.post("/internal/admin/feedback")
async def feedback(request: Request):
    settings, envelope, context = await _verified_dispatch(
        request,
        expected_kind="feedback",
    )
    payload = envelope["payload"]
    feedback_type = str(payload.get("feedback_type") or "").strip()
    allowed_types = set(ADMIN_BRAIN_FEEDBACK_ACTIONS.values()) | {"incorrect"}
    if feedback_type not in allowed_types:
        raise HTTPException(status_code=400, detail="feedback type is invalid")
    if str(payload.get("requester_user_id") or "") != context.acting_slack_user_id:
        raise HTTPException(status_code=403, detail="unauthorized")
    query_id = str(payload.get("query_id") or "").strip()
    claim_id = str(payload.get("claim_id") or "").strip() or None
    correction_text = str(payload.get("correction_text") or "").strip()
    if not query_id or (feedback_type == "incorrect" and (not claim_id or not correction_text)):
        raise HTTPException(status_code=400, detail="feedback payload is invalid")

    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        service_principal_key=settings.ORG_BRAIN_API_KEY,
        surface="admin",
        actor_context=context,
    )
    try:
        await client.submit_org_memory_feedback(
            query_id=query_id,
            feedback_type=feedback_type,
            claim_id=claim_id,
            correction_text=correction_text or None,
            timeout=float(settings.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS),
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="feedback unavailable") from exc

    message = {
        "relevant": "Thanks — I recorded that this answer was helpful.",
        "stale": "Thanks — I flagged this answer as stale for review.",
        "missing": "Thanks — I flagged the missing context for review.",
        "incorrect": "Thanks — I sent your correction for human review.",
    }[feedback_type]
    return {
        "message": message,
        "destination": {
            "channel_id": context.slack_channel_id,
            "thread_ts": context.slack_thread_ts,
            "requester_user_id": context.acting_slack_user_id,
        },
    }
