"""
Bridge configuration.

Pydantic Settings, mirroring roo/config.py conventions (env file, extra=ignore,
singleton getter). MLAI is the hub: one MLAI bot token, plus a list of channel
PAIRS, one per partner workspace channel. Each pair brings its own remote bot
token + channel, so adding a workspace is just one more entry in BRIDGE_PAIRS.

Channels may be given as an ID (e.g. C0123ABCD) or a name (e.g. exp-victor-ai),
which is resolved at startup. Both sides are POLLED (no webhooks, no secret).
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BridgePair(BaseModel):
    """One mirrored channel: an MLAI channel <-> a channel in a partner workspace."""

    label: str  # short id, no ':' (e.g. "hex", "stone-and-chalk")
    mlai_channel: str  # name or ID in the MLAI workspace
    remote_token: str  # the Bridge bot token for the partner workspace
    remote_channel: str  # name or ID in the partner workspace
    # Plain-text cross-workspace mention prefix. With label="hex", people in
    # MLAI can write @hex:alice to ping Alice in the partner workspace.
    mention_alias: Optional[str] = None
    # Explicit MLAI user id -> partner user id mappings. These take precedence
    # over automatic email matching (useful when the two accounts use different
    # email addresses).
    user_map: Dict[str, str] = Field(default_factory=dict)


class BridgeSettings(BaseSettings):
    """Bridge settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Hub (MLAI) ---
    MLAI_BOT_TOKEN: str
    MLAI_TEAM_ID: Optional[str] = None  # resolved at startup
    MLAI_BOT_USER_ID: Optional[str] = None  # resolved at startup

    # --- Spokes ---
    # JSON list in the env var, e.g.
    # BRIDGE_PAIRS=[{"label":"hex","mlai_channel":"exp-victor-ai","remote_token":"xoxb-...","remote_channel":"exp-victor-ai"}]
    BRIDGE_PAIRS: List[BridgePair] = []

    # --- Behaviour ---
    POLL_SECONDS: float = 5.0
    # Thread replies are swept by re-scanning recent threads each poll; replies
    # on threads whose parent is older than this window are not picked up.
    THREAD_SWEEP_SECONDS: float = 3 * 24 * 3600
    BRIDGE_DELIVERY_POLL_SECONDS: float = 2.0
    BRIDGE_MAX_DELIVERY_ATTEMPTS: int = 5
    BRIDGE_DB_PATH: str = "data/slack_bridge.db"
    # Bridge messages from bots (e.g. Roo's own posts) across.
    BRIDGE_RELAY_BOT_MESSAGES: bool = True
    # Best-effort re-upload of shared files across workspaces.
    BRIDGE_RELAY_FILES: bool = True
    # Circuit breaker: if the bridge posts more than this in 60s, pause + alert.
    BRIDGE_MAX_POSTS_PER_MIN: int = 30
    # Slack user id (MLAI side) to DM on errors. Usually Sam.
    BRIDGE_ALERT_DM_USER_ID: Optional[str] = None
    # Prune dedupe/registry rows older than this many days.
    BRIDGE_PRUNE_AFTER_DAYS: int = 7

    # --- Cross-workspace mentions ---
    # The corresponding plain-text prefix for users who exist only in MLAI.
    MLAI_MENTION_ALIAS: str = "mlai"
    # plain: legacy inert @Name rendering
    # observe: resolve candidates and record counters, but do not notify
    # native: emit <@destination-user-id> and trigger a real Slack mention
    BRIDGE_MENTION_MODE: Literal["plain", "observe", "native"] = "plain"
    # Refresh active-user directories without requiring a service restart when
    # people join, leave, rename themselves, or change profile email.
    BRIDGE_IDENTITY_REFRESH_SECONDS: float = 3600.0

    DEBUG: bool = False


_settings: Optional[BridgeSettings] = None


def get_bridge_settings() -> BridgeSettings:
    """Get or create the bridge settings singleton."""
    global _settings
    if _settings is None:
        _settings = BridgeSettings()
    return _settings
