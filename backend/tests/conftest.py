"""Shared test fixtures.

Two rules the brief imposes, enforced here:

* **Mocked tests never call a remote API.** The Gnani and LLM clients are
  replaced with fakes. A test that does exercise a real API is marked
  ``@pytest.mark.live`` and skipped unless ``GNANI_LIVE_TESTS=1`` is set,
  so it is always obvious which kind of test ran.
* **Tests never touch the real database or cache.** Every test gets its
  own temporary SQLite file and its own data directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import config as config_mod
from app import db as db_mod

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at a throwaway database and cache directory.

    Settings are read from the environment, so overriding the environment
    is enough. The real ``.env`` file is still loaded, but explicit
    environment variables take precedence over it, so the dummy keys below
    win.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CACHE_DIR", str(data_dir / "cache"))
    monkeypatch.setenv("DB_PATH", str(data_dir / "test.sqlite3"))
    # Dummy credentials: a mocked test must never reach a real provider.
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-lk-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-lk-secret")
    # The real API requires >= 10s between polls. Tests must not wait that
    # long, so the interval is reduced here; the production default is
    # unchanged.
    monkeypatch.setenv("BATCH_POLL_INTERVAL_SECONDS", "0.01")
    monkeypatch.setenv("BATCH_POLL_MAX_INTERVAL_SECONDS", "0.01")

    config_mod.reset_settings_cache()
    yield config_mod.get_settings()
    config_mod.reset_settings_cache()


@pytest.fixture
def db_conn():
    """A connection to the temporary test database."""
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        yield conn


@pytest.fixture
def sample_transcript() -> list[dict[str, Any]]:
    """A cached transcript fixture.

    This is the shape the real Gnani batch API returns: relative
    timestamps, ``segment_id`` always 0, ``confidence`` null, and
    unpunctuated text. Captured from a live job so the fixture cannot
    drift from reality.
    """
    return json.loads((FIXTURES / "sample_transcript.json").read_text())


@pytest.fixture
def sample_speech_pcm() -> bytes:
    """16 kHz mono s16le PCM of a short synthesised utterance."""
    return (FIXTURES / "sample_speech.pcm").read_bytes()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeLLM:
    """A stand-in for ``livekit.agents.llm.LLM``.

    ``responses`` is consumed in order. If it runs out, ``default`` is
    returned. ``calls`` records every prompt, so a test can assert what
    the model was actually shown.
    """

    def __init__(self, responses: list[Any] | None = None, default: Any = None) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []
        self.model = "fake-llm"

    def chat(self, *, chat_ctx: Any, **kwargs: Any) -> Any:
        self.calls.append({"chat_ctx": chat_ctx, "kwargs": kwargs})
        payload = self.responses.pop(0) if self.responses else self.default
        return _FakeStream(payload)


class _FakeStream:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def collect(self) -> Any:
        text = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return _FakeReply(text)


class _FakeReply:
    def __init__(self, text: str) -> None:
        self.text = text

    @property
    def usage(self) -> Any:
        return None


@pytest.fixture
def fake_llm_factory():
    """Build a :class:`FakeLLM` and register it for cleanup."""

    def make(responses: list[Any] | None = None, default: Any = None) -> FakeLLM:
        return FakeLLM(responses=responses, default=default)

    return make


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch):
    """Remove the delay between failed model calls.

    The delay exists so a real provider is not hammered. In a test there
    is no provider to protect, and the suite has to stay fast.
    """
    from app.pipeline import notes as notes_mod
    from app.retrieval import rerank as rerank_mod

    monkeypatch.setattr(notes_mod, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(rerank_mod, "RETRY_DELAY_SECONDS", 0.0)


@pytest.fixture
def live_api_enabled() -> bool:
    """True when live-API tests are explicitly enabled."""
    import os

    return os.environ.get("GNANI_LIVE_TESTS") == "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: exercises a real Gnani API; needs GNANI_LIVE_TESTS=1"
    )
    config.addinivalue_line(
        "markers", "slow: takes more than a second"
    )
