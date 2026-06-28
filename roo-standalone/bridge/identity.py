"""
Author resolution and Slack-markup translation.

Two jobs:
  * Resolve a user id to a display name + avatar (cached per side), so messages
    can be attributed to the real person.
  * Rewrite Slack's wire markup into plain text that makes sense in the *other*
    workspace, where user/channel ids mean nothing. Crucially, mentions become
    inert "@Name" text — we deliberately do NOT ping across orgs in v1 (that
    needs real puppet accounts; see SLACK_BRIDGE_PLAN.md).
"""
import re
from typing import Any, Dict

_USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_RE = re.compile(r"<!(here|channel|everyone)>")
_SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|(@?[^>]+))?>")
_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]+))?>")


class IdentityResolver:
    def __init__(self):
        # keyed by "team:user_id" so the same id in two workspaces never collides
        self._user_cache: Dict[str, Dict[str, Any]] = {}

    def user_info(self, client, user_id: str, team: str = "") -> Dict[str, Any]:
        key = f"{team}:{user_id}"
        cached = self._user_cache.get(key)
        if cached is not None:
            return cached

        info = {"id": user_id, "name": user_id, "display_name": "", "real_name": "", "image": ""}
        try:
            resp = client.users_info(user=user_id)
            if resp.get("ok"):
                u = resp["user"]
                p = u.get("profile", {})
                info = {
                    "id": user_id,
                    "name": u.get("name", ""),
                    "display_name": p.get("display_name", ""),
                    "real_name": u.get("real_name", p.get("real_name", "")),
                    "image": p.get("image_512") or p.get("image_192") or p.get("image_72") or "",
                }
        except Exception as e:
            # Don't let a lookup miss break a relay — fall back to the raw id.
            print(f"⚠️ users_info failed for {user_id}: {e}")

        self._user_cache[key] = info
        return info

    def display_name(self, client, user_id: str, team: str = "") -> str:
        info = self.user_info(client, user_id, team)
        return info.get("display_name") or info.get("real_name") or info.get("name") or user_id

    def avatar(self, client, user_id: str, team: str = "") -> str:
        return self.user_info(client, user_id, team).get("image") or ""

    def translate(self, client, text: str, team: str = "") -> str:
        """Rewrite Slack markup for display in the other workspace."""
        if not text:
            return ""

        def _user(m: "re.Match") -> str:
            uid, label = m.group(1), m.group(2)
            return f"@{label}" if label else f"@{self.display_name(client, uid, team)}"

        text = _USER_RE.sub(_user, text)
        text = _CHANNEL_RE.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
        # @here/@channel as plain text — informative but does NOT ping the other org.
        text = _SPECIAL_RE.sub(lambda m: f"@{m.group(1)}", text)
        text = _SUBTEAM_RE.sub(lambda m: (m.group(1) or "@group"), text)
        text = _LINK_RE.sub(
            lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) else m.group(1), text
        )
        # Slack escapes these in message text; unescape for the destination.
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


_resolver = IdentityResolver()


def get_resolver() -> IdentityResolver:
    return _resolver
