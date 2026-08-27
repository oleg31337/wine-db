"""Application settings. All configuration comes from the environment (docker .env).

The frontend never receives or configures backend settings.
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core ---
    app_name: str = "wine-db"
    environment: str = Field(default="production")
    web_port: int = 8080

    # Secret used to sign session JWTs. MUST be set in production.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    session_ttl_minutes: int = 60 * 12
    # Cookies are marked Secure by default: TLS is terminated by the reverse proxy.
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # --- Database ---
    # Default is a single-file SQLite database INSIDE data_dir so everything
    # (db + uploads) lives in one host folder that you can back up / bind-mount.
    # In Docker the compose file overrides this with Postgres.
    data_dir: str = "./data"
    database_url: str = "sqlite:///./data/winedb.sqlite"

    # --- Registration ---
    # Public self-registration is disabled by design: only an admin (seeded from
    # the environment below) may create accounts. These switches remain for
    # advanced self-hosting scenarios but default to closed.
    allow_open_registration: bool = False
    registration_code: str | None = None

    # --- Admin bootstrap ---
    # The single admin account is provisioned from the environment, never via a
    # signup form. ADMIN_PASSWORD_HASH is an Argon2id hash (produce one with
    # `python -m app.tools.admin_hash "<password>"`). In production the app
    # refuses to start if either is missing; in development a random password is
    # generated and logged once if unset.
    admin_username: str | None = None
    admin_password_hash: str | None = None

    # --- Uploads ---
    data_dir: str = "/data"
    max_upload_bytes: int = 8 * 1024 * 1024
    max_backup_bytes: int = 512 * 1024 * 1024

    # --- Enrichment: vision + summary models (separate providers allowed) ---
    # Each stage is an OpenAI-compatible chat-completions endpoint. This covers
    # both hosted APIs and local Ollama (Ollama exposes an OpenAI-compatible
    # shim at /v1/chat/completions, so point VISION_BASE_URL at the Ollama host,
    # e.g. http://192.168.1.222:11434). The URL is normalised to end in
    # /v1/chat/completions automatically, so you don't need the /v1 suffix.
    vision_base_url: str | None = None
    vision_api_key: str | None = None
    vision_model: str = "qwen2.5vl:latest"  # reads label photos (fast VL model)

    # Optional text-only model for the "best-effort from knowledge" step.
    # Defaults to vision_model when empty.
    text_model: str | None = None

    summary_base_url: str | None = None
    summary_api_key: str | None = None
    # Summarises raw internet search results into structured fields. The
    # vision model is heavy, so a small fast model does this job.
    summary_model: str = "nemotron-3-nano:4b"

    ai_timeout_seconds: float = 120.0

    # --- Web search enrichment ---
    web_search_enabled: bool = True
    # Which internet search backend to use for text lookups. "duckduckgo" (the
    # html endpoint, no key) is the default; "searxng" uses a self-hosted
    # instance if you provide one.
    web_search_provider: str = "duckduckgo"
    searxng_base_url: str | None = None
    web_search_timeout_seconds: float = 15.0

    # --- Security knobs ---
    rate_limit_login_per_minute: int = 8
    rate_limit_api_per_minute: int = 240
    rate_limit_ai_per_minute: int = 12
    trusted_hosts: str = "*"

    @field_validator("trusted_hosts")
    @classmethod
    def _strip_hosts(cls, v: str) -> str:
        return v.strip() or "*"

    @model_validator(mode="after")
    def _check_production_secrets(self) -> "Settings":
        """A weak signing key would let anyone forge a session cookie.

        In development a random key is generated per process, which is fine. In
        production the key must be supplied and long enough, and refusing to
        start is safer than silently accepting a guessable one.
        """
        if self.environment.strip().lower() not in {"production", "prod"}:
            return self

        placeholders = {
            "change-me",
            "changeme",
            "secret",
            "please-change",
            "your-secret-key",
            "wine-db",
        }
        key = (self.secret_key or "").strip()
        if key.lower() in placeholders:
            raise ValueError(
                "SECRET_KEY is still a placeholder. Generate one with "
                "`openssl rand -base64 48` and set it in your .env file."
            )
        if len(key) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters in production. Generate one "
                "with `openssl rand -base64 48`."
            )
        return self

    @property
    def trusted_host_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]

    @property
    def uploads_dir(self) -> str:
        return f"{self.data_dir.rstrip('/')}/uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()
