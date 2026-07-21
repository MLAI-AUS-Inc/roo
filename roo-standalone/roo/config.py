"""
Roo Standalone Configuration

Pydantic Settings for environment-based configuration.
"""
import hmac
import re
from typing import Optional
from pydantic import model_validator
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
    SLACK_CONTEXTUAL_STATE_DB_PATH: str = "data/slack_contextual_responses.db"

    # Context-aware channel replies. Disabled by default and restricted to an
    # explicit channel allowlist before any untagged message can be considered.
    ROO_CONTEXTUAL_RESPONSES_ENABLED: bool = False
    ROO_CONTEXTUAL_SHADOW_MODE: bool = True
    ROO_CONTEXTUAL_CHANNEL_IDS: str = ""
    ROO_CONTEXTUAL_MIN_CONFIDENCE: float = 0.90
    ROO_CONTEXTUAL_INDIRECT_MENTION_CONFIDENCE: float = 0.90
    ROO_CONTEXTUAL_ADJACENCY_SECONDS: int = 3 * 60
    ROO_CONTEXTUAL_THREAD_TTL_SECONDS: int = 30 * 60
    ROO_CONTEXTUAL_MESSAGE_RECEIPT_TTL_SECONDS: int = 10 * 60
    ROO_CONTEXTUAL_CLASSIFIER_TIMEOUT_SECONDS: float = 5.0
    ROO_CONTEXTUAL_MODEL: Optional[str] = None
    ROO_IMPLICIT_ACTION_ALLOWLIST: str = (
        "respond_in_chat,mlai-points:balance,mlai-points:topup_points"
    )
    
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
    LINEAR_CONTEXT_MAX_MESSAGES: int = 50
    LINEAR_CONTEXT_LOOKBACK_HOURS: int = 24
    LINEAR_CONTEXT_MAX_CHARS: int = 16000
    LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED: bool = True
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
    START_HERE_INTRO_ENABLED: bool = True
    START_HERE_INTRO_CHANNEL_NAME: str = "_start-here"
    START_HERE_INTRO_DB_PATH: str = "data/start_here_introductions.db"
    START_HERE_INTRO_MIN_CONFIDENCE: float = 0.8
    START_HERE_INTRO_RETRY_POLL_SECONDS: float = 15.0
    START_HERE_INTRO_MAX_RETRY_ATTEMPTS: int = 5
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
    # Contest cases the authenticated gateway may target (comma-separated
    # cases.yaml ids). Two wards run concurrently, so the gateway forwards the
    # player's chosen case; anything outside this set is refused so hidden or
    # retired cases can never leak dialogue or verdicts. Mirrors MLAI
    # Backend's HEALTH_HACK_OPEN_CASE_IDS default.
    SIM_OPEN_CASE_IDS: str = "1,2"

    @staticmethod
    def _split_configured_values(raw: str) -> frozenset[str]:
        return frozenset(
            value.strip()
            for value in str(raw or "").replace(",", " ").split()
            if value.strip()
        )

    @property
    def contextual_channel_ids(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_CONTEXTUAL_CHANNEL_IDS)

    @property
    def implicit_action_allowlist(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_IMPLICIT_ACTION_ALLOWLIST)

    @model_validator(mode="after")
    def validate_contextual_responses(self):
        if not str(self.SLACK_CONTEXTUAL_STATE_DB_PATH or "").strip():
            raise ValueError("SLACK_CONTEXTUAL_STATE_DB_PATH is required")
        if not 0.5 <= self.ROO_CONTEXTUAL_MIN_CONFIDENCE <= 1.0:
            raise ValueError("ROO_CONTEXTUAL_MIN_CONFIDENCE must be between 0.5 and 1.0")
        if not 0.5 <= self.ROO_CONTEXTUAL_INDIRECT_MENTION_CONFIDENCE <= 1.0:
            raise ValueError(
                "ROO_CONTEXTUAL_INDIRECT_MENTION_CONFIDENCE must be between 0.5 and 1.0"
            )
        if self.ROO_CONTEXTUAL_ADJACENCY_SECONDS <= 0:
            raise ValueError("ROO_CONTEXTUAL_ADJACENCY_SECONDS must be positive")
        if self.ROO_CONTEXTUAL_THREAD_TTL_SECONDS <= 0:
            raise ValueError("ROO_CONTEXTUAL_THREAD_TTL_SECONDS must be positive")
        if self.ROO_CONTEXTUAL_MESSAGE_RECEIPT_TTL_SECONDS <= 0:
            raise ValueError("ROO_CONTEXTUAL_MESSAGE_RECEIPT_TTL_SECONDS must be positive")
        if not 0 < self.ROO_CONTEXTUAL_CLASSIFIER_TIMEOUT_SECONDS <= 30:
            raise ValueError(
                "ROO_CONTEXTUAL_CLASSIFIER_TIMEOUT_SECONDS must be between 0 and 30"
            )
        if any(
            not re.fullmatch(r"[CG][A-Z0-9]+", channel_id)
            for channel_id in self.contextual_channel_ids
        ):
            raise ValueError("ROO_CONTEXTUAL_CHANNEL_IDS contains invalid channel IDs")
        if self.ROO_CONTEXTUAL_RESPONSES_ENABLED and not self.contextual_channel_ids:
            raise ValueError(
                "Contextual Roo responses require ROO_CONTEXTUAL_CHANNEL_IDS"
            )
        return self

    @property
    def sim_open_case_ids(self) -> frozenset[int]:
        """Open contest cases. Malformed tokens are skipped and the active
        case is always included, so a bad env value narrows the set at worst —
        it can never brick the default (active-case) path."""
        ids = {self.SIM_ACTIVE_CASE_ID}
        for token in self.SIM_OPEN_CASE_IDS.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                ids.add(int(token))
            except ValueError:
                continue
        return frozenset(ids)

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

    if hmac.compare_digest(api_key.encode("utf-8"), safety_salt.encode("utf-8")):
        raise RuntimeError(
            "SIM_PATIENT_API_KEY and SIM_PATIENT_SAFETY_SALT must be distinct"
        )

    registry_key = (settings.ROO_API_KEY or "").strip()
    if len(registry_key) < 32:
        raise RuntimeError(
            "ROO_API_KEY must be configured with at least 32 characters "
            "when ROO_ENVIRONMENT=production"
        )
    if any(
        hmac.compare_digest(registry_key.encode("utf-8"), other.encode("utf-8"))
        for other in (api_key, safety_salt)
    ):
        raise RuntimeError(
            "ROO_API_KEY must be distinct from SIM_PATIENT_API_KEY and "
            "SIM_PATIENT_SAFETY_SALT"
        )

    if not (settings.OPENAI_API_KEY or "").strip():
        raise RuntimeError(
            "OPENAI_API_KEY must be configured when ROO_ENVIRONMENT=production"
        )

    timeout = float(settings.SIM_PATIENT_OPENAI_TIMEOUT_SECONDS)
    if timeout <= 0 or timeout > 20:
        raise RuntimeError(
            "SIM_PATIENT_OPENAI_TIMEOUT_SECONDS must be greater than 0 and no more than 20"
        )
