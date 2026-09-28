"""Central configuration. Every secret is read from the environment (or a local,
git-ignored .env file); nothing sensitive is ever hardcoded here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
RUNTIME_DIR = DATA_DIR / "runtime"
ENV_FILE = ROOT_DIR / ".env"


def _load_env_file() -> None:
    """Merge .env into the process environment.

    A real environment variable wins, but an *empty* one does not shadow a value
    from .env (an exported `GROQ_API_KEY=` would otherwise silently disable the key)."""
    if not ENV_FILE.exists():
        return
    for key, value in dotenv_values(ENV_FILE).items():
        if value is not None and not (os.environ.get(key) or "").strip():
            os.environ[key] = value


_load_env_file()

_PLACEHOLDER_PREFIX = "your_"


def _secret(name: str) -> str | None:
    """Return a secret, treating empty strings and .env.example placeholders as unset."""
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith(_PLACEHOLDER_PREFIX):
        return None
    return value


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Hindsight
    hindsight_api_key: str | None = field(default_factory=lambda: _secret("HINDSIGHT_API_KEY"))
    hindsight_base_url: str = field(
        default_factory=lambda: (os.getenv("HINDSIGHT_BASE_URL") or "https://api.hindsight.vectorize.io").rstrip("/")
    )
    hindsight_bank_id: str = field(default_factory=lambda: os.getenv("HINDSIGHT_BANK_ID") or "field-service-copilot")
    hindsight_per_fleet_banks: bool = field(default_factory=lambda: _bool("HINDSIGHT_PER_FLEET_BANKS"))
    # Interactive budget for recall. Past it the agent answers from local memory while the
    # Hindsight call finishes in the background and warms the cache.
    hindsight_timeout_s: float = field(default_factory=lambda: _float("HINDSIGHT_TIMEOUT_S", 1.5))
    # Retain is a write that happens after the answer is on screen, so it gets its own, longer
    # budget (per attempt, with retries) instead of the interactive recall budget.
    hindsight_retain_timeout_s: float = field(default_factory=lambda: _float("HINDSIGHT_RETAIN_TIMEOUT_S", 15.0))
    # Reflect is an LLM synthesis on the Hindsight side (~15 s); it runs in the background and is cached.
    hindsight_reflect_timeout_s: float = field(default_factory=lambda: _float("HINDSIGHT_REFLECT_TIMEOUT_S", 45.0))
    hindsight_prewarm: bool = field(default_factory=lambda: _bool("HINDSIGHT_PREWARM", True))

    # Groq
    groq_api_key: str | None = field(default_factory=lambda: _secret("GROQ_API_KEY"))
    groq_base_url: str = field(default_factory=lambda: os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1")
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b")
    # Optional separate model for the baseline agent (rate limits are per model on Groq).
    groq_baseline_model: str = field(default_factory=lambda: os.getenv("GROQ_BASELINE_MODEL") or "")
    groq_whisper_model: str = field(default_factory=lambda: os.getenv("GROQ_WHISPER_MODEL") or "whisper-large-v3-turbo")
    groq_reasoning_effort: str = field(default_factory=lambda: os.getenv("GROQ_REASONING_EFFORT") or "low")
    groq_max_retries: int = field(default_factory=lambda: _int("GROQ_MAX_RETRIES", 4))

    # App
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT") or "development")
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in (os.getenv("CORS_ORIGINS") or "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        )
    )
    # Optional shared access token. When set, every /api route except /api/health requires it,
    # which keeps a hosted demo from burning the Groq/Hindsight quota.
    app_access_token: str | None = field(default_factory=lambda: _secret("APP_ACCESS_TOKEN"))
    # Per-client limit for the expensive endpoints (diagnose, transcribe, bulletins), per minute.
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 20))

    @property
    def hindsight_enabled(self) -> bool:
        return self.hindsight_api_key is not None

    @property
    def groq_enabled(self) -> bool:
        return self.groq_api_key is not None

    @property
    def env_file_present(self) -> bool:
        return ENV_FILE.exists()

    def setup_hints(self) -> list[str]:
        hints = []
        if not self.env_file_present and not (self.groq_enabled and self.hindsight_enabled):
            hints.append(f"No .env file at {ENV_FILE}. Run `cp .env.example .env` and add your keys.")
        if not self.groq_enabled:
            hints.append("GROQ_API_KEY is not set, so answers use the deterministic brief.")
        if not self.hindsight_enabled:
            hints.append("HINDSIGHT_API_KEY is not set, so memory runs on the local fallback store.")
        return hints

    def public_summary(self) -> dict:
        """Configuration safe to expose to the UI: flags only, never key material."""
        return {
            "hindsight_configured": self.hindsight_enabled,
            "hindsight_base_url": self.hindsight_base_url,
            "bank_id": self.hindsight_bank_id,
            "per_fleet_banks": self.hindsight_per_fleet_banks,
            "hindsight_timeout_s": self.hindsight_timeout_s,
            "hindsight_retain_timeout_s": self.hindsight_retain_timeout_s,
            "groq_configured": self.groq_enabled,
            "groq_model": self.groq_model,
            "groq_baseline_model": self.groq_baseline_model or self.groq_model,
            "voice_enabled": self.groq_enabled,
            "access_token_required": self.app_access_token is not None,
            "environment": self.environment,
            "setup_hints": self.setup_hints(),
        }


settings = Settings()
