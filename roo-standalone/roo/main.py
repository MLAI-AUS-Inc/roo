from __future__ import annotations

"""
Roo Standalone - FastAPI Application

Main entrypoint for the Roo AI agent service.
"""
import asyncio
import json
import hmac
import hashlib
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from uuid import uuid4
import httpx

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from .config import get_settings, Settings
from .agent import RooAgent, get_agent
from .content_factory_progress import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    build_live_status_blocks,
    get_content_factory_article_cost_points,
    normalize_content_factory_domain,
)
from .slack_client import post_message, send_dm

# Pending intents for auto-continue after prerequisite steps complete.
# Key: "{slack_user_id}:{domain}" → {"action": "write", "topic": "...", "channel_id": "...", "thread_ts": "..."}
_pending_intents: dict = {}
_pending_intents_by_job: dict[str, str] = {}
PENDING_INTENT_TTL_SECONDS = 30 * 60
CONTENT_FACTORY_WATCHDOG_POLL_SECONDS = 120
CONTENT_FACTORY_WATCHDOG_STOP_STATUSES = {
    "awaiting_confirmation",
    "awaiting_delivery_mode",
    "awaiting_approval",
    "approval_required",
    "completed",
    "failed",
    "error",
    "blocked",
    "blocked_verification",
    "denied",
    "cancelled",
}


def _pending_intent_key(slack_user_id: Optional[str], domain: Optional[str]) -> Optional[str]:
    if not slack_user_id or not domain:
        return None
    return f"{slack_user_id}:{domain}"


def _pop_pending_intent_by_key(intent_key: Optional[str]) -> Optional[dict[str, Any]]:
    if not intent_key:
        return None
    pending = _pending_intents.pop(intent_key, None)
    if not pending:
        return None
    pending_job_id = pending.get("job_id")
    if pending_job_id and _pending_intents_by_job.get(pending_job_id) == intent_key:
        _pending_intents_by_job.pop(pending_job_id, None)
    return pending


def _prune_pending_intents(now: Optional[float] = None) -> None:
    current_time = now if now is not None else time.time()
    for intent_key, pending in list(_pending_intents.items()):
        expires_at = pending.get("expires_at")
        if expires_at is not None and expires_at <= current_time:
            _pop_pending_intent_by_key(intent_key)


def _content_factory_client_request_id(raw_value: Any) -> str:
    """Return a stable request id, generating one for older Slack buttons."""
    client_request_id = str(raw_value or "").strip()
    if client_request_id:
        return client_request_id
    return f"content-factory-{uuid4().hex}"


def _remember_pending_intent(
    slack_user_id: Optional[str],
    domain: Optional[str],
    *,
    intent_data: Optional[dict[str, Any]] = None,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    wait_for: str,
    job_id: Optional[str] = None,
    clear_job_id: bool = False,
) -> Optional[dict[str, Any]]:
    _prune_pending_intents()
    intent_key = _pending_intent_key(slack_user_id, domain)
    if not intent_key:
        return None

    existing = _pending_intents.get(intent_key, {})
    previous_job_id = existing.get("job_id")
    if previous_job_id and previous_job_id != job_id and _pending_intents_by_job.get(previous_job_id) == intent_key:
        _pending_intents_by_job.pop(previous_job_id, None)

    now = time.time()
    pending = dict(existing)
    if intent_data:
        pending.update(intent_data)
    if channel_id is not None:
        pending["channel_id"] = channel_id
    if thread_ts is not None:
        pending["thread_ts"] = thread_ts
    pending["wait_for"] = wait_for
    pending["created_at"] = pending.get("created_at", now)
    pending["updated_at"] = now
    pending["expires_at"] = now + PENDING_INTENT_TTL_SECONDS
    if job_id:
        pending["job_id"] = job_id
        _pending_intents_by_job[job_id] = intent_key
    elif clear_job_id:
        pending.pop("job_id", None)

    _pending_intents[intent_key] = pending
    return pending


def _get_pending_intent(
    slack_user_id: Optional[str],
    domain: Optional[str],
    *,
    job_id: Optional[str] = None,
    wait_for: Optional[str] = None,
    consume: bool = False,
) -> Optional[dict[str, Any]]:
    _prune_pending_intents()

    intent_key = _pending_intents_by_job.get(job_id) if job_id else None
    if intent_key:
        pending = _pending_intents.get(intent_key)
        if not pending:
            _pending_intents_by_job.pop(job_id, None)
            return None
        if wait_for and pending.get("wait_for") != wait_for:
            return None
        return _pop_pending_intent_by_key(intent_key) if consume else pending

    intent_key = _pending_intent_key(slack_user_id, domain)
    if not intent_key:
        return None
    pending = _pending_intents.get(intent_key)
    if not pending:
        return None
    if wait_for and pending.get("wait_for") != wait_for:
        return None
    return _pop_pending_intent_by_key(intent_key) if consume else pending


def _extract_job_id(payload: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    for key in ("job_id", "run_id"):
        value = payload.get(key)
        if value:
            return str(value)

    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        nested_job_id = _extract_job_id(nested_data)
        if nested_job_id:
            return nested_job_id

    nested_job = payload.get("job")
    if isinstance(nested_job, dict):
        nested_job_id = _extract_job_id(nested_job)
        if nested_job_id:
            return nested_job_id

    return None


async def _watch_content_factory_quiet_run(job_id: str) -> None:
    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )
    consecutive_failures = 0

    while True:
        await asyncio.sleep(CONTENT_FACTORY_WATCHDOG_POLL_SECONDS)
        try:
            status_data = await client.check_generation_status(job_id)
            consecutive_failures = 0
            status_value = str(status_data.get("status") or "").strip().lower()
            if not status_value or status_value in CONTENT_FACTORY_WATCHDOG_STOP_STATUSES:
                return
            await client.maybe_send_content_still_working(
                job_id,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
        except Exception as exc:
            consecutive_failures += 1
            print(f"⚠️ Quiet-run watchdog failed for {job_id} ({consecutive_failures}/5): {exc}")
            if consecutive_failures >= 5:
                return


async def _maybe_attach_content_factory_progress(
    result_data: Any,
    post_response: Optional[dict],
    *,
    channel_id: Optional[str],
    thread_ts: Optional[str],
) -> None:
    if not isinstance(result_data, dict):
        return

    job_id = str(result_data.get("content_factory_progress_job_id") or "").strip()
    if not job_id:
        return

    message_ts = str((post_response or {}).get("ts") or "").strip()
    if not message_ts:
        return

    workflow = str(
        result_data.get("content_factory_workflow")
        or result_data.get("content_factory_watchdog_mode")
        or ""
    ).strip() or None
    domain = str(result_data.get("content_factory_domain") or "").strip() or None

    from .clients.mlai_backend import MLAIBackendClient

    settings = get_settings()
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )
    try:
        await client.attach_content_progress_message(
            job_id,
            progress_message_ts=message_ts,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            slack_root_message_ts=thread_ts,
            request_source=CONTENT_FACTORY_REQUEST_SOURCE,
        )
    except Exception as exc:
        print(f"⚠️ Failed to attach progress message {message_ts} to job {job_id}: {exc}")
        return

    _remember_content_thread_context(
        channel_id,
        thread_ts,
        domain,
        workflow or "write",
        active_job_id=job_id,
    )

    if result_data.get("content_factory_watchdog"):
        asyncio.create_task(_watch_content_factory_quiet_run(job_id))


async def _trigger_article_generation_from_pending(
    pending: dict[str, Any],
    *,
    slack_user_id: str,
    domain: str,
    fallback_channel_id: Optional[str] = None,
    fallback_thread_ts: Optional[str] = None,
) -> bool:
    intent_channel = pending.get("channel_id") or fallback_channel_id
    intent_thread = pending.get("thread_ts") or fallback_thread_ts
    topic = pending.get("topic")
    include_decision_stage = not bool(topic)

    print(f"🔄 Auto-continuing: triggering article generation for {domain} (topic: {topic})")

    progress_message_ts = None
    if intent_channel:
        topic_note = f" for *{topic}*" if topic else ""
        summary_text = (
            f"Articles directory is ready. Starting article generation{topic_note}. I'll keep this message updated."
            if topic
            else "Articles directory is ready. Starting discovery to find the best article opportunity. I'll keep this message updated."
        )
        response = post_message(
            channel=intent_channel,
            thread_ts=intent_thread,
            text=f"Starting Content Factory for {domain}",
            blocks=build_live_status_blocks(
                domain,
                summary_text=summary_text,
                include_decision_stage=include_decision_stage,
                current_stage="preparing",
            ),
        )
        progress_message_ts = str((response or {}).get("ts") or "").strip() or None

    _remember_content_thread_context(
        intent_channel,
        intent_thread,
        domain,
        "research" if include_decision_stage else "write",
    )

    from .clients.mlai_backend import MLAIBackendClient
    from .slack_client import get_user_info
    settings = get_settings()
    backend_client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
    )
    try:
        slack_info = get_user_info(slack_user_id)
        real_name = str(slack_info.get("real_name") or "").strip()
        name_parts = real_name.split(" ", 1) if real_name else []
        result = await backend_client.trigger_article_generation(
            slack_user_id=slack_user_id,
            domain=domain,
            topic=topic,
            target_keyword=pending.get("target_keyword"),
            slack_channel_id=intent_channel,
            slack_thread_ts=intent_thread,
            progress_message_ts=progress_message_ts,
            client_request_id=pending.get("client_request_id"),
            request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            user_email=str(slack_info.get("email") or "").strip().lower() or None,
            user_first_name=name_parts[0] if name_parts else None,
            user_last_name=name_parts[1] if len(name_parts) > 1 else None,
            user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
        )
        print(f"✅ Auto-generation triggered for {domain}")
        if result.get("job_id") or result.get("run_id"):
            _remember_content_thread_context(
                intent_channel,
                intent_thread,
                domain,
                "research" if include_decision_stage else "write",
                active_job_id=result.get("job_id") or result.get("run_id"),
            )
            asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        return True
    except Exception as e:
        print(f"❌ Auto-generation failed: {e}")
        if intent_channel:
            post_message(
                channel=intent_channel,
                thread_ts=intent_thread,
                text=f"❌ Error starting article generation: {e}"
            )
        return False


