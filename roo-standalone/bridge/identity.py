"""Author identity and destination-aware Slack-markup translation.

Slack user IDs are scoped to a workspace. A real cross-workspace mention must
therefore replace the source ID with the same person's destination ID. We build
small, in-memory workspace directories and map people by explicit configuration
first, then by exact normalized email. Emails are never persisted or logged.

People who only exist in the other workspace can be addressed with an explicit
plain-text alias such as ``hex:alice``. Active bots can be addressed the same
way when the handle is an exact match, for example ``mlai:roo``. Bot handles
are never email-matched or fuzzily guessed. The legacy ``@hex:alice`` form
remains supported, but the form without ``@`` avoids Slack's local mention
autocomplete. Unknown or ambiguous identities remain plain text: the bridge
must never guess and notify the wrong identity.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

_USER_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_RE = re.compile(r"<!(here|channel|everyone)>")
_SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|(@?[^>]+))?>")
_LINK_RE = re.compile(r"<((?:https?://|mailto:)[^>|]+)(?:\|([^>]+))?>")
_CODE_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
_EXPLICIT_MENTION_RE = re.compile(
    r"(?<![\w@])@?(?P<workspace>[A-Za-z0-9_-]+):(?P<handle>[\w][\w.-]*)",
    re.UNICODE,
)
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def _normalized_email(value: str) -> str:
    return (value or "").strip().casefold()


def _normalized_handle(value: str) -> str:
    value = (value or "").strip().lstrip("@").casefold()
    return re.sub(r"\s+", "-", value)


def _edit_distance(left: str, right: str) -> int:
    """Damerau-Levenshtein distance with adjacent transpositions."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    rows = [list(range(len(right) + 1))]
    for left_index, left_char in enumerate(left, start=1):
        row = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            row.append(
                min(
                    row[right_index - 1] + 1,
                    rows[-1][right_index] + 1,
                    rows[-1][right_index - 1] + (left_char != right_char),
                )
            )
            if (
                left_index > 1
                and right_index > 1
                and left_char == right[right_index - 2]
                and left[left_index - 2] == right_char
            ):
                row[right_index] = min(
                    row[right_index], rows[-2][right_index - 2] + 1
                )
        rows.append(row)
    return rows[-1][-1]


@dataclass(frozen=True)
class UserProfile:
    id: str
    name: str = ""
    display_name: str = ""
    real_name: str = ""
    image: str = ""
    email: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.real_name or self.name or self.id

    @classmethod
    def from_slack_user(cls, user: Mapping[str, Any]) -> "UserProfile":
        profile = user.get("profile") or {}
        return cls(
            id=user.get("id", ""),
            name=user.get("name", ""),
            display_name=profile.get("display_name", ""),
            real_name=user.get("real_name", profile.get("real_name", "")),
            image=profile.get("image_512")
            or profile.get("image_192")
            or profile.get("image_72")
            or "",
            email=_normalized_email(profile.get("email", "")),
        )

    def as_info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "real_name": self.real_name,
            "image": self.image,
            "email": self.email,
        }


@dataclass(frozen=True)
class WorkspaceDirectory:
    by_id: Dict[str, UserProfile] = field(default_factory=dict)
    by_email: Dict[str, UserProfile] = field(default_factory=dict)
    by_handle: Dict[str, UserProfile] = field(default_factory=dict)
    qualified_by_id: Dict[str, UserProfile] = field(default_factory=dict)
    qualified_by_handle: Dict[str, UserProfile] = field(default_factory=dict)
    refreshed_at: float = 0.0

    @property
    def email_count(self) -> int:
        return sum(1 for profile in self.by_id.values() if profile.email)

    @property
    def bot_count(self) -> int:
        return max(0, len(self.qualified_by_id) - len(self.by_id))


def _unique_index(entries: Iterable[Tuple[str, UserProfile]]) -> Dict[str, UserProfile]:
    """Build an index that drops ambiguous keys instead of picking a winner."""
    result: Dict[str, UserProfile] = {}
    ambiguous = set()
    for raw_key, profile in entries:
        key = raw_key or ""
        if not key or key in ambiguous:
            continue
        existing = result.get(key)
        if existing and existing.id != profile.id:
            result.pop(key, None)
            ambiguous.add(key)
        else:
            result[key] = profile
    return result


