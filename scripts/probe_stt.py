#!/usr/bin/env python
"""Live protocol probe for Gnani's streaming STT and TTS.

This exists to settle two things that the documentation does not state,
and that the implementation depends on:

1. **Which WebSocket message types does the server actually send?**
   The ``livekit-plugins-gnani`` plugin handles ``speech_start``,
   ``vad_start``, ``speech_end`` and ``vad_end``, but the Gnani
   documentation lists only ``connected``, ``processing``, ``transcript``,
   ``error`` and ``settings_updated``. If the server never sends the
   speech events, the plugin never emits ``START_OF_SPEECH`` /
   ``END_OF_SPEECH`` to LiveKit. This probe records the raw frame types.

2. **What is the end-to-end latency of a live utterance?** The server
   closes a segment after ``x-min-silence-ms`` of silence (500 ms
   default) and then transcribes, so the transcript arrives well after
   the user stopped speaking. The probe measures that gap.

The probe synthesises its audio locally with macOS ``say`` -- no
download, no external service, and only a few seconds of billable audio.

Usage:
    .venv/bin/python scripts/probe_stt.py [--lang hi-IN] [--seconds 6]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402

# Text per language, in the script the voice speaks.
SPEECH = {
    "en-IN": (
        "Welcome to the lecture on computer networks. Today we will study "
        "the OSI model and the seven layers of the protocol stack."
    ),
    "hi-IN": (
        "नमस्ते दोस्तों। आज हम कंप्यूटर नेटवर्क के बारे में पढ़ेंगे। "
        "आज का विषय है ओ एस आई मॉडल, जो कि नेटवर्क कम्युनिकेशन के सात स्तरों "
        "को समझाता है।"
    ),
    "te-IN": (
        "నమస్కారం బంధురా. ఈరోజు మనం కంప్యూటర్ నెట్‌వర్క్‌ల గురించి చదువుతున్నాము. "
        "ఈరోజు అంశం ఓ ఎస్ ఐ మోడల్, ఇది నెట్‌వర్క్ కమ్యూనికేషన్ ఏడు లేయర్లను "
        "వివరిస్తుంది."
    ),
    "kn-IN": (
        "ನಮಸ್ಕಾರ ಸ್ನೇಹಿತರು. ಇಂದು ನಾವು ಕಂಪ್ಯೂಟರ್ ನೆಟ್‌ವರ್ಕ್ ಬಗ್ಗೆ ಓದುತ್ತೇವೆ. "
        "ಇಂದಿನ ವಿಷಯವು ಒ ಎಸ್ ಐ ಮಾದರಲ್, ಇದು ನೆಟ್‌ವರ್ಕ್ ಸಂವಹನದ ಏಳು ಪದರಗಳನ್ನು ವಿವರಿಸುತ್ತದೆ."
    ),
    "ta-IN": (
        "வணக்கம் நண்பர்களே. இன்று நாம் கணினி நெட்ட்வர்க் பற்றி படிப்போம். "
        "இன்றைய விஷயம் ஓ எஸ் ஐ மாடல்."
    ),
}

VOICE = {
    "en-IN": "Aman",
    "hi-IN": "Lekha",
    "te-IN": "Geeta",
    "kn-IN": "Soumya",
    "ta-IN": "Vani",
}


def make_pcm(lang: str, workdir: Path) -> tuple[bytes, float]:
    """Synthesise speech and return 16 kHz mono s16le PCM plus its duration."""
    aiff = workdir / "speech.aiff"
    wav = workdir / "speech.wav"

    voice = VOICE.get(lang)
    cmd = ["say", "-o", str(aiff)]
    if voice:
        cmd += ["-v", voice]
    cmd += [SPEECH.get(lang, SPEECH["en-IN"])]
    subprocess.run(cmd, check=True, capture_output=True)

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(aiff), "-ar", "16000", "-ac", "1", "-f", "s16le", str(wav)],
        check=True,
        capture_output=True,
    )
    pcm = wav.read_bytes()
    duration = len(pcm) / (2 * 16000)
    return pcm, duration


def ws_header_kwargs(headers: dict[str, str]) -> dict:
    """``websockets`` renamed the header kwarg in v13."""
    import websockets

    major = int(websockets.__version__.split(".", 1)[0])
    if major >= 13:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


async def probe_streaming(
    pcm: bytes,
    *,
    lang: str,
    min_silence_ms: int | None = None,
    vad_threshold: float | None = None,
    trailing_silence_seconds: float = 2.0,
) -> dict:
    """Stream PCM to Gnani and record every message the server sends.

    ``trailing_silence_seconds`` matters: the server closes a VAD segment
    only when it *hears* silence, so the client must send actual silence
    frames after the speech. Stopping the send loop without sending
    silence leaves the segment open and no transcript ever arrives.
    """
    import websockets

    settings = get_settings()
    headers = {
        "x-api-key-id": settings.gnani_api_key,
        "lang_code": lang,
        "x-sample-rate": "16000",
    }
    if min_silence_ms is not None:
        headers["x-min-silence-ms"] = str(min_silence_ms)
    if vad_threshold is not None:
        headers["x-vad-threshold"] = str(vad_threshold)

    url = "wss://api.vachana.ai/stt/v3/stream"
    events: list[dict] = []
    transcripts: list[dict] = []
    t_start = time.monotonic()
    t_audio_done = 0.0
    error: str | None = None

    try:
        async with websockets.connect(url, **ws_header_kwargs(headers)) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            events.append({"t": time.monotonic() - t_start, "type": "raw",
                           "data": _safe(raw)})

            # Send audio at real-time cadence: 1024 bytes = 32 ms of 16 kHz
            # s16le mono. Buffering and bursting degrades the server's VAD.
            frame_size = 1024
            frames = [pcm[i : i + frame_size] for i in range(0, len(pcm), frame_size)]
            frame_seconds = frame_size / (2 * 16000)

            receiver = asyncio.create_task(_receive(ws, events, transcripts, t_start))

            for frame in frames:
                await ws.send(frame)
                await asyncio.sleep(frame_seconds)
            t_audio_done = time.monotonic() - t_start

            # Send real silence so the server's VAD can close the segment.
            silence = b"\x00" * frame_size
            silence_frames = int(trailing_silence_seconds / frame_seconds)
            for _ in range(silence_frames):
                await ws.send(silence)
                await asyncio.sleep(frame_seconds)

            # Wait for the server's VAD to close the segment and deliver the
            # transcript. It needs min-silence-ms of quiet, plus its own
            # transcription time.
            await asyncio.wait_for(receiver, timeout=30)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    return {
        "lang": lang,
        "headers_sent": {k: (v if k != "x-api-key-id" else "***") for k, v in headers.items()},
        "audio_seconds": round(t_audio_done, 3),
        "events": events,
        "transcripts": transcripts,
        "error": error,
    }


async def _receive(ws, events: list, transcripts: list, t_start: float) -> None:
    async for msg in ws:
        now = time.monotonic() - t_start
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            events.append({"t": round(now, 3), "type": "unparsed", "data": _safe(msg)})
            continue
        mtype = data.get("type", "?")
        events.append({"t": round(now, 3), "type": mtype, "data": _safe(data)})
        if mtype == "transcript":
            transcripts.append(
                {
                    "t": round(now, 3),
                    "text": data.get("text", ""),
                    "segment_id": data.get("segment_id"),
                    "segment_index": data.get("segment_index"),
                    "latency_ms": data.get("latency"),
                    "audio_duration_ms": data.get("audio_duration_ms"),
                }
            )


def _safe(obj):
    """Reduce a frame to something safe to print."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("text", "message", "audio"):
                out[k] = f"<{len(str(v))} chars>" if k == "audio" else v
            else:
                out[k] = v
        return out
    s = str(obj)
    return s if len(s) < 300 else f"<{len(s)} chars>"


