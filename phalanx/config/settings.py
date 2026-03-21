"""
Application settings — loaded from environment variables.
Single source of truth for all configuration.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    forge_env: str = "development"
    forge_secret_key: str = "change-me"
    log_level: str = "DEBUG"
    log_format: str = "pretty"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://forge:forge_dev_password@postgres:5432/forge"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "forge"
    postgres_password: str = "forge_dev_password"
    postgres_db: str = "forge"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    # ── AI — Anthropic (Claude) ───────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_model_default: str = "claude-opus-4-6"
    anthropic_model_fast: str = "claude-haiku-4-5-20251001"
    anthropic_max_tokens_default: int = 8096
    anthropic_max_retries: int = 3

    # ── AI — OpenAI ───────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", description="OpenAI API key")
    openai_model_default: str = "gpt-4o"

    # ── AI — Grok (xAI) ───────────────────────────────────────────────────────
    grok_api_key: str = Field(default="", description="xAI Grok API key")
    grok_model_default: str = "grok-beta"

    # Token / cost limits
    forge_max_tokens_per_run: int = 500_000
    forge_max_daily_spend_usd: float = 100.0
    forge_cost_alert_percent: int = 80

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_signing_secret: str = ""
    slack_socket_mode: bool = True

    # ── GitHub ────────────────────────────────────────────────────────────────
    github_token: str = ""
    github_webhook_secret: str = ""

    # ── AWS S3 ───────────────────────────────────────────────────────────────
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    forge_s3_bucket: str = "forge-artifacts-dev"

    # ── Git ops ───────────────────────────────────────────────────────────────
    git_workspace: str = "/tmp/forge-repos"
    git_author_name: str = "FORGE"
    git_author_email: str = "forge-bot@acme.com"

    # ── Feature flags ─────────────────────────────────────────────────────────
    forge_enable_pgvector: bool = True
    forge_enable_discord: bool = False
    forge_enable_skill_drills: bool = False
    forge_enable_daily_digest: bool = False
    forge_enable_deploy_verify: bool = False
    phalanx_enable_dag_orchestration: bool = False

    # ── API ───────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # Service-to-service API key. Empty = auth disabled (dev only).
    # In production, set FORGE_API_KEY to a strong random value.
    forge_api_key: str = ""
    # Comma-separated CORS origins; empty = no browser access (API-only).
    # Dev override: "*"  Production: leave empty or set explicit domains.
    forge_cors_origins: str = ""

    @property
    def is_production(self) -> bool:
        return self.forge_env == "production"

    @property
    def is_development(self) -> bool:
        return self.forge_env == "development"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Call this everywhere instead of Settings()."""
    return Settings()
