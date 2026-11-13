"""The conversational LLM chain and the context trimming around it.

The voice path used to send the whole conversation on every turn, which is
what pushed Groq into per-minute rate limits during a long session. These
tests pin the window that replaces it, the fallback chain behind it, and the
plumbing that keeps structured output working through that chain.
"""

from __future__ import annotations

from typing import Any

import pytest
from livekit.agents.llm import LLM, ChatContext, ChatMessage
from livekit.agents.llm.chat_context import DYNAMIC_INSTRUCTIONS_MESSAGE_ID
from pydantic import BaseModel

from app import config as config_mod
from app.llm import factory


def _ctx(*turns: tuple[str, str]) -> ChatContext:
    return ChatContext([ChatMessage(role=role, content=[text]) for role, text in turns])


def _texts(ctx: ChatContext) -> list[tuple[str, str]]:
    return [
        (item.role, item.content[0])
        for item in ctx.items
        if item.type == "message" and isinstance(item.content[0], str)
    ]


class TestTrimChatContext:
    def test_a_short_context_is_returned_unchanged(self):
        ctx = _ctx(("system", "sys"), ("user", "u1"), ("assistant", "a1"))
        assert factory.trim_chat_context(ctx) is ctx

    def test_it_keeps_the_last_three_user_and_two_assistant_messages(self):
        ctx = _ctx(
            ("system", "sys"),
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
            ("assistant", "a3"),
            ("user", "u4"),
        )

        trimmed = factory.trim_chat_context(ctx)

        # One extra turn survives: the window has to start at a message
        # boundary, so the earliest bound (u2) also keeps a2 after it.
        assert _texts(trimmed) == [
            ("system", "sys"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
            ("assistant", "a3"),
            ("user", "u4"),
        ]

    def test_the_window_follows_the_earlier_of_the_two_bounds(self):
        ctx = _ctx(
            ("system", "sys"),
            ("user", "u1"),
            ("user", "u2"),
            ("user", "u3"),
            ("user", "u4"),
            ("user", "u5"),
        )

        trimmed = factory.trim_chat_context(ctx)

        # No assistant messages exist, so only the user bound applies.
        assert _texts(trimmed) == [("system", "sys"), ("user", "u3"), ("user", "u4"), ("user", "u5")]

    def test_the_session_list_is_never_mutated(self):
        ctx = _ctx(
            ("system", "sys"),
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
            ("assistant", "a3"),
            ("user", "u4"),
        )
        before = list(ctx.items)

        trimmed = factory.trim_chat_context(ctx)

        assert trimmed is not ctx
        assert len(ctx.items) == len(before)
        assert [item.id for item in ctx.items] == [item.id for item in before]
        # The kept items are the same objects, not copies.
        assert trimmed.items[-1] is ctx.items[-1]

    def test_dynamic_instructions_travel_with_the_system_message(self):
        dynamic = ChatMessage(
            id=DYNAMIC_INSTRUCTIONS_MESSAGE_ID,
            role="system",
            content=["dynamic rules"],
        )
        ctx = ChatContext(
            [ChatMessage(role="system", content=["base rules"]), dynamic]
            + [ChatMessage(role="user", content=[f"u{i}"]) for i in range(5)]
        )

        trimmed = factory.trim_chat_context(ctx)

        leading = [item.content[0] for item in trimmed.items[:2]]
        assert leading == ["base rules", "dynamic rules"]

    def test_an_empty_context_is_left_alone(self):
        ctx = ChatContext([])
        assert factory.trim_chat_context(ctx) is ctx


class _RecordingLLM(LLM):
    """A provider stand-in that records what the shim handed it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Any] | None = None,
        conn_options: Any = None,
        parallel_tool_calls: Any = None,
        tool_choice: Any = None,
        extra_kwargs: Any = None,
        response_format: Any = None,
    ) -> Any:
        self.calls.append(
            {
                "chat_ctx": chat_ctx,
                "tools": tools,
                "conn_options": conn_options,
                "parallel_tool_calls": parallel_tool_calls,
                "tool_choice": tool_choice,
                "extra_kwargs": extra_kwargs,
                "response_format": response_format,
            }
        )
        return "stream"


class _Schema(BaseModel):
    answer: str


class _OtherSchema(BaseModel):
    other: int


class TestSchemaForwarding:
    def test_response_format_moves_to_its_named_argument(self):
        inner = _RecordingLLM()

        stream = factory._SchemaForwardingLLM(inner).chat(
            chat_ctx=ChatContext([]),
            extra_kwargs={"response_format": _Schema, "temperature": 0.2},
        )

        assert stream == "stream"
        call = inner.calls[0]
        assert call["response_format"] is _Schema
        assert call["extra_kwargs"] == {"temperature": 0.2}

    def test_a_named_response_format_wins_over_the_packed_one(self):
        inner = _RecordingLLM()

        factory._SchemaForwardingLLM(inner).chat(
            chat_ctx=ChatContext([]),
            extra_kwargs={"response_format": _Schema},
            response_format=_OtherSchema,
        )

        assert inner.calls[0]["response_format"] is _OtherSchema

    def test_plain_extras_are_forwarded_untouched(self):
        inner = _RecordingLLM()

        factory._SchemaForwardingLLM(inner).chat(
            chat_ctx=ChatContext([]),
            extra_kwargs={"reasoning_effort": "none"},
        )

        call = inner.calls[0]
        assert call["extra_kwargs"] == {"reasoning_effort": "none"}
        assert call["response_format"] is None


class _StubAdapter:
    """Just enough adapter surface for :class:`ChatLLM`."""

    def __init__(self) -> None:
        self.events: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.model = "stub"
        self.provider = "stub"
        self.metrics_metadata: dict[str, Any] = {}

    def on(self, event: str, callback: Any) -> None:
        self.events.setdefault(event, []).append(callback)

    def chat(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "adapter-stream"

    def prewarm(self, *, loop: Any = None) -> None:
        self.prewarmed = loop

    async def aclose(self) -> None:
        self.closed = True


class TestChatLLMWrapper:
    def test_it_packs_the_schema_and_trims_the_context(self):
        adapter = _StubAdapter()
        wrapper = factory.ChatLLM(adapter)
        ctx = _ctx(
            ("system", "sys"),
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
            ("assistant", "a3"),
            ("user", "u4"),
        )

        stream = wrapper.chat(chat_ctx=ctx, response_format=_Schema)

        assert stream == "adapter-stream"
        call = adapter.calls[0]
        assert call["extra_kwargs"] == {"response_format": _Schema}
        assert _texts(call["chat_ctx"]) == [
            ("system", "sys"),
            ("user", "u2"),
            ("assistant", "a2"),
            ("user", "u3"),
            ("assistant", "a3"),
            ("user", "u4"),
        ]
        # The caller's context is untouched.
        assert len(ctx.items) == 8

    def test_metrics_and_errors_are_re_emitted_on_the_wrapper(self):
        adapter = _StubAdapter()
        wrapper = factory.ChatLLM(adapter)
        seen: list[tuple[str, tuple[Any, ...]]] = []
        wrapper.on("metrics_collected", lambda *a, **k: seen.append(("metrics", a)))
        wrapper.on("error", lambda *a, **k: seen.append(("error", a)))

        adapter.events["metrics_collected"][0]("metric")
        error = ValueError("boom")
        adapter.events["error"][0](error)

        assert seen == [("metrics", ("metric",)), ("error", (error,))]

    def test_prewarm_is_skipped_under_pytest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_chat_llm.py::test_x")
        adapter = _StubAdapter()

        factory.ChatLLM(adapter).prewarm()

        assert not hasattr(adapter, "prewarmed")

    def test_prewarm_delegates_outside_pytest(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        adapter = _StubAdapter()

        factory.ChatLLM(adapter).prewarm(loop="loop")

        assert adapter.prewarmed == "loop"

    async def test_aclose_closes_the_chain(self):
        adapter = _StubAdapter()
        await factory.ChatLLM(adapter).aclose()
        assert adapter.closed is True


class TestChatChain:
    """Construction of the cached chain, with settings swapped per case."""

    def _settings(self, monkeypatch: pytest.MonkeyPatch, **keys: Any) -> Any:
        for name in ("GOOGLE_API_KEY", "GROQ_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        return config_mod.Settings(_env_file=None, **keys)

    def test_the_primary_is_gemini_then_groq_then_fallbacks(self, monkeypatch):
        monkeypatch.setattr(
            factory,
            "get_settings",
            lambda: self._settings(
                monkeypatch,
                google_api_key="k",
                groq_api_key="k",
                llm_model_chat_google="probe-gemini-x",
                llm_model_chat="probe-qwen-y",
                llm_model_chat_fallbacks=["probe-oss-a", "probe-oss-b"],
            ),
        )

        llm = factory.chat_llm()

        assert llm.models == ["probe-gemini-x", "probe-qwen-y", "probe-oss-a", "probe-oss-b"]

    def test_duplicate_models_are_collapsed(self, monkeypatch):
        monkeypatch.setattr(
            factory,
            "get_settings",
            lambda: self._settings(
                monkeypatch,
                google_api_key="k",
                groq_api_key="k",
                llm_model_chat_google="probe-gemini-x",
                llm_model_chat="probe-qwen-y",
                llm_model_chat_fallbacks=["probe-qwen-y", "probe-oss-b"],
            ),
        )

        assert factory.chat_llm().models == ["probe-gemini-x", "probe-qwen-y", "probe-oss-b"]

    def test_groq_alone_starts_the_chain(self, monkeypatch):
        monkeypatch.setattr(
            factory,
            "get_settings",
            lambda: self._settings(
                monkeypatch,
                groq_api_key="k",
                llm_model_chat_google="probe-gemini-x",
                llm_model_chat="probe-qwen-y",
                llm_model_chat_fallbacks=["probe-oss-b"],
            ),
        )

        assert factory.chat_llm().models == ["probe-qwen-y", "probe-oss-b"]

    def test_no_provider_raises(self, monkeypatch):
        monkeypatch.setattr(factory, "get_settings", lambda: self._settings(monkeypatch))

        with pytest.raises(factory.LLMError):
            factory.chat_llm()

    def test_the_same_settings_reuse_the_cached_instance(self, monkeypatch):
        monkeypatch.setattr(
            factory,
            "get_settings",
            lambda: self._settings(
                monkeypatch,
                google_api_key="k",
                groq_api_key="k",
                llm_model_chat_google="probe-cache-a",
                llm_model_chat="probe-cache-b",
            ),
        )

        assert factory.chat_llm() is factory.chat_llm()

    def test_a_changed_setting_rebuilds_the_chain(self, monkeypatch):
        values = {"model": "probe-change-a"}

        def get_settings() -> Any:
            return self._settings(
                monkeypatch,
                google_api_key="k",
                groq_api_key="k",
                llm_model_chat_google=values["model"],
                llm_model_chat="probe-change-b",
            )

        monkeypatch.setattr(factory, "get_settings", get_settings)
        first = factory.chat_llm()

        values["model"] = "probe-change-c"
        second = factory.chat_llm()

        assert first is not second
        assert second.models[0] == "probe-change-c"
