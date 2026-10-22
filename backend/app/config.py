"""Application configuration.

All secrets are read from the environment (via a local .env file). Nothing here
ever logs or echoes a secret value. Settings are frozen at import time so a
missing key fails fast at startup rather than at first request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Environment-driven settings.

    The two LLM workloads are configured independently, as required: notes
    generation favours long context and factual fidelity, conversational
    doubts favour time-to-first-token and streaming.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Gnani ---
    gnani_api_key: str = Field(default="", repr=False)
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_stt_model: str = "gnani-prisma-v2.5"

    # --- LiveKit Cloud ---
    livekit_url: str = ""
    livekit_api_key: str = Field(default="", repr=False)
    livekit_api_secret: str = Field(default="", repr=False)

    # --- LLM: Workload A (notes) ---
    google_api_key: str = Field(default="", repr=False)
    llm_model_notes: str = "gemini-4-argon"
    llm_model_notes_segments: str = "gemini-3.8-flash"

    # --- LLM: Workload B (conversational doubts) ---
    groq_api_key: str = Field(default="", repr=False)
    llm_model_chat: str = "llama-3.3-70b-versatile"

    # --- Storage ---
    data_dir: Path = DATA_DIR
    db_path: Path = DATA_DIR / "app.sqlite3"
    cache_dir: Path = DATA_DIR / "cache"

    # --- Upload limits ---
    # Gnani's batch API caps a single file at 10 MB. We transcode and chunk
    # below this, with margin, so a chunk never lands on the boundary.
    gnani_max_file_bytes: int = 10 * 1024 * 1024
    gnani_chunk_target_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 500 * 1024 * 1024
    max_audio_duration_seconds: int = 6 * 60 * 60

    # --- Batch STT polling ---
    # Documented minimum poll interval is 10s; 30s is the documented
    # preference for larger jobs. We honour the floor and back off on 429.
    batch_poll_interval_seconds: float = 10.0
    batch_poll_max_interval_seconds: float = 30.0
    batch_job_timeout_seconds: int = 60 * 60

    # --- Credit safety ---
    # Refuse to start a new batch job if estimated credits for the day would
    # exceed this. Gnani publishes no rate card, so the estimate is a
    # conservative planning figure, not a quote.
    daily_credit_ceiling: float = 300.0
    estimated_credits_per_audio_hour: float = 27.0

    # --- Request timeouts (seconds) ---
    http_connect_timeout: float = 15.0
    http_read_timeout: float = 120.0
    gnani_poll_timeout: float = 60.0

    @field_validator("data_dir", "cache_dir", "db_path", mode="before")
    @classmethod
    def _expand(cls, v: str | Path) -> Path:
        p = Path(v)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def has_gnani(self) -> bool:
        return bool(self.gnani_api_key)

    def has_google(self) -> bool:
        return bool(self.google_api_key)

    def has_groq(self) -> bool:
        return bool(self.groq_api_key)

    def has_livekit(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)

    def estimated_credits(self, audio_seconds: float) -> float:
        """Conservative credit estimate for a transcription job."""
        return (audio_seconds / 3600.0) * self.estimated_credits_per_audio_hour


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    """Used by tests that patch the environment."""
    get_settings.cache_clear()


# Keep a module-level instance for convenience; it is lazily created on first
# access so importing the package never touches the filesystem.
settings = get_settings()
