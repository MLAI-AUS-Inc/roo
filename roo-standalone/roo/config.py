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
    # Explicit deployment environment. Security-sensitive routes fail closed
    # only when this is "production"; local development remains frictionless.
    ROO_ENVIRONMENT: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    SKILLS_DIR: str = "skills"
    TIMEZONE: str = "Australia/Melbourne"
    # Used by router v2 and the legacy LLM fallback. Override via ROUTER_MODEL
    # in .env; re-run `scripts/run_routing_eval.py --mode v2` after any change.
    ROUTER_MODEL: str = "gpt-5.5"
    LINEAR_MEETING_LLM_MODEL: str = "gpt-5.5"
    LINEAR_MEETING_LLM_REASONING_EFFORT: str = "low"
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
    JOBS_SLACK_CHANNEL: Optional[str] = None
    JOBS_POST_TO_NOTION: bool = True
    JOBS_MAX_PAGES: Optional[int] = 1
    JOBS_PER_KEYWORD_LIMIT: Optional[int] = 5
    JOBS_RETRY_ATTEMPTS: int = 3
    JOBS_RETRY_DELAY_SECONDS: int = 300
    JOBS_FAILURE_STOP_AFTER_DAYS: int = 3

    # Simulated patient endpoint (health-hack 3D ward "Guess the Diagnosis").
    # Bearer auth may be omitted only in local development. Production startup
    # validation below requires a strong key and a separate safety-id salt.
    SIM_PATIENT_API_KEY: Optional[str] = None
    SIM_PATIENT_SAFETY_SALT: Optional[str] = None
    SIM_PATIENT_MODEL: str = "gpt-5.6-terra"
    SIM_PATIENT_REASONING_EFFORT: str = "low"
    # Must remain below MLAI Backend's 24s read timeout so abandoned model
    # calls do not outlive their authenticated gateway request.
    SIM_PATIENT_OPENAI_TIMEOUT_SECONDS: float = 20.0
    # The ward contest's active case (cases.yaml id). Pinned server-side so web
    # clients can never pick a case and farm tickets from cases not in play.
    SIM_ACTIVE_CASE_ID: int = 1

    @property
    def is_production(self) -> bool:
        """Whether production-only fail-closed controls must be enforced."""
        return self.ROO_ENVIRONMENT.strip().lower() in {"production", "prod"}

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


def validate_runtime_security(settings: Settings) -> None:
    """Reject an unsafe production configuration before the app serves traffic.

    This is deliberately a startup check rather than a permissive route-time
    fallback: a production instance must never silently expose the Health Hack
    model endpoints because an environment variable was omitted.
    """
    if not settings.is_production:
        return

    api_key = (settings.SIM_PATIENT_API_KEY or "").strip()
    if len(api_key) < 32:
        raise RuntimeError(
            "SIM_PATIENT_API_KEY must be configured with at least 32 characters "
            "when ROO_ENVIRONMENT=production"
        )

    safety_salt = (settings.SIM_PATIENT_SAFETY_SALT or "").strip()
    if len(safety_salt) < 32:
        raise RuntimeError(
            "SIM_PATIENT_SAFETY_SALT must be configured with at least 32 characters "
            "when ROO_ENVIRONMENT=production"
        )

    timeout = float(settings.SIM_PATIENT_OPENAI_TIMEOUT_SECONDS)
    if timeout <= 0 or timeout > 22:
        raise RuntimeError(
            "SIM_PATIENT_OPENAI_TIMEOUT_SECONDS must be greater than 0 and no more than 22"
        )
