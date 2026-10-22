"""
Bridge service.

MLAI is the hub. For each pair in BRIDGE_PAIRS the service holds the partner
workspace's bot token, resolves both channel IDs, and runs two capture pollers
(MLAI channel + partner channel). A single delivery worker drains the shared
inbound queue and posts into the destination workspace.

No inbound webhooks; runs as its own process on the droplet, independent of Roo.
A minimal FastAPI app exposes GET /healthz for nginx/monitoring.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Dict, List, Set

from fastapi import FastAPI

from .config import get_bridge_settings
from .identity import get_resolver
from .poller import channel_poll_loop, delivery_loop, poll_error_backoff
from .relay import Relay, ResolvedPair
from .slack import make_bot_client, resolve_channel_id, resolve_identity
from .store import get_store


async def _refresh_identity_directories(
    identity, workspace_clients, workspace_channels
) -> None:
    """Refresh each unique workspace without blocking message delivery."""
    for team, client in workspace_clients.items():
        try:
            directory = await asyncio.to_thread(
                identity.refresh_workspace,
                client,
                team,
                workspace_channels.get(team, ()),
            )
            print(
                f"👥 identity directory {team}: {len(directory.by_id)} addressable users, "
                f"{directory.email_count} with email"
            )
        except Exception as e:
            # Existing directories remain in place after a failed refresh. If
            # this is the first load, mention rendering safely falls back to
            # inert text until a later refresh succeeds.
            print(f"⚠️ identity directory refresh failed for {team}: {e}")


async def _identity_refresh_loop(
    identity, workspace_clients, workspace_channels, refresh_seconds: float
) -> None:
    interval = max(60.0, float(refresh_seconds))
    while True:
        await asyncio.sleep(interval)
        await _refresh_identity_directories(
            identity, workspace_clients, workspace_channels
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_bridge_settings()
    store = get_store()
    identity = get_resolver()

    print("🌉 Slack bridge starting...")

    mlai_client = make_bot_client(settings.MLAI_BOT_TOKEN)
    try:
        me = resolve_identity(mlai_client)
        settings.MLAI_BOT_USER_ID = settings.MLAI_BOT_USER_ID or me["user_id"]
        settings.MLAI_TEAM_ID = settings.MLAI_TEAM_ID or me["team_id"]
        print(
            f"   MLAI hub: bot {settings.MLAI_BOT_USER_ID} @ team {settings.MLAI_TEAM_ID}"
        )
    except Exception as e:
        print(f"❌ MLAI bot auth.test failed — check MLAI_BOT_TOKEN: {e}")
        raise

    # Resolve each pair into live clients + channel IDs. A pair that can't be
    # resolved (channel missing or bot not invited) is skipped with a warning so
    # the rest of the bridge still runs; fix it and restart.
    resolved: List[ResolvedPair] = []
    poll_specs = []  # (display, client, channel_id, team, label, to_mlai)
    for pair in settings.BRIDGE_PAIRS:
        try:
            remote_client = make_bot_client(pair.remote_token)
            who = resolve_identity(remote_client)
            remote_team = who["team_id"]
        except Exception as e:
            print(f"⚠️ pair {pair.label!r}: remote token auth failed, skipping: {e}")
            continue

        mlai_ch = resolve_channel_id(mlai_client, pair.mlai_channel)
        remote_ch = resolve_channel_id(remote_client, pair.remote_channel)
        if not mlai_ch:
            print(
                f"⚠️ pair {pair.label!r}: MLAI channel {pair.mlai_channel!r} not found "
                f"(create it + invite the bot, then restart) — skipping"
            )
            continue
        if not remote_ch:
            print(
                f"⚠️ pair {pair.label!r}: partner channel {pair.remote_channel!r} not found "
                f"(invite the bot there, then restart) — skipping"
            )
            continue

        mention_alias = (pair.mention_alias or "").strip() or pair.label
        resolved.append(
            ResolvedPair(
                pair.label,
                remote_client,
                remote_team,
                mlai_ch,
                remote_ch,
                who["user_id"],
                mention_alias,
                dict(pair.user_map),
            )
        )
        poll_specs.append(
            (
                f"MLAI#{pair.label}",
                mlai_client,
                mlai_ch,
                settings.MLAI_TEAM_ID,
                pair.label,
                False,
            )
        )
        poll_specs.append(
            (
                f"{pair.label}#remote",
                remote_client,
                remote_ch,
                remote_team,
                pair.label,
                True,
            )
        )
        print(
            f"   pair {pair.label!r}: MLAI {mlai_ch} <-> {remote_team}/{remote_ch} "
            f"(mentions {mention_alias}:handle)"
        )

    if not resolved:
        print(
            "⚠️ no usable pairs — bridge is idle until BRIDGE_PAIRS is configured + channels exist"
        )

    workspace_clients = {settings.MLAI_TEAM_ID or "": mlai_client}
    workspace_clients.update(
        {pair.remote_team: pair.remote_client for pair in resolved}
    )
    workspace_channels: Dict[str, Set[str]] = {
        team: set() for team in workspace_clients
    }
    for pair in resolved:
        workspace_channels[settings.MLAI_TEAM_ID or ""].add(pair.mlai_channel_id)
        workspace_channels[pair.remote_team].add(pair.remote_channel_id)
    if settings.BRIDGE_MENTION_MODE != "plain":
        await _refresh_identity_directories(
            identity, workspace_clients, workspace_channels
        )

    relay = Relay(
        settings=settings,
        store=store,
        identity=identity,
        mlai_client=mlai_client,
        pairs=resolved,
    )
    app.state.ready = True
    app.state.identity = identity
    app.state.mention_mode = settings.BRIDGE_MENTION_MODE

    tasks: List[asyncio.Task] = []
    for display, client, channel_id, team, label, to_mlai in poll_specs:
        tasks.append(
            asyncio.create_task(
                channel_poll_loop(
                    label=f"📥 {display}",
                    client=client,
                    channel_id=channel_id,
                    hwm_key=f"hwm:{team}:{channel_id}",
                    handler=(
                        lambda msg, _l=label, _t=to_mlai: relay.capture(_l, _t, msg)
                    ),
                    store=store,
                    poll_seconds=settings.POLL_SECONDS,
                    thread_sweep_seconds=settings.THREAD_SWEEP_SECONDS,
                    on_error=(
                        lambda e, _n=display: poll_error_backoff(
                            e, label=_n, relay=relay
                        )
                    ),
                )
            )
        )
    tasks.append(
        asyncio.create_task(
            delivery_loop(
                relay=relay,
                store=store,
                poll_seconds=settings.BRIDGE_DELIVERY_POLL_SECONDS,
            )
        )
    )
    if settings.BRIDGE_MENTION_MODE != "plain":
        tasks.append(
            asyncio.create_task(
                _identity_refresh_loop(
                    identity,
                    workspace_clients,
                    workspace_channels,
                    settings.BRIDGE_IDENTITY_REFRESH_SECONDS,
                )
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


app = FastAPI(title="Slack Cross-Org Bridge", version="0.2.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    payload = {
        "status": "ok" if getattr(app.state, "ready", False) else "starting",
        "mention_mode": getattr(app.state, "mention_mode", "plain"),
    }
    identity = getattr(app.state, "identity", None)
    if identity is not None:
        payload["identity"] = identity.health()
    return payload
