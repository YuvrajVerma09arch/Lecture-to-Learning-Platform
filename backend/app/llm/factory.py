"""LLM access, kept behind a provider-neutral interface.

The brief requires the provider to stay configurable and the two workloads
to have independent model settings. So:

* Workload A (notes)  -- long context, factual fidelity, structured output.
* Workload B (chat)    -- time-to-first-token, streaming, Indic quality.

Each is created through :func:`notes_llm` / :func:`chat_llm`, which are the
only places in the codebase that name a provider. Everything downstream
takes the ``livekit.agents.llm.LLM`` interface, so swapping providers is a
one-line change in this file and a test.

Note on Gemini 4: ``gemini-4-*`` models reject ``temperature``, ``top_p`` and
``top_k`` outright with a 400, so we deliberately do not set them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    """A model call failed in a way the caller should handle."""


@dataclass
class LLMReply:
    """A normalised model reply."""

    text: str
    raw: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""


def _extract_json(text: str) -> Any:
    """Pull a JSON object out of a model reply.

    Models wrap JSON in prose or markdown fences often enough that a bare
    ``json.loads`` on the whole reply fails regularly. We try the strict
    parse first, then unwrap fences, then find the outermost braces.
    """
    cleaned = text.strip()
    if not cleaned:
        raise LLMError("Model returned an empty response")

    # Strip markdown fences.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find the first balanced {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"Model response was not valid JSON: {cleaned[:200]}")


def _strip_schemas(response_format: Any) -> Any:
    """Recursively drop unsupported ``$schema`` / ``additionalProperties`` keys.

    Gemini's structured-output schema validator rejects a few JSON-Schema
    keywords that OpenAI accepts. Stripping them here means one prompt
    definition works on both.
    """
    if isinstance(response_format, dict):
        return {
            k: _strip_schemas(v)
            for k, v in response_format.items()
            if k not in ("$schema", "additionalProperties")
        }
    if isinstance(response_format, list):
        return [_strip_schemas(v) for v in response_format]
    return response_format


def notes_llm() -> Any:
    """Workload A: the notes-generation model.

    Long context and structured output matter here; it runs a handful of
    times per lecture.
    """
    settings = get_settings()
    if not settings.has_google():
        raise LLMError("GOOGLE_API_KEY is not set. Add it to your .env file.")

    from livekit.plugins import google

    model = settings.llm_model_notes
    log.info("notes llm selected", extra={"model": model, "provider": "google"})
    return google.LLM(model=model, api_key=settings.google_api_key)


def notes_segments_llm() -> Any:
    """Workload A, per-segment pass.

    A cheaper, faster model for the many small per-segment calls, while the
    expensive model handles the merge that needs the whole lecture in view.
    """
    settings = get_settings()
    if not settings.has_google():
        raise LLMError("GOOGLE_API_KEY is not set. Add it to your .env file.")

    from livekit.plugins import google

    model = settings.llm_model_notes_segments
    log.info("notes segment llm selected", extra={"model": model, "provider": "google"})
    return google.LLM(model=model, api_key=settings.google_api_key)


def chat_llm() -> Any:
    """Workload B: the conversational model.

    Time-to-first-token dominates the perceived latency of a voice answer,
    so this is a fast, streaming model on a different provider from
    Workload A.
    """
    settings = get_settings()
    if not settings.has_groq():
        raise LLMError("GROQ_API_KEY is not set. Add it to your .env file.")

    from livekit.plugins import groq

    model = settings.llm_model_chat
    log.info("chat llm selected", extra={"model": model, "provider": "groq"})
    return groq.LLM(model=model, api_key=settings.groq_api_key)


async def complete_json(
    llm: Any,
    *,
    prompt: str,
    system: str = "",
    schema: dict[str, Any] | None = None,
    max_output_tokens: int = 8192,
) -> LLMReply:
    """Run one non-streaming completion and parse a JSON reply.

    We use the LiveKit ``LLM`` interface rather than a provider SDK so the
    same code path works for either workload and the provider can be swapped
    without touching the pipeline.
    """
    from livekit.agents.llm import ChatContext
    from livekit.agents.types import MessageContent, TextContent

    chat_ctx = ChatContext(
        messages=[
            MessageContent(content=[TextContent(text=system)], role="system"),
            MessageContent(content=[TextContent(text=prompt)], role="user"),
        ]
    )

    kwargs: dict[str, Any] = {"chat_ctx": chat_ctx}
    if schema is not None:
        kwargs["response_format"] = _strip_schemas(schema)

    try:
        stream = llm.chat(**kwargs)
        reply = await stream.collect()
    except Exception as exc:  # noqa: BLE001 - normalise provider errors
        raise LLMError(f"Model call failed: {exc}") from exc

    text = (reply.text or "").strip()
    if not text:
        raise LLMError("Model returned an empty response")

    usage = getattr(reply, "usage", None)
    return LLMReply(
        text=text,
        raw=text,
        model=getattr(llm, "model", "") or "",
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


async def complete_text(
    llm: Any,
    *,
    prompt: str,
    system: str = "",
    max_output_tokens: int = 4096,
) -> LLMReply:
    """Run one non-streaming completion and return plain text."""
    from livekit.agents.llm import ChatContext
    from livekit.agents.types import MessageContent, TextContent

    chat_ctx = ChatContext(
        messages=[
            MessageContent(content=[TextContent(text=system)], role="system"),
            MessageContent(content=[TextContent(text=prompt)], role="user"),
        ]
    )
    try:
        stream = llm.chat(chat_ctx=chat_ctx)
        reply = await stream.collect()
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Model call failed: {exc}") from exc

    text = (reply.text or "").strip()
    if not text:
        raise LLMError("Model returned an empty response")

    usage = getattr(reply, "usage", None)
    return LLMReply(
        text=text,
        raw=text,
        model=getattr(llm, "model", "") or "",
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def parse_json_reply(reply: LLMReply) -> Any:
    """Parse a JSON reply, raising :class:`LLMError` on malformed output."""
    return _extract_json(reply.text)
