"""Application configuration.

All secrets are read from the environment (via a local .env file). Nothing here
ever logs or echoes a secret value. Settings are frozen at import time so a
missing key fails fast at startup rather than at first request.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging_setup import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

#: Origins Vite serves in development.
_DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


def _is_local_origin(url: str) -> bool:
    """True for http(s) origins on localhost or a loopback address."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}


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
    # The defaults are the models that were *measured* to work with this
    # project's key on 2026-10-22, not the newest published models.
    # `gemini-4-argon` returned 429 (quota) and `gemini-3.8-flash` returned
    # 503/504 (high demand) on that account; `gemini-3.5-flash` answered
    # in 2.9s. Override with LLM_MODEL_NOTES when the quota allows a
    # better model.
    google_api_key: str = Field(default="", repr=False)
    llm_model_notes: str = "gemini-3.5-flash"
    llm_model_notes_segments: str = "gemini-3.5-flash"

    # --- LLM: Workload B (conversational doubts) ---
    # `llama-3.3-70b-versatile` returned 404 for this account, so it is
    # not a usable default. `qwen/qwen3.8-27b` answered in 0.35s, is the
    # fastest available option, and Qwen3 is among the stronger families
    # for Hindi/Hinglish. It streams clean content with no reasoning
    # preamble, which matters because the answer is spoken aloud.
    groq_api_key: str = Field(default="", repr=False)
    llm_model_chat: str = "qwen/qwen3.8-27b"
    # Extra Groq models behind the primary chat model, tried in order when it
    # fails. Both were measured to answer on this account on 2026-11-13; the
    # older `llama-3.3-70b-versatile` default returned 404 and several newer
    # ids are not served here at all.
    llm_model_chat_fallbacks: list[str] = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    # Primary for the voice path. Probed on 2026-11-13: only
    # `gemini-3.1-flash-lite` answered (the others hit quota or 5xx/404).
    # Used only when GOOGLE_API_KEY is set; otherwise Groq starts the chain.
    llm_model_chat_google: str = "gemini-3.1-flash-lite"

    # --- Storage ---
    # These read DATA_DIR / DB_PATH / CACHE_DIR from the environment, so a
    # deployment can point the app at a mounted volume, and tests can point
    # it at a temporary directory.
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

    # --- "Is this really a lecture?" gates ---
    # The size and duration caps above stop abuse; these stop *waste*. A
    # 3-second clip or a 40-character transcript cannot produce notes or
    # flashcards, and each attempt still costs a transcribe job.
    min_audio_duration_seconds: float = 5.0
    min_lecture_words: int = 40
    min_lecture_characters: int = 200

    # --- Sessions and sign-in ---
    # The signed cookie carries only an opaque session id; the row it names
    # lives in `user_sessions`. That indirection is deliberate: signing out
    # (or revoking a stolen cookie) is a row delete that takes effect on the
    # next request, instead of whenever the cookie would have expired.
    session_secret: str = Field(default="", repr=False)
    session_cookie_name: str = "l2lp_session"
    session_max_age_seconds: int = 14 * 24 * 60 * 60
    # Secure cookies require HTTPS. Unset means derive from FRONTEND_URL, so
    # an HTTPS deployment is safe without an extra setting; an explicit value
    # overrides the derivation (useful behind a TLS-terminating proxy).
    session_cookie_secure: bool | None = None

    google_client_id: str = ""
    google_client_secret: str = Field(default="", repr=False)
    # Base URL of the single-page app. Empty means "the origin this request
    # arrived on", which is correct in production, where this process also
    # serves the built frontend. In development Vite owns :5173, so it must
    # be set: the browser has to be sent back to the dev server (and the
    # OAuth redirect registered with Google has to match it) rather than to
    # the API port.
    frontend_url: str = ""
    # Extra origins allowed to call the API from a browser, comma separated.
    allowed_origins: str = ""

    # Open mode is an explicit local-demo opt-in. Without Google credentials
    # AND this flag, data routes answer 401 instead of silently running open.
    # Credentials always win: OPEN_MODE never disables a configured sign-in.
    open_mode: bool = False
    # Quota and operator role. 0 disables the lecture quota; admin emails are
    # compared against the email from a verified Google id_token only.
    max_lectures_per_user: int = 25
    admin_emails: str = ""

    # --- Rate limits (per client address, per minute) ---
    rate_limit_write_per_minute: int = 120
    rate_limit_upload_per_minute: int = 10
    rate_limit_auth_per_minute: int = 30

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

    def has_google_oauth(self) -> bool:
        """True when Google sign-in is configured.

        The client id alone is enough for the browser to be sent to Google;
        the secret is needed one step later, at the token exchange. Both are
        checked here so a half-configured deployment fails visibly at login
        time rather than at the callback.
        """
        return bool(self.google_client_id and self.google_client_secret)

    def auth_required(self) -> bool:
        """Whether data routes must see a signed-in user.

        True unless the local demo explicitly opted into open mode with no
        Google credentials. A half-configured deployment therefore fails
        closed instead of exposing everyone's lectures.
        """
        return self.has_google_oauth() or not self.open_mode

    def is_admin(self, email: str) -> bool:
        """Whether a verified account email is an operator."""
        value = (email or "").strip().lower()
        return bool(value) and value in {
            item.strip().lower() for item in self.admin_emails.split(",") if item.strip()
        }

    def secure_session_cookies(self) -> bool:
        """Whether the session cookie carries the Secure flag.

        Derived from FRONTEND_URL because SessionMiddleware is configured
        once when the app is created, not per request. An empty URL is the
        local http:// demo and stays insecure on purpose.
        """
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return urlparse(self.frontend_url).scheme.lower() == "https"

    def frontend_base(self, request_base_url: str) -> str:
        """Where to send the browser after an action, with no trailing slash.

        ``FRONTEND_URL`` wins when set: in development Vite owns another
        port, and the origin the API saw is the Vite proxy, not the page.
        Otherwise the caller's own scheme/host/port is right, because in
        production this process serves the built frontend too.
        """
        base = (self.frontend_url or request_base_url or "").rstrip("/")
        return base

    def cors_origins(self) -> list[str]:
        """Origins allowed to call the API from a browser.

        The frontend origin always counts. The localhost dev origins are
        added only while the app itself is being served from localhost, so
        pointing FRONTEND_URL at a real domain never silently opens the API
        to anyone running a copy of the dev server.
        """
        origins: set[str] = set()
        if self.frontend_url:
            origins.add(self.frontend_url.rstrip("/"))
        for extra in self.allowed_origins.split(","):
            origin = extra.strip().rstrip("/")
            if origin:
                origins.add(origin)
        local_dev = not self.frontend_url or _is_local_origin(self.frontend_url)
        if local_dev:
            origins |= _DEV_ORIGINS
        return sorted(origins)

    def session_secret_value(self) -> str:
        """The signing key for the session cookie.

        An explicit SESSION_SECRET is always used when present. Otherwise a
        random key is generated once and kept in the data directory, so
        sessions survive a restart instead of signing everyone out on every
        deploy or reload.
        """
        if self.session_secret:
            return self.session_secret
        path = self.data_dir / ".session_secret"
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("session secret unreadable; generating an ephemeral one")
            return secrets.token_urlsafe(48)
        value = secrets.token_urlsafe(48)
        try:
            # O_EXCL: if two processes race, the loser reads the winner's key
            # rather than overwriting it and invalidating live sessions.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        return value

    def estimated_credits(self, audio_seconds: float) -> float:
        """Conservative credit estimate for a transcription job."""
        return (audio_seconds / 3600.0) * self.estimated_credits_per_audio_hour


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    """Drop the cached settings so the next call re-reads the environment.

    Tests use this after changing an environment variable.
    """
    get_settings.cache_clear()
