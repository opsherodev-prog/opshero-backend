from functools import lru_cache
from typing import Union
import json
from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_env: str = "development"
    app_name: str = "OpsHero"
    app_version: str = "0.1.0"
    debug: bool = False
    allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: Union[str, list]) -> list[str]:
        """Parse ALLOWED_ORIGINS from JSON string or list."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                # Fallback: split by comma
                return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @computed_field
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    # ── MongoDB ───────────────────────────────────────────────────────────────
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db: str = "opshero"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Auth — Users ──────────────────────────────────────────────────────────
    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 168        # 7 days
    jwt_refresh_expire_days: int = 90  # 3 months

    # ── Auth — Admin ──────────────────────────────────────────────────────────
    admin_jwt_secret: str = Field(..., min_length=32)
    admin_jwt_expire_hours: int = 8
    admin_totp_encryption_key: str = Field(..., min_length=32)

    # ── GitHub OAuth ──────────────────────────────────────────────────────────
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "https://opshero.me/auth/callback"

    # ── GitHub Webhooks & Patterns repo ───────────────────────────────────────
    # Secret configured in GitHub repo → Settings → Webhooks → Secret
    github_webhook_secret: str = ""
    # PAT with read access to the patterns repo (falls back to github_client_secret)
    github_patterns_token: str = ""

    # ── Groq LLM ──────────────────────────────────────────────────────────────
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # ── LLM Runtime Config ────────────────────────────────────────────────────
    llm_enabled: bool = True
    llm_confidence_threshold: float = 0.65
    llm_primary_model: str = "llama-3.3-70b-versatile"
    llm_fast_model: str = "llama-3.1-8b-instant"
    llm_long_context_model: str = "mixtral-8x7b-32768"
    llm_short_log_threshold: int = 500
    llm_long_log_threshold: int = 4000
    llm_daily_budget_usd: float = 10.0
    llm_monthly_budget_usd: float = 200.0
    llm_alert_threshold_pct: float = 0.80

    # Free tier doesn't use LLM by default
    llm_enabled_for_free: bool = False
    llm_enabled_for_pro: bool = True
    llm_enabled_for_team: bool = True

    # LLM calls per day per tier
    llm_calls_per_day_pro: int = 50
    llm_calls_per_day_team: int = 200

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_pro_price_id: str = ""
    stripe_team_price_id: str = ""

    # ── Email (Gmail SMTP) ────────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""       # Google App Password (no spaces)
    email_from: str = "OpsHero <opshero.dev@gmail.com>"
    email_enabled: bool = True

    # ── Patterns ──────────────────────────────────────────────────────────────
    patterns_dir: str = "../shared/patterns"

    # ── Auto-learning system ───────────────────────────────────────────────────
    # Enable the background learning loop (pattern generation + rerank)
    learning_enabled: bool = True
    # How often the learning loop runs (seconds) — default 1 hour
    learning_job_interval_seconds: int = 3600
    # Minimum times an unknown error must be seen before auto-generating a pattern
    learning_auto_promote_min_sightings: int = 10
    # Minimum LLM confidence for a candidate to be considered for auto-promotion
    learning_auto_promote_min_confidence: float = 0.80
    # Max candidates processed per learning cycle (avoids long-running bursts)
    learning_auto_promote_batch_size: int = 20
    # Min feedback votes before reranking solutions
    learning_rerank_min_feedback: int = 5

    # ── OpenTelemetry ─────────────────────────────────────────────────────────
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "opshero-backend"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Convenience alias
settings = get_settings()
