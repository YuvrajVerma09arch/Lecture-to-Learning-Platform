"""Gnani streaming STT, tuned for turn-taking.

This is a thin extension of the official ``livekit-plugins-gnani`` plugin,
not a reimplementation. Two things are added, both measured against the
live API (see ``docs/live-api-findings.md`` and
``scripts/probe_tuned_stt.py``):

1. **The VAD-tuning headers.** Gnani's server does its own voice
   activity detection and closes a segment after ``x-min-silence-ms`` of
   silence. The plugin does not expose those headers, so the server's
   endpointing cannot be tuned.

2. **Trailing silence.** The server closes a segment only when it *hears*
   silence. Measured: a stream that stops sending audio without sending
   silence frames produces **no transcript at all** -- the segment stays
   open. The plugin forwards whatever LiveKit pushes, and LiveKit only
   pushes silence when it flushes the STT at turn-commit time, which is
   after the end-of-turn decision. Injecting silence as soon as the
   input goes quiet gets the transcript ready sooner.

A note on turn control, because the brief asks for it explicitly:
LiveKit's own VAD drives the turn lifecycle, and the turn detector is
audio-based, so it does not need partial transcripts. The Gnani server
does send ``speech_start``, but it never sends ``speech_end`` -- so the
official plugin emits ``START_OF_SPEECH`` and never ``END_OF_SPEECH``.
That is **not** a blocker: LiveKit's VAD supplies the end-of-speech
boundary independently. The subclass therefore does not synthesise
speech events from the STT. It tunes the server's endpointing and
removes the wait.

How the headers get added: the installed plugin builds its WebSocket
headers inline in ``SpeechStream._run``, so there is no method to
override. Rather than copy that method -- which would silently drift
when the plugin is upgraded -- the ``websockets.connect`` call is
wrapped once, and the extra headers are carried in a
:class:`contextvars.ContextVar`. Each STT stream's task inherits the
context it was created in, so two concurrent sessions send different
headers without interfering.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from dataclasses import replace
from typing import Any

from livekit import rtc
from livekit.agents import stt
from livekit.agents.stt import RecognizeStream
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given
from livekit.plugins import gnani

from app.logging_setup import get_logger

log = get_logger(__name__)

#: How long the input must be quiet before we assume the student has
#: stopped speaking and send silence so the server closes the segment.
#:
#: Measured (see ``scripts/probe_tuned_stt.py``): this and the server's
#: ``x-min-silence-ms`` must *both* exceed the longest pause we want to
#: tolerate, or the segment splits in the middle of an utterance. At
#: 0.8 s and 700 ms, a 0.6 s mid-sentence pause produced one
#: transcript; at 0.5 s and 500 ms it split the same pause into two.
#: The cost is a later transcript: about 2.2 s after the student stops
#: speaking, against 1.3 s for the faster settings that split.
DEFAULT_SILENCE_TIMEOUT_SECONDS = 0.80

#: How much silence to send once the student has stopped. It has to
#: exceed the server's ``x-min-silence-ms`` for the segment to close.
DEFAULT_SILENCE_FILL_SECONDS = 0.80

#: The headers Gnani's server accepts for its voice activity detection.
#: The official plugin sends none of them.
#:
#: Only ``x-min-silence-ms`` is changed. The other three are left at
#: the server's own defaults: there is no measured reason to move them,
#: and a higher ``x-min-speech-ms`` risks clipping a short syllable.
DEFAULT_VAD_HEADERS: dict[str, str] = {
    "x-vad-threshold": "0.7",
    "x-min-silence-ms": "700",
    "x-min-speech-ms": "100",
    "x-min-segment-ms": "120",
}

#: The extra headers for the STT stream currently being opened. A
#: context variable rather than an instance attribute, because the
#: connect call happens inside a task the plugin created, and a task
#: inherits the context it was created in.
_VAD_HEADERS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "gnani_vad_headers", default=None
)

_PATCH_INSTALLED = False


def with_vad_headers(
    headers: dict[str, str], extra: dict[str, str] | None
) -> dict[str, str]:
    """Merge the VAD headers into the headers being sent.

    The plugin's own headers win on a key clash, because they carry
    the API key and the negotiated language.
    """
    if not extra:
        return dict(headers)
    merged = dict(headers)
    merged.update({k: v for k, v in extra.items() if k not in merged})
    return merged


def _install_connect_patch() -> None:
    """Wrap ``websockets.connect`` so it can add the VAD headers.

    The plugin builds its headers inline, so there is nothing to
    override. Wrapping the connect call keeps this a thin extension
    rather than a copy of the plugin's ``_run``, which would drift on
    every plugin upgrade.
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return

    import websockets

    original = websockets.connect

    def connect_with_vad_headers(*args: Any, **kwargs: Any) -> Any:
        extra = _VAD_HEADERS.get()
        if extra:
            # ``websockets`` renamed the keyword in v13.
            key = (
                "additional_headers"
                if "additional_headers" in kwargs
                else "extra_headers"
            )
            kwargs[key] = with_vad_headers(dict(kwargs.get(key) or {}), extra)
        return original(*args, **kwargs)

    websockets.connect = connect_with_vad_headers  # type: ignore[assignment]
    _PATCH_INSTALLED = True
    log.debug("wrapped websockets.connect for Gnani VAD headers")


