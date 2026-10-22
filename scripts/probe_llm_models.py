#!/usr/bin/env python
"""Discover which LLM models the configured keys can actually use.

The published model catalogues are not the same as what an account can
reach: a model can be listed and still return ``model_not_found`` for a
particular key. This probes the live endpoints with a one-token request
and reports what works, what is rate-limited, and what is missing.

No key material is printed.

Usage:
    .venv/bin/python scripts/probe_llm_models.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.llm.factory import LLMError, complete_json  # noqa: E402


class Ping(BaseModel):
    ok: bool = False
    echo: str = ""


GROQ_CANDIDATES = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3-32b",
    "moonshotai/kimi-k2-instruct-0905",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "deepseek-r1-distill-llama-70b",
    "gemma2-9b-it",
]

GEMINI_CANDIDATES = [
    "gemini-4-argon",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]


async def probe_groq(model: str) -> tuple[str, float, str]:
    from livekit.plugins import groq

    settings = get_settings()
    llm = groq.LLM(model=model, api_key=settings.groq_api_key)
    t0 = time.monotonic()
    try:
        reply = await complete_json(
            llm,
            system="Return JSON with keys ok (boolean) and echo (string).",
            prompt='Return {"ok": true, "echo": "x"}',
            schema=Ping,
        )
        return "OK", time.monotonic() - t0, str(reply.data)[:60]
    except LLMError as exc:
        msg = str(exc)
        if "model_not_found" in msg or "does not exist" in msg:
            return "NOT AVAILABLE", time.monotonic() - t0, msg[:120]
        if "429" in msg or "rate_limit" in msg.lower():
            return "RATE LIMITED", time.monotonic() - t0, msg[:120]
        return "ERROR", time.monotonic() - t0, msg[:160]


async def probe_gemini(model: str) -> tuple[str, float, str]:
    from livekit.plugins import google

    settings = get_settings()
    llm = google.LLM(model=model, api_key=settings.google_api_key)
    t0 = time.monotonic()
    try:
        reply = await complete_json(
            llm,
            system="Return JSON with keys ok (boolean) and echo (string).",
            prompt='Return {"ok": true, "echo": "x"}',
            schema=Ping,
        )
        return "OK", time.monotonic() - t0, str(reply.data)[:60]
    except LLMError as exc:
        msg = str(exc)
        if "exceeded your current quota" in msg or "429" in msg:
            return "QUOTA EXCEEDED", time.monotonic() - t0, msg[:140]
        if "503" in msg or "UNAVAILABLE" in msg or "504" in msg:
            return "UNAVAILABLE", time.monotonic() - t0, msg[:140]
        if "NOT_FOUND" in msg or "not found" in msg:
            return "NOT AVAILABLE", time.monotonic() - t0, msg[:120]
        return "ERROR", time.monotonic() - t0, msg[:160]


async def main() -> int:
    settings = get_settings()

    if settings.has_groq():
        print("=" * 74)
        print("GROQ")
        print("=" * 74)
        for model in GROQ_CANDIDATES:
            status, elapsed, detail = await probe_groq(model)
            print(f"  {model:48} {status:16} {elapsed:6.2f}s")
            if status not in ("OK",):
                print(f"      {detail}")
    else:
        print("GROQ_API_KEY is not set")

    if settings.has_google():
        print()
        print("=" * 74)
        print("GEMINI")
        print("=" * 74)
        for model in GEMINI_CANDIDATES:
            status, elapsed, detail = await probe_gemini(model)
            print(f"  {model:48} {status:16} {elapsed:6.2f}s")
            if status not in ("OK",):
                print(f"      {detail}")
    else:
        print("GOOGLE_API_KEY is not set")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
