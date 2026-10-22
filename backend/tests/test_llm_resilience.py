"""Provider-fallback tests.

These cover the failure that actually bit during the end-to-end run:
Google's free tier allows 20 requests per model per day, and its quota
error is a 429 -- the same status code as a per-minute rate limit.
Retrying a quota error burns the rest of the daily allowance and still
fails, so the pipeline must recognise it and switch provider instead.
"""

from __future__ import annotations

import pytest

from app.llm import factory
from app.llm.factory import ErrorKind, LLMError, classify_error
from app.pipeline import notes as notes_mod
from app.pipeline.segment import Segment

from .conftest import FakeLLM


def settings_model() -> str:
    from app.config import get_settings

    return get_settings().llm_model_chat


class TestErrorClassification:
    def test_a_daily_quota_error_is_not_retryable(self):
        exc = classify_error(
            Exception(
                "ClientError: 429 Too Many Requests. 'Quota exceeded for metric: "
                "generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash. "
                "Please retry in 20h38m.'"
            )
        )
        assert exc.kind == ErrorKind.QUOTA
        assert exc.is_quota is True
        assert exc.retryable is False

    def test_a_per_minute_rate_limit_is_retryable(self):
        exc = classify_error(Exception("429 Too Many Requests, rate limit reached"))
        assert exc.kind == ErrorKind.RATE_LIMIT
        assert exc.retryable is True

    def test_a_server_error_is_retryable(self):
        exc = classify_error(Exception("503 UNAVAILABLE: high demand"))
        assert exc.kind == ErrorKind.SERVER
        assert exc.retryable is True

    def test_an_auth_error_is_not_retryable(self):
        exc = classify_error(Exception("401 invalid_api_key"))
        assert exc.kind == ErrorKind.CLIENT
        assert exc.retryable is False

    def test_a_shape_error_is_retryable(self):
        exc = classify_error(Exception("Model response was not valid JSON: nope"))
        assert exc.kind == ErrorKind.SHAPE
        assert exc.retryable is True

    def test_an_unknown_error_is_not_retried_by_default(self):
        exc = classify_error(Exception("something odd happened"))
        assert exc.kind == ErrorKind.UNKNOWN
        assert exc.retryable is False


class _QuotaExhaustedLLM(FakeLLM):
    """A provider whose quota is spent."""

    def chat(self, *, chat_ctx, **kwargs):
        self.calls.append({"chat_ctx": chat_ctx, "kwargs": kwargs})
        raise LLMError("429 quota exhausted", kind=ErrorKind.QUOTA)


class TestProviderFallback:
    async def test_a_quota_error_switches_to_the_fallback_provider(self):
        primary = _QuotaExhaustedLLM()
        fallback = FakeLLM(
            default={
                "heading": "Networks",
                "explanation": "An explanation long enough to be useful.",
                "key_terms": [],
                "equations": [],
                "uncertainty_note": "",
            }
        )
        segments = [
            Segment(index=i, start_seconds=i * 60, end_seconds=i * 60 + 45,
                    text=f"Segment {i} text.")
            for i in range(3)
        ]

        produced = await notes_mod.generate_segment_notes(
            segments, chain=[primary, fallback]
        )

        # Every segment got a note, produced on the fallback.
        assert len(produced) == 3
        assert primary.calls, "the primary should have been tried first"
        assert fallback.calls, "the fallback should have been used"

    async def test_a_quota_error_is_not_retried_on_the_same_provider(self):
        """Retrying a quota error burns the remaining daily allowance."""
        primary = _QuotaExhaustedLLM()
        segment = Segment(
            index=0, start_seconds=0, end_seconds=45, text="Segment 0 text."
        )

        with pytest.raises(LLMError) as exc:
            await notes_mod.generate_segment_note(primary, segment)

        assert exc.value.is_quota
        # Exactly one attempt: the quota error propagates immediately.
        assert len(primary.calls) == 1

    async def test_a_rate_limit_is_retried_on_the_same_provider(self):
        class FlakyLLM(FakeLLM):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def chat(self, *, chat_ctx, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise LLMError("429 rate limit", kind=ErrorKind.RATE_LIMIT)
                return super().chat(chat_ctx=chat_ctx, **kwargs)

        llm = FlakyLLM()
        llm.responses = [
            {
                "heading": "Networks",
                "explanation": "An explanation long enough to be useful.",
                "key_terms": [],
                "equations": [],
                "uncertainty_note": "",
            }
        ]
        segment = Segment(
            index=0, start_seconds=0, end_seconds=45, text="Segment 0 text."
        )
        note = await notes_mod.generate_segment_note(llm, segment)
        assert note.heading == "Networks"
        assert llm.attempts == 2


class TestChainConstruction:
    def _settings(self, monkeypatch, **keys):
        """Build settings with only the given providers configured.

        The conftest sets dummy provider keys in the environment, so the
        keys under test have to be removed explicitly.
        """
        from app import config as config_mod

        for name in ("GOOGLE_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(
            factory,
            "get_settings",
            lambda: config_mod.Settings(_env_file=None, **keys),
        )

    def test_the_chain_prefers_google_then_groq(self, monkeypatch):
        self._settings(monkeypatch, google_api_key="k", groq_api_key="k")
        chain = factory.notes_llm_chain(segment=True)
        assert len(chain) == 2
        # The plugins report different `provider` strings, so identify them
        # by the model they were built with.
        assert chain[0].model.startswith("gemini")
        assert chain[1].model == settings_model()

    def test_the_chain_works_with_one_provider(self, monkeypatch):
        self._settings(monkeypatch, google_api_key="k")
        chain = factory.notes_llm_chain(segment=True)
        assert len(chain) == 1
        assert chain[0].model.startswith("gemini")

    def test_groq_only(self, monkeypatch):
        self._settings(monkeypatch, groq_api_key="k")
        chain = factory.notes_llm_chain(segment=True)
        assert len(chain) == 1
        assert "/" in chain[0].model, "the groq model id contains a provider prefix"

    def test_no_provider_raises(self, monkeypatch):
        self._settings(monkeypatch)
        with pytest.raises(LLMError, match="No LLM provider"):
            factory.notes_llm_chain()
