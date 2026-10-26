"""The voice tutor.

Two things live here, and they are separated on purpose:

* **The wiring** -- which STT, which LLM, which TTS, and how the turn is
  decided. That is a handful of parameters and it is where the measured
  behaviour of the providers is encoded.
* **The grounding** -- the tool the LLM calls to look something up in the
  lecture, and the rules the answer has to follow.

The tutor answers **only** from the lecture. The retrieval layer already
decides whether the lecture covers a question at all, and the tool
propagates that verdict: when the lecture does not cover it, the tool
says so, and the tutor says so out loud rather than inventing something.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# The LiveKit plugins register themselves at import time, and the
# registration is rejected from a worker thread. Importing them here, at
# module level, keeps that on the main thread.
from livekit.agents import Agent, inference, stt, tokenize, tts
from livekit.agents.llm import function_tool
from livekit.plugins import gnani

from app.config import get_settings
from app.logging_setup import get_logger
from app.retrieval import fts, rerank

from .stt_gnani import TunedGnaniSTT

log = get_logger(__name__)

#: The prefix the backend uses for a lecture's room.
ROOM_PREFIX = "lecture-"

#: The name the worker registers under, and the name the backend's token
#: dispatches (``backend/app/api/livekit.py``). A test pins the two
#: constants together: a mismatch means rooms open without a tutor.
AGENT_NAME = "lecture-tutor"

#: The per-session suffix the backend appends to a room name: eight hex
#: characters that make every minted room name unique. Stripped before
#: the lecture id is read. (Regex, so the lecture id itself -- which
#: never contains a dash -- cannot be confused for the suffix.)
_SESSION_SUFFIX = re.compile(r"-[0-9a-f]{8}$")

#: The language a session falls back to when the room says nothing.
DEFAULT_LANGUAGE = "en-IN"

#: Every Timbre v2.5 voice, by language.
#:
#: A v2.5 voice has exactly one preferred language, so the voice has to
#: change with the language. The API *rejects* a voice that is not in
#: the model's list -- a wrong entry here is a runtime 400, not a
#: warning -- so this table is the contract with the service rather
#: than a preference.
TTS_VOICES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "en-IN": ("Kaveri", "Trupti", "Devika", "Pranav", "Shlok", "Girish"),
    "hi-IN": (
        "Nalini", "Bhavna", "Yashvi", "Urmila", "Jwala", "Chitra", "Ambuja",
        "Deepak", "Roopesh", "Vikrant", "Hemraj", "Jalaj", "Omkar",
    ),
    "hi-en": ("Poorvi",),
    "ta-IN": ("Asmita", "Trisha", "Brinda", "Vedika", "Noopur"),
    "te-IN": ("Suhana", "Lehara", "Lavanya", "Yukti", "Varuni"),
    "kn-IN": ("Saanvi", "Kavin"),
    "ml-IN": ("Reshma", "Riyaan"),
    "mr-IN": ("Zahira", "Ishaan"),
    "bn-IN": ("Kirra", "Dhruva"),
    "gu-IN": ("Falak", "Veera"),
    "pa-IN": ("Mehuli", "Zayan"),
}

#: The voice used for each language.
TTS_VOICES: dict[str, str] = {
    language: voices[0] for language, voices in TTS_VOICES_BY_LANGUAGE.items()
}

#: The voice used when a language is not in the table above.
FALLBACK_VOICE = "Nalini"


def lecture_id_from_room(room_name: str) -> str | None:
    """The lecture a room belongs to.

    The backend mints a token for a room called
    ``lecture-<lecture_id>-<session>`` (the older, suffix-less
    ``lecture-<lecture_id>`` shape parses too), so the room name is
    where the agent learns which lecture to ground its answers in.
    The session suffix is stripped before the id is read.
    """
    if not room_name:
        return None
    if not room_name.startswith(ROOM_PREFIX):
        return None
    lecture_id = _SESSION_SUFFIX.sub("", room_name[len(ROOM_PREFIX) :]).strip()
    return lecture_id or None


def voice_for_language(language: str | None) -> str:
    """The TTS voice for a language."""
    if not language:
        return FALLBACK_VOICE
    return TTS_VOICES.get(language, FALLBACK_VOICE)


def language_for_room(room_name: str, metadata: dict[str, Any] | None) -> str:
    """The language the student is speaking.

    The room metadata wins, because that is where the browser puts it; the
    language of the lecture is the next best guess.
    """
    if metadata:
        for key in ("language", "language_code", "stt_language"):
            value = metadata.get(key)
            if value:
                return str(value)
    return DEFAULT_LANGUAGE


INSTRUCTIONS = """You are a tutor for Indian college students, helping them revise a lecture they have just watched.

