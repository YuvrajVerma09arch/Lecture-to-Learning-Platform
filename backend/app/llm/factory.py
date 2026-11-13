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
import os
import re
from dataclasses import dataclass
from typing import Any, get_args

from livekit.agents.llm import LLM, ChatContext, ChatItem, LLMStream
from livekit.agents.llm.chat_context import DYNAMIC_INSTRUCTIONS_MESSAGE_ID
from livekit.agents.llm.fallback_adapter import FallbackAdapter
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions
from livekit.agents.utils.misc import is_given
from pydantic import BaseModel, TypeAdapter, ValidationError, create_model

from ..config import get_settings
from ..logging_setup import get_logger

# The LiveKit plugins must be imported on the main thread: they register
# themselves at import time, and the registration is rejected from a
# worker thread. Importing them here, at module load, means a request
# handler only does a cached module lookup.
from .plugins import google_plugin as _google_plugin  # noqa: E402
from .plugins import groq_plugin as _groq_plugin  # noqa: E402

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
    # LiveKit wraps a provider deadline as "after 4 attempts (caused by
    # CancelledError)", losing the status code. It is still a transient
    # failure, and callers that can switch providers need to recognise it.
    if (
        "cancellederror" in lowered
        or "deadline exceeded" in lowered
        or "timed out" in lowered
        or "timeout" in lowered
    ):
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


#: How much of the conversation the voice path keeps.
#:
#: A spoken answer only needs the last few turns to stay coherent. Everything
#: older still costs input tokens on every request, and during a long study
#: session that is what pushed the chat model into per-minute rate limits.
KEEP_LAST_USER_MESSAGES = 3
KEEP_LAST_ASSISTANT_MESSAGES = 2

_FUNCTION_ITEM_TYPES = frozenset({"function_call", "function_call_output"})


def _leading_instructions(items: list[ChatItem]) -> list[ChatItem]:
    """The instruction preamble at the front of ``items``.

    Mirrors ``ChatContext._leading_instructions``: the first system/developer
    message, plus the item right after it only when it carries the dynamic
    instructions id.
    """
    idx = next(
        (
            i
            for i, item in enumerate(items)
            if item.type == "message" and item.role in ("system", "developer")
        ),
        None,
    )
    if idx is None:
        return []
    kept = items[idx : idx + 2]
    if len(kept) == 2 and kept[1].id != DYNAMIC_INSTRUCTIONS_MESSAGE_ID:
        kept = kept[:1]
    return kept


def trim_chat_context(chat_ctx: ChatContext) -> ChatContext:
    """Return a bounded view of ``chat_ctx`` for the voice path.

    Keeps the last :data:`KEEP_LAST_USER_MESSAGES` user messages, the last
    :data:`KEEP_LAST_ASSISTANT_MESSAGES` assistant messages and the leading
    instructions. The window starts at whichever of those two falls earlier,
    so the result is always one contiguous tail -- a context split mid-turn
    would leave the provider with a function output whose call was dropped.

    Returns a *new* :class:`ChatContext` sharing the item objects; the
    session's own list is never mutated. When nothing has to be dropped the
    context is returned unchanged.
    """
    items = chat_ctx.items

    user_indices = [
        i for i, item in enumerate(items) if item.type == "message" and item.role == "user"
    ]
    assistant_indices = [
        i
        for i, item in enumerate(items)
        if item.type == "message" and item.role == "assistant"
    ]

    bounds: list[int] = []
    if len(user_indices) > KEEP_LAST_USER_MESSAGES:
        bounds.append(user_indices[-KEEP_LAST_USER_MESSAGES])
    if len(assistant_indices) > KEEP_LAST_ASSISTANT_MESSAGES:
        bounds.append(assistant_indices[-KEEP_LAST_ASSISTANT_MESSAGES])
    if not bounds:
        return chat_ctx

    start = min(bounds)
    new_items: list[ChatItem] = []
    seen: set[str] = set()
    for item in [*_leading_instructions(items[:start]), *items[start:]]:
        if item.id in seen:
            continue
        seen.add(item.id)
        new_items.append(item)

    # The context should not open mid-tool-call either.
    while new_items and new_items[0].type in _FUNCTION_ITEM_TYPES:
        new_items.pop(0)

    return ChatContext(new_items)