async def probe_tts(text: str, lang: str, voice: str, model: str) -> dict:
    """Probe the TTS REST endpoint and measure time-to-first-byte."""
    import aiohttp

    settings = get_settings()
    url = f"{settings.gnani_base_url}/api/v1/tts/inference"
    payload = {
        "text": text,
        "voice": voice,
        "model": model,
        "language": lang,
        "speed": 1.0,
        "audio_config": {
            "sample_rate": 16000,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "wav",
        },
    }
    headers = {"X-API-Key-ID": settings.gnani_api_key, "Content-Type": "application/json"}

    t0 = time.monotonic()
    first_byte = None
    total = 0
    status = None
    error = None
    content_type = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type")
                async for chunk in resp.content.iter_chunked(4096):
                    if first_byte is None:
                        first_byte = time.monotonic() - t0
                    total += len(chunk)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    return {
        "model": model,
        "voice": voice,
        "language": lang,
        "status": status,
        "content_type": content_type,
        "bytes": total,
        "ttfb_seconds": round(first_byte, 3) if first_byte else None,
        "total_seconds": round(time.monotonic() - t0, 3),
        "error": error,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="hi-IN", choices=sorted(SPEECH))
    parser.add_argument("--tts-voice", default="Nalini")
    parser.add_argument("--tts-model", default="timbre-v2.5")
    parser.add_argument("--min-silence-ms", type=int, default=None)
    parser.add_argument("--vad-threshold", type=float, default=None)
    parser.add_argument("--skip-tts", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_gnani():
        print("GNANI_API_KEY is not set", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        print(f"generating {args.lang} speech locally with macOS 'say' ...", file=sys.stderr)
        pcm, duration = make_pcm(args.lang, workdir)
        print(f"  {duration:.2f}s of audio, {len(pcm)} bytes of PCM", file=sys.stderr)

        print("probing streaming STT ...", file=sys.stderr)
        stt = await probe_streaming(
            pcm,
            lang=args.lang,
            min_silence_ms=args.min_silence_ms,
            vad_threshold=args.vad_threshold,
        )

    if not args.skip_tts:
        print("probing TTS ...", file=sys.stderr)
        tts = await probe_tts(
            "नमस्ते, यह एक परीक्षण वाक्य है।", args.lang, args.tts_voice, args.tts_model
        )
    else:
        tts = None

    print("\n" + "=" * 72)
    print("STREAMING STT PROBE")
    print("=" * 72)
    print(f"language sent      : {stt['lang']}")
    print(f"headers sent       : {stt['headers_sent']}")
    print(f"audio streamed     : {stt['audio_seconds']}s")
    print(f"error              : {stt['error']}")
    print("\nserver frames received:")
    for e in stt["events"]:
        print(f"  t={e['t']:>7.3f}s  type={e['type']:<14} {e['data']}")
    print("\ntranscripts:")
    for t in stt["transcripts"]:
        print(f"  t={t['t']:>7.3f}s  idx={t['segment_index']}  "
              f"server_latency={t['latency_ms']}ms  audio={t['audio_duration_ms']}ms")
        print(f"             text: {t['text']}")
    types = {e["type"] for e in stt["events"]}
    print(f"\nobserved frame types: {sorted(types)}")
    speech_events = types & {"speech_start", "vad_start", "speech_end", "vad_end"}
    print(f"speech/vad events observed: {sorted(speech_events) or 'NONE'}")

    if tts:
        print("\n" + "=" * 72)
        print("TTS PROBE")
        print("=" * 72)
        for k, v in tts.items():
            print(f"  {k:18}: {v}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
