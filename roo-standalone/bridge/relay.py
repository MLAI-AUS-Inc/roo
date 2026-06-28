"""
Relay — capture + delivery, generalised to many MLAI<->partner channel pairs.

MLAI is the hub. Each pair has a label and two directions:
  * to_remote — an MLAI message → the partner channel
  * to_mlai   — a partner message → the MLAI channel

CAPTURE (called by the channel pollers): cheap filtering + enqueue into the
durable inbound queue. The queue row's `direction` is "<label>:to_remote" or
"<label>:to_mlai" so delivery knows which pair + way to route it. Capture never
posts, so a delivery problem can't lose a captured message.

DELIVERY (called by the delivery worker): look up the pair, render the message
(real name + avatar via chat:write.customize), post into the destination, record
the loop-guard + thread mapping, mark delivered. On failure, retry with backoff;
give up after BRIDGE_MAX_DELIVERY_ATTEMPTS.

Loop prevention is authoritative via the posted_registry, keyed by (team,
channel, ts) so it works across all pairs. Edits/deletes are not mirrored in
poll mode. See SLACK_BRIDGE_PLAN.md.
"""
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .config import BridgeSettings
from .identity import IdentityResolver
from .slack import is_auth_error
from .store import BridgeStore

_RELAYABLE_SUBTYPES = {None, "", "bot_message", "file_share", "thread_broadcast", "me_message"}


@dataclass
class ResolvedPair:
    """A pair with live clients + resolved channel IDs (built at startup)."""

    label: str
    remote_client: Any
    remote_team: str
    mlai_channel_id: str
    remote_channel_id: str


