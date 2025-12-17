"""
Roo Standalone - FastAPI Application

Main entrypoint for the Roo AI agent service.
"""
import json
import hmac
import hashlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse

from .config import get_settings, Settings
from .agent import RooAgent, get_agent
from .slack_client import post_message


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()
    print(f"🦘 Roo Standalone starting...")
    print(f"   LLM Provider: {settings.default_llm_provider}")
    print(f"   Skills Dir: {settings.SKILLS_DIR}")
    
    # Initialize database
    from .database import get_db
    # db = get_db()
    # await db.init_integrations_table()
    
    # Initialize agent on startup
    agent = get_agent()
    print(f"   Loaded {len(agent.skills)} skills")
    
    yield
    
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
    
    if event_type == "app_mention":
        # Process mention asynchronously
        import asyncio
        asyncio.create_task(_handle_mention(event))
        return JSONResponse(status_code=200, content={})
    
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        is_dm = event.get("channel_type") == "im"
        if is_dm:
            print(f"📨 Received DM from {event.get('user')}")
            import asyncio
            asyncio.create_task(_handle_mention(event))
            return JSONResponse(status_code=200, content={})
    
    return JSONResponse(status_code=200, content={})


async def _handle_mention(event: dict):
    """Handle an @Roo mention asynchronously."""
    try:
        user_id = event.get("user")
        text = event.get("text", "")
        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        
        print(f"\n🦘 ROO MENTION: from {user_id} in {channel_id}")
        print(f"   Text: {text[:100]}...")
        
        agent = get_agent()
        result = await agent.handle_mention(
            text=text,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts
        )
        
        if result.get("message"):
            post_message(
                channel=channel_id,
                text=result["message"],
                thread_ts=thread_ts
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
        
        if result.get("message"):
            post_message(
                channel=channel_id,
                text=result["message"],
                thread_ts=thread_ts
            )
            
    except Exception as e:
        print(f"❌ Error resuming intent: {e}")
        if intent.get("channel"):
            post_message(intent["channel"], "Sorry, I had trouble resuming your request.", intent.get("ts"))


@app.post("/slack/commands")
async def slack_commands(request: Request):
    """Slack Slash Commands webhook."""
    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "")
    user_id = form.get("user_id", "")
    
    print(f"📨 Slash command: {command} from {user_id}")
    
    return {
        "response_type": "ephemeral",
        "text": f"Command '{command}' received! (Not yet implemented)"
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


@app.get("/auth/github/login")
async def github_login(state: str):
    """
    Redirect to GitHub OAuth login.
    state: The slack_user_id to bind the token to.
    """
    settings = get_settings()
    if not settings.GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub Client ID not configured")

    scope = "repo user:email"
    redirect_uri = f"{settings.SLACK_APP_URL}/auth/github/callback"
    
    # Construct GitHub OAuth URL
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.GITHUB_CLIENT_ID}"
        f"&scope={scope}"
        f"&state={state}"
        f"&redirect_uri={redirect_uri}"
    )
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/auth/github/callback")
async def github_callback(code: str, state: str):
    """
    Handle GitHub OAuth callback.
    Exchanges code for access token and saves it.
    """
    settings = get_settings()
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="GitHub credentials not configured")
        
    # Exchange code for token
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{settings.SLACK_APP_URL}/auth/github/callback"
            }
        )
        data = response.json()
        
    access_token = data.get("access_token")
    if not access_token:
        error = data.get("error_description") or "Unknown error"
        return JSONResponse(status_code=400, content={"error": f"Failed to get token: {error}"})
        
    # Get user info for metadata (optional but good for logs)
    user_name = "unknown"
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {access_token}",
                "Accept": "application/json"
            }
        )
        if user_resp.status_code == 200:
            user_data = user_resp.json()
            user_name = user_data.get("login", "unknown")

    # Save to database
    from .database import get_db
    db = get_db()
    
    # state param contains the slack_user_id
    slack_user_id = state
    
    await db.save_github_token(
        slack_user_id=slack_user_id,
        token=access_token,
        user_name=user_name,
        scopes=["repo", "user:email"]
    )
    
    # Notify user in Slack
    from .slack_client import send_dm
    send_dm(
        slack_user_id,
        f"🎉 success! I've connected to your GitHub account (`{user_name}`).\nYou can now ask me to scan your repos!"
    )

    # Check for pending intent
    integration = await db.get_integration(slack_user_id)
    pending_intent = integration.get("pending_intent") if integration else None
    
    if pending_intent:
        import json
        try:
            intent = json.loads(pending_intent)
            
            # Clear it immediately
            await db.clear_pending_intent(slack_user_id)
            
            # Resume asynchronously
            import asyncio
            asyncio.create_task(_resume_intent(slack_user_id, intent))
            
        except Exception as e:
            print(f"Failed to resume intent: {e}")

    return JSONResponse(content={"status": "success", "message": "GitHub connected! You can close this window."})
