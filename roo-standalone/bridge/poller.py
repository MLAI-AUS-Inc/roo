"""
Channel poll loop — shared by every pair side.

Each bridged channel is polled with its workspace's bot token. Two things are
tracked, because Slack surfaces them differently:

  * New top-level messages — conversations.history(oldest=hwm). The hwm advances
    past everything processed, so we never replay or miss a root message.
  * New thread replies — these do NOT appear in conversations.history, and a
    reply can land on a parent that is OLDER than the hwm (so the parent is never
    returned by the oldest=hwm query). So we run a separate "thread sweep": each
    poll re-scans recent history for any thread whose latest_reply is newer than
    a reply high-water mark, and drains those replies via conversations.replies.

Both marks are persisted in the kv store so restarts neither replay nor miss.
A delivery worker (below) drains the inbound queue the handlers fill.
"""
import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

Handler = Callable[[Dict[str, Any]], Awaitable[None]]
ErrorBackoff = Callable[[Exception], float]

# Replies on threads whose parent is older than this are not swept (keeps the
# per-poll scan bounded). Replies almost always arrive well within this window.
DEFAULT_THREAD_SWEEP_SECONDS = 3 * 24 * 3600


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
    thread_sweep_seconds: float = DEFAULT_THREAD_SWEEP_SECONDS,
) -> None:
    reply_key = f"{hwm_key}:replies"

    # On first ever run, start from "now" so we don't replay history.
    last_ts = store.get_kv(hwm_key)
    if last_ts is None:
        last_ts = f"{time.time():.6f}"
        store.set_kv(hwm_key, last_ts)
        store.set_kv(reply_key, last_ts)
        print(f"{label} ingest starting fresh from {last_ts}")
    else:
        if store.get_kv(reply_key) is None:  # existing install, new reply mark
            store.set_kv(reply_key, last_ts)
        print(f"{label} ingest resuming from {last_ts}")

    poll = max(1.0, float(poll_seconds))

    while True:
        try:
            last_ts = await _poll_once(
                label, client, channel_id, hwm_key, reply_key, handler, store, last_ts, thread_sweep_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            sleep_for = on_error(e) if on_error else 10.0
            await asyncio.sleep(sleep_for)
            continue
        await asyncio.sleep(poll)


async def _poll_once(
    label, client, channel_id, hwm_key, reply_key, handler, store, last_ts: str, sweep_seconds: float
) -> str:
    # 1) New top-level messages.
    resp = client.conversations_history(channel=channel_id, oldest=last_ts, limit=200)
    if not resp.get("ok"):
        print(f"⚠️ {label} history not ok: {resp.get('error')}")
        return last_ts

    roots: List[Dict[str, Any]] = list(reversed(resp.get("messages", [])))  # chronological
    high = float(last_ts)
    for msg in roots:
        ts = msg.get("ts")
        if ts and float(ts) > float(last_ts):
            await handler(msg)
            high = max(high, float(ts))
    new_hwm = f"{high:.6f}"
    if new_hwm != last_ts:
        store.set_kv(hwm_key, new_hwm)

    # 2) Thread replies — independent of the top-level hwm.
    await _sweep_threads(label, client, channel_id, reply_key, handler, store, sweep_seconds)
    return new_hwm


async def _sweep_threads(label, client, channel_id, reply_key, handler, store, sweep_seconds: float) -> None:
    """Re-scan recent threads for replies newer than the reply high-water mark."""
    reply_hwm = float(store.get_kv(reply_key) or 0)
    oldest = f"{max(0.0, time.time() - sweep_seconds):.6f}"
    resp = client.conversations_history(channel=channel_id, oldest=oldest, limit=200)
    if not resp.get("ok"):
        return

    new_hwm = reply_hwm
    for msg in resp.get("messages", []):
        latest_reply = msg.get("latest_reply")
        if msg.get("reply_count") and latest_reply and float(latest_reply) > reply_hwm:
            new_hwm = max(new_hwm, await _drain_replies(label, client, channel_id, handler, msg, reply_hwm))
    if new_hwm > reply_hwm:
        store.set_kv(reply_key, f"{new_hwm:.6f}")


async def _drain_replies(label, client, channel_id, handler, parent, reply_hwm: float) -> float:
    high = reply_hwm
    parent_ts = parent.get("thread_ts") or parent.get("ts")
    try:
        replies = client.conversations_replies(
            channel=channel_id, ts=parent_ts, oldest=f"{reply_hwm:.6f}", limit=200
        )
        for r in replies.get("messages", []):
            ts = r.get("ts")
            # Skip the parent itself and anything not newer than the mark.
            if not ts or ts == parent_ts or float(ts) <= reply_hwm:
                continue
            await handler(r)
            high = max(high, float(ts))
    except Exception as e:
        print(f"⚠️ {label} reply drain failed for {parent_ts}: {e}")
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
