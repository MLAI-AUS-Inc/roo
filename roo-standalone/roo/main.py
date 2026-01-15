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

    # Process Quests
    try:
        from .quests import handle_quests
        import asyncio
        asyncio.create_task(handle_quests(event))
    except Exception as e:
        print(f"⚠️ Quest processing failed: {e}")
    
    if event_type == "app_mention":
        # Process mention asynchronously
        import asyncio
        asyncio.create_task(_handle_mention(event))
        return JSONResponse(status_code=200, content={})
    
    if event_type == "message" and not event.get("bot_id") and not event.get("subtype"):
        # Note: #_start-here logic is now handled by quests.py
        
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
    
    print(f"📨 Slash command: {command} {text} from {user_id}")
    
    if command == "/roo" and "connect type" in text:  # Handle other connects?
        pass

    # Handle "connect github"
    if "connect github" in text.lower():
        settings = get_settings()
        from .skills.mlai_points.client import MLAIBackendClient
        
        try:
            client = MLAIBackendClient(
                base_url=settings.MLAI_BACKEND_URL,
                api_key=settings.MLAI_API_KEY
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


@app.post("/api/callbacks/content-factory")
async def content_factory_callback(request: Request):
    """
    Handle callbacks from mlai-backend Content Factory.
    
    Supports:
    - topic_selection: When a topic is proposed for user confirmation
    - article_complete: When an article has been generated
    """
    try:
        payload = await request.json()
        event_type = payload.get("event_type")
        slack_user_id = payload.get("slack_user_id")
        
        print(f"🏭 Content Factory event: {event_type} for {slack_user_id}")
        
        if event_type == "topic_selection":
            # Extract data
            selection = payload.get("selection", {})
            keyword = selection.get("selected_keyword")
            reason = selection.get("selection_reason")
            volume = selection.get("volume")
            difficulty = selection.get("difficulty")
            tier = selection.get("tier", "").replace("_", " ").title()
            score = selection.get("opportunity_index")
            alternatives = selection.get("top_alternatives", [])
            job_id = payload.get("job_id")
            domain = payload.get("domain")
            
            # Format Block Kit message
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Article Topic Selected",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"I've researched content opportunities for *{domain}* and found:"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recommended:* `{keyword}`\n• Volume: {volume}/mo • Difficulty: {difficulty}/100\n• Tier: 🔵 {tier}\n• Score: {score}"
                    }
                }
            ]
            
            # Add alternatives if available
            if alternatives:
                alt_text = "\n".join([f"• {alt}" for alt in alternatives])
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Alternatives:*\n{alt_text}"
                    }
                })
            
            # Prepare alternative options for select menu
            alt_options = []
            for alt in alternatives:
                # Ensure text and value limits
                text = alt[:75]
                value = f"{job_id}|{domain}|{alt}"
                alt_options.append({
                    "text": {
                        "type": "plain_text",
                        "text": text,
                        "emoji": True
                    },
                    "value": value
                })

            # Action Buttons
            actions = [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Write This Article",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": f"{job_id}|{domain}|{keyword}",
                    "action_id": "content_factory_confirm"
                }
            ]
            
            if alt_options:
                actions.append({
                    "type": "static_select",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "🔄 Pick Alternative",
                        "emoji": True
                    },
                    "options": alt_options,
                    "action_id": "content_factory_select_alt"
                })
                
            actions.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": "cancel",
                "action_id": "content_factory_cancel"
            })
            
            blocks.append({
                "type": "actions",
                "elements": actions
            })
            
            # Send DM
            dm_channel = from_slack_client_open_dm(slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Topic selected: {keyword}",  # Fallback text
                    blocks=blocks
                )
            return {"status": "ok"}
            
        elif event_type == "article_complete":
            article_url = payload.get("article_url")
            pr_url = payload.get("pr_url")
            domain = payload.get("domain")
            
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🎉 *Article Published for {domain}*!"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Your article is ready and a pull request has been created.\n\n🔗 *< {article_url} | View Live Article >*\n🔨 *< {pr_url} | View Pull Request >*"
                    }
                }
            ]
            
            dm_channel = from_slack_client_open_dm(slack_user_id)
            if dm_channel:
                post_message(
                    channel=dm_channel,
                    text=f"Article complete: {article_url}",
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
        import asyncio
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
        
        # Trigger re-entry into the skill, but this time imply confirmation
        import asyncio
        agent = get_agent()
        
        # We act as if the user said "Proceed with content factory confirmed=true"
        # The LLM extraction in executor.py will pick up 'confirmed': True (or we hope so)
        # Actually, to be safer, we should rely on the saved pending intent or just
        # send a text that explicitly sets the param if possible, OR
        # better yet, since we use LLM extraction, adding "confirmed=true" to text might work
        # providing the LLM understands it.
        # Let's try sending a clear directive.
        
        asyncio.create_task(agent.handle_mention(
            text="Proceed with content factory. confirmed=True",
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts
        ))
        return JSONResponse(status_code=200, content={})

    if action_id == "content_factory_confirm":
        value = actions[0].get("value", "")
        if not value:
            return JSONResponse(status_code=400, content={"error": "Missing value"})
            
        # Value format: job_id|domain|keyword
        parts = value.split("|")
        if len(parts) < 3:
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})
            
        job_id, domain, keyword = parts[0], parts[1], parts[2]
        
        print(f"✅ User {user_id} confirmed topic: {keyword} for job {job_id}")
        
        # Call backend to confirm
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY
        )
        
        # Respond immediately to update UI
        try:
             # We fire and forget the confirmation to keep UI snappy, or await it if fast enough.
             # Better to await to handle errors
            await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                domain=domain,
                confirmed_keyword=keyword
            )
            
            # Update message to remove buttons and show confirmation
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral", # Or update in place if we could
                "replace_original": "true",
                "text": f"✅ Topic *{keyword}* confirmed! Generating article now...",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ Topic *{keyword}* confirmed! Generating article now..."
                        }
                    }
                ]
            })
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error confirming topic: {e}"
            })

    # Handler for confirm_topic_btn (sent by mlai-backend)
    # Value format: "confirm_topic:{job_id}"
    if action_id == "confirm_topic_btn":
        value = actions[0].get("value", "")
        if not value or not value.startswith("confirm_topic:"):
            return JSONResponse(status_code=400, content={"error": "Invalid value format"})

        job_id = value.replace("confirm_topic:", "")
        print(f"✅ User {user_id} confirmed topic for job {job_id} (via mlai-backend button)")

        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY
        )

        try:
            await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id
            )

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": "true",
                "text": "✅ Topic confirmed! Generating article now...",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "✅ Topic confirmed! Generating article now..."
                        }
                    }
                ]
            })
        except Exception as e:
            print(f"❌ Failed to confirm topic: {e}")
            import traceback
            traceback.print_exc()
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error confirming topic: {e}"
            })

    # Handler for cancel_topic_btn (sent by mlai-backend)
    if action_id == "cancel_topic_btn":
        print(f"❌ User {user_id} cancelled topic selection")
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": "true",
            "text": "❌ Article generation cancelled.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "❌ Article generation cancelled."
                    }
                }
            ]
        })

    if action_id == "content_factory_select_alt":
        # Handle dropdown selection
        # For now, we just acknowledge it and let the user click "Write This Article"
        # Ideally, we would update the message to swap the confirmed topic.
        # But Block Kit dynamic updates are complex without a full interactive backend state.
        # Simplest approach: The dropdown IS the confirmation/selection action, OR
        # better: The dropdown selection triggers a confirmation immediately.
        
        selected_option = actions[0].get("selected_option")
        if not selected_option:
            return JSONResponse(status_code=200, content={})
            
        value = selected_option.get("value")
        # Value format: job_id|domain|keyword
        parts = value.split("|")
        if len(parts) < 3:
             return JSONResponse(status_code=200, content={})
             
        job_id, domain, keyword = parts[0], parts[1], parts[2]
        
        # Confirm immediately
        from .clients.mlai_backend import MLAIBackendClient
        settings = get_settings()
        client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.MLAI_API_KEY
        )
        
        try:
            await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                domain=domain,
                confirmed_keyword=keyword
            )
             
            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                 "replace_original": "true",
                "text": f"✅ Alternative topic *{keyword}* selected! Generating article now...",
                 "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"✅ Alternative topic *{keyword}* selected! Generating article now..."
                        }
                    }
                ]
            })
        except Exception as e:
             return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "text": f"❌ Error confirming topic: {e}"
            })

    if action_id == "content_factory_cancel":
        return JSONResponse(status_code=200, content={
            "response_type": "ephemeral",
            "replace_original": "true",
            "text": "❌ Article generation cancelled.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "❌ Article generation cancelled."
                    }
                }
            ]
        })

    return JSONResponse(status_code=200, content={})