def _remember_content_thread_context(
    channel_id: str | None,
    thread_ts: str | None,
    domain: str | None,
    workflow: str,
    *,
    active_job_id: str | None = None,
) -> None:
    """Keep content-factory as the active skill for follow-ups in this thread."""
    if not channel_id or not thread_ts:
        return

    try:
        get_agent().remember_thread_context(
            "content-factory",
            channel_id,
            thread_ts,
            domain=domain,
            workflow=workflow,
            active_job_id=active_job_id,
        )
    except Exception as e:
        print(f"⚠️ Failed to persist content thread context: {e}")


def _build_article_delivery_mode_blocks(
    *,
    domain: str,
    job_id: str,
    topic: Optional[str] = None,
    recommended_delivery_mode: Optional[str] = None,
) -> list[dict]:
    topic_line = f"*Topic:* {topic}\n" if topic else ""
    recommended_line = ""
    if recommended_delivery_mode == "content_only":
        recommended_line = "\n_Recommended right now: content-only._"
    elif recommended_delivery_mode == "publish_code":
        recommended_line = "\n_Recommended right now: publish via code._"

    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Content Only", "emoji": True},
            "value": json.dumps({"job_id": job_id, "domain": domain, "delivery_mode": "content_only"}),
            "action_id": "select_article_delivery_mode",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Publish Via Code", "emoji": True},
            "value": json.dumps({"job_id": job_id, "domain": domain, "delivery_mode": "publish_code"}),
            "action_id": "select_article_delivery_mode",
        },
    ]
    if recommended_delivery_mode == "publish_code":
        buttons[1]["style"] = "primary"
    else:
        buttons[0]["style"] = "primary"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Choose how you want me to deliver the article for *{domain}*.\n\n"
                    f"{topic_line}"
                    "• *Content-only*: research and write the article for manual upload.\n"
                    "• *Publish via code*: continue through the repo/PR flow."
                    f"{recommended_line}"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": "article_delivery_mode_actions",
            "elements": buttons,
        },
    ]


def _build_confirm_topic_follow_up(
    result: Any,
    *,
    default_domain: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        result = {}

    cf_response = result.get("cf_response")
    if not isinstance(cf_response, dict):
        cf_response = {}

    top_level_status = str(result.get("status") or "").strip().lower()
    callback_status = str(cf_response.get("status") or "").strip().lower()
    requires_delivery_mode = "awaiting_delivery_mode" in {top_level_status, callback_status}
    status_value = "awaiting_delivery_mode" if requires_delivery_mode else (top_level_status or callback_status)
    active_job_id = _extract_job_id(result) or _extract_job_id(cf_response)
    domain = str(result.get("domain") or cf_response.get("domain") or default_domain or "this domain").strip() or "this domain"

    return {
        "status": status_value,
        "active_job_id": active_job_id,
        "domain": domain,
        "recommended_delivery_mode": (
            result.get("recommended_delivery_mode")
            or cf_response.get("recommended_delivery_mode")
        ),
        "requires_delivery_mode": bool(active_job_id and requires_delivery_mode),
    }


def _maybe_schedule_content_factory_watchdog(active_job_id: Optional[str], status_value: str) -> None:
    if not active_job_id:
        return
    if status_value and status_value in CONTENT_FACTORY_WATCHDOG_STOP_STATUSES:
        return
    asyncio.create_task(_watch_content_factory_quiet_run(active_job_id))


def _confirm_topic_json_response(keyword: str, follow_up: dict[str, Any]) -> JSONResponse:
    if follow_up.get("requires_delivery_mode"):
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": f"Choose delivery mode for {keyword}",
            "blocks": _build_article_delivery_mode_blocks(
                domain=follow_up["domain"],
                job_id=follow_up["active_job_id"],
                topic=keyword,
                recommended_delivery_mode=follow_up.get("recommended_delivery_mode"),
            ),
        })

    _maybe_schedule_content_factory_watchdog(
        follow_up.get("active_job_id"),
        str(follow_up.get("status") or ""),
    )
    return JSONResponse(status_code=200, content={
        "response_type": "ephemeral",
        "replace_original": True,
        "text": f"⏳ Queued generation for `{keyword}`. No additional Roo points will be charged for this confirmation.",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏳ Queued generation for `{keyword}`.\n_No additional Roo points will be charged for topic confirmation._"
                }
            }
        ]
    })


async def _medhack_daily_case_loop():
    """Background task that posts a new diagnosis case each day."""
    import asyncio
    from .slack_client import get_channel_id, post_message
    from .utils import get_current_date

    # Wait a bit for the app to fully start
    await asyncio.sleep(10)

    while True:
        try:
            today = get_current_date()
            # Try medhack-testing first (for dev), then medhack-frontiers (for prod)
            channel_id = get_channel_id("medhack-testing") or get_channel_id("medhack-frontiers")
            if not channel_id:
                print("⚠️ MedHack: neither #medhack-testing nor #medhack-frontiers channel found, skipping daily case")
                await asyncio.sleep(3600)  # Retry in an hour
                continue

            # Load the medhack client
            from pathlib import Path
            import sys
            settings = get_settings()
            skills_dir = Path(settings.SKILLS_DIR) / "medhack"
            client_path = skills_dir / "client.py"

            if not client_path.exists():
                print("⚠️ MedHack client.py not found, skipping daily case")
                await asyncio.sleep(3600)
                continue

            # Import the client
            import importlib.util
            spec = importlib.util.spec_from_file_location("medhack_daily_client", client_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            client = mod.MedHackClient()

            # Check if there's already a case for today
            current = await client.get_current_case(today)
            if current is None:
                # Start a new case
                new_case = await client.start_new_case(today)
                if new_case:
                    difficulty = new_case.get("difficulty", "medium").upper()
                    title = new_case.get("title", "")
                    title_str = f' - _{title}_' if title else ""
                    header = f"*GUESS THE DIAGNOSIS* - Daily Challenge [{difficulty}]{title_str}"

                    # New-style cases have an ed_first_look narrative + triage note
                    if new_case.get("ed_first_look"):
                        scene = new_case["ed_first_look"].strip()
                        triage = new_case["presenting_complaint"].strip()
                        message = (
                            f"{header}\n\n"
                            f"{scene}\n\n"
                            f"*Triage note:* {triage}\n\n"
                            f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                            f"Ask me anything you'd ask them and I'll relay their answer. "
                            f"You can also request examinations and investigations, but be specific — "
                            f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                            f"When you're ready, tell me your diagnosis!\n\n"
                            f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                            f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                        )
                    else:
                        complaint = new_case["presenting_complaint"].strip()
                        message = (
                            f"{header}\n\n"
                            f"{complaint}\n\n"
                            f"Tag *@Roo* to interact — I'm your gateway to the patient. "
                            f"Ask me anything you'd ask them and I'll relay their answer. "
                            f"You can also request examinations and investigations, but be specific — "
                            f"the hospital has limited resources and inappropriate or costly tests may be denied.\n\n"
                            f"When you're ready, tell me your diagnosis!\n\n"
                            f"_You get *one guess* — make it count! First correct answer wins 12 MLAI points "
                            f"+ DM Dr Sam for a free ticket code to MedHack: Frontiers!_"
                        )

                    # Build blocks with optional image
                    image_url = new_case.get("image_url", "")
                    if image_url:
                        blocks = [
                            {
                                "type": "image",
                                "image_url": image_url,
                                "alt_text": f"Guess the Diagnosis - {new_case.get('title', 'Daily Case')}",
                            },
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": message},
                            },
                        ]
                        post_message(channel=channel_id, text=message, blocks=blocks)
                    else:
                        post_message(channel=channel_id, text=message)
                    print(f"Posted new MedHack case #{new_case['id']} for {today}")
                else:
                    print("⚠️ No available MedHack cases to post")

        except Exception as e:
            print(f"❌ MedHack daily case error: {e}")
            import traceback
            traceback.print_exc()

        # Sleep until tomorrow (calculate seconds until next 10 AM AEST)
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(get_settings().TIMEZONE)
        now = datetime.now(tz)
        next_post = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now >= next_post:
            next_post += timedelta(days=1)
        sleep_seconds = (next_post - now).total_seconds()
        print(f"MedHack: Next case in {sleep_seconds/3600:.1f} hours")
        await asyncio.sleep(sleep_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()
    print(f"🦘 Roo Standalone starting...")
    print(f"   LLM Provider: {settings.default_llm_provider}")
    print(f"   Skills Dir: {settings.SKILLS_DIR}")

    # Initialize agent on startup
    agent = get_agent()
    print(f"   Loaded {len(agent.skills)} skills")

    # MedHack daily case scheduler (currently disabled)
    # import asyncio
    # medhack_task = asyncio.create_task(_medhack_daily_case_loop())
    # print("   Started MedHack daily case scheduler")

    yield

    # Cancel the background task on shutdown (disabled)
    # medhack_task.cancel()
    print("🦘 Roo Standalone shutting down...")


app = FastAPI(
    title="Roo Standalone",
    description="AI Agent Service with Skills-based Architecture",
    version="1.0.0",
    lifespan=lifespan
)


def verify_slack_signature(
    request: Request,
    settings: Settings = Depends(get_settings)
) -> bool:
    """Verify Slack request signature."""
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    
    # Check timestamp is recent (within 5 minutes)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            raise HTTPException(status_code=403, detail="Request timestamp too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")
    
    return True  # Full verification in middleware


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "roo",
        "message": "G'day! Roo is awake and ready 🦘"
    }


