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

# The LiveKit plugins must be imported on the main thread: they register
# themselves at import time, and the registration is rejected from a
# worker thread. Importing them here, at module load, means a request
# handler only does a cached module lookup.
from livekit.plugins import google as _google_plugin  # noqa: E402
from livekit.plugins import groq as _groq_plugin  # noqa: E402
from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)


class ErrorKind:
    """Why a model call failed, and whether retrying can help."""

    QUOTA = "quota"          # daily/period quota exhausted; retrying now is useless
    RATE_LIMIT = "rate_limit"  # per-minute limit; retrying after a pause may work
    SERVER = "server"        # provider 5xx; retrying usually works
    CLIENT = "client"        # request rejected; retrying the same request will not
    SHAPE = "shape"          # the reply did not match the schema; a retry may
    UNKNOWN = "unknown"


#: Kinds worth retrying straight away.
RETRYABLE_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.SERVER, ErrorKind.SHAPE})


class LLMError(Exception):
    """A model call failed in a way the caller should handle.

    ``kind`` says why. This matters: Google's free tier allows 20 requests
    per model per day, and its quota error is a 429 -- the same status as a
    per-minute rate limit. Retrying a quota error immediately burns the
    remaining daily allowance and still fails, so the two must be
    distinguished.

    ``retry_after_seconds`` carries the provider's own instruction about
    when to try again ("Please try again in 41.4s"). When it is present it
    is the authoritative wait: a per-minute token budget refills on a
    schedule the caller cannot guess.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = ErrorKind.UNKNOWN,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def is_quota(self) -> bool:
        return self.kind == ErrorKind.QUOTA


#: "Please try again in 41.4s", "try again in 420ms", "retry in 20h38m".
_RETRY_AFTER = re.compile(
    r"try again in\s+([\d.]+)\s*(ms|s|m|h)\b", re.IGNORECASE
)

_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_retry_after(message: str) -> float | None:
    """Extract the provider's requested wait from an error message."""
    match = _RETRY_AFTER.search(message)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value * _UNIT_SECONDS.get(match.group(2).lower(), 1.0)


def classify_error(exc: Exception) -> LLMError:
    """Turn a provider exception into a classified :class:`LLMError`."""
    # An error that is already classified keeps its classification.
    # Re-deriving it from the message would lose the distinction: a
    # quota error and a rate limit are both 429, and re-deriving would
    # turn a non-retryable quota error into a retryable rate limit.
    if isinstance(exc, LLMError):
        return exc

    message = str(exc)
    lowered = message.lower()
    retry_after = parse_retry_after(message)

    if "exceeded your current quota" in lowered or "resource_exhausted" in lowered:
        return LLMError(message, kind=ErrorKind.QUOTA, retry_after_seconds=retry_after)
    if "quota" in lowered:
        return LLMError(message, kind=ErrorKind.QUOTA, retry_after_seconds=retry_after)
    if "429" in message or "rate limit" in lowered or "too many requests" in lowered:
        return LLMError(
            message, kind=ErrorKind.RATE_LIMIT, retry_after_seconds=retry_after
        )
    if "503" in message or "502" in message or "504" in message or "unavailable" in lowered:
        return LLMError(message, kind=ErrorKind.SERVER, retry_after_seconds=retry_after)
    if "401" in message or "403" in message or "invalid_api_key" in lowered:
        return LLMError(message, kind=ErrorKind.CLIENT)
    if "404" in message and "model" in lowered:
        return LLMError(message, kind=ErrorKind.CLIENT)
    if "413" in message or "request too large" in lowered:
        return LLMError(
            message, kind=ErrorKind.RATE_LIMIT, retry_after_seconds=retry_after
        )
    if "did not match the required shape" in lowered or "not valid json" in lowered:
        return LLMError(message, kind=ErrorKind.SHAPE)
    return LLMError(message, kind=ErrorKind.UNKNOWN)


@dataclass
class LLMReply:
    """A normalised model reply."""

    text: str
    raw: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    # The parsed JSON payload, when the reply was JSON.
    data: dict[str, Any] | None = None


def _extract_json(text: str) -> Any:
    """Pull a JSON object out of a model reply.

    Models wrap JSON in prose or markdown fences often enough that a bare
    ``json.loads`` on the whole reply fails regularly. We try the strict
    parse first, then unwrap fences, then find the outermost braces.
    """
    cleaned = text.strip()
    if not cleaned:
        raise LLMError("Model returned an empty response", kind=ErrorKind.SHAPE)

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
    raise LLMError(
        f"Model response was not valid JSON: {cleaned[:200]}", kind=ErrorKind.SHAPE
    )


def notes_llm() -> Any:
    """Workload A: the notes-generation model.

    Long context and structured output matter here; it runs a handful of
    times per lecture.
    """
    settings = get_settings()
    if not settings.has_google():
        raise LLMError("GOOGLE_API_KEY is not set. Add it to your .env file.")

    model = settings.llm_model_notes
    log.info("notes llm selected", extra={"model": model, "provider": "google"})
    return _google_plugin.LLM(model=model, api_key=settings.google_api_key)


