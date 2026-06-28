"""
Channel poll loop — shared by both sides.

Neither side uses a webhook: the MLAI side is polled with Roo's bot token, the
S&C side with Sam's web session. Both poll conversations.history for new
messages (and conversations.replies for active threads) and hand each message
to a relay handler. A high-water mark (newest ts seen) is persisted in the kv
store so restarts neither replay nor miss.

The S&C poll is the deliberate v1 substitute for the web client's WebSocket;
the clean upgrade is to swap that side's loop for a `client.userBoot` + WS
listener. The relay handlers don't care where a message comes from.

Behave like a real client: modest interval, cached user lookups, honour rate
limits — anti-abuse heuristics invalidate the S&C session if it looks like a
scraper.
"""
import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

Handler = Callable[[Dict[str, Any]], Awaitable[None]]
ErrorBackoff = Callable[[Exception], float]


async def channel_poll_loop(
    *,
    label: str,
    client,
    channel_id: str,
    hwm_key: str,
    handler: Handler,
    store,
    poll_seconds: float,
    on_error: Optional[ErrorBackoff] = None,
) -> None:
    # On first ever run, start from "now" so we don't replay history.
    last_ts = store.get_kv(hwm_key)
    if last_ts is None:
        last_ts = f"{time.time():.6f}"
        store.set_kv(hwm_key, last_ts)
        print(f"{label} ingest starting fresh from {last_ts}")
    else:
        print(f"{label} ingest resuming from {last_ts}")

    poll = max(1.0, float(poll_seconds))

    while True:
        try:
            last_ts = await _poll_once(label, client, channel_id, hwm_key, handler, store, last_ts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sleep_for = on_error(e) if on_error else 10.0
            await asyncio.sleep(sleep_for)
            continue
        await asyncio.sleep(poll)


async def _poll_once(label, client, channel_id, hwm_key, handler, store, last_ts: str) -> str:
    resp = client.conversations_history(channel=channel_id, oldest=last_ts, limit=200)
    if not resp.get("ok"):
        print(f"⚠️ {label} history not ok: {resp.get('error')}")
        return last_ts

    # History returns newest-first; process chronologically.
    roots: List[Dict[str, Any]] = list(reversed(resp.get("messages", [])))
    high = float(last_ts)

    for msg in roots:
        ts = msg.get("ts")
        if ts and float(ts) > float(last_ts):
            await handler(msg)
            high = max(high, float(ts))

        # Thread replies don't appear in history — pull them for active threads.
        latest_reply = msg.get("latest_reply")
        if latest_reply and float(latest_reply) > float(last_ts):
            high = max(high, await _drain_thread(label, client, channel_id, handler, msg, last_ts))

    new_hwm = f"{high:.6f}"
    if new_hwm != last_ts:
        store.set_kv(hwm_key, new_hwm)
    return new_hwm


async def _drain_thread(label, client, channel_id, handler, root, last_ts: str) -> float:
    high = float(last_ts)
    try:
        replies = client.conversations_replies(
            channel=channel_id,
            ts=root.get("thread_ts") or root.get("ts"),
            oldest=last_ts,
            limit=200,
        )
        for r in replies.get("messages", []):
            ts = r.get("ts")
            # Skip the parent (handled as a root) and anything not new.
            if not ts or ts == root.get("ts") or float(ts) <= float(last_ts):
                continue
            await handler(r)
            high = max(high, float(ts))
    except Exception as e:
        print(f"⚠️ {label} thread drain failed for {root.get('ts')}: {e}")
    return high


async def delivery_loop(*, relay, store, poll_seconds: float) -> None:
    """Drain the inbound queue, posting each message to the other workspace.

    Capture (the channel pollers) and delivery are decoupled by the DB queue, so
    a post failure here retries with backoff instead of losing the message.
    """
    poll = max(0.5, float(poll_seconds))
    print(f"🚚 delivery worker started poll_seconds={poll}")
    while True:
        try:
            due = store.claim_due_inbound(limit=20, now=time.time())
            for row in due:
                await relay.deliver(row)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ delivery worker error: {e}")
        await asyncio.sleep(poll)


def poll_error_backoff(e: Exception, *, label: str, relay) -> float:
    """Map a poll exception to a backoff (seconds), alerting on auth failure."""
    retry = getattr(getattr(e, "response", None), "headers", {}) or {}
    if "ratelimited" in str(e).lower() or retry.get("Retry-After"):
        wait = float(retry.get("Retry-After", 5))
        print(f"⏳ {label} rate limited — backing off {wait}s")
        return wait

    from .slack import is_auth_error

    if is_auth_error(e):
        print(f"🚫 {label} auth error ({e}) — token revoked or scopes changed? Reinstall the Bridge app.")
        asyncio.create_task(
            relay._alert(f"🚫 Bridge {label} auth error: {e}. Reinstall the Bridge app / refresh the token.")
        )
        return 60.0

    print(f"⚠️ {label} poll error: {e}")
    return 10.0