@app.post("/slack/events")
async def slack_events(request: Request):
    """
    Slack Events API webhook.
    
    Handles:
    - url_verification challenges
    - app_mention events
    - direct messages
    - reaction approvals
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    # Handle URL verification challenge
    if payload.get("type") == "url_verification":
        print("✅ Slack URL verification challenge")
        return {"challenge": payload.get("challenge")}
    
    # Handle events
    event = payload.get("event", {})
    event_type = event.get("type")
    
    print(f"📨 Received Slack event: {event_type}")

    # Quests disabled for now
    # try:
    #     from .quests import handle_quests
    #     import asyncio
    #     asyncio.create_task(handle_quests(event))
    # except Exception as e:
    #     print(f"⚠️ Quest processing failed: {e}")
    
    if event_type == "app_mention":
        # Process mention asynchronously
        asyncio.create_task(_handle_mention(event))
        return JSONResponse(status_code=200, content={})

    if event_type == "reaction_added":
        asyncio.create_task(_handle_reaction_added(event))
        return JSONResponse(status_code=200, content={})
    
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        from .slack_client import get_channel_id

        start_here_id = get_channel_id("_start-here")
        if start_here_id and event.get("channel") == start_here_id:
            if event.get("thread_ts"):
                print(f"🧵 Ignoring thread reply in #_start-here from {event.get('user')}")
                return JSONResponse(status_code=200, content={})

            asyncio.create_task(_handle_start_here_intro(event))
            return JSONResponse(status_code=200, content={})
        
        is_dm = event.get("channel_type") == "im"
        if is_dm:
            print(f"📨 Received DM from {event.get('user')}")
            asyncio.create_task(_handle_mention(event))
            return JSONResponse(status_code=200, content={})
    
    return JSONResponse(status_code=200, content={})


async def _handle_start_here_intro(event: dict):
    """Award the intro bonus for a qualifying top-level #_start-here post."""
    user_id = event.get("user")
    channel_id = event.get("channel")
    message_ts = event.get("ts")

    if not user_id or not channel_id or not message_ts:
        return

    try:
        from .clients.mlai_backend import MLAIBackendClient

        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )
        result = await client.award_first_channel_post(user_id, channel_id)
    except Exception as exc:
        print(f"⚠️ Failed to process #_start-here intro award for {user_id} in {channel_id}: {exc}")
        return

    if not result.get("awarded"):
        return

    post_message(
        channel=channel_id,
        thread_ts=message_ts,
        text=f"Welcome <@{user_id}>! You've earned 2 Roo points for introducing yourself here.",
    )


async def _handle_mention(event: dict):
    """Handle an @Roo mention asynchronously."""
    try:
        user_id = event.get("user")
        text = event.get("text", "")
        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        param_overrides = event.get("param_overrides")
        
        print(f"\n🦘 ROO MENTION: from {user_id} in {channel_id}")
        print(f"   Text: {text[:100]}...")
        
        agent = get_agent()
        result = await agent.handle_mention(
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            param_overrides=param_overrides if isinstance(param_overrides, dict) else None,
        )
        
        if result.get("message") and not result.get("suppress_post"):
            post_kwargs = {
                "channel": channel_id,
                "text": result["message"],
                "thread_ts": thread_ts,
            }
            if result.get("blocks"):
                post_kwargs["blocks"] = result["blocks"]
            response = post_message(**post_kwargs)
            await _maybe_attach_content_factory_progress(
                result.get("data"),
                response,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

        print(f"✅ Mention handled successfully (skill: {result.get('skill_used')})")
        
    except Exception as e:
        print(f"❌ Error handling mention: {e}")
        import traceback
        traceback.print_exc()
        
        try:
            post_message(
                channel=event.get("channel"),
                text="Sorry mate, I ran into a bit of trouble. Mind trying again? 🤔",
                thread_ts=event.get("thread_ts") or event.get("ts")
            )
        except Exception:
            pass


async def _resume_intent(user_id: str, intent: dict):
    """Resume a pending intent after authentication."""
    try:
        text = intent.get("text")
        channel_id = intent.get("channel")
        thread_ts = intent.get("ts")
        
        print(f"🔄 Resuming intent for {user_id}: {text[:50]}...")
        
        if channel_id:
            post_message(channel_id, "✅ You're connected! Resuming your request...", thread_ts)
        
        agent = get_agent()
        result = await agent.handle_mention(
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts
        )
        
        if result.get("message") and not result.get("suppress_post"):
            post_kwargs = {
                "channel": channel_id,
                "text": result["message"],
                "thread_ts": thread_ts,
            }
            if result.get("blocks"):
                post_kwargs["blocks"] = result["blocks"]
            response = post_message(**post_kwargs)
            await _maybe_attach_content_factory_progress(
                result.get("data"),
                response,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

    except Exception as e:
        print(f"❌ Error resuming intent: {e}")
        if intent.get("channel"):
            post_message(intent["channel"], "Sorry, I had trouble resuming your request.", intent.get("ts"))


def _extract_backend_error_message(exc: Exception) -> str:
    """Pull the most useful error message from a backend exception."""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    return payload.get("error") or payload.get("detail") or str(exc)
            except Exception:
                pass
    except Exception:
        pass

    return str(exc)


async def _handle_reaction_added(event: dict):
    """Handle Slack emoji approvals for pending points requests."""
    reactor_user_id = event.get("user")
    reaction = event.get("reaction")
    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if reaction != "white_check_mark":
        return
    if not reactor_user_id or item.get("type") != "message" or not channel_id or not message_ts:
        return

    print(f"✅ Reaction approval attempt from {reactor_user_id} on {channel_id}:{message_ts}")

    try:
        import httpx
        from .clients.mlai_backend import MLAIBackendClient

        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        request_record = await client.get_points_request_by_slack_message(channel_id, message_ts)
        if not request_record:
            return

        if request_record.get("status") not in (None, "", "pending"):
            return

        request_id = request_record.get("id")
        if not request_id:
            return

        try:
            result = await client.approve_points_request(int(request_id), reactor_user_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 409):
                return
            send_dm(
                reactor_user_id,
                (
                    "I couldn't approve that points request. "
                    f"{_extract_backend_error_message(exc)}"
                ),
            )
            return
        except Exception as exc:
            send_dm(
                reactor_user_id,
                (
                    "I couldn't approve that points request just now. "
                    f"{_extract_backend_error_message(exc)}"
                ),
            )
            return

        requester = request_record.get("target_slack_id") or request_record.get("requester_slack_id")
        points = result.get("points_awarded", request_record.get("points", 0))
        reason = request_record.get("reason", "No reason provided")
        new_balance = result.get("new_balance")
        balance_line = f"\nNew balance: {new_balance} pts" if new_balance is not None else ""
        thread_ts = request_record.get("slack_thread_ts") or message_ts

        post_message(
            channel=channel_id,
            text=(
                f"✅ Points request approved by <@{reactor_user_id}>.\n\n"
                f"<@{requester}> received {points} points.\n"
                f"Reason: {reason}{balance_line}"
            ),
            thread_ts=thread_ts,
        )

    except Exception as exc:
        print(f"❌ Error handling reaction approval: {exc}")


@app.post("/slack/commands")
async def slack_commands(request: Request):
    """Slack Slash Commands webhook."""
    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "")
    user_id = form.get("user_id", "")
    
    print(f"📨 Slash command: {command} {text} from {user_id}")
    
    if command == "/roo" and "connect type" in text:  # Handle other connects?
        pass

    # Handle "connect github"
    if "connect github" in text.lower():
        settings = get_settings()
        from .clients.mlai_backend import MLAIBackendClient
        
        try:
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
            )
            
            # Get Auth URL
            auth_response = await client.get_github_auth_url(user_id)
            auth_url = auth_response.get("auth_url")
            
            if not auth_url:
                return {
                    "response_type": "ephemeral",
                    "text": "Sorry mate, I couldn't get the authorization URL. Please try again later."
                }
                
            return {
                "response_type": "ephemeral",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Click the button below to connect your GitHub account to Roo."
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Connect GitHub",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github_cmd",
                                "style": "primary"
                            }
                        ]
                    }
                ]
            }
            
        except Exception as e:
            print(f"Failed to handle connect github command: {e}")
            return {
                "response_type": "ephemeral",
                "text": "Sorry mate, ran into a snag getting the connection link."
            }
    
    return {
        "response_type": "ephemeral",
        "text": f"Command '{command} {text}' received! (Not yet implemented)"
    }


@app.post("/api/mention")
async def api_mention(request: Request):
    """
    Direct API endpoint for triggering Roo mentions.
    
    Can be called from mlai-backend or other services.
    """
    payload = await request.json()
    
    text = payload.get("text", "")
    user_id = payload.get("user_id", "")
    channel_id = payload.get("channel_id")
    thread_ts = payload.get("thread_ts")
    
    agent = get_agent()
    result = await agent.handle_mention(
        text=text,
        user_id=user_id,
        channel_id=channel_id,
        thread_ts=thread_ts
    )
    
    return result


def _format_tier_display(tier: str) -> str:
    """Format tier string for display with emoji."""
    tier_lower = tier.lower().replace("_", " ")
    if "blue" in tier_lower or "tier_1" in tier_lower or "tier 1" in tier_lower:
        return "🔵 Blue Ocean"
    elif "green" in tier_lower or "tier_2" in tier_lower or "tier 2" in tier_lower:
        return "🟢 Green"
    elif "yellow" in tier_lower or "tier_3" in tier_lower or "tier 3" in tier_lower:
        return "🟡 Yellow"
    elif "red" in tier_lower or "tier_4" in tier_lower or "tier 4" in tier_lower:
        return "🔴 Red"
    return tier.replace("_", " ").title()