class Relay:
    def __init__(
        self,
        *,
        settings: BridgeSettings,
        store: BridgeStore,
        identity: IdentityResolver,
        mlai_client,
        pairs: List[ResolvedPair],
    ):
        self.s = settings
        self.store = store
        self.identity = identity
        self.mlai = mlai_client
        self.pairs: Dict[str, ResolvedPair] = {p.label: p for p in pairs}
        self._recent_posts: deque[float] = deque(maxlen=512)
        self._paused = False
        self._last_alert_at = 0.0

    # ========================================================================
    # capture (pollers → queue)
    # ========================================================================

    async def capture(self, label: str, to_mlai: bool, msg: Dict[str, Any]) -> None:
        pair = self.pairs.get(label)
        if not pair:
            return
        if to_mlai:
            src_team, src_channel = pair.remote_team, pair.remote_channel_id
            direction = f"{label}:to_mlai"
        else:
            src_team, src_channel = self.s.MLAI_TEAM_ID or "", pair.mlai_channel_id
            direction = f"{label}:to_remote"

        ts = msg.get("ts")
        if not ts or msg.get("subtype") not in _RELAYABLE_SUBTYPES:
            return
        # Our own echo (we posted this) — never re-bridge.
        if self.store.is_self_posted(src_team, src_channel, ts):
            return
        if msg.get("bot_id") and not self.s.BRIDGE_RELAY_BOT_MESSAGES:
            return
        if self.store.enqueue_inbound(direction, src_team, src_channel, ts, json.dumps(msg)):
            print(f"📥 captured {direction} {ts}")

    # ========================================================================
    # delivery (worker → destination workspace)
    # ========================================================================

    async def deliver(self, row: Dict[str, Any]) -> None:
        try:
            msg = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as e:
            self.store.mark_failed(row["id"], f"bad payload: {e}")
            return

        label, _, way = row["direction"].partition(":")
        pair = self.pairs.get(label)
        if not pair:
            self.store.mark_failed(row["id"], f"unknown pair {label!r}")
            return

        if way == "to_mlai":
            src_client, src_team, src_channel = pair.remote_client, pair.remote_team, pair.remote_channel_id
            dst_client, dst_team, dst_channel = self.mlai, self.s.MLAI_TEAM_ID or "", pair.mlai_channel_id
            arrow = f"⬅️  {label}→MLAI"
        else:
            src_client, src_team, src_channel = self.mlai, self.s.MLAI_TEAM_ID or "", pair.mlai_channel_id
            dst_client, dst_team, dst_channel = pair.remote_client, pair.remote_team, pair.remote_channel_id
            arrow = f"➡️  MLAI→{label}"

        # Circuit breaker — leave the row pending and try again shortly.
        if not self._allow_post():
            self.store.mark_retry(row["id"], "circuit breaker", time.time() + 5, row["attempt_count"])
            return

        try:
            dst_ts = self._post(src_client, src_team, src_channel, dst_client, dst_channel, msg)
        except Exception as e:
            print(f"❌ post {arrow} error: {e}")
            self._fail_or_retry(row, e)
            return

        if not dst_ts:
            self._fail_or_retry(row, Exception("post returned not ok"))
            return

        self.store.record_posted(dst_team, dst_channel, dst_ts)
        self.store.map_message(src_team, src_channel, msg["ts"], dst_team, dst_channel, dst_ts)
        self.store.mark_delivered(row["id"])
        print(f"{arrow} delivered {msg['ts']} → {dst_ts}")

        if self.s.BRIDGE_RELAY_FILES and msg.get("files"):
            dst_thread_ts = self._dst_thread_ts(src_team, src_channel, msg)
            self._relay_files(
                src_client=src_client, files=msg["files"],
                dst_client=dst_client, dst_channel=dst_channel,
                dst_thread_ts=dst_thread_ts or dst_ts,
            )

    def _post(self, src_client, src_team, src_channel, dst_client, dst_channel, msg) -> Optional[str]:
        """Render + post one message. Returns the destination ts, or None."""
        if msg.get("bot_id"):
            name = self._bot_name(msg)
            avatar = (msg.get("icons", {}) or {}).get("image_72", "")
        else:
            author_id = msg.get("user", "")
            name = self.identity.display_name(src_client, author_id, src_team)
            avatar = self.identity.avatar(src_client, author_id, src_team)

        body = self.identity.translate(src_client, msg.get("text", ""), src_team)
        dst_thread_ts = self._dst_thread_ts(src_team, src_channel, msg)

        kwargs: Dict[str, Any] = {
            "channel": dst_channel,
            "text": body or " ",
            "username": name,
            "thread_ts": dst_thread_ts,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if avatar:
            kwargs["icon_url"] = avatar

        resp = dst_client.chat_postMessage(**kwargs)
        if not resp.get("ok"):
            print(f"❌ chat.postMessage not ok: {resp.get('error')}")
            return None
        return resp["ts"]

    def _fail_or_retry(self, row: Dict[str, Any], e: Exception) -> None:
        attempt = (row["attempt_count"] or 0) + 1
        if attempt >= self.s.BRIDGE_MAX_DELIVERY_ATTEMPTS:
            self.store.mark_failed(row["id"], str(e))
            print(f"❌ delivery permanently failed id={row['id']} after {attempt} tries: {e}")
            import asyncio
            asyncio.create_task(self._alert(f"❌ Bridge gave up on a message after {attempt} tries: {e}"))
        else:
            backoff = min(60.0, float(2 ** attempt))
            self.store.mark_retry(row["id"], str(e), time.time() + backoff, attempt)
            print(f"⏳ delivery retry id={row['id']} attempt={attempt} in {backoff:.0f}s: {e}")
        if is_auth_error(e):
            import asyncio
            asyncio.create_task(self._alert(f"⚠️ Bridge delivery auth error: {e}"))

    # ========================================================================
    # helpers
    # ========================================================================

    @staticmethod
    def _bot_name(msg: Dict[str, Any]) -> str:
        return msg.get("username") or (msg.get("bot_profile", {}) or {}).get("name") or "bot"

    def _dst_thread_ts(self, src_team: str, src_channel: str, msg: Dict[str, Any]) -> Optional[str]:
        thread_ts = msg.get("thread_ts")
        if not thread_ts or thread_ts == msg.get("ts"):
            return None
        dst = self.store.dst_for(src_team, src_channel, thread_ts)
        return dst[2] if dst else None

    def _allow_post(self) -> bool:
        """Circuit breaker: never let a loop bug flood the channels."""
        now = time.time()
        while self._recent_posts and now - self._recent_posts[0] > 60:
            self._recent_posts.popleft()
        if len(self._recent_posts) >= self.s.BRIDGE_MAX_POSTS_PER_MIN:
            if not self._paused:
                self._paused = True
                print("🛑 Bridge circuit breaker tripped — pausing posts")
                import asyncio
                asyncio.create_task(
                    self._alert(
                        "🛑 Bridge circuit breaker tripped (>%d posts/min) — paused. "
                        "Messages stay queued; check for a loop and restart." % self.s.BRIDGE_MAX_POSTS_PER_MIN
                    )
                )
            return False
        self._paused = False
        self._recent_posts.append(now)
        return True

    def _relay_files(self, *, src_client, files, dst_client, dst_channel, dst_thread_ts) -> None:
        """Best-effort cross-workspace file copy: file URLs are not portable
        between workspaces, so download with the source creds and re-upload."""
        for f in files:
            try:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                data = self._download(src_client, url)
                dst_client.files_upload_v2(
                    channel=dst_channel,
                    file=data,
                    filename=f.get("name") or "file",
                    title=f.get("title") or f.get("name"),
                    thread_ts=dst_thread_ts,
                )
                print(f"📎 relayed file {f.get('name')}")
            except Exception as e:
                print(f"⚠️ file relay failed for {f.get('name')}: {e}")

    @staticmethod
    def _download(client, url: str) -> bytes:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {client.token}"},
            timeout=30.0,
            follow_redirects=False,
        )
        resp.raise_for_status()
        return resp.content

    async def _alert(self, text: str) -> None:
        """DM Sam (MLAI side) about problems, throttled to avoid spam."""
        user = self.s.BRIDGE_ALERT_DM_USER_ID
        if not user:
            return
        now = time.time()
        if now - self._last_alert_at < 300:
            return
        self._last_alert_at = now
        try:
            opened = self.mlai.conversations_open(users=user)
            if opened.get("ok"):
                self.mlai.chat_postMessage(channel=opened["channel"]["id"], text=text)
        except Exception as e:
            print(f"⚠️ alert DM failed: {e}")
