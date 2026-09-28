"""Central configuration. Every secret is read from the environment (or a local,
git-ignored .env file); nothing sensitive is ever hardcoded here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
RUNTIME_DIR = DATA_DIR / "runtime"

load_dotenv(ROOT_DIR / ".env", override=False)

_PLACEHOLDER_PREFIX = "your_"


def _secret(name: str) -> str | None:
    """Return a secret, treating empty strings and .env.example placeholders as unset."""
    value = (os.getenv(name) or "").strip()
    if not value or value.startswith(_PLACEHOLDER_PREFIX):
        return None
    return value


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Hindsight
    hindsight_api_key: str | None = field(default_factory=lambda: _secret("HINDSIGHT_API_KEY"))
    hindsight_base_url: str = field(
        default_factory=lambda: os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io").rstrip("/")
    )
    hindsight_bank_id: str = field(default_factory=lambda: os.getenv("HINDSIGHT_BANK_ID", "field-service-copilot"))
    hindsight_per_fleet_banks: bool = field(default_factory=lambda: _bool("HINDSIGHT_PER_FLEET_BANKS"))
    hindsight_timeout_s: float = field(default_factory=lambda: _float("HINDSIGHT_TIMEOUT_S", 1.5))
    hindsight_reflect_timeout_s: float = field(default_factory=lambda: _float("HINDSIGHT_REFLECT_TIMEOUT_S", 12.0))

    # Groq
    groq_api_key: str | None = field(default_factory=lambda: _secret("GROQ_API_KEY"))
    groq_base_url: str = field(default_factory=lambda: os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"))
    groq_reasoning_effort: str = field(default_factory=lambda: os.getenv("GROQ_REASONING_EFFORT", "medium"))
    groq_max_retries: int = field(default_factory=lambda: _int("GROQ_MAX_RETRIES", 4))

    # App
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        )
    )

    @property
    def hindsight_enabled(self) -> bool:
        return self.hindsight_api_key is not None

    @property
    def groq_enabled(self) -> bool:
        return self.groq_api_key is not None

    def public_summary(self) -> dict:
        """Configuration safe to expose to the UI: flags only, never key material."""
        return {
            "hindsight_configured": self.hindsight_enabled,
            "hindsight_base_url": self.hindsight_base_url,
            "bank_id": self.hindsight_bank_id,
            "per_fleet_banks": self.hindsight_per_fleet_banks,
            "hindsight_timeout_s": self.hindsight_timeout_s,
            "groq_configured": self.groq_enabled,
            "groq_model": self.groq_model,
            "environment": self.environment,
        }


settings = Settings()