class IdentityResolver:
    def __init__(self):
        # keyed by "team:user_id" so the same id in two workspaces never collides
        self._user_cache: Dict[str, Dict[str, Any]] = {}
        self._directories: Dict[str, WorkspaceDirectory] = {}
        self._refresh_errors: Dict[str, str] = {}
        self._mention_counts: Counter[str] = Counter()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Workspace directories
    # ------------------------------------------------------------------

    def refresh_workspace(
        self, client, team: str, channel_ids: Iterable[str] = ()
    ) -> WorkspaceDirectory:
        """Fetch and atomically replace one workspace's addressable directory.

        ``users.list`` can omit people who participate through Slack Connect.
        Merge members of the configured bridged channels via ``users.info`` so
        qualified handles can resolve every person the bridge can address.
        """
        members = []
        cursor = None
        try:
            while True:
                response = client.users_list(limit=200, cursor=cursor)
                if not response.get("ok"):
                    raise RuntimeError(
                        response.get("error") or "users.list returned not ok"
                    )
                members.extend(response.get("members", []))
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break

            known_ids = {member.get("id") for member in members if member.get("id")}
            for channel_id in dict.fromkeys(channel_ids):
                if not channel_id:
                    continue
                cursor = None
                while True:
                    response = client.conversations_members(
                        channel=channel_id, limit=200, cursor=cursor
                    )
                    if not response.get("ok"):
                        raise RuntimeError(
                            response.get("error")
                            or f"conversations.members returned not ok for {channel_id}"
                        )
                    for user_id in response.get("members", []):
                        if not user_id or user_id in known_ids:
                            continue
                        info = client.users_info(user=user_id)
                        if not info.get("ok") or not info.get("user"):
                            raise RuntimeError(
                                info.get("error")
                                or f"users.info returned not ok for {user_id}"
                            )
                        members.append(info["user"])
                        known_ids.add(user_id)
                    cursor = (response.get("response_metadata") or {}).get(
                        "next_cursor"
                    )
                    if not cursor:
                        break
            directory = self.index_workspace(team, members)
        except Exception as exc:
            with self._lock:
                self._refresh_errors[team] = str(exc)
            raise

        with self._lock:
            self._directories[team] = directory
            self._refresh_errors.pop(team, None)
        return directory

    def index_workspace(
        self, team: str, members: Iterable[Mapping[str, Any]]
    ) -> WorkspaceDirectory:
        """Build a directory from Slack user objects (also useful for tests)."""
        profiles = []
        qualified_profiles = []
        for user in members:
            if (
                not user.get("id")
                or user.get("deleted")
                or user.get("id") == "USLACKBOT"
            ):
                continue
            profile = UserProfile.from_slack_user(user)
            qualified_profiles.append(profile)
            if user.get("is_bot") or user.get("is_app_user"):
                continue
            profiles.append(profile)

        by_id = {profile.id: profile for profile in profiles}
        by_email = _unique_index((profile.email, profile) for profile in profiles)
        handle_entries = []
        for profile in profiles:
            # Slack's unique `name` is preferred, but unique normalized display
            # and real names make hex:alice-smith ergonomic too.
            for value in (profile.name, profile.display_name, profile.real_name):
                handle_entries.append((_normalized_handle(value), profile))
        qualified_handle_entries = []
        for profile in qualified_profiles:
            for value in (profile.name, profile.display_name, profile.real_name):
                qualified_handle_entries.append((_normalized_handle(value), profile))
        directory = WorkspaceDirectory(
            by_id=by_id,
            by_email=by_email,
            by_handle=_unique_index(handle_entries),
            qualified_by_id={profile.id: profile for profile in qualified_profiles},
            qualified_by_handle=_unique_index(qualified_handle_entries),
            refreshed_at=time.time(),
        )
        with self._lock:
            self._directories[team] = directory
            self._refresh_errors.pop(team, None)
        return directory

    def health(self) -> Dict[str, Any]:
        with self._lock:
            teams = {
                team: {
                    "users": len(directory.by_id),
                    "qualified_bots": directory.bot_count,
                    "users_with_email": directory.email_count,
                    "refreshed_at": directory.refreshed_at,
                    "last_error": self._refresh_errors.get(team),
                }
                for team, directory in self._directories.items()
            }
            for team, error in self._refresh_errors.items():
                teams.setdefault(
                    team,
                    {
                        "users": 0,
                        "qualified_bots": 0,
                        "users_with_email": 0,
                        "refreshed_at": 0.0,
                        "last_error": error,
                    },
                )
            return {"teams": teams, "mention_counts": dict(self._mention_counts)}

    def user_info(self, client, user_id: str, team: str = "") -> Dict[str, Any]:
        key = f"{team}:{user_id}"
        with self._lock:
            directory = self._directories.get(team)
            profile = directory.by_id.get(user_id) if directory else None
            cached = self._user_cache.get(key)
        if profile is not None:
            return profile.as_info()
        if cached is not None:
            return cached

        info = {
            "id": user_id,
            "name": user_id,
            "display_name": "",
            "real_name": "",
            "image": "",
            "email": "",
        }
        try:
            resp = client.users_info(user=user_id)
            if resp.get("ok"):
                info = UserProfile.from_slack_user(resp["user"]).as_info()
        except Exception as e:
            # Don't let a lookup miss break a relay — fall back to the raw id.
            print(f"⚠️ users_info failed for {user_id}: {e}")

        with self._lock:
            self._user_cache[key] = info
        return info

    def display_name(self, client, user_id: str, team: str = "") -> str:
        info = self.user_info(client, user_id, team)
        return (
            info.get("display_name")
            or info.get("real_name")
            or info.get("name")
            or user_id
        )

    def avatar(self, client, user_id: str, team: str = "") -> str:
        return self.user_info(client, user_id, team).get("image") or ""

    def translate(
        self,
        client,
        text: str,
        team: str = "",
        *,
        dst_team: str = "",
        src_alias: str = "",
        dst_alias: str = "",
        user_map: Optional[Mapping[str, str]] = None,
        mention_mode: str = "plain",
    ) -> str:
        """Rewrite Slack markup for the destination workspace.

        ``user_map`` is directional: source user id -> destination user id.
        ``observe`` performs the same resolution as ``native`` but emits inert
        text, allowing mappings to be verified before notifications go live.
        """
        if not text:
            return ""
        if mention_mode not in {"plain", "observe", "native"}:
            mention_mode = "plain"
        directional_map = dict(user_map or {})

        parts = _CODE_RE.split(text)
        for index in range(0, len(parts), 2):
            parts[index] = self._translate_segment(
                client=client,
                text=parts[index],
                src_team=team,
                dst_team=dst_team,
                src_alias=src_alias,
                dst_alias=dst_alias,
                user_map=directional_map,
                mention_mode=mention_mode,
            )
        return "".join(parts)

    def _translate_segment(
        self,
        *,
        client,
        text: str,
        src_team: str,
        dst_team: str,
        src_alias: str,
        dst_alias: str,
        user_map: Mapping[str, str],
        mention_mode: str,
    ) -> str:
        # Protect link markup so a workspace:handle inside a URL or link label
        # can never turn into a notification.
        links = []

        def _protect_link(match: "re.Match[str]") -> str:
            target, label = match.group(1), match.group(2)
            if target.startswith("mailto:"):
                links.append(label or target.removeprefix("mailto:"))
            else:
                links.append(f"{label} ({target})" if label else target)
            return f"\x00BRIDGE_LINK_{len(links) - 1}\x00"

        text = _LINK_RE.sub(_protect_link, text)

        def _user(match: "re.Match[str]") -> str:
            source_id, wire_label = match.group(1), match.group(2)
            source = self._source_profile(client, src_team, source_id)
            label = wire_label or source.label
            fallback = self._fallback(label, src_alias)
            destination_id, method = self._mapped_user(
                source, source_id, dst_team, user_map
            )
            if not destination_id:
                self._record("structured_unresolved")
                return fallback
            self._record(f"structured_{method}")
            if mention_mode == "native":
                return f"<@{destination_id}>"
            if mention_mode == "observe":
                print(
                    f"👀 mention candidate {src_team}/{source_id} -> "
                    f"{dst_team}/{destination_id} via {method}"
                )
            return fallback

        text = _USER_RE.sub(_user, text)
        text = _CHANNEL_RE.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
        # Mass mentions and workspace-local user groups must never fan out into
        # another organization.
        text = _SPECIAL_RE.sub(lambda m: f"@{m.group(1)}", text)
        text = _SUBTEAM_RE.sub(lambda m: m.group(1) or "@group", text)

        def _explicit(match: "re.Match[str]") -> str:
            if (
                not dst_alias
                or match.group("workspace").casefold() != dst_alias.casefold()
            ):
                return match.group(0)
            destination, fuzzy = self._destination_alias(
                dst_team, match.group("handle")
            )
            if destination is None:
                self._record("explicit_unresolved")
                return match.group(0)
            self._record("explicit_resolved")
            if fuzzy:
                self._record("explicit_fuzzy")
            if mention_mode == "native":
                return f"<@{destination.id}>"
            if mention_mode == "observe":
                print(f"👀 explicit mention candidate {dst_team}/{destination.id}")
                return self._fallback(destination.label, dst_alias)
            return match.group(0)

        if mention_mode != "plain":
            text = _EXPLICIT_MENTION_RE.sub(_explicit, text)

        for index, link in enumerate(links):
            text = text.replace(f"\x00BRIDGE_LINK_{index}\x00", link)
        # Keep Slack's &amp;/&lt;/&gt; wire escaping intact. Slack will decode it
        # for display; decoding here could turn user-authored literal text such
        # as &lt;@U123&gt; into an unintended real notification.
        return text

    def _source_profile(self, client, team: str, user_id: str) -> UserProfile:
        with self._lock:
            directory = self._directories.get(team)
            profile = directory.by_id.get(user_id) if directory else None
        if profile is not None:
            return profile
        info = self.user_info(client, user_id, team)
        return UserProfile(
            id=user_id,
            name=info.get("name", ""),
            display_name=info.get("display_name", ""),
            real_name=info.get("real_name", ""),
            image=info.get("image", ""),
            email=_normalized_email(info.get("email", "")),
        )

    def _mapped_user(
        self,
        source: UserProfile,
        source_id: str,
        dst_team: str,
        user_map: Mapping[str, str],
    ) -> Tuple[Optional[str], str]:
        with self._lock:
            destination = self._directories.get(dst_team)
        if destination is None:
            return None, ""

        explicit_id = user_map.get(source_id)
        if explicit_id and explicit_id in destination.by_id:
            return explicit_id, "override"
        if source.email:
            match = destination.by_email.get(source.email)
            if match:
                return match.id, "email"
        return None, ""

    def _destination_alias(
        self, dst_team: str, handle: str
    ) -> Tuple[Optional[UserProfile], bool]:
        with self._lock:
            destination = self._directories.get(dst_team)
        if destination is None:
            return None, False
        if _USER_ID_RE.match(handle.upper()):
            return destination.qualified_by_id.get(handle.upper()), False

        normalized = _normalized_handle(handle)
        exact = destination.qualified_by_handle.get(normalized)
        if exact is not None:
            return exact, False

        # A typo may notify someone, so fuzzy matching is intentionally narrow:
        # one edit for short names, two for names of eight or more characters,
        # at least 75% similarity, and exactly one closest person. Aliases for
        # the same person are collapsed before checking for a tie.
        if len(normalized) < 3:
            return None, False
        candidates: Dict[str, Tuple[int, UserProfile]] = {}
        for profile in destination.by_id.values():
            aliases = {
                _normalized_handle(value)
                for value in (profile.name, profile.display_name, profile.real_name)
                if value
            }
            for alias in aliases:
                longest = max(len(normalized), len(alias))
                if longest < 4:
                    continue
                allowed = 2 if longest >= 8 else 1
                if abs(len(normalized) - len(alias)) > allowed:
                    continue
                distance = _edit_distance(normalized, alias)
                if distance > allowed or 1 - (distance / longest) < 0.75:
                    continue
                current = candidates.get(profile.id)
                if current is None or distance < current[0]:
                    candidates[profile.id] = (distance, profile)

        if not candidates:
            return None, False
        best_distance = min(distance for distance, _ in candidates.values())
        best = [
            profile
            for distance, profile in candidates.values()
            if distance == best_distance
        ]
        if len(best) != 1:
            return None, False
        return best[0], True

    @staticmethod
    def _fallback(label: str, workspace_alias: str) -> str:
        suffix = f" ({workspace_alias.upper()})" if workspace_alias else ""
        return f"@{label}{suffix}"

    def _record(self, outcome: str) -> None:
        with self._lock:
            self._mention_counts[outcome] += 1


_resolver = IdentityResolver()


def get_resolver() -> IdentityResolver:
    return _resolver
