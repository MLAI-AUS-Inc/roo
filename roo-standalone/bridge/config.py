"""
Bridge configuration.

Pydantic Settings, mirroring roo/config.py conventions (env file, extra=ignore,
singleton getter). The bridge is a dedicated "Bridge" Slack app installed in BOTH
workspaces, so each side has its own normal bot token. Roo is not involved.
Both sides are POLLED (no inbound webhooks), so there is no signing secret here.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class BridgeSettings(BaseSettings):
    """Bridge settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- MLAI side (Bridge app's MLAI install) ---
    MLAI_BOT_TOKEN: str
    MLAI_CHANNEL_ID: str
    MLAI_POLL_SECONDS: float = 5.0
    # Resolved at startup via auth.test if left unset.
    MLAI_TEAM_ID: Optional[str] = None
    MLAI_BOT_USER_ID: Optional[str] = None

    # --- Stone & Chalk side (Bridge app's S&C install) ---
    SNC_BOT_TOKEN: Optional[str] = None
    SNC_CHANNEL_ID: str
    SNC_POLL_SECONDS: float = 5.0
    # Resolved at startup via auth.test if left unset.
    SNC_TEAM_ID: Optional[str] = None
    SNC_BOT_USER_ID: Optional[str] = None

    # --- Behaviour ---
    BRIDGE_DB_PATH: str = "data/slack_bridge.db"
    # Bridge messages from bots (e.g. Roo's own posts in the channel) across.
    BRIDGE_RELAY_BOT_MESSAGES: bool = True
    # Best-effort re-upload of shared files across workspaces.
    BRIDGE_RELAY_FILES: bool = True
    # Circuit breaker: if the bridge posts more than this in 60s, pause + alert.
    BRIDGE_MAX_POSTS_PER_MIN: int = 30
    # Store-and-forward delivery worker: how often it drains the queue, and how
    # many times to retry a message before giving up (with exponential backoff).
    BRIDGE_DELIVERY_POLL_SECONDS: float = 2.0
    BRIDGE_MAX_DELIVERY_ATTEMPTS: int = 5
    # Slack user id (MLAI side) to DM on errors / revoked tokens. Usually Sam.
    BRIDGE_ALERT_DM_USER_ID: Optional[str] = None
    # Prune dedupe/registry rows older than this many days.
    BRIDGE_PRUNE_AFTER_DAYS: int = 7

    DEBUG: bool = False

    @property
    def snc_configured(self) -> bool:
        return bool(self.SNC_BOT_TOKEN)


_settings: Optional[BridgeSettings] = None


def get_bridge_settings() -> BridgeSettings:
    """Get or create the bridge settings singleton."""
    global _settings
    if _settings is None:
        _settings = BridgeSettings()
    return _settings
