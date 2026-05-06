"""
Roo Standalone Configuration

Pydantic Settings for environment-based configuration.
"""
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # Slack
    SLACK_BOT_TOKEN: str
    SLACK_SIGNING_SECRET: str
    
    # LLM Providers (at least one required)
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_VISION_MODEL: str = "gpt-4.1-mini"
    GOOGLE_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    
    # External Services
    CONTENT_FACTORY_URL: Optional[str] = None
    CONTENT_FACTORY_API_KEY: Optional[str] = None
    MLAI_BACKEND_URL: Optional[str] = None
    MLAI_API_KEY: Optional[str] = None
    ROO_API_KEY: Optional[str] = None
    INTERNAL_API_KEY: Optional[str] = None
    LINEAR_DEFAULT_TEAM: Optional[str] = None

    # GitHub OAuth
    GITHUB_CLIENT_ID: Optional[str] = None
    GITHUB_CLIENT_SECRET: Optional[str] = None
    SLACK_APP_URL: Optional[str] = None  # e.g. https://api.yourbot.com
    
    # Application
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    SKILLS_DIR: str = "skills"
    TIMEZONE: str = "Australia/Melbourne"
    ROUTER_MODEL: str = "gpt-5.4"
    LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE: float = 0.85
    LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE: float = 0.65
    COWORKING_INTENTS_DB_PATH: str = "data/coworking_booking_intents.db"
    COWORKING_RETRY_POLL_SECONDS: float = 30.0
    ROO_POINTS_TOPUP_ENABLED: bool = False
    BOOST_LINK_LOVE_ENABLED: bool = True
    BOOST_LINK_LOVE_CHANNEL_NAME: str = "boost-my-startup"
    BOOST_LINK_LOVE_DB_PATH: str = "data/link_love_awards.db"
    BOOST_LINK_LOVE_NOTIFICATION_DELAY_SECONDS: int = 60
    BOOST_LINK_LOVE_RETRY_POLL_SECONDS: float = 15.0
    BOOST_LINK_LOVE_MAX_RETRY_ATTEMPTS: int = 5
    BOOST_LINK_LOVE_MAX_ROOT_AGE_DAYS: int = 7
    JOBS_SCHEDULER_ENABLED: bool = False
    JOBS_API_URL: Optional[str] = None
    JOBS_TRIGGER_TOKEN: Optional[str] = None
    JOBS_SCHEDULE_HOUR: int = 7
    JOBS_SCHEDULE_MINUTE: int = 0
    JOBS_COLLECT_LIVE: bool = True
    JOBS_POST_TO_SLACK: bool = False
    JOBS_POST_TO_NOTION: bool = True
    JOBS_MAX_PAGES: Optional[int] = 1
    JOBS_PER_KEYWORD_LIMIT: Optional[int] = 5
    JOBS_RETRY_ATTEMPTS: int = 3
    JOBS_RETRY_DELAY_SECONDS: int = 300
    JOBS_FAILURE_STOP_AFTER_DAYS: int = 3

    @property
    def default_llm_provider(self) -> str:
        """Determine default LLM provider based on available keys."""
        if self.GOOGLE_API_KEY:
            return "gemini"
        if self.OPENAI_API_KEY:
            return "openai"
        if self.ANTHROPIC_API_KEY:
            return "anthropic"
        raise ValueError("No LLM API key configured")


# Singleton settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