def notes_segments_llm() -> Any:
    """Workload A, per-segment pass.

    A cheaper, faster model for the many small per-segment calls, while the
    expensive model handles the merge that needs the whole lecture in view.
    """
    settings = get_settings()
    if not settings.has_google():
        raise LLMError("GOOGLE_API_KEY is not set. Add it to your .env file.")

    model = settings.llm_model_notes_segments
    log.info("notes segment llm selected", extra={"model": model, "provider": "google"})
    return _google_plugin.LLM(model=model, api_key=settings.google_api_key)


def chat_llm() -> Any:
    """Workload B: the conversational model.

    Time-to-first-token dominates the perceived latency of a voice answer,
    so this is a fast, streaming model on a different provider from
    Workload A.
    """
    settings = get_settings()
    if not settings.has_groq():
        raise LLMError("GROQ_API_KEY is not set. Add it to your .env file.")

    model = settings.llm_model_chat
    log.info("chat llm selected", extra={"model": model, "provider": "groq"})
    return _groq_plugin.LLM(model=model, api_key=settings.groq_api_key)


def notes_llm_chain(*, segment: bool = False) -> list[Any]:
    """The notes workload's provider chain, in preference order.

    The first entry is the primary. The second is a fallback on a
    *different* provider, because the failure that actually bites is a
    quota exhaustion on one account: Google's free tier allows 20
    requests per model per day, and a lecture with eight topic segments
    needs nine. When the primary's quota is gone, the pipeline continues
    on the fallback rather than failing.

    Both entries are provider-agnostic from here on: the rest of the
    pipeline only sees the LiveKit ``LLM`` interface.
    """
    settings = get_settings()
    chain: list[Any] = []

    if settings.has_google():
        model = (
            settings.llm_model_notes_segments if segment else settings.llm_model_notes
        )
        chain.append(_google_plugin.LLM(model=model, api_key=settings.google_api_key))
        log.info("notes llm chain", extra={"provider": "google", "model": model, "role": "primary"})

    if settings.has_groq():
        # The fallback reuses the conversational model: it is fast, streams
        # cleanly and handles Indic text.
        chain.append(_groq_plugin.LLM(model=settings.llm_model_chat, api_key=settings.groq_api_key))
        log.info(
            "notes llm chain",
            extra={"provider": "groq", "model": settings.llm_model_chat, "role": "fallback"},
        )

    if not chain:
        raise LLMError(
            "No LLM provider is configured. Set GOOGLE_API_KEY or GROQ_API_KEY."
        )
    return chain


async def complete_json(
    llm: Any,
    *,
    prompt: str,
    system: str = "",
    schema: type[BaseModel] | None = None,
    max_output_tokens: int = 8192,
) -> LLMReply:
    """Run one non-streaming completion and parse a JSON reply.

    We use the LiveKit ``LLM`` interface rather than a provider SDK so the
    same code path works for either workload and the provider can be swapped
    without touching the pipeline.

    ``schema`` is a pydantic model class. LiveKit's structured-output
    plumbing requires a model (or a TypedDict); a raw JSON-schema dict is
    explicitly unsupported, so the payloads are typed here instead.
    """

    chat_ctx = _build_context(system, prompt)

    kwargs: dict[str, Any] = {"chat_ctx": chat_ctx}
    if schema is not None:
        kwargs["response_format"] = schema

    try:
        stream = llm.chat(**kwargs)
        reply = await stream.collect()
    except Exception as exc:  # noqa: BLE001 - normalise provider errors
        raise classify_error(exc) from exc

    text = (reply.text or "").strip()
    if not text:
        raise LLMError("Model returned an empty response", kind=ErrorKind.SHAPE)

    # Parse the JSON and, when a schema was given, validate it. A shape
    # error is reported here rather than surfacing as a confusing
    # KeyError deeper in the pipeline.
    data = _extract_json(text)
    if schema is not None:
        try:
            data = schema.model_validate(data).model_dump()
        except ValidationError as exc:
            raise LLMError(
                f"Model response did not match the required shape: {exc}",
                kind=ErrorKind.SHAPE,
            ) from exc

    usage = getattr(reply, "usage", None)
    return LLMReply(
        text=text,
        raw=text,
        model=getattr(llm, "model", "") or "",
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        data=data,
    )


def _build_context(system: str, prompt: str) -> Any:
    """Build a two-message chat context.

    ``ChatContext`` takes a list of ``ChatMessage`` objects, and a message's
    content is a list of content parts, where plain text is just a string.
    """
    from livekit.agents.llm import ChatContext, ChatMessage

    items = []
    if system:
        items.append(ChatMessage(role="system", content=[system]))
    items.append(ChatMessage(role="user", content=[prompt]))
    return ChatContext(items=items)


async def complete_text(
    llm: Any,
    *,
    prompt: str,
    system: str = "",
    max_output_tokens: int = 4096,
) -> LLMReply:
    """Run one non-streaming completion and return plain text."""
    chat_ctx = _build_context(system, prompt)
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