class TunedSpeechStream(gnani.SpeechStream):
    """The plugin's stream, with prompt silence injection."""

    def __init__(
        self,
        *,
        stt: stt.STT,
        opts: Any,
        conn_options: Any,
        silence_timeout: float = DEFAULT_SILENCE_TIMEOUT_SECONDS,
        silence_fill: float = DEFAULT_SILENCE_FILL_SECONDS,
    ) -> None:
        super().__init__(stt=stt, opts=opts, conn_options=conn_options)
        self._silence_timeout = silence_timeout
        self._silence_fill = silence_fill
        self._last_audio_at = time.monotonic()
        self._silence_sent = False

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        super().push_frame(frame)
        self._last_audio_at = time.monotonic()
        self._silence_sent = False

    async def _run(self) -> None:
        """Run the plugin's stream, with a silence watcher alongside it."""
        watcher = asyncio.create_task(self._silence_watcher(), name="gnani-stt-silence")
        try:
            await super()._run()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def _silence_watcher(self) -> None:
        """Send silence once the student stops talking.

        The server closes a segment when it hears silence, so without
        this the transcript only arrives after LiveKit's own flush --
        which happens after the turn has already been committed.
        """
        interval = max(self._silence_timeout / 2, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._maybe_send_silence()
            except Exception as exc:  # noqa: BLE001 - never break the stream
                log.warning(
                    "failed to send silence to the STT", extra={"error": str(exc)[:200]}
                )

    async def _maybe_send_silence(self) -> None:
        if self._silence_sent:
            return
        if time.monotonic() - self._last_audio_at < self._silence_timeout:
            return

        self._silence_sent = True
        self._push_silence(self._silence_fill)

    def _push_silence(self, seconds: float) -> None:
        """Push a silent audio frame into the plugin's input channel.

        The plugin's send loop reads from that channel, so the silence
        reaches the server in the same 1024-byte frames as real audio.
        """
        sample_rate = self._needed_sr or 16000
        num_samples = max(1, int(sample_rate * seconds))
        frame = rtc.AudioFrame(
            data=b"\x00" * (num_samples * 2),
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=num_samples,
        )
        self._input_ch.send_nowait(frame)
        log.debug(
            "sent silence to the STT",
            extra={"seconds": seconds, "sample_rate": sample_rate},
        )


class TunedGnaniSTT(gnani.STT):
    """Gnani STT with server-side VAD tuning and prompt silence.

    Everything else -- the API contract, the framing, the transcript
    events -- is the official plugin's.
    """

    def __init__(
        self,
        *,
        vad_headers: dict[str, str] | None = None,
        silence_timeout: float = DEFAULT_SILENCE_TIMEOUT_SECONDS,
        silence_fill: float = DEFAULT_SILENCE_FILL_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Custom headers override the defaults one by one, so a caller
        # can change the endpointing without restating the rest.
        self._vad_headers = {**DEFAULT_VAD_HEADERS, **(vad_headers or {})}
        self._silence_timeout = silence_timeout
        self._silence_fill = silence_fill
        _install_connect_patch()

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
    ) -> RecognizeStream:
        opts = replace(self._opts)
        if is_given(language):
            opts.language = language

        # The stream's task is created inside the base constructor and
        # inherits this context, so the connect call inside it picks the
        # headers up.
        _VAD_HEADERS.set(self._vad_headers)

        return TunedSpeechStream(
            stt=self,
            opts=opts,
            conn_options=self._single_attempt(conn_options),
            silence_timeout=self._silence_timeout,
            silence_fill=self._silence_fill,
        )
