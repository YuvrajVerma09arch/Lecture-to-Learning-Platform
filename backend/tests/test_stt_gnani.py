"""Tests for the tuned Gnani STT.

These are MOCKED: the WebSocket is never opened. They check the parts
of the subclass that are ours -- the VAD headers it adds and the
silence it injects -- not the server's behaviour, which is measured by
``scripts/probe_tuned_stt.py`` against the live API.
"""

from __future__ import annotations

import time

import pytest
from livekit import rtc

from agent.stt_gnani import (
    DEFAULT_SILENCE_FILL_SECONDS,
    DEFAULT_SILENCE_TIMEOUT_SECONDS,
    DEFAULT_VAD_HEADERS,
    TunedGnaniSTT,
    with_vad_headers,
)


@pytest.fixture
def tuned_stt():
    return TunedGnaniSTT(language="hi-IN", api_key="test-key")


class TestVADHeaders:
    def test_every_vad_header_the_server_accepts_is_sent(self):
        merged = with_vad_headers(
            {"x-api-key-id": "k", "lang_code": "hi-IN"}, DEFAULT_VAD_HEADERS
        )
        for header in (
            "x-vad-threshold",
            "x-min-silence-ms",
            "x-min-speech-ms",
            "x-min-segment-ms",
        ):
            assert header in merged, f"{header} was not sent to the server"

    def test_the_plugin_headers_are_preserved(self):
        merged = with_vad_headers(
            {"x-api-key-id": "k", "lang_code": "hi-IN"}, DEFAULT_VAD_HEADERS
        )
        assert merged["x-api-key-id"] == "k"
        assert merged["lang_code"] == "hi-IN"

    def test_no_extra_headers_leaves_the_original_alone(self):
        original = {"x-api-key-id": "k"}
        assert with_vad_headers(original, None) == original
        assert with_vad_headers(original, {}) == original

    def test_the_plugin_wins_on_a_key_clash(self):
        merged = with_vad_headers(
            {"x-min-silence-ms": "999"}, {"x-min-silence-ms": "500"}
        )
        assert merged["x-min-silence-ms"] == "999"

    def test_the_defaults_match_the_measured_winner(self, tuned_stt):
        """700 ms of server silence and a 0.8 s injection timeout was the
        only configuration that did not split a 0.6 s mid-sentence
        pause."""
        assert tuned_stt._vad_headers["x-min-silence-ms"] == "700"
        assert tuned_stt._silence_timeout == DEFAULT_SILENCE_TIMEOUT_SECONDS
        assert tuned_stt._silence_fill == DEFAULT_SILENCE_FILL_SECONDS

    def test_custom_headers_override_the_defaults(self):
        stt = TunedGnaniSTT(
            language="hi-IN",
            api_key="k",
            vad_headers={"x-min-silence-ms": "900"},
        )
        assert stt._vad_headers["x-min-silence-ms"] == "900"
        # The rest of the defaults are kept.
        assert stt._vad_headers["x-vad-threshold"] == "0.7"

    def test_a_missing_api_key_is_rejected(self, monkeypatch):
        from livekit.plugins.gnani import STT as OfficialSTT

        monkeypatch.delenv("GNANI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="Gnani API key is required"):
            OfficialSTT(language="hi-IN", api_key=None)


class TestSilenceInjection:
    def _frame(self, samples: int = 512) -> rtc.AudioFrame:
        return rtc.AudioFrame(
            data=b"\x00" * (samples * 2),
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=samples,
        )

    async def test_silence_is_injected_once_the_input_goes_quiet(self, tuned_stt):
        stream = tuned_stt.stream()
        try:
            # The input has been quiet for longer than the timeout.
            stream._last_audio_at = 0.0
            stream._silence_sent = False

            await stream._maybe_send_silence()
            # The second call is a no-op: the segment is already closing.
            await stream._maybe_send_silence()
            assert stream._silence_sent is True
        finally:
            await stream.aclose()

    async def test_no_silence_while_the_input_is_live(self, tuned_stt):
        stream = tuned_stt.stream()
        try:
            stream._last_audio_at = time.monotonic()
            stream._silence_sent = False
            await stream._maybe_send_silence()
            # Nothing should have been queued.
            assert stream._silence_sent is False
        finally:
            await stream.aclose()

    async def test_pushing_audio_resets_the_silence_timer(self, tuned_stt):
        stream = tuned_stt.stream()
        try:
            stream._last_audio_at = 0.0
            stream._silence_sent = True
            stream.push_frame(self._frame())
            # New audio means the student is speaking again.
            assert stream._silence_sent is False
            assert time.monotonic() - stream._last_audio_at < 1.0
        finally:
            await stream.aclose()