@app.post("/api/callbacks/content-factory")
async def content_factory_callback(request: Request):
    """
    Handle callbacks from mlai-backend Content Factory.

    Supports:
    - topic_confirmation_request: When research is complete and topic needs confirmation
    - topic_selection: Legacy format - When a topic is proposed for user confirmation
    - article_complete: When an article has been generated
    """
    try:
        payload = await request.json()
        event_type = payload.get("type") or payload.get("event_type")
        slack_user_id = payload.get("slack_user_id")

        print(f"🏭 Content Factory event: {event_type} for {slack_user_id}")

        # Handle new topic_confirmation_request format
        if event_type == "topic_confirmation_request":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            data = payload.get("data", {})

            keyword = data.get("selected_keyword")
            reason = data.get("selection_reason", "")
            metrics = data.get("metrics", {})
            volume = metrics.get("volume", 0)
            difficulty = metrics.get("difficulty", 0)
            tier = metrics.get("tier", "")
            score = metrics.get("score", 0)
            alternatives = data.get("alternatives", [])

            # Format tier for display
            tier_display = _format_tier_display(tier)

            # Build Block Kit message matching the specification
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Content Research Complete",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommended Topic:* `{keyword}`\n\nI found this topic has high potential for *{domain}*."
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Volume:*\n{volume:,}/mo"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Difficulty:*\n{difficulty}/100"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Tier:*\n{tier_display}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Opportunity Score:*\n{score}"
                        }
                    ]
                }
            ]

            # Add alternatives section if present
            if alternatives:
                alt_text = "*Alternatives:*\n" + "\n".join(f"• {alt}" for alt in alternatives)
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": alt_text
                    }
                })

            # Build action elements
            action_elements = [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Write This Article",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": f"confirm:{keyword}:{job_id}",
                    "action_id": "confirm_topic"
                }
            ]

            # Add static_select dropdown for alternatives if present
            if alternatives:
                alt_options = [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": alt[:75]  # Slack limit for option text
                        },
                        "value": f"confirm:{alt}:{job_id}"
                    }
                    for alt in alternatives[:10]  # Limit to 10 options
                ]
                action_elements.append({
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Pick Alternative",
                        "emoji": True
                    },
                    "options": alt_options,
                    "action_id": "select_alternative"
                })

            # Add cancel button
            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": f"cancel:{job_id}",
                "action_id": "cancel_topic"
            })

            blocks.append({
                "type": "actions",
                "block_id": "topic_confirmation_actions",
                "elements": action_elements
            })

            # Send DM
            dm_channel = from_slack_client_open_dm(slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Content research complete. Recommended topic: {keyword}",
                    blocks=blocks
                )
            return {"status": "ok"}

        # Handle legacy topic_selection format for backwards compatibility
        elif event_type == "topic_selection":
            selection = payload.get("selection", {})
            keyword = selection.get("selected_keyword")
            reason = selection.get("selection_reason")
            volume = selection.get("volume")
            difficulty = selection.get("difficulty")
            tier = selection.get("tier", "")
            score = selection.get("opportunity_index")
            alternatives = selection.get("top_alternatives", [])
            job_id = payload.get("job_id")
            domain = payload.get("domain")

            tier_display = _format_tier_display(tier)

            # Use the new Block Kit format for legacy events too
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Content Research Complete",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommended Topic:* `{keyword}`\n\nI found this topic has high potential for *{domain}*."
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Volume:*\n{volume:,}/mo" if volume else "*Volume:*\n--"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Difficulty:*\n{difficulty}/100" if difficulty else "*Difficulty:*\n--"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Tier:*\n{tier_display}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Opportunity Score:*\n{score}" if score else "*Opportunity Score:*\n--"
                        }
                    ]
                }
            ]

            if alternatives:
                alt_text = "*Alternatives:*\n" + "\n".join(f"• {alt}" for alt in alternatives)
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": alt_text
                    }
                })

            action_elements = [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Write This Article",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": f"confirm:{keyword}:{job_id}",
                    "action_id": "confirm_topic"
                }
            ]

            if alternatives:
                alt_options = [
                    {
                        "text": {
                            "type": "plain_text",
                            "text": alt[:75]
                        },
                        "value": f"confirm:{alt}:{job_id}"
                    }
                    for alt in alternatives[:10]
                ]
                action_elements.append({
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Pick Alternative",
                        "emoji": True
                    },
                    "options": alt_options,
                    "action_id": "select_alternative"
                })

            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": f"cancel:{job_id}",
                "action_id": "cancel_topic"
            })

            blocks.append({
                "type": "actions",
                "block_id": "topic_confirmation_actions",
                "elements": action_elements
            })

            dm_channel = from_slack_client_open_dm(slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Topic selected: {keyword}",
                    blocks=blocks
                )
            return {"status": "ok"}

        elif event_type == "article_complete":
            article_url = payload.get("article_url")
            pr_url = payload.get("pr_url")
            domain = payload.get("domain")
            topic = payload.get("topic", "")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "**✅ Article Published!**"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Topic:*\n{topic}" if topic else f"*Domain:*\n{domain}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*URL:*\n<{article_url}|View Article>"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*PR:* <{pr_url}|View Pull Request>"
                    }
                }
            ]

            dm_channel = from_slack_client_open_dm(slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Article published: {article_url}",
                    blocks=blocks
                )
            return {"status": "ok"}

        elif event_type == "scan_complete":
            job_id = payload.get("job_id")
            run_id = payload.get("run_id") or job_id
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            components_count = payload.get("components_count", 0)
            component_names = payload.get("component_names", [])
            pillar_count = payload.get("pillar_count")
            pillar_names = payload.get("pillar_names")
            requested_action = str(payload.get("requested_action") or "").strip()
            scaffold_status = str(payload.get("scaffold_status") or "").strip()
            scaffold_queued = bool(payload.get("scaffold_queued"))
            scaffold_job_id = str(payload.get("scaffold_job_id") or "").strip()
            approval_required = (
                requested_action == "scaffold_publish_route"
                and scaffold_status == "approval_required"
            )

            print(f"📦 Scan complete for {domain}: {components_count} components, {pillar_count} pillars")
            _remember_content_thread_context(channel_id, thread_ts, domain, "scan")

            if components_count > 0:
                # Build component summary: "ArticleHeroHeader, ArticleCard, ArticleFAQ, +27 more"
                shown_names = component_names[:3]
                component_summary = ", ".join(shown_names)
                remaining = components_count - len(shown_names)
                if remaining > 0:
                    component_summary += f", +{remaining} more"

                summary = f"I've analysed your codebase and generated:\n• *{components_count} article components* ({component_summary})"

                if pillar_count and pillar_names:
                    pillar_list = ", ".join(pillar_names)
                    summary += f"\n• *{pillar_count} content pillars:* {pillar_list}"
                elif pillar_count:
                    summary += f"\n• *{pillar_count} content pillars*"

                if approval_required:
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"✅ *Scan complete for {domain}*\n\n{summary}"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Ready to create your articles directory? This will open a PR with:\n- An articles listing page\n- All {0} reusable components\n- A demo article showcasing how they look".format(components_count)
                            }
                        },
                        {
                            "type": "actions",
                            "block_id": "scaffold_actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Create Articles Directory",
                                        "emoji": True
                                    },
                                    "style": "primary",
                                    "value": json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                        "client_request_id": f"content-factory-{uuid4().hex}",
                                    }),
                                    "action_id": "scaffold_confirm"
                                },
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Not Now",
                                        "emoji": True
                                    },
                                    "value": json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    }),
                                    "action_id": "scaffold_skip"
                                }
                            ]
                        }
                    ]
                else:
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"✅ *Scan complete for {domain}*\n\n{summary}"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Scan completed successfully. If you need an articles directory scaffold, run a fresh scan so Roo can request approval-first scaffolding."
                            }
                        }
                    ]
            else:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ *Scan complete for {domain}*\n\nNo new components were detected."
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Scan complete for {domain}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted scan results to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Scan complete for {domain}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {slack_user_id}")
                dm_channel = from_slack_client_open_dm(slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Scan complete for {domain}",
                        blocks=blocks
                    )

            # Auto-continue: check for pending intent after scan completes
            pending = _get_pending_intent(
                slack_user_id,
                domain,
                job_id=job_id,
                wait_for="scan_complete",
            )
            if pending:
                pending_action = pending.get("action")
                intent_channel = pending.get("channel_id") or channel_id
                intent_thread = pending.get("thread_ts") or thread_ts

                if approval_required and pending_action in ("scaffold", "write"):
                    print(f"⏸️ Waiting for scaffold approval before continuing {pending_action} for {domain}")
                elif pending_action in ("scaffold", "write"):
                    if scaffold_queued:
                        print(f"⏳ Scan already queued scaffold for {domain}; waiting for scaffold completion")
                        if pending_action == "write":
                            _remember_pending_intent(
                                slack_user_id,
                                domain,
                                intent_data=pending,
                                channel_id=intent_channel,
                                thread_ts=intent_thread,
                                wait_for="scaffold_complete",
                                job_id=scaffold_job_id or None,
                                clear_job_id=True,
                            )
                            if intent_channel:
                                post_message(
                                    channel=intent_channel,
                                    thread_ts=intent_thread,
                                    text=f"📁 Scan complete! I’m waiting for the articles directory PR for *{domain}* before continuing your article request."
                                )
                        else:
                            _get_pending_intent(
                                slack_user_id,
                                domain,
                                job_id=job_id,
                                wait_for="scan_complete",
                                consume=True,
                            )
                            if intent_channel:
                                post_message(
                                    channel=intent_channel,
                                    thread_ts=intent_thread,
                                    text=f"📁 Scan complete! The articles directory setup is already queued for *{domain}*."
                                )
                    elif scaffold_status == "not_needed":
                        consumed = _get_pending_intent(
                            slack_user_id,
                            domain,
                            job_id=job_id,
                            wait_for="scan_complete",
                            consume=True,
                        ) or pending
                        if pending_action == "write":
                            await _trigger_article_generation_from_pending(
                                consumed,
                                slack_user_id=slack_user_id,
                                domain=domain,
                                fallback_channel_id=channel_id,
                                fallback_thread_ts=thread_ts,
                            )
                        elif intent_channel:
                            post_message(
                                channel=intent_channel,
                                thread_ts=intent_thread,
                                text=f"✅ Scan complete! Your repo already has the publish route Roo needs for *{domain}*."
                            )
                    else:
                        _get_pending_intent(
                            slack_user_id,
                            domain,
                            job_id=job_id,
                            wait_for="scan_complete",
                            consume=True,
                        )
                        print(
                            f"⚠️ Scan for {domain} did not provide approval-ready scaffold metadata "
                            f"(scaffold_status={scaffold_status or 'unknown'}); not auto-triggering scaffold"
                        )
                        if intent_channel:
                            post_message(
                                channel=intent_channel,
                                thread_ts=intent_thread,
                                text=(
                                    f"⚠️ I need a fresh scan result with scaffold approval metadata before I can continue for *{domain}*. "
                                    f"Please run the scan again."
                                ),
                            )

            return {"status": "ok"}

        elif event_type == "scaffold_complete":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            pr_url = payload.get("pr_url")
            files_created = payload.get("files_created", 0)
            pillar_count = payload.get("pillar_count", 0)
            component_count = payload.get("component_count", 0)
            already_exists = payload.get("already_exists", False)
            preview_url = payload.get("preview_url")
            build_verified = payload.get("build_verified", False)
            client_request_id = _content_factory_client_request_id(payload.get("client_request_id"))

            print(f"📁 Scaffold complete for {domain}: PR={pr_url}")
            _remember_content_thread_context(channel_id, thread_ts, domain, "scaffold")

            if already_exists:
                detail_lines = []
                if pr_url:
                    detail_lines.append(f"• PR: <{pr_url}|View pull request>")
                if preview_url:
                    detail_lines.append(f"• Preview: <{preview_url}|View live preview> :eyes:")
                details = f"\n\n" + "\n".join(detail_lines) if detail_lines else ""
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"ℹ️ Articles directory already exists - no changes needed.{details}"
                        }
                    }
                ]
            elif pr_url:
                details = (
                    f"• {files_created} files created ({component_count} components, {pillar_count} content pillars)"
                    if files_created
                    else f"• Reused the existing scaffold branch/PR ({component_count} components, {pillar_count} content pillars)"
                )
                details += f"\n• PR: <{pr_url}|View pull request>"
                details += (
                    "\n• Build: Passed :white_check_mark:"
                    if build_verified
                    else "\n• Build: Not verified"
                )

                if preview_url:
                    preview_label = "View live preview"
                    if "stackblitz.com" in preview_url:
                        preview_label += " (may take a moment to load)"
                    details += f"\n• Preview: <{preview_url}|{preview_label}> :eyes:"

                extra_sections = []
                if preview_url:
                    extra_sections.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Check out the demo article in the preview to see how your components look!\n_Note: Preview may take a moment to load._"
                        }
                    })

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ *Articles directory created for {domain}*\n\n{details}"
                        }
                    },
                    *extra_sections,
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "Ready to write your first article?"
                        }
                    },
                    {
                        "type": "actions",
                        "block_id": "write_article_actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Write First Article",
                                    "emoji": True
                                },
                                "style": "primary",
                                "value": json.dumps({
                                    "domain": domain,
                                    "slack_user_id": slack_user_id,
                                    "channel_id": channel_id,
                                    "thread_ts": thread_ts,
                                    "client_request_id": client_request_id,
                                }),
                                "action_id": "write_first_article"
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Not Now",
                                    "emoji": True
                                },
                                "value": json.dumps({"domain": domain}),
                                "action_id": "write_article_skip"
                            }
                        ]
                    }
                ]
            else:
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📁 Articles directory scaffolded for *{domain}*, but the PR could not be created. Check your repo for a `feature/articles-scaffolding` branch."
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Scaffold complete for {domain}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted scaffold results to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Scaffold complete for {domain}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {slack_user_id}")
                dm_channel = from_slack_client_open_dm(slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Scaffold complete for {domain}",
                        blocks=blocks
                    )

            # Auto-continue: check for pending write intent after scaffold completes
            pending = _get_pending_intent(
                slack_user_id,
                domain,
                job_id=job_id,
                wait_for="scaffold_complete",
                consume=True,
            )
            if pending and pending.get("action") == "write":
                await _trigger_article_generation_from_pending(
                    pending,
                    slack_user_id=slack_user_id,
                    domain=domain,
                    fallback_channel_id=channel_id,
                    fallback_thread_ts=thread_ts,
                )

            return {"status": "ok"}

        elif event_type == "generation_failed":
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            channel_id = payload.get("channel_id")
            thread_ts = payload.get("thread_ts")
            error_message = payload.get("error_message") or payload.get("error", "Unknown error")
            error_code = payload.get("error_code")
            stage = payload.get("stage", "generation")  # scan, scaffold, generation

            print(f"❌ Generation failed for {domain} at {stage}: {error_message} (code: {error_code})")
            wait_for = {
                "scan": "scan_complete",
                "scaffold": "scaffold_complete",
            }.get(stage)
            if wait_for:
                cleared_pending = _get_pending_intent(
                    slack_user_id,
                    domain,
                    job_id=job_id,
                    wait_for=wait_for,
                    consume=True,
                )
                if cleared_pending:
                    print(f"🧹 Cleared pending intent after {stage} failure for {slack_user_id}:{domain}")

            # Provide specific error messages based on error_code
            if error_code == "INVALID_CREDENTIALS":
                user_message = "❌ I need fresh GitHub access. Please reconnect your GitHub account to continue."
                # Fetch auth URL so we can show a reconnect button
                try:
                    from .clients.mlai_backend import MLAIBackendClient
                    settings = get_settings()
                    auth_client = MLAIBackendClient(
                        base_url=settings.MLAI_BACKEND_URL,
                        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
                    )
                    auth_response = await auth_client.get_github_auth_url(slack_user_id, domain=domain)
                    auth_url = auth_response.get("auth_url")
                except Exception as auth_err:
                    print(f"⚠️ Failed to fetch auth URL: {auth_err}")
                    auth_url = None

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]
                if auth_url:
                    blocks.append({
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Re-connect GitHub",
                                    "emoji": True
                                },
                                "url": auth_url,
                                "action_id": "connect_github",
                                "style": "danger"
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "I've Connected - Resume",
                                    "emoji": True
                                },
                                "action_id": "resume_scan",
                                "value": json.dumps({"domain": domain}),
                                "style": "primary"
                            }
                        ]
                    })
            elif error_code == "MISSING_CONFIG":
                user_message = "❌ I don't have scan data for this site. Let me run a scan first."
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]
            else:
                user_message = f"❌ Something went wrong setting up the articles directory: {error_message}\n\nLet me know if you'd like to try again."
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": user_message
                        }
                    }
                ]

            # Post in thread if context available, otherwise DM
            if channel_id and thread_ts:
                try:
                    post_message(
                        channel=channel_id,
                        text=f"Error during {stage}",
                        thread_ts=thread_ts,
                        blocks=blocks
                    )
                    print(f"✅ Posted error to thread {thread_ts}")
                except Exception as e:
                    print(f"⚠️ Failed to post in thread, falling back to DM: {e}")
                    dm_channel = from_slack_client_open_dm(slack_user_id)
                    if dm_channel:
                        post_message(
                            channel=dm_channel,
                            text=f"Error during {stage}",
                            blocks=blocks
                        )
            else:
                # Fallback to DM
                print(f"⚠️ No thread context, sending DM to {slack_user_id}")
                dm_channel = from_slack_client_open_dm(slack_user_id)
                if dm_channel:
                    post_message(
                        channel=dm_channel,
                        text=f"Error during {stage}",
                        blocks=blocks
                    )

            return {"status": "ok"}

    except Exception as e:
        print(f"❌ Error handling content factory callback: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ignored"}


