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
            api_key=settings.MLAI_API_KEY
        )

        try:
            await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                confirmed_keyword=keyword
            )

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": True,
                "text": f"⏳ Queued generation for `{keyword}`...",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⏳ Queued generation for `{keyword}`..."
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
            api_key=settings.MLAI_API_KEY
        )

        try:
            await client.confirm_article_topic(
                job_id=job_id,
                slack_user_id=user_id,
                confirmed_keyword=keyword
            )

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": True,
                "text": f"⏳ Queued generation for `{keyword}`...",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"⏳ Queued generation for `{keyword}`..."
                        }
                    }
                ]
            })
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
                    api_key=settings.MLAI_API_KEY
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

    # Legacy handlers for backwards compatibility
    # Handler for confirm_topic_btn (legacy format)
    # Value format: "confirm_topic:{job_id}:{index}"
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

        print(f"✅ User {user_id} confirmed topic for job {job_id}, option {option_index} (legacy)")

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
                option_index=option_index
            )

            return JSONResponse(status_code=200, content={
                "response_type": "ephemeral",
                "replace_original": True,
                "text": "⏳ Queued generation...",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⏳ Queued generation..."
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

    # Handler for cancel_topic_btn (legacy)
    if action_id == "cancel_topic_btn":
        print(f"❌ User {user_id} cancelled topic selection (legacy)")
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



    return JSONResponse(status_code=200, content={})