class _SchemaForwardingLLM(LLM):
    """Adapt one provider model so schema requests survive the fallback chain.

    ``FallbackAdapter`` carries request options through ``extra_kwargs`` and
    exposes no ``response_format`` parameter. The plugins accept it under
    that name and translate it into the provider's structured-output form;
    handed over as an ordinary extra kwarg it would reach the HTTP layer as a
    raw pydantic class. Instances wrapped for the adapter therefore pop it
    back out and pass it where the plugin expects it.

    Metrics raised by the wrapped instance are re-emitted here, because the
    adapter subscribes to the object it was given, not to the real plugin.
    """

    def __init__(self, inner: LLM) -> None:
        super().__init__()
        self._inner = inner
        self._on_metrics_collected = self._reemit_metrics
        inner.on("metrics_collected", self._on_metrics_collected)

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def metrics_metadata(self) -> Any:
        return self._inner.metrics_metadata

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Any] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
        response_format: Any = NOT_GIVEN,
    ) -> LLMStream:
        extra = dict(extra_kwargs) if is_given(extra_kwargs) else {}
        schema = extra.pop("response_format", NOT_GIVEN)
        if is_given(response_format):
            schema = response_format

        kwargs: dict[str, Any] = {
            "chat_ctx": chat_ctx,
            "tools": tools,
            "conn_options": conn_options,
            "parallel_tool_calls": parallel_tool_calls,
            "tool_choice": tool_choice,
        }
        if extra:
            kwargs["extra_kwargs"] = extra
        if is_given(schema):
            kwargs["response_format"] = schema
        return self._inner.chat(**kwargs)

    def prewarm(self, *, loop: Any = None) -> None:
        self._inner.prewarm(loop=loop)

    async def aclose(self) -> None:
        self._inner.off("metrics_collected", self._on_metrics_collected)
        await self._inner.aclose()

    def _reemit_metrics(self, *args: Any, **kwargs: Any) -> None:
        self.emit("metrics_collected", *args, **kwargs)


class ChatLLM(LLM):
    """The session-facing chat model: a fallback chain behind one interface.

    ``FallbackAdapter`` owns its own stream, so metrics and terminal errors
    arrive on the adapter rather than on the object the session holds. The
    adapter's stream is returned untouched; this wrapper only forwards the two
    events the session subscribes to and trims the context before each call.
    """

    def __init__(self, adapter: FallbackAdapter) -> None:
        super().__init__()
        self._adapter = adapter
        self._on_metrics_collected = self._forward_metrics
        self._on_error = self._forward_error
        adapter.on("metrics_collected", self._on_metrics_collected)
        adapter.on("error", self._on_error)

    @property
    def model(self) -> str:
        return self._adapter.model

    @property
    def provider(self) -> str:
        return self._adapter.provider

    @property
    def metrics_metadata(self) -> Any:
        return self._adapter.metrics_metadata

    @property
    def models(self) -> list[str]:
        """The chain's models, best first."""
        return [instance.model for instance in self._adapter._llm_instances]

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Any] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
        response_format: Any = NOT_GIVEN,
    ) -> LLMStream:
        extra = dict(extra_kwargs) if is_given(extra_kwargs) else {}
        if is_given(response_format):
            extra["response_format"] = response_format
        return self._adapter.chat(
            chat_ctx=trim_chat_context(chat_ctx),
            tools=tools,
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra,
        )

    def prewarm(self, *, loop: Any = None) -> None:
        # Under pytest the dummy GOOGLE_API_KEY would make the primary's
        # prewarm a network call to models.list; the suite must not do that.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        self._adapter.prewarm(loop=loop)

    async def aclose(self) -> None:
        await self._adapter.aclose()
        await super().aclose()

    def _forward_metrics(self, *args: Any, **kwargs: Any) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    def _forward_error(self, *args: Any, **kwargs: Any) -> None:
        self.emit("error", *args, **kwargs)


_CHAT_LLM_CACHE: tuple[tuple[Any, ...], ChatLLM] | None = None


def _chat_chain_key(settings: Any) -> tuple[Any, ...]:
    """What the cached chain depends on, so a settings change rebuilds it."""
    return (
        bool(settings.has_google()),
        bool(settings.has_groq()),
        settings.llm_model_chat_google,
        settings.llm_model_chat,
        tuple(settings.llm_model_chat_fallbacks),
    )


def _build_chat_chain(settings: Any) -> list[LLM]:
    """Assemble the conversational fallback chain, best first.

    Gemini leads when its key is configured -- it is the faster answer on
    this account -- then the configured Groq models. Missing providers are
    skipped rather than fatal, so one dead key cannot take the voice path
    down, and duplicate model ids are collapsed.
    """
    chain: list[LLM] = []

    if settings.has_google():
        model = settings.llm_model_chat_google
        log.info("chat llm chain", extra={"provider": "google", "model": model, "role": "primary"})
        chain.append(_google_plugin.LLM(model=model, api_key=settings.google_api_key))

    if settings.has_groq():
        model = settings.llm_model_chat
        log.info(
            "chat llm chain",
            extra={
                "provider": "groq",
                "model": model,
                "role": "fallback",
                # A hybrid-reasoning model that the plugin's own table does
                # not special-case: without this it spends tokens on thinking
                # that shows up as narration or delays the first spoken word.
                "reasoning_effort": "none",
            },
        )
        chain.append(
            _groq_plugin.LLM(model=model, api_key=settings.groq_api_key, reasoning_effort="none")
        )
        for model in settings.llm_model_chat_fallbacks:
            log.info("chat llm chain", extra={"provider": "groq", "model": model, "role": "fallback"})
            chain.append(_groq_plugin.LLM(model=model, api_key=settings.groq_api_key))

    if not chain:
        raise LLMError("No LLM provider is configured. Set GOOGLE_API_KEY or GROQ_API_KEY.")

    deduped: list[LLM] = []
    seen: set[str] = set()
    for instance in chain:
        if instance.model in seen:
            continue
        seen.add(instance.model)
        deduped.append(instance)

    return [_SchemaForwardingLLM(instance) for instance in deduped]


