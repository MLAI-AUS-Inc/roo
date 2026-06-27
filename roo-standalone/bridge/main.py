"""
Bridge service.

A dedicated "Bridge" Slack app is installed in both workspaces; this service
holds both bot tokens. Store-and-forward design:

  * two channel pollers CAPTURE new messages into the inbound DB queue, and
  * one delivery worker drains the queue, posting into the other workspace with
    retries.

No inbound webhooks; runs as its own process on the droplet, independent of Roo.
A minimal FastAPI app exposes GET /healthz for nginx/monitoring.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from typing import List

from fastapi import FastAPI

from .config import get_bridge_settings
from .identity import get_resolver
from .poller import channel_poll_loop, delivery_loop, poll_error_backoff
from .relay import Relay
from .slack import make_bot_client, resolve_identity
from .store import get_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_bridge_settings()
    store = get_store()
    identity = get_resolver()

    print("🌉 Slack bridge starting...")
    print(f"   MLAI channel: {settings.MLAI_CHANNEL_ID}")
    print(f"   S&C channel:  {settings.SNC_CHANNEL_ID}")

    mlai_client = make_bot_client(settings.MLAI_BOT_TOKEN)
    try:
        me = resolve_identity(mlai_client)
        settings.MLAI_BOT_USER_ID = settings.MLAI_BOT_USER_ID or me["user_id"]
        settings.MLAI_TEAM_ID = settings.MLAI_TEAM_ID or me["team_id"]
        print(f"   MLAI bot: {settings.MLAI_BOT_USER_ID} @ team {settings.MLAI_TEAM_ID}")
    except Exception as e:
        print(f"❌ MLAI bot auth.test failed — check MLAI_BOT_TOKEN: {e}")
        raise

    snc_client = None
    if settings.snc_configured:
        snc_client = make_bot_client(settings.SNC_BOT_TOKEN)
        try:
            who = resolve_identity(snc_client)
            settings.SNC_BOT_USER_ID = settings.SNC_BOT_USER_ID or who["user_id"]
            settings.SNC_TEAM_ID = settings.SNC_TEAM_ID or who["team_id"]
            print(f"   S&C bot: {settings.SNC_BOT_USER_ID} @ team {settings.SNC_TEAM_ID}")
        except Exception as e:
            print(f"⚠️ S&C bot auth.test failed — check SNC_BOT_TOKEN: {e}")
    else:
        print("⚠️ SNC_BOT_TOKEN not set — S&C side disabled until configured")

    relay = Relay(
        settings=settings, store=store, identity=identity,
        mlai_client=mlai_client, snc_client=snc_client,
    )
    app.state.ready = True

    tasks: List[asyncio.Task] = []
    # Capture pollers.
    tasks.append(
        asyncio.create_task(
            channel_poll_loop(
                label="📥 MLAI", client=mlai_client, channel_id=settings.MLAI_CHANNEL_ID,
                hwm_key="mlai_last_ts", handler=relay.capture_from_mlai, store=store,
                poll_seconds=settings.MLAI_POLL_SECONDS,
                on_error=lambda e: poll_error_backoff(e, label="MLAI", relay=relay),
            )
        )
    )
    if snc_client is not None:
        tasks.append(
            asyncio.create_task(
                channel_poll_loop(
                    label="📥 S&C", client=snc_client, channel_id=settings.SNC_CHANNEL_ID,
                    hwm_key="snc_last_ts", handler=relay.capture_from_snc, store=store,
                    poll_seconds=settings.SNC_POLL_SECONDS,
                    on_error=lambda e: poll_error_backoff(e, label="S&C", relay=relay),
                )
            )
        )
    # Delivery worker.
    tasks.append(
        asyncio.create_task(
            delivery_loop(relay=relay, store=store, poll_seconds=settings.BRIDGE_DELIVERY_POLL_SECONDS)
        )
    )

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t
        print("🌉 Slack bridge shutting down...")


app = FastAPI(title="Slack Cross-Org Bridge", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok" if getattr(app.state, "ready", False) else "starting"}