How you answer:
- Answer ONLY from the lecture. Use the search tool to look things up. If you have not looked something up, you do not know it.
- If the lecture does not cover what the student asked, say so plainly and briefly: "That is not in this lecture." Do not guess, and do not fill the gap with general knowledge.
- Answer in the language the student used. If they write Hinglish, answer in Hinglish; if they speak Telugu, answer in Telugu. Keep the lecturer's own technical terms.
- Keep it to two or four sentences. Your answer is spoken aloud, not read.
- If the lecturer referred to something shown on a board or a slide that was not spoken, say that the visual material is needed. Never reconstruct it.
- Be encouraging and direct. No filler, no praise for the question, no preamble."""


# --------------------------------------------------------------------------
# Latency markers
# --------------------------------------------------------------------------


@dataclass
class TurnTimings:
    """The markers the benchmark needs, for one turn.

    Every value is a ``time.monotonic()`` reading. The benchmark
    correlates these with the client's own readings through the room's
    data channel, because the two processes do not share a clock.
    """

    end_of_speech: float | None = None
    final_transcript: float | None = None
    retrieval_start: float | None = None
    retrieval_end: float | None = None
    llm_first_token: float | None = None
    llm_complete: float | None = None
    tts_first_byte: float | None = None
    tts_complete: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {
            "end_of_speech": self.end_of_speech,
            "final_transcript": self.final_transcript,
            "retrieval_start": self.retrieval_start,
            "retrieval_end": self.retrieval_end,
            "llm_first_token": self.llm_first_token,
            "llm_complete": self.llm_complete,
            "tts_first_byte": self.tts_first_byte,
            "tts_complete": self.tts_complete,
        }

    def durations(self) -> dict[str, float | None]:
        """The intervals between the markers, in seconds."""
        def gap(a: float | None, b: float | None) -> float | None:
            if a is None or b is None:
                return None
            return round(b - a, 4)

        return {
            "transcription_delay_s": gap(self.end_of_speech, self.final_transcript),
            "retrieval_s": gap(self.retrieval_start, self.retrieval_end),
            "llm_ttft_s": gap(self.retrieval_end, self.llm_first_token),
            "llm_total_s": gap(self.llm_first_token, self.llm_complete),
            "tts_ttfb_s": gap(self.llm_complete, self.tts_first_byte),
            "end_to_voice_s": gap(self.end_of_speech, self.tts_first_byte),
        }


@dataclass
class TutorState:
    """What the tutor needs to answer, and what it has measured."""

    lecture_id: str | None = None
    language: str = DEFAULT_LANGUAGE
    timings: TurnTimings = field(default_factory=TurnTimings)
    #: The question the student asked, for the log line.
    question: str = ""

    def reset_turn(self) -> None:
        self.timings = TurnTimings()
        self.question = ""


# --------------------------------------------------------------------------
# The grounding tool
# --------------------------------------------------------------------------


@dataclass
class GroundingResult:
    """What the lookup found."""

    covered: bool
    answer: str
    cited_segments: list[int] = field(default_factory=list)
    source_texts: list[str] = field(default_factory=list)
    note: str = ""


async def look_up_in_lecture(
    lecture_id: str | None, question: str, *, timings: TurnTimings | None = None
) -> GroundingResult:
    """Search the lecture for the answer to a question.

    This is the whole grounding rule in one place. If the lecture does not
    cover the question, the result says so -- and the tutor is told to say
    so too, rather than answering from its own knowledge.
    """
    if not lecture_id:
        return GroundingResult(
            covered=False,
            answer="",
            note="No lecture is selected for this session.",
        )

    if timings is not None:
        timings.retrieval_start = time.monotonic()

    from app import db as db_mod

    try:
        with db_mod.get_conn() as conn:
            candidates = await fts.hybrid_search(conn, question, lecture_id=lecture_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval failed", extra={"error": str(exc)[:200]})
        if timings is not None:
            timings.retrieval_end = time.monotonic()
        return GroundingResult(
            covered=False, answer="", note="The lecture could not be searched."
        )

    if timings is not None:
        timings.retrieval_end = time.monotonic()

    candidates = fts.merge_adjacent(candidates)[:MAX_CONTEXT_SEGMENTS]
    if not fts.looks_answerable(candidates):
        return GroundingResult(
            covered=False,
            answer="",
            note="The lecture does not cover this.",
        )

    result = await rerank.answer_grounded(question, candidates)
    return GroundingResult(
        covered=result.covered,
        answer=result.answer,
        cited_segments=result.cited_segments,
        source_texts=[s.text for s in result.retrieved],
        note=result.note,
    )


#: How many segments go into the model's context. The provider's
#: per-minute input-token budget is the limit; a few hundred characters
#: per segment keeps a useful amount of context inside it.
MAX_CONTEXT_SEGMENTS = 5


def make_lookup_tool(state: TutorState) -> Any:
    """The function tool the LLM calls to look things up.

    The tool is rebuilt per session because it closes over that
    session's state -- the lecture it is grounded in and the timings it
    is recording.
    """

    @function_tool(
        name="search_lecture",
        description=(
            "Search the lecture the student is revising for the passage that "
            "answers their question. Returns the answer if the lecture "
            "covers it, and says so plainly if it does not. Always call "
            "this before answering a question about the lecture."
        ),
    )
    async def search_lecture(query: str) -> dict[str, Any]:
        state.question = query
        result = await look_up_in_lecture(
            state.lecture_id, query, timings=state.timings
        )
        if not result.covered:
            return {
                "covered": False,
                "answer": "",
                "note": result.note or "The lecture does not cover this question.",
            }
        return {
            "covered": True,
            "answer": result.answer,
            "cited_segments": result.cited_segments,
            "source_texts": result.source_texts,
        }

    return search_lecture


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


def build_stt(language: str) -> stt.STT:
    """The tuned Gnani STT for a language."""
    settings = get_settings()
    return TunedGnaniSTT(
        language=language,
        api_key=settings.gnani_api_key,
    )


def build_tts(language: str) -> tts.StreamAdapter:
    """The Gnani Timbre TTS for a language, wrapped to speak incrementally.

    The plugin's own ``stream()`` collects the whole answer before it
    opens its WebSocket request, so without the wrapper the first audible
    byte would wait for the LLM to finish. ``StreamAdapter`` chunks the
    answer at sentence boundaries instead: each complete sentence becomes
    its own WebSocket request as soon as the next one starts arriving,
    and the first sentence plays while the rest of the answer is still
    being generated.

    The wrapper reports ``capabilities.streaming``, which is what stops
    the framework's ``tts_node`` from wrapping it a second time, and
    Gnani bills per audio hour (``docs/live-api-findings.md`` section 6),
    so one request per sentence costs the same credits as one request per
    answer.
    """
    settings = get_settings()
    gnani_tts = gnani.TTS(
        voice=voice_for_language(language),
        model="timbre-v2.5",
        language=language,
        api_key=settings.gnani_api_key,
        synthesize_method="websocket",
    )
    return tts.StreamAdapter(
        tts=gnani_tts,
        sentence_tokenizer=tokenize.blingfire.SentenceTokenizer(retain_format=True),
    )


def build_turn_handling() -> Any:
    """The turn-handling options.

    The turn detector is the *core* one, not the deprecated
    ``livekit-plugins-turn-detector`` package. ``v1-mini`` runs locally as
    a bundled ONNX model: no network, no extra package, and no credits.

    The endpointing delay is deliberately long. A student who pauses
    mid-sentence -- before a technical term, say -- must not have the
    question answered half-way.
    """
    from livekit.agents.voice.turn import TurnHandlingOptions

    return TurnHandlingOptions(
        turn_detection=inference.TurnDetector(version="v1-mini"),
        endpointing={
            "mode": "fixed",
            "min_delay": 0.6,
            "max_delay": 1.5,
        },
        interruption={
            "enabled": True,
            "resume_false_interruption": True,
            "false_interruption_timeout": 1.5,
            "min_duration": 0.5,
        },
    )


def build_session(state: TutorState) -> Any:
    """The AgentSession, configured for one student."""
    from livekit.agents.voice.agent_session import AgentSession

    from app.llm.factory import chat_llm

    session = AgentSession(
        stt=build_stt(state.language),
        llm=chat_llm(),
        tts=build_tts(state.language),
        vad=inference.VAD(model="silero", min_silence_duration=0.5),
        turn_handling=build_turn_handling(),
    )
    log.info(
        "tutor session configured",
        extra={
            "lecture_id": state.lecture_id,
            "language": state.language,
            "voice": voice_for_language(state.language),
        },
    )
    return session


def build_agent(state: TutorState) -> Agent:
    """The agent itself."""
    return Agent(
        instructions=INSTRUCTIONS,
        tools=[make_lookup_tool(state)],
    )
