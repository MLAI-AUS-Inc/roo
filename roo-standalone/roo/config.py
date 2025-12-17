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
    
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://roo:password@localhost:5432/roo"
    
    # Slack
    SLACK_BOT_TOKEN: str
    SLACK_SIGNING_SECRET: str
    
    # LLM Providers (at least one required)
    OPENAI_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    
    # External Services
    CONTENT_FACTORY_URL: Optional[str] = None
    CONTENT_FACTORY_API_KEY: Optional[str] = None
    MLAI_BACKEND_URL: Optional[str] = None
    MLAI_API_KEY: Optional[str] = None
    INTERNAL_API_KEY: Optional[str] = None
    
    # Application
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    SKILLS_DIR: str = "skills"
    
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