def chat_llm() -> Any:
    """Workload B: the conversational model, with provider fallbacks.

    Time-to-first-token dominates the perceived latency of a voice answer,
    so the chain leads with the fastest available provider and falls through
    to the others instead of failing a turn.

    Built once per process: each entry owns an HTTP client, so rebuilding it
    per question leaked connections. The cache is keyed on the settings the
    chain depends on, which also keeps tests that swap settings independent.
    """
    global _CHAT_LLM_CACHE
    settings = get_settings()
    key = _chat_chain_key(settings)
    if _CHAT_LLM_CACHE is not None and _CHAT_LLM_CACHE[0] == key:
        return _CHAT_LLM_CACHE[1]

    chain = _build_chat_chain(settings)
    adapter = FallbackAdapter(
        chain,
        # Each attempt gets a generous budget: a slow Gemini answer is still
        # preferable to a failover storm. Retries stay off inside the chain --
        # the adapter moves to the next provider instead -- and only the
        # caller's conn_options govern retries of the whole chain.
        attempt_timeout=30.0,
        max_retry_per_llm=0,
        retry_on_chunk_sent=False,
    )
    llm = ChatLLM(adapter)
    log.info("chat llm chain ready", extra={"models": llm.models})
    _CHAT_LLM_CACHE = (key, llm)
    return llm


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
        # The remaining configured Groq models follow, so a quota or
        # rate-limit on one model does not stop the pipeline: the free tier
        # budget is per model, not per account.
        for model in settings.llm_model_chat_fallbacks:
            if model == settings.llm_model_chat:
                continue
            chain.append(_groq_plugin.LLM(model=model, api_key=settings.groq_api_key))
            log.info(
                "notes llm chain",
                extra={"provider": "groq", "model": model, "role": "fallback"},
            )

    if not chain:
        raise LLMError(
            "No LLM provider is configured. Set GOOGLE_API_KEY or GROQ_API_KEY."
        )
    return chain


#: The field a batch request is wrapped in. LiveKit's plugins only convert a
#: bare pydantic model into a provider schema, so ``list[Card]`` has to travel
#: inside an object and be unwrapped again on the way back.
_BATCH_FIELD = "items"


def _as_request_schema(schema: Any) -> tuple[Any, bool]:
    """Return the schema to send, and whether the reply needs unwrapping.

    A plain pydantic model goes as-is. Anything else -- in practice
    ``list[Model]``, but any annotation is allowed -- is sent as a
    single-field envelope so every provider sees a shape it understands.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema, False

    args = get_args(schema)
    label = args[0].__name__ if len(args) == 1 and hasattr(args[0], "__name__") else "batch"
    envelope = create_model(f"{label}Envelope", **{_BATCH_FIELD: (schema, ...)})
    return envelope, True


async def complete_json(
    llm: Any,
    *,
    prompt: str,
    system: str = "",
    schema: Any = None,
    max_output_tokens: int = 8192,
) -> LLMReply:
    """Run one non-streaming completion and parse a JSON reply.

    We use the LiveKit ``LLM`` interface rather than a provider SDK so the
    same code path works for either workload and the provider can be swapped
    without touching the pipeline.

    ``schema`` is a pydantic model class, or an annotation of them such as
    ``list[FlashcardPayload]``. A model goes to the provider unchanged; a
    batch annotation is requested as a single-field envelope and unwrapped
    before validation, because LiveKit converts only bare models.
    """

    chat_ctx = _build_context(system, prompt)

    wrapped = False
    kwargs: dict[str, Any] = {"chat_ctx": chat_ctx}
    if schema is not None:
        request_schema, wrapped = _as_request_schema(schema)
        kwargs["response_format"] = request_schema

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
    if wrapped:
        if isinstance(data, dict) and _BATCH_FIELD in data:
            data = data[_BATCH_FIELD]
        elif not isinstance(data, list):
            # A model that answered something other than the batch -- an
            # object without the field, a string, a number -- is a shape
            # failure. A bare list is accepted: some providers ignore the
            # envelope and return the batch directly, which is still useful.
            raise LLMError(
                f"Model response did not match the required shape: expected "
                f"an object with a '{_BATCH_FIELD}' field",
                kind=ErrorKind.SHAPE,
            )
    if schema is not None:
        try:
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                data = schema.model_validate(data).model_dump()
            else:
                # A list of models, or any other annotated type.
                data = TypeAdapter(schema).dump_python(
                    TypeAdapter(schema).validate_python(data)
                )
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
