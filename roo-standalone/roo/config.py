"""
Roo Standalone Configuration

Pydantic Settings for environment-based configuration.
"""
import hmac
import re
from typing import ClassVar, Literal, Optional
from urllib.parse import urlparse
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    PUBLIC_DEFAULT_SKILLS: ClassVar[frozenset[str]] = frozenset(
        {
            "committee-candidate-emails",
            "connect-users",
            "content-factory",
            "github-integration",
            "healthhack",
            "linear-meeting-actions",
            "luma-events",
            "medhack",
            "mlai-data-query",
            "mlai-points",
            "reconciliation-report",
            "start-here-introductions",
            "tone-of-voice",
            "watt-the-hack",
        }
    )
    PRIVATE_SKILLS: ClassVar[frozenset[str]] = frozenset(
        {"admin-actions", "admin-brain", "org-memory"}
    )
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # Slack
    SLACK_BOT_TOKEN: Optional[str] = None
    SLACK_SIGNING_SECRET: Optional[str] = None
    SLACK_REQUEST_MAX_AGE_SECONDS: int = 300
    SLACK_RECEIPT_TTL_SECONDS: int = 10 * 60
    SLACK_RECEIPTS_DB_PATH: str = "data/slack_request_receipts.db"
    SLACK_CONTEXTUAL_STATE_DB_PATH: str = "data/slack_contextual_responses.db"
    SLACK_MODERATOR_USER_TOKEN: Optional[str] = None
    SLACK_MODERATOR_USER_ID: str = ""
    SLACK_MODERATOR_TEAM_ID: str = ""

    # Context-aware channel replies. Disabled by default and restricted to an
    # explicit channel allowlist before any untagged message can be considered.
    ROO_CONTEXTUAL_RESPONSES_ENABLED: bool = False
    ROO_CONTEXTUAL_SHADOW_MODE: bool = False
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
    INTERNAL_MENTION_API_KEY: Optional[str] = None
    RECONCILIATION_DOMAIN: str = "mlai.au"
    RECONCILIATION_AGENT_URL: Optional[str] = None
    RECONCILIATION_AGENT_TIMEOUT_SECONDS: float = 30.0
    LINEAR_DEFAULT_TEAM: Optional[str] = None

    # Public/Admin trust boundary. Admin starts with no skills and no private
    # memory access until an explicit allowlist and scoped credential exist.
    ROO_SURFACE: Literal["public", "admin"] = "public"
    ROO_ENABLED_SKILLS: str = ""
    ROO_ALLOWED_CHANNEL_IDS: str = ""
    ROO_ALLOWED_DM_USER_IDS: str = ""
    ORG_BRAIN_ENABLED: bool = False
    ORG_BRAIN_ACTIONS_ENABLED: bool = False
    ORG_BRAIN_API_KEY: Optional[str] = None
    ORG_BRAIN_BACKEND_TIMEOUT_SECONDS: float = 20.0
    ORG_BRAIN_MAX_CONTEXT_TOKENS: int = 6000
    ROO_UNIFIED_ADMIN_ROUTING_ENABLED: bool = False
    ORG_BRAIN_ROUTER_API_KEY: Optional[str] = None
    ROO_ADMIN_INTERNAL_URL: Optional[str] = None
    ROO_ADMIN_DISPATCH_SECRET: Optional[str] = None
    ROO_ADMIN_INTERNAL_ONLY: bool = False
    ROO_ADMIN_DISPATCH_MAX_AGE_SECONDS: int = 60
    ROO_ADMIN_DISPATCH_RECEIPT_TTL_SECONDS: int = 10 * 60
    ROO_ADMIN_DISPATCH_RECEIPTS_DB_PATH: str = "data/admin_dispatch_receipts.db"

    # Read-only Victor AI application reports. Access is based on the resolved
    # Slack channel name, so channel or user ID allowlists are not required.
    VICTOR_AI_SKILL_ENABLED: bool = False
    VICTOR_AI_ROO_SIGNING_SECRET: Optional[str] = None
    VICTOR_AI_SLACK_CHANNEL_NAME: str = "exp-victor-ai"
    VICTOR_AI_BACKEND_TIMEOUT_SECONDS: float = 20.0

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
    LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE: float = 0.85
    LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE: float = 0.65
    LINEAR_MEETING_EXTRACTION_CONCURRENCY: int = 3
    LINEAR_MEETING_EXTRACTION_TOTAL_TIMEOUT_SECONDS: float = 360.0
    LINEAR_MEETING_EXTRACTION_CHUNK_MAX_CHARS: int = 8000
    LINEAR_MEETING_EXTRACTION_RECOVERY_MAX_CHARS: int = 4000
    LINEAR_MEETING_EXTRACTION_RECOVERY_DEPTH: int = 2
    LINEAR_TASK_SIZING_MODE: Optional[str] = None
    LINEAR_TASK_SIZING_AUTO_CREATE_MIN_CONFIDENCE: Optional[float] = None
    LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS: Optional[int] = None
    LINEAR_TASK_SIZING_BATCH_SIZE: Optional[int] = None
    LINEAR_TASK_SIZING_RUBRIC_VERSION: Optional[str] = None
    LINEAR_PROJECT_SIZING_MAX_ISSUES: int = 500
    LINEAR_PROJECT_SIZING_INFERENCE_CONCURRENCY: int = 3
    # Deprecated fallbacks retained for existing deployments.
    LINEAR_STUDIO_SIZING_MODE: str = "required"
    LINEAR_STUDIO_SIZING_AUTO_CREATE_MIN_CONFIDENCE: float = 0.75
    LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS: int = 40000
    LINEAR_STUDIO_SIZING_BATCH_SIZE: int = 3
    LINEAR_STUDIO_SIZING_RUBRIC_VERSION: str = "studio-effort-v1"
    LINEAR_CONTEXT_MAX_MESSAGES: int = 50
    LINEAR_CONTEXT_LOOKBACK_HOURS: int = 24
    LINEAR_CONTEXT_MAX_CHARS: int = 16000
    LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED: bool = True
    COWORKING_INTENTS_DB_PATH: str = "data/coworking_booking_intents.db"
    COWORKING_RETRY_POLL_SECONDS: float = 30.0
    ROO_POINTS_TOPUP_ENABLED: bool = False
    ROO_POINTS_TOPUP_BUTTONS_ENABLED: bool = False
    ROO_POINTS_STRIPE_CHECKOUT_HOSTS: str = "checkout.stripe.com"
    MEETING_ROOM_BOOKING_ENABLED: bool = False
    BOOST_LINK_LOVE_ENABLED: bool = True
    BOOST_LINK_LOVE_CHANNEL_NAME: str = "boost-my-startup"
    BOOST_LINK_LOVE_CHANNEL_ID: str = ""
    BOOST_LINK_LOVE_DB_PATH: str = "data/link_love_awards.db"
    BOOST_LINK_LOVE_NOTIFICATION_DELAY_SECONDS: int = 60
    BOOST_LINK_LOVE_RETRY_POLL_SECONDS: float = 15.0
    BOOST_LINK_LOVE_MAX_RETRY_ATTEMPTS: int = 5
    BOOST_LINK_LOVE_MAX_ROOT_AGE_DAYS: int = 7
    BOOST_POST_MODERATION_ENABLED: bool = False
    BOOST_POST_AUTO_DELETE_ENABLED: bool = False
    BOOST_POST_ENFORCEMENT_CUTOFF_TS: str = ""
    BOOST_POST_DECISION_TIMEOUT_SECONDS: float = 30.0
    BOOST_POST_RETRY_POLL_SECONDS: float = 15.0
    BOOST_POST_MAX_RETRY_ATTEMPTS: int = 5
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
    def has_explicit_skill_allowlist(self) -> bool:
        return bool(self._split_configured_values(self.ROO_ENABLED_SKILLS))

    @property
    def enabled_skill_names(self) -> frozenset[str]:
        configured = self._split_configured_values(self.ROO_ENABLED_SKILLS)
        if configured:
            return configured
        if self.ROO_SURFACE == "public":
            enabled = set(self.PUBLIC_DEFAULT_SKILLS)
            if self.VICTOR_AI_SKILL_ENABLED:
                enabled.add("victor-ai-applications")
            if self.MEETING_ROOM_BOOKING_ENABLED:
                enabled.add("meeting-room-booking")
            return frozenset(enabled)
        return frozenset()

    @property
    def allowed_channel_ids(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_ALLOWED_CHANNEL_IDS)

    @property
    def allowed_dm_user_ids(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_ALLOWED_DM_USER_IDS)

    @property
    def contextual_channel_ids(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_CONTEXTUAL_CHANNEL_IDS)

    @property
    def implicit_action_allowlist(self) -> frozenset[str]:
        return self._split_configured_values(self.ROO_IMPLICIT_ACTION_ALLOWLIST)

    @property
    def roo_points_stripe_checkout_hosts(self) -> frozenset[str]:
        return frozenset(
            value.lower()
            for value in self._split_configured_values(
                self.ROO_POINTS_STRIPE_CHECKOUT_HOSTS
            )
        )

    @property
    def victor_ai_slack_channel_name(self) -> str:
        return str(self.VICTOR_AI_SLACK_CHANNEL_NAME or "").strip().lstrip("#").lower()

    def is_victor_ai_context_allowed(self, *, channel_name: Optional[str]) -> bool:
        resolved_name = str(channel_name or "").strip().lstrip("#").lower()
        return bool(
            self.VICTOR_AI_SKILL_ENABLED
            and resolved_name
            and resolved_name == self.victor_ai_slack_channel_name
        )

    def is_slack_context_allowed(
        self,
        *,
        channel_id: Optional[str],
        user_id: Optional[str],
        channel_type: Optional[str] = None,
    ) -> bool:
        if self.ROO_SURFACE == "public":
            return True
        if channel_type == "im":
            return bool(user_id and user_id in self.allowed_dm_user_ids)
        return bool(channel_id and channel_id in self.allowed_channel_ids)

    @model_validator(mode="after")
    def validate_contextual_responses(self):
        internal_admin = bool(
            self.ROO_SURFACE == "admin" and self.ROO_ADMIN_INTERNAL_ONLY
        )
        if not internal_admin and (
            not str(self.SLACK_BOT_TOKEN or "").strip()
            or not str(self.SLACK_SIGNING_SECRET or "").strip()
        ):
            raise ValueError("Slack bot token and signing secret are required")
        if not 1 <= self.SLACK_REQUEST_MAX_AGE_SECONDS <= 300:
            raise ValueError("SLACK_REQUEST_MAX_AGE_SECONDS must be between 1 and 300")
        if self.SLACK_RECEIPT_TTL_SECONDS < self.SLACK_REQUEST_MAX_AGE_SECONDS:
            raise ValueError(
                "SLACK_RECEIPT_TTL_SECONDS must be at least SLACK_REQUEST_MAX_AGE_SECONDS"
            )
        if not str(self.SLACK_RECEIPTS_DB_PATH or "").strip():
            raise ValueError("SLACK_RECEIPTS_DB_PATH is required")
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
        if self.ROO_SURFACE == "admin" and self.ROO_CONTEXTUAL_RESPONSES_ENABLED:
            raise ValueError("Admin Roo cannot enable contextual channel responses")
        if self.ROO_SURFACE == "admin" and self.ROO_CONTEXTUAL_SHADOW_MODE:
            raise ValueError("Admin Roo cannot enable contextual shadow mode")
        if (
            self.ROO_POINTS_TOPUP_BUTTONS_ENABLED
            and not self.roo_points_stripe_checkout_hosts
        ):
            raise ValueError(
                "ROO_POINTS_STRIPE_CHECKOUT_HOSTS is required when top-up buttons are enabled"
            )
        if self.VICTOR_AI_SKILL_ENABLED:
            if self.ROO_SURFACE != "public":
                raise ValueError(
                    "Victor AI application access is available only on Public Roo"
                )
            if not str(self.MLAI_BACKEND_URL or "").strip():
                raise ValueError(
                    "MLAI_BACKEND_URL is required when VICTOR_AI_SKILL_ENABLED is true"
                )
            if len(str(self.VICTOR_AI_ROO_SIGNING_SECRET or "")) < 32:
                raise ValueError(
                    "VICTOR_AI_ROO_SIGNING_SECRET must contain at least 32 characters"
                )
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,79}",
                self.victor_ai_slack_channel_name,
            ):
                raise ValueError("VICTOR_AI_SLACK_CHANNEL_NAME is invalid")
            if not 1 <= self.VICTOR_AI_BACKEND_TIMEOUT_SECONDS <= 60:
                raise ValueError(
                    "VICTOR_AI_BACKEND_TIMEOUT_SECONDS must be between 1 and 60"
                )

        boost_channel_id = str(self.BOOST_LINK_LOVE_CHANNEL_ID or "").strip()
        moderator_user_id = str(self.SLACK_MODERATOR_USER_ID or "").strip()
        moderator_team_id = str(self.SLACK_MODERATOR_TEAM_ID or "").strip()
        cutoff_ts = str(self.BOOST_POST_ENFORCEMENT_CUTOFF_TS or "").strip()
        if boost_channel_id and not re.fullmatch(r"C[A-Z0-9]+", boost_channel_id):
            raise ValueError("BOOST_LINK_LOVE_CHANNEL_ID must be a public Slack channel ID")
        if moderator_user_id and not re.fullmatch(r"[UW][A-Z0-9]+", moderator_user_id):
            raise ValueError("SLACK_MODERATOR_USER_ID is invalid")
        if moderator_team_id and not re.fullmatch(r"T[A-Z0-9]+", moderator_team_id):
            raise ValueError("SLACK_MODERATOR_TEAM_ID is invalid")
        if cutoff_ts and not re.fullmatch(r"\d{8,}\.\d+", cutoff_ts):
            raise ValueError("BOOST_POST_ENFORCEMENT_CUTOFF_TS must be a Slack timestamp")
        if not 1 <= self.BOOST_POST_DECISION_TIMEOUT_SECONDS <= 120:
            raise ValueError("BOOST_POST_DECISION_TIMEOUT_SECONDS must be between 1 and 120")
        if not 1 <= self.BOOST_POST_RETRY_POLL_SECONDS <= 300:
            raise ValueError("BOOST_POST_RETRY_POLL_SECONDS must be between 1 and 300")
        if not 1 <= self.BOOST_POST_MAX_RETRY_ATTEMPTS <= 20:
            raise ValueError("BOOST_POST_MAX_RETRY_ATTEMPTS must be between 1 and 20")
        if self.BOOST_POST_MODERATION_ENABLED:
            if self.ROO_SURFACE != "public":
                raise ValueError("Boost-post moderation is available only on Public Roo")
            if not self.BOOST_LINK_LOVE_ENABLED:
                raise ValueError("Boost-post moderation requires BOOST_LINK_LOVE_ENABLED")
            if not boost_channel_id:
                raise ValueError("Boost-post moderation requires BOOST_LINK_LOVE_CHANNEL_ID")
            if not cutoff_ts:
                raise ValueError(
                    "Boost-post moderation requires BOOST_POST_ENFORCEMENT_CUTOFF_TS"
                )
            if not self.MLAI_BACKEND_URL:
                raise ValueError("Boost-post moderation requires MLAI_BACKEND_URL")
        if self.BOOST_POST_AUTO_DELETE_ENABLED:
            if not self.BOOST_POST_MODERATION_ENABLED:
                raise ValueError("Boost-post auto-delete requires moderation to be enabled")
            if not self.SLACK_MODERATOR_USER_TOKEN:
                raise ValueError("Boost-post auto-delete requires SLACK_MODERATOR_USER_TOKEN")
            if not moderator_user_id or not moderator_team_id:
                raise ValueError(
                    "Boost-post auto-delete requires moderator user and team IDs"
                )
            token = str(self.SLACK_MODERATOR_USER_TOKEN)
            if not (token.startswith("xoxp-") or token.startswith("xoxe.xoxp-")):
                raise ValueError("SLACK_MODERATOR_USER_TOKEN must be a Slack user token")
        if self.ROO_SURFACE == "admin" and self.SLACK_MODERATOR_USER_TOKEN:
            raise ValueError("Admin Roo cannot receive the Slack moderator user token")

        enabled_skills = self.enabled_skill_names
        invalid_skill_names = {
            name
            for name in enabled_skills
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
        }
        if invalid_skill_names:
            raise ValueError(
                "ROO_ENABLED_SKILLS contains invalid names: "
                + ", ".join(sorted(invalid_skill_names))
            )
        if (
            "victor-ai-applications" in enabled_skills
            and not self.VICTOR_AI_SKILL_ENABLED
        ):
            raise ValueError(
                "victor-ai-applications cannot be enabled unless "
                "VICTOR_AI_SKILL_ENABLED is true"
            )
        if (
            "meeting-room-booking" in enabled_skills
            and not self.MEETING_ROOM_BOOKING_ENABLED
        ):
            raise ValueError(
                "meeting-room-booking cannot be enabled unless "
                "MEETING_ROOM_BOOKING_ENABLED is true"
            )
        if self.MEETING_ROOM_BOOKING_ENABLED:
            if self.ROO_SURFACE != "public":
                raise ValueError(
                    "Meeting-room booking is available only on Public Roo"
                )
            if not str(self.MLAI_BACKEND_URL or "").strip():
                raise ValueError(
                    "MLAI_BACKEND_URL is required when "
                    "MEETING_ROOM_BOOKING_ENABLED is true"
                )
            if not str(self.ROO_API_KEY or "").strip():
                raise ValueError(
                    "ROO_API_KEY is required when "
                    "MEETING_ROOM_BOOKING_ENABLED is true"
                )

        if self.ROO_SURFACE == "public":
            if (
                self.ORG_BRAIN_ENABLED
                or self.ORG_BRAIN_ACTIONS_ENABLED
                or self.ORG_BRAIN_API_KEY
            ):
                raise ValueError(
                    "Public Roo cannot receive organisational-brain access"
                )
            private_skills = enabled_skills & self.PRIVATE_SKILLS
            if private_skills:
                raise ValueError(
                    "Public Roo cannot enable private skills: "
                    + ", ".join(sorted(private_skills))
                )
            unified_values_present = bool(
                self.ORG_BRAIN_ROUTER_API_KEY
                or self.ROO_ADMIN_INTERNAL_URL
                or self.ROO_ADMIN_DISPATCH_SECRET
            )
            if self.ROO_UNIFIED_ADMIN_ROUTING_ENABLED:
                if not str(self.MLAI_BACKEND_URL or "").strip():
                    raise ValueError(
                        "Unified Admin routing requires MLAI_BACKEND_URL"
                    )
                if not re.fullmatch(
                    r"mlai_sp_[0-9a-f]{32}\.[A-Za-z0-9_-]{32,128}",
                    str(self.ORG_BRAIN_ROUTER_API_KEY or ""),
                ):
                    raise ValueError(
                        "Unified Admin routing requires a scoped router service principal"
                    )
                parsed_admin_url = urlparse(
                    str(self.ROO_ADMIN_INTERNAL_URL or "").strip()
                )
                if (
                    parsed_admin_url.scheme not in {"http", "https"}
                    or not parsed_admin_url.hostname
                    or parsed_admin_url.username
                    or parsed_admin_url.password
                    or parsed_admin_url.query
                    or parsed_admin_url.fragment
                    or parsed_admin_url.path not in {"", "/"}
                ):
                    raise ValueError(
                        "Unified Admin routing requires a valid internal Admin URL"
                    )
                if self.is_production and (
                    parsed_admin_url.scheme != "http"
                    or parsed_admin_url.hostname != "roo-admin"
                    or parsed_admin_url.port != 8000
                ):
                    raise ValueError(
                        "Production Admin routing must use http://roo-admin:8000"
                    )
                if len(str(self.ROO_ADMIN_DISPATCH_SECRET or "")) < 32:
                    raise ValueError(
                        "Unified Admin routing requires a 32-character dispatch secret"
                    )
                if hmac.compare_digest(
                    str(self.ROO_ADMIN_DISPATCH_SECRET),
                    str(self.SLACK_SIGNING_SECRET),
                ):
                    raise ValueError(
                        "Admin dispatch and Slack signing secrets must be distinct"
                    )
            elif unified_values_present:
                raise ValueError(
                    "Unified Admin routing credentials require ROO_UNIFIED_ADMIN_ROUTING_ENABLED"
                )
            return self

        if not 1 <= self.ORG_BRAIN_BACKEND_TIMEOUT_SECONDS <= 60:
            raise ValueError(
                "ORG_BRAIN_BACKEND_TIMEOUT_SECONDS must be between 1 and 60"
            )
        if not 1000 <= self.ORG_BRAIN_MAX_CONTEXT_TOKENS <= 12000:
            raise ValueError(
                "ORG_BRAIN_MAX_CONTEXT_TOKENS must be between 1000 and 12000"
            )
        if (
            not internal_admin
            and not self.allowed_channel_ids
            and not self.allowed_dm_user_ids
        ):
            raise ValueError(
                "Admin Roo requires ROO_ALLOWED_CHANNEL_IDS or ROO_ALLOWED_DM_USER_IDS"
            )
        if any(
            not re.fullmatch(r"G[A-Z0-9]+", value)
            for value in self.allowed_channel_ids
        ):
            raise ValueError(
                "Admin Roo allowlists contain invalid public or direct-message "
                "channel IDs; channels must use private G-prefixed Slack IDs"
            )
        if any(
            not re.fullmatch(r"[UW][A-Z0-9]+", value)
            for value in self.allowed_dm_user_ids
        ):
            raise ValueError("Admin Roo contains an invalid Slack user ID")
        if self.ORG_BRAIN_ENABLED:
            if not self.ORG_BRAIN_API_KEY:
                raise ValueError(
                    "Admin Roo brain access requires ORG_BRAIN_API_KEY"
                )
            if not re.fullmatch(
                r"mlai_sp_[0-9a-f]{32}\.[A-Za-z0-9_-]{32,128}",
                self.ORG_BRAIN_API_KEY,
            ):
                raise ValueError(
                    "ORG_BRAIN_API_KEY must be a scoped service-principal credential"
                )
            if "admin-brain" not in enabled_skills:
                raise ValueError(
                    "ORG_BRAIN_ENABLED requires admin-brain in ROO_ENABLED_SKILLS"
                )
        elif self.ORG_BRAIN_API_KEY:
            raise ValueError(
                "ORG_BRAIN_API_KEY cannot be present while ORG_BRAIN_ENABLED is false"
            )
        if self.ORG_BRAIN_ACTIONS_ENABLED:
            if not self.ORG_BRAIN_ENABLED:
                raise ValueError(
                    "Admin Roo controlled actions require ORG_BRAIN_ENABLED"
                )
            if "admin-actions" not in enabled_skills:
                raise ValueError(
                    "ORG_BRAIN_ACTIONS_ENABLED requires admin-actions in ROO_ENABLED_SKILLS"
                )
        elif "admin-actions" in enabled_skills:
            raise ValueError(
                "admin-actions cannot be enabled while ORG_BRAIN_ACTIONS_ENABLED is false"
            )
        if internal_admin:
            if self.SLACK_BOT_TOKEN or self.SLACK_SIGNING_SECRET:
                raise ValueError(
                    "Internal Admin Roo must not receive Slack application credentials"
                )
            if self.OPENAI_API_KEY or self.GOOGLE_API_KEY or self.ANTHROPIC_API_KEY:
                raise ValueError(
                    "Internal Admin Roo must not receive an LLM provider credential"
                )
            if (
                self.ROO_UNIFIED_ADMIN_ROUTING_ENABLED
                or self.ORG_BRAIN_ROUTER_API_KEY
                or self.ROO_ADMIN_INTERNAL_URL
            ):
                raise ValueError(
                    "Internal Admin Roo cannot act as the public routing gateway"
                )
            if len(str(self.ROO_ADMIN_DISPATCH_SECRET or "")) < 32:
                raise ValueError(
                    "Internal Admin Roo requires a 32-character dispatch secret"
                )
            if not str(self.MLAI_BACKEND_URL or "").strip():
                raise ValueError("Internal Admin Roo requires MLAI_BACKEND_URL")
            if hmac.compare_digest(
                str(self.ROO_ADMIN_DISPATCH_SECRET),
                str(self.ORG_BRAIN_API_KEY),
            ):
                raise ValueError(
                    "Admin dispatch and memory credentials must be distinct"
                )
            if not str(self.ROO_ADMIN_DISPATCH_RECEIPTS_DB_PATH or "").strip():
                raise ValueError("Internal Admin Roo requires a dispatch receipt database")
            if not 1 <= self.ROO_ADMIN_DISPATCH_MAX_AGE_SECONDS <= 60:
                raise ValueError(
                    "ROO_ADMIN_DISPATCH_MAX_AGE_SECONDS must be between 1 and 60"
                )
            if (
                self.ROO_ADMIN_DISPATCH_RECEIPT_TTL_SECONDS
                < self.ROO_ADMIN_DISPATCH_MAX_AGE_SECONDS
            ):
                raise ValueError(
                    "ROO_ADMIN_DISPATCH_RECEIPT_TTL_SECONDS must cover the replay window"
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

    # The internal Admin worker has no Public Roo HTTP capabilities and does
    # not receive Public Roo's registry or simulated-patient credentials.
    if (
        getattr(settings, "ROO_SURFACE", "") == "admin"
        and bool(getattr(settings, "ROO_ADMIN_INTERNAL_ONLY", False))
    ):
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
