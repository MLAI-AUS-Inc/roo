"""
Bridge configuration.

Pydantic Settings, mirroring roo/config.py conventions (env file, extra=ignore,
singleton getter). MLAI is the hub: one MLAI bot token, plus a list of channel
PAIRS, one per partner workspace channel. Each pair brings its own remote bot
token + channel, so adding a workspace is just one more entry in BRIDGE_PAIRS.

Channels may be given as an ID (e.g. C0123ABCD) or a name (e.g. exp-victor-ai),
which is resolved at startup. Both sides are POLLED (no webhooks, no secret).
"""
from typing import List, Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class BridgePair(BaseModel):
    """One mirrored channel: an MLAI channel <-> a channel in a partner workspace."""

    label: str                 # short id, no ':' (e.g. "hex", "stone-and-chalk")
    mlai_channel: str          # name or ID in the MLAI workspace
    remote_token: str          # the Bridge bot token for the partner workspace
    remote_channel: str        # name or ID in the partner workspace


class BridgeSettings(BaseSettings):
    """Bridge settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Hub (MLAI) ---
    MLAI_BOT_TOKEN: str
    MLAI_TEAM_ID: Optional[str] = None       # resolved at startup
    MLAI_BOT_USER_ID: Optional[str] = None   # resolved at startup

    # --- Spokes ---
    # JSON list in the env var, e.g.
    # BRIDGE_PAIRS=[{"label":"hex","mlai_channel":"exp-victor-ai","remote_token":"xoxb-...","remote_channel":"exp-victor-ai"}]
    BRIDGE_PAIRS: List[BridgePair] = []

    # --- Behaviour ---
    POLL_SECONDS: float = 5.0
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

    DEBUG: bool = False


_settings: Optional[BridgeSettings] = None


def get_bridge_settings() -> BridgeSettings:
    """Get or create the bridge settings singleton."""
    global _settings
    if _settings is None:
        _settings = BridgeSettings()
    return _settings