# Import helper to avoid circular imports if needed, or just use existing
from .slack_client import open_dm as from_slack_client_open_dm



@app.post("/slack/actions")
async def slack_actions(request: Request):
    """Handle interactive actions (e.g. button clicks)."""
    form = await request.form()
    payload_json = form.get("payload")
    if not payload_json:
        return JSONResponse(status_code=400, content={"error": "Missing payload"})
        
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})
        
    actions = payload.get("actions", [])
    if not actions:
        return JSONResponse(status_code=200, content={})
        
    action_id = actions[0].get("action_id")
    user_id = payload.get("user", {}).get("id")
    channel_id = payload.get("channel", {}).get("id")
    # Interactive messages structure is slightly different for TS
    message = payload.get("message", {})
    thread_ts = message.get("thread_ts") or message.get("ts")
    
    print(f"🖱️ Action: {action_id} from {user_id}")
    
    if action_id == "resume_scan":
        print(f"🔄 Resuming scan/writing for {user_id} via button click")
        
        # Acknowledge immediately (prevents timeout error on button)
        # In background, trigger the scan command as if user typed "@Roo scan repo"
        # This will trigger the backend scan and subsequent flow
        agent = get_agent()
        asyncio.create_task(agent.handle_mention(
            text="scan repo",
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts
        ))
        
        # We could update the message here to say "Resuming...", but ack is sufficient for now.
        return JSONResponse(status_code=200, content={})

    if action_id == "confirm_content_factory":
        print(f"✅ User {user_id} confirmed content factory requirements")

        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        original_text = value_data.get("text")
        original_channel_id = value_data.get("channel_id") or channel_id
        original_thread_ts = value_data.get("thread_ts") or thread_ts
        param_overrides = value_data.get("params") or {}
        if not isinstance(param_overrides, dict):
            param_overrides = {}

        if not original_text:
            if channel_id:
                post_message(
                    channel_id,
                    "I couldn't recover your original content request. Please resend the article request so I can continue.",
                    thread_ts=thread_ts,
                )
            return JSONResponse(status_code=200, content={})

        resumed_params = {**param_overrides, "confirmed": True}

        asyncio.create_task(_handle_mention(
            {
                "user": user_id,
                "text": original_text,
                "channel": original_channel_id,
                "thread_ts": original_thread_ts,
                "param_overrides": resumed_params,
            }
        ))
        return JSONResponse(status_code=200, content={})

    if action_id == "publish_content_pr":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value) if value else {}
        except json.JSONDecodeError:
            value_data = {}

        job_id = str(value_data.get("job_id") or "").strip()
        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")
        reply_channel = value_channel_id or channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        if user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who requested this article can publish it as a PR."
            })

        if not job_id:
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text="⚠️ I couldn't determine which completed article bundle to publish from this button. Please use the original content-ready thread."
                )
            return JSONResponse(status_code=200, content={})

        _remember_content_thread_context(
            reply_channel,
            reply_thread_ts,
            domain,
            "publish_pr",
            active_job_id=job_id,
        )

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = []
            for block in original_blocks:
                if block.get("block_id") == "content_ready_publish_actions":
                    continue
                if block.get("type") == "actions":
                    elements = [
                        element for element in block.get("elements", [])
                        if element.get("action_id") != "publish_content_pr"
                    ]
                    if not elements:
                        continue
                    updated_blocks.append({**block, "elements": elements})
                    continue
                updated_blocks.append(block)
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "⏳ Publishing this article as a draft PR..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update publish-content-pr message: {e}")

        asyncio.create_task(_handle_mention(
            {
                "user": user_id,
                "text": "publish this article as a PR",
                "channel": reply_channel,
                "thread_ts": reply_thread_ts,
                "param_overrides": {
                    "action": "publish_pr",
                    "job_id": job_id,
                    "domain": domain,
                },
            }
        ))
        return JSONResponse(status_code=200, content={})

    # Handler for confirm_topic (new format)
    # Value format: "confirm:{keyword}:{job_id}"
    if action_id == "confirm_topic":
        value = actions[0].get("value", "")
        if not value or not value.startswith("confirm:"):
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        parts = value.split(":", 2)  # Split into max 3 parts
        if len(parts) < 3:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        keyword = parts[1]
        job_id = parts[2]

        print(f"✅ User {user_id} confirmed topic '{keyword}' for job {job_id}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                confirmed_keyword=keyword,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
            follow_up = _build_confirm_topic_follow_up(result)
            return _confirm_topic_json_response(keyword, follow_up)
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            import traceback
            traceback.print_exc()
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error confirming topic: {e}"
            })

    # Handler for select_alternative (dropdown selection)
    # Value format: "confirm:{keyword}:{job_id}"
    if action_id == "select_alternative":
        selected_option = actions[0].get("selected_option", {})
        value = selected_option.get("value", "")
        if not value or not value.startswith("confirm:"):
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        parts = value.split(":", 2)
        if len(parts) < 3:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        keyword = parts[1]
        job_id = parts[2]

        print(f"✅ User {user_id} selected alternative topic '{keyword}' for job {job_id}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                confirmed_keyword=keyword,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
            follow_up = _build_confirm_topic_follow_up(result)
            return _confirm_topic_json_response(keyword, follow_up)
        except Exception as e:
            print(f"❌ Failed to confirm alternative topic: {e}")
            import traceback
            traceback.print_exc()
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error confirming topic: {e}"
            })

    # Handler for cancel_topic (new format)
    # Value format: "cancel:{job_id}"
    if action_id == "cancel_topic":
        value = actions[0].get("value", "")
        job_id = value.split(":", 1)[1] if ":" in value else None

        print(f"❌ User {user_id} cancelled topic selection for job {job_id}")

        # Optionally notify backend to clean up the job
        if job_id:
            try:
                from .clients.mlai_backend import MLAIBackendClient
                settings = get_settings()
                client = MLAIBackendClient(
                    base_url=settings.MLAI_BACKEND_URL,
                    api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
                )
                await client.cancel_job(job_id, user_id)
            except Exception as e:
                print(f"⚠️ Failed to notify backend of cancellation: {e}")

        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": True,
            "text": "❌ Job cancelled.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "❌ Job cancelled."
                    }
                }
            ]
        })

    # Handler for scaffold_confirm
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "scaffold_confirm":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        delivery_mode = value_data.get("delivery_mode")
        scan_run_id = str(value_data.get("scan_run_id") or "").strip()

        # Get message context for button removal
        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        # Security check: only the original user can confirm
        if user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who initiated the scan can confirm this action."
            })

        print(f"📁 User {user_id} confirmed scaffold for {domain}")

        # Use value's thread context if available, fallback to message context
        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        if not scan_run_id:
            try:
                from .slack_client import get_slack_client
                slack_client = get_slack_client()
                original_blocks = payload.get("message", {}).get("blocks", [])
                updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                updated_blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "⚠️ This scaffold button is from an older scan. Run a fresh scan and try again."}]
                })
                slack_client.chat_update(
                    channel=msg_channel,
                    ts=msg_ts,
                    text=payload.get("message", {}).get("text", ""),
                    blocks=updated_blocks
                )
            except Exception as e:
                print(f"⚠️ Failed to update stale scaffold message: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"⚠️ I need a fresh scan result before I can create the articles directory for *{domain}*. Please run the scan again."
            )
            return JSONResponse(status_code=200, content={})

        # 1. Remove buttons and show status via context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Creating articles directory..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # 2. Call mlai-backend scaffold API
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.decide_scaffold(
                scan_run_id=scan_run_id,
                decision="approve",
                domain=domain,
                slack_user_id=user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
            )

            status_code = result.get("status_code")
            data = result.get("data", {})

            if scan_run_id and status_code in {200, 202}:
                scaffold_job_id = data.get("scaffold_job_id")
                pending = _get_pending_intent(
                    user_id,
                    domain,
                    wait_for="scan_complete",
                    consume=True,
                )
                if pending and pending.get("action") == "write" and scaffold_job_id:
                    _remember_pending_intent(
                        user_id,
                        domain,
                        intent_data=pending,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scaffold_complete",
                        job_id=scaffold_job_id,
                        clear_job_id=True,
                    )
                print(f"✅ Scaffold initiated for {domain}")
            elif status_code == 200:
                # Already scaffolded
                pr_url = data.get("pr_url", "")
                preview_url = data.get("preview_url", "")
                details = []
                if pr_url:
                    details.append(f"PR: {pr_url}")
                if preview_url:
                    details.append(f"Preview: {preview_url}")
                detail_text = f" {' | '.join(details)}" if details else ""
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                )
            elif status_code == 400:
                error = data.get("error", "Unknown error")
                if data.get("needs_github_auth"):
                    oauth_url = data.get("oauth_url", "")
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {oauth_url}"
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Could not start scaffolding: {error}"
                    )
            elif status_code == 404:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ No configuration found for *{domain}*."
                    )
            elif status_code == 202:
                print(f"✅ Scaffold initiated for {domain}")
            elif status_code == 409:
                error = data.get("detail") or data.get("error") or "This scaffold request is no longer awaiting approval."
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"⚠️ Could not create articles directory for *{domain}*: {error}"
                )
            else:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Unexpected response from backend (status {status_code})"
                )

        except Exception as e:
            print(f"❌ Failed to trigger scaffold: {e}")
            import traceback
            traceback.print_exc()
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error creating articles directory: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for scaffold_skip
    # Value format: JSON string with domain
    if action_id == "scaffold_skip":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain", "your site")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        scan_run_id = str(value_data.get("scan_run_id") or "").strip()

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        if original_user_id and user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who initiated the scan can confirm this action."
            })

        print(f"⏭️ User {user_id} skipped scaffold for {domain}")

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Skipped. You can scaffold later with `@Roo scaffold {domain}`"}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        if scan_run_id:
            reply_channel = value_channel_id or msg_channel
            reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts
            from .clients.mlai_backend import MLAIBackendClient
            settings = get_settings()
            backend_client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
            )
            try:
                result = await backend_client.decide_scaffold(
                    scan_run_id=scan_run_id,
                    decision="deny",
                    domain=domain,
                    slack_user_id=user_id,
                    slack_channel_id=reply_channel,
                    slack_thread_ts=reply_thread_ts,
                )
                if result.get("status_code") not in {200, 202, 409}:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"⚠️ Could not record scaffold skip for *{domain}*."
                    )
                _get_pending_intent(
                    user_id,
                    domain,
                    wait_for="scan_complete",
                    consume=True,
                )
            except Exception as e:
                print(f"⚠️ Failed to deny scaffold approval: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for write_first_article
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "write_first_article":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        client_request_id = _content_factory_client_request_id(value_data.get("client_request_id"))
        delivery_mode = value_data.get("delivery_mode")
        delivery_mode_confirmed = bool(value_data.get("delivery_mode_confirmed")) if delivery_mode is not None else None

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        if user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who initiated the scan can confirm this action."
            })

        print(f"✍️ User {user_id} requested first article for {domain}")

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        # 1. Remove buttons and show status via context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            updated_blocks = build_live_status_blocks(
                domain,
                summary_text="Starting article generation. I'll keep this message updated.",
                include_decision_stage=False,
                current_stage="preparing",
            )
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"Starting Content Factory for {domain}",
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # 2. Trigger article generation (no topic = auto-research)
        from .clients.mlai_backend import MLAIBackendClient
        from .slack_client import get_user_info
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            slack_info = get_user_info(user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            result = await backend_client.trigger_article_generation(
                slack_user_id=user_id,
                domain=domain,
                delivery_mode=delivery_mode,
                delivery_mode_confirmed=delivery_mode_confirmed,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                progress_message_ts=msg_ts,
                client_request_id=client_request_id,
                request_source="roo_slackbot",
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
            )
            print(f"✅ Article generation triggered for {domain}: {result}")
            if result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "write",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        except Exception as e:
            print(f"❌ Failed to trigger article generation: {e}")
            import traceback
            traceback.print_exc()
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting article generation: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for write_article_skip
    # Value format: JSON string with domain
    if action_id in ("write_article_skip", "write_skip"):
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain", "your site")

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        print(f"⏭️ User {user_id} skipped first article for {domain}")

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Skipped. Say `@Roo write me an article about [topic]` anytime."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for article_research_best
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "article_research_best":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        client_request_id = _content_factory_client_request_id(value_data.get("client_request_id"))
        delivery_mode = value_data.get("delivery_mode")
        delivery_mode_confirmed = bool(value_data.get("delivery_mode_confirmed")) if delivery_mode is not None else None

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        if user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who initiated this request can choose the article direction."
            })

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts
        print(f"🔍 User {user_id} chose article discovery for {domain}")
        _remember_content_thread_context(reply_channel, reply_thread_ts, domain, "research")

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            updated_blocks = build_live_status_blocks(
                domain,
                summary_text="Starting discovery to find the best article opportunity. I'll keep this message updated.",
                include_decision_stage=True,
                current_stage="preparing",
            )
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=f"Starting Content Factory for {domain}",
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        from .clients.mlai_backend import MLAIBackendClient
        from .slack_client import get_user_info

        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )
        try:
            slack_info = get_user_info(user_id)
            real_name = str(slack_info.get("real_name") or "").strip()
            name_parts = real_name.split(" ", 1) if real_name else []
            result = await backend_client.trigger_article_generation(
                slack_user_id=user_id,
                domain=domain,
                delivery_mode=delivery_mode,
                delivery_mode_confirmed=delivery_mode_confirmed,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                progress_message_ts=msg_ts,
                client_request_id=client_request_id,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
                user_email=str(slack_info.get("email") or "").strip().lower() or None,
                user_first_name=name_parts[0] if name_parts else None,
                user_last_name=name_parts[1] if len(name_parts) > 1 else None,
                user_avatar_url=str(slack_info.get("image_192") or "").strip() or None,
            )
            print(f"✅ Article discovery triggered for {domain}: {result}")
            if str(result.get("status") or "").strip().lower() == "awaiting_delivery_mode":
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "awaiting_delivery_mode",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                )
                try:
                    from .slack_client import get_slack_client
                    get_slack_client().chat_update(
                        channel=msg_channel,
                        ts=msg_ts,
                        text=f"Choose delivery mode for {domain}",
                        blocks=_build_article_delivery_mode_blocks(
                            domain=domain,
                            job_id=result.get("job_id") or result.get("run_id"),
                            recommended_delivery_mode=result.get("recommended_delivery_mode"),
                        ),
                    )
                except Exception as update_error:
                    print(f"⚠️ Failed to update delivery-mode prompt: {update_error}")
            elif result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    reply_channel,
                    reply_thread_ts,
                    domain,
                    "research",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))
        except Exception as e:
            print(f"❌ Failed to trigger article discovery: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting article discovery: {e}"
            )
        return JSONResponse(status_code=200, content={})

    # Handler for article_provide_topic
    # Value format: JSON string with domain, slack_user_id, channel_id, thread_ts
    if action_id == "article_provide_topic":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        delivery_mode = value_data.get("delivery_mode")

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        if user_id != original_user_id:
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": "⚠️ Only the user who initiated this request can choose the article direction."
            })

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts
        is_dm = bool(reply_channel and reply_channel.startswith("D"))
        normalized_domain = normalize_content_factory_domain(domain) or domain or "this domain"
        article_cost_points = get_content_factory_article_cost_points(domain)
        cost_guidance = (
            f"Articles for {normalized_domain} are free."
            if article_cost_points == 0
            else f"Starting the article run deducts {article_cost_points} Roo points."
        )
        guidance_text = (
            "✅ Reply here with the topic you want me to write about. "
            "I'll still research the best keywords, title, and talking points so it has the best chance to rank. "
            f"{cost_guidance}"
            if is_dm else
            "✅ Reply in this thread with something like `@Roo write about AI for clinic workflows`. "
            "I'll still research the best keywords, title, and talking points so it has the best chance to rank. "
            f"{cost_guidance}"
        )
        if delivery_mode == "content_only":
            guidance_text += " I'll keep this in content-only mode unless you tell me otherwise."
        elif delivery_mode == "publish_code":
            guidance_text += " I'll keep this in publish-via-code mode unless you tell me otherwise."

        print(f"📝 User {user_id} will provide the article topic for {domain}")
        _remember_content_thread_context(reply_channel, reply_thread_ts, domain, "write")

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": guidance_text}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    if action_id == "select_article_delivery_mode":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        job_id = value_data.get("job_id")
        domain = value_data.get("domain")
        delivery_mode = value_data.get("delivery_mode")

        if not job_id or delivery_mode not in {"content_only", "publish_code"}:
            return JSONResponse(status_code=400, content={"error": "job_id and delivery_mode are required"})

        from .clients.mlai_backend import MLAIBackendClient

        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            result = await client.set_article_delivery_mode(
                job_id,
                delivery_mode,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
            if result.get("job_id") or result.get("run_id"):
                _remember_content_thread_context(
                    payload.get("channel", {}).get("id"),
                    payload.get("message", {}).get("thread_ts") or payload.get("message", {}).get("ts"),
                    domain,
                    "awaiting_delivery_mode",
                    active_job_id=result.get("job_id") or result.get("run_id"),
                )
                asyncio.create_task(_watch_content_factory_quiet_run(result.get("job_id") or result.get("run_id")))

            selected_label = "content-only" if delivery_mode == "content_only" else "publish-via-code"
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": True,
                "text": f"⏳ {selected_label} selected. Roo is continuing the article run now.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⏳ *{selected_label}* selected for *{domain or 'this domain'}*.\nRoo is continuing the article run now.",
                        },
                    }
                ],
            })
        except httpx.HTTPStatusError as exc:
            error_data = {}
            try:
                error_data = exc.response.json()
            except Exception:
                error_data = {"error": str(exc)}

            if exc.response.status_code == 412 and error_data.get("error_code") == "PUBLISH_TARGET_ACTION_REQUIRED":
                auth_url = None
                if domain:
                    auth_response = await client.get_github_auth_url(user_id, domain=domain)
                    auth_url = auth_response.get("auth_url")

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Publish mode isn't ready for *{domain or 'this domain'}* yet.\n\n"
                                "I need a connected GitHub repo before I can open the PR flow. "
                                "You can connect GitHub now or switch back to content-only."
                            ),
                        },
                    },
                ]
                if auth_url:
                    blocks.append(
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": f"Connect GitHub for {domain or 'this domain'}", "emoji": True},
                                    "url": auth_url,
                                    "action_id": "connect_github",
                                    "style": "primary",
                                }
                            ],
                        }
                    )
                return JSONResponse(status_code=200, content={
                    "response_type": "ephemeral",
                    "replace_original": True,
                    "text": error_data.get("error") or error_data.get("message") or "Publish mode needs GitHub first.",
                    "blocks": blocks,
                })

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error selecting delivery mode: {error_data.get('error', str(exc))}",
            })

    # Handler for article_system_* decisions
    if action_id in {"article_system_use_detected", "article_system_rescan", "article_system_scaffold"}:
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        decision = {
            "article_system_use_detected": "use_detected",
            "article_system_rescan": "rescan",
            "article_system_scaffold": "scaffold",
        }[action_id]
        pending_wait_for = {
            "rescan": "scan_complete",
            "scaffold": "scaffold_complete",
        }.get(decision)

        progress_text = {
            "use_detected": "✅ Using detected article structure...",
            "rescan": "✅ Re-scanning repository...",
            "scaffold": "✅ Creating articles directory...",
        }[decision]

        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": progress_text}],
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks,
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        )

        try:
            result = await backend_client.decide_article_system(
                domain=domain,
                slack_user_id=user_id,
                decision=decision,
            )
            status_code = result.get("status_code")
            data = result.get("data", {})

            if status_code in {200, 202}:
                if original_intent and pending_wait_for:
                    pending = _remember_pending_intent(
                        user_id,
                        domain,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for=pending_wait_for,
                        job_id=_extract_job_id(data),
                        clear_job_id=True,
                    )
                    if pending:
                        print(f"   📌 Stored pending intent: {original_intent.get('action')} for {user_id}:{domain}")
                elif decision == "use_detected":
                    _get_pending_intent(user_id, domain, consume=True)

                if decision == "use_detected":
                    detected = (
                        (data.get("article_system") or {}).get("directory_path")
                        or (data.get("article_system") or {}).get("directory_name")
                        or "the detected article directory"
                    )
                    if data.get("resume_triggered"):
                        post_message(
                            channel=reply_channel,
                            thread_ts=reply_thread_ts,
                            text=(
                                f"✅ Using the detected article system at `{detected}`.\n"
                                f"I've resumed your pending article request."
                            ),
                        )
                    else:
                        post_message(
                            channel=reply_channel,
                            thread_ts=reply_thread_ts,
                            text=f"✅ Using the detected article system at `{detected}`.",
                        )
                elif decision == "rescan":
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"🔄 Re-scanning *{domain}* now. I'll reply here when it's complete.",
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"📁 Setting up the articles directory for *{domain}* now.",
                    )
            else:
                error_msg = data.get("error", f"Unexpected backend response ({status_code})")
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Could not process that article-system decision: {error_msg}",
                )
        except Exception as e:
            print(f"❌ Failed to process article-system decision: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error processing article-system decision: {e}",
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_scan
    # User clicked "Scan Codebase" from a prerequisite message
    if action_id == "prerequisite_scan":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        print(f"🔍 User {user_id} triggered prerequisite scan for {domain}")

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        # Remove buttons and show status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Scanning codebase..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # Trigger scan via backend
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.trigger_repo_scan(
                slack_user_id=user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts,
                domain=domain
            )
            if result.get("error"):
                error_msg = result.get("message", "Unknown error")
                # Check if auth-related error — show reconnect button
                if result.get("needs_github_auth"):
                    oauth_url = result.get("oauth_url")
                    if not oauth_url:
                        try:
                            auth_resp = await backend_client.get_github_auth_url(user_id, domain=domain)
                            oauth_url = auth_resp.get("auth_url")
                        except Exception:
                            oauth_url = None
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"❌ Scan failed: {error_msg}"
                            }
                        }
                    ]
                    if oauth_url:
                        blocks.append({
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Re-connect GitHub",
                                        "emoji": True
                                    },
                                    "url": oauth_url,
                                    "action_id": "connect_github",
                                    "style": "danger"
                                },
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "I've Connected - Resume",
                                        "emoji": True
                                    },
                                    "action_id": "resume_scan",
                                    "value": json.dumps({"domain": domain}),
                                    "style": "primary"
                                }
                            ]
                        })
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scan failed: {error_msg}",
                        blocks=blocks
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scan failed: {error_msg}"
                    )
            else:
                if original_intent:
                    pending = _remember_pending_intent(
                        user_id,
                        domain,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scan_complete",
                        job_id=_extract_job_id(result),
                        clear_job_id=True,
                    )
                    if pending:
                        print(f"   📌 Stored pending intent: {original_intent.get('action')} for {user_id}:{domain}")
                print(f"✅ Scan triggered for {domain}")
        except Exception as e:
            print(f"❌ Failed to trigger scan: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error starting scan: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_scaffold
    # User clicked "Set Up Articles Directory" from a prerequisite message
    if action_id == "prerequisite_scaffold":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON value format"})

        domain = value_data.get("domain")
        original_user_id = value_data.get("slack_user_id")
        value_channel_id = value_data.get("channel_id")
        value_thread_ts = value_data.get("thread_ts")
        original_intent = value_data.get("original_intent", {})

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        print(f"📁 User {user_id} triggered prerequisite scaffold for {domain}")

        reply_channel = value_channel_id or msg_channel
        reply_thread_ts = value_thread_ts or payload.get("message", {}).get("thread_ts") or msg_ts

        # Remove buttons and show status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ Creating articles directory..."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        # Trigger scaffold via backend
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await backend_client.scaffold_articles(
                domain=domain,
                slack_user_id=user_id,
                slack_channel_id=reply_channel,
                slack_thread_ts=reply_thread_ts
            )

            status_code = result.get("status_code")
            data = result.get("data", {})

            if status_code == 200:
                pr_url = data.get("pr_url", "")
                if original_intent and original_intent.get("action") == "write":
                    await _trigger_article_generation_from_pending(
                        {
                            **original_intent,
                            "channel_id": reply_channel,
                            "thread_ts": reply_thread_ts,
                        },
                        slack_user_id=user_id,
                        domain=domain,
                        fallback_channel_id=reply_channel,
                        fallback_thread_ts=reply_thread_ts,
                    )
                else:
                    preview_url = data.get("preview_url", "")
                    details = []
                    if pr_url:
                        details.append(f"PR: {pr_url}")
                    if preview_url:
                        details.append(f"Preview: {preview_url}")
                    detail_text = f" {' | '.join(details)}" if details else ""
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"📁 Articles directory already exists for *{domain}*.{detail_text}"
                    )
            elif status_code == 400:
                error = data.get("error", "Unknown error")
                if data.get("needs_github_auth"):
                    oauth_url = data.get("oauth_url", "")
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ GitHub authentication required for *{domain}*.\n\nPlease reconnect: {oauth_url}"
                    )
                else:
                    post_message(
                        channel=reply_channel,
                        thread_ts=reply_thread_ts,
                        text=f"❌ Scaffolding failed: {error}"
                    )
            elif status_code == 202:
                if original_intent:
                    pending = _remember_pending_intent(
                        user_id,
                        domain,
                        intent_data=original_intent,
                        channel_id=reply_channel,
                        thread_ts=reply_thread_ts,
                        wait_for="scaffold_complete",
                        job_id=_extract_job_id(result),
                        clear_job_id=True,
                    )
                    if pending:
                        print(f"   📌 Stored pending intent: {original_intent.get('action')} for {user_id}:{domain}")
                print(f"✅ Scaffold initiated for {domain}")
            else:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Unexpected response (status {status_code})"
                )

        except Exception as e:
            print(f"❌ Failed to trigger scaffold: {e}")
            post_message(
                channel=reply_channel,
                thread_ts=reply_thread_ts,
                text=f"❌ Error creating articles directory: {e}"
            )

        return JSONResponse(status_code=200, content={})

    # Handler for prerequisite_cancel
    if action_id == "prerequisite_cancel":
        value = actions[0].get("value", "")
        try:
            value_data = json.loads(value)
        except json.JSONDecodeError:
            value_data = {}

        domain = value_data.get("domain", "")
        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        print(f"⏭️ User {user_id} cancelled prerequisite for {domain}")

        # Clear any pending intent
        _get_pending_intent(user_id, domain, consume=True)

        # Replace buttons with context block
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Cancelled."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})

    # Handler for confirm_topic_btn_* (backend sends these during topic selection)
    # Value format: "confirm_topic:{job_id}:{option_index}"
    if action_id.startswith("confirm_topic_btn"):
        value = actions[0].get("value", "")
        if not value or not value.startswith("confirm_topic:"):
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        parts = value.split(":")
        if len(parts) < 2:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        job_id = parts[1]

        try:
            option_index = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            option_index = 0

        print(f"✅ User {user_id} confirmed topic for job {job_id}, option {option_index}")

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        # Call confirm endpoint
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY
        )

        try:
            result = await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                option_index=option_index,
                request_source=CONTENT_FACTORY_REQUEST_SOURCE,
            )
            print(f"✅ Topic confirmed for job {job_id}")
            follow_up = _build_confirm_topic_follow_up(result)
            _remember_content_thread_context(
                payload.get("channel", {}).get("id"),
                payload.get("message", {}).get("thread_ts") or payload.get("message", {}).get("ts"),
                follow_up.get("domain"),
                "awaiting_delivery_mode" if follow_up.get("requires_delivery_mode") else "write",
                active_job_id=follow_up.get("active_job_id"),
            )

            try:
                from .slack_client import get_slack_client

                slack_client = get_slack_client()
                if follow_up.get("requires_delivery_mode"):
                    updated_blocks = _build_article_delivery_mode_blocks(
                        domain=follow_up["domain"],
                        job_id=follow_up["active_job_id"],
                        recommended_delivery_mode=follow_up.get("recommended_delivery_mode"),
                    )
                    updated_text = f"Choose delivery mode for {follow_up['domain']}"
                else:
                    original_blocks = payload.get("message", {}).get("blocks", [])
                    updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
                    updated_blocks.append({
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "✅ Generating article. No additional Roo points will be charged for this confirmation."}]
                    })
                    updated_text = payload.get("message", {}).get("text", "")
                    _maybe_schedule_content_factory_watchdog(
                        follow_up.get("active_job_id"),
                        str(follow_up.get("status") or ""),
                    )

                slack_client.chat_update(
                    channel=msg_channel,
                    ts=msg_ts,
                    text=updated_text,
                    blocks=updated_blocks
                )
            except Exception as e:
                print(f"⚠️ Failed to update message: {e}")
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            import traceback
            traceback.print_exc()
            reply_channel = msg_channel
            reply_thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
            if reply_channel:
                post_message(
                    channel=reply_channel,
                    thread_ts=reply_thread_ts,
                    text=f"❌ Error confirming topic: {e}"
                )

        return JSONResponse(status_code=200, content={})

    # Handler for cancel_topic_btn
    if action_id == "cancel_topic_btn":
        print(f"❌ User {user_id} cancelled topic selection")

        msg_channel = payload.get("channel", {}).get("id")
        msg_ts = payload.get("message", {}).get("ts")

        # Remove buttons and show cancelled status
        try:
            from .slack_client import get_slack_client
            slack_client = get_slack_client()
            original_blocks = payload.get("message", {}).get("blocks", [])
            updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
            updated_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "❌ Cancelled."}]
            })
            slack_client.chat_update(
                channel=msg_channel,
                ts=msg_ts,
                text=payload.get("message", {}).get("text", ""),
                blocks=updated_blocks
            )
        except Exception as e:
            print(f"⚠️ Failed to update message: {e}")

        return JSONResponse(status_code=200, content={})



    return JSONResponse(status_code=200, content={})
