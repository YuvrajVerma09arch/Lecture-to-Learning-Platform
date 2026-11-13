"""Reranking and grounded answering.

The lexical stage gives candidates; this module decides which of them
actually answer the question and writes the answer.

Two rules drive the design:

* **Grounding.** The answer may only use the retrieved segments. The
  prompt forbids outside knowledge, and the answer must cite the segment
  numbers it used.
* **Abstention.** When the lecture does not cover the question, the
  system says so plainly instead of producing a plausible-sounding but
  unsupported answer.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..llm.factory import ErrorKind, LLMError, complete_json
from ..logging_setup import get_logger
from .fts import RetrievedSegment, format_context

log = get_logger(__name__)

# Delay between a failed model call and the next attempt.
RETRY_DELAY_SECONDS = 0.5

# Pause before retrying a rate-limited or unavailable call. The budget is
# per minute, so the retry has to land in a later window.
RATE_LIMIT_RETRY_SECONDS = 10.0

class GroundedAnswerPayload(BaseModel):
    """The structured shape the model must return for a grounded answer."""

    covered: bool = False
    answer: str = ""
    cited_segments: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    note: str = ""

ANSWER_SYSTEM = """You answer a student's question using ONLY the lecture excerpts provided.

The student only ever hears `answer`, so treat it as a spoken line:
- Plain speech, nothing else: no preamble, no reasoning, no narration of what you are doing ("The student asked ...", "Let me look ..."), no greeting, no sign-off, no emoji, no symbols, no markdown, no headings, no bullet lists, no bracketed citations.
- One to three short sentences, about 40 words at most. Give the answer and stop.
- Answer in the language the student asked in, written in that language's own script: Hindi in Devanagari, Gujarati in Gujarati script, Telugu in Telugu script, Hinglish in Latin letters, English in English. Asked for a language, switch into it and keep its script until they ask for another. Keep the lecturer's technical vocabulary.

Rules you must follow:
- Use only what the excerpts say. If the answer is not in the excerpts, set `covered` to false and write one short sentence saying that the lecture does not cover this. Do not guess, and do not fill the gap with general knowledge.
- `cited_segments` must list the numbers of the excerpts you actually used, exactly as they are numbered in the prompt.
- If an excerpt refers to something shown on screen or on a board that is not in the audio, say that the visual material is needed. Never reconstruct it.
- `confidence` is your confidence from 0 to 1 that the excerpts support the answer."""

NO_ANSWER_MESSAGE = (
    "This part isn't covered in the lecture I have. "
    "Try asking about a topic that was taught in it."
)


@dataclass
class GroundedAnswer:
    """The result of a grounded question."""

    covered: bool
    answer: str
    cited_segments: list[int] = field(default_factory=list)
    confidence: float = 0.0
    note: str = ""
    retrieved: list[RetrievedSegment] = field(default_factory=list)
    used_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "covered": self.covered,
            "answer": self.answer,
            "cited_segments": self.cited_segments,
            "confidence": self.confidence,
            "note": self.note,
            "retrieved": [s.to_dict() for s in self.retrieved],
            "used_llm": self.used_llm,
        }


def _build_prompt(question: str, segments: list[RetrievedSegment]) -> str:
    return (
        "Lecture excerpts:\n\n"
        f"{format_context(segments)}\n\n"
        f"Student question: {question}\n\n"
        "Answer using only these excerpts."
    )


def _validate(
    data: dict[str, Any], segments: list[RetrievedSegment]
) -> list[str]:
    """Reject answers that cite excerpts which were not provided.

    The model cites the *excerpt number* -- the ``[1]``, ``[2]`` labels
    in the prompt -- not the source segment's sequence number. Validating
    one against the other rejects correct answers, so the excerpt
    numbering is what a citation is checked against.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["model did not return a JSON object"]
    cited = data.get("cited_segments") or []
    valid = {s.excerpt_index for s in segments if s.excerpt_index is not None}
    for c in cited:
        try:
            index = int(c)
        except (TypeError, ValueError):
            problems.append(f"cited excerpt {c!r} is not an integer")
            continue
        if index not in valid:
            problems.append(f"cited excerpt {index} was not in the excerpts")
    if data.get("covered") and not cited:
        problems.append("answer claims coverage but cites nothing")
    return problems


def cited_source_segments(
    cited: list[int], segments: list[RetrievedSegment]
) -> list[RetrievedSegment]:
    """Map the excerpt numbers the model cited back to source segments."""
    by_excerpt = {s.excerpt_index: s for s in segments if s.excerpt_index is not None}
    out: list[RetrievedSegment] = []
    for c in cited:
        try:
            index = int(c)
        except (TypeError, ValueError):
            continue
        seg = by_excerpt.get(index)
        if seg is not None:
            out.append(seg)
    return out


async def answer_grounded(
    question: str,
    segments: list[RetrievedSegment],
    *,
    llm: Any = None,
    max_attempts: int = 2,
) -> GroundedAnswer:
    """Answer a question from retrieved segments, or abstain.

    ``segments`` is the output of the retrieval stage. If it is empty the
    function abstains without calling the model at all -- that is the
    cheapest and most reliable way to avoid a fabricated answer.
    """
    if not segments:
        return GroundedAnswer(
            covered=False,
            answer=NO_ANSWER_MESSAGE,
            note="No relevant lecture segments were found for this question.",
            retrieved=[],
            used_llm=False,
        )

    model = llm
    if model is None:
        from ..llm.factory import chat_llm

        model = chat_llm()

    prompt = _build_prompt(question, segments)
    last_error = ""

    for attempt in range(max_attempts):
        try:
            reply = await complete_json(
                model, prompt=prompt, system=ANSWER_SYSTEM, schema=GroundedAnswerPayload
            )
            data = reply.data or {}
        except LLMError as exc:
            if not exc.retryable:
                raise
            last_error = str(exc)
            # The provider says how long to wait, and for a per-minute
            # token budget that is the only correct answer.
            delay = RETRY_DELAY_SECONDS * (attempt + 1)
            if exc.kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER):
                delay = RATE_LIMIT_RETRY_SECONDS * (2**attempt)
            if exc.retry_after_seconds:
                delay = max(delay, exc.retry_after_seconds + 1.0)
            await asyncio.sleep(delay)
            continue

        problems = _validate(data, segments)
        if problems:
            last_error = "; ".join(problems)
            log.warning(
                "grounded answer rejected, retrying",
                extra={"attempt": attempt + 1, "problems": last_error[:300]},
            )
            prompt = _build_prompt(question, segments) + (
                "\n\nYour previous answer had these problems, which you must fix: "
                + last_error
            )
            continue

        covered = bool(data.get("covered"))
        answer_text = str(data.get("answer") or "").strip()
        if not answer_text:
            last_error = "empty answer"
            continue

        cited = [int(c) for c in (data.get("cited_segments") or [])]
        source_segments = cited_source_segments(cited, segments)
        return GroundedAnswer(
            covered=covered,
            # The model's answer is spoken and shown as-is, so it is
            # cleaned here rather than trusted to be clean.
            answer=clean_speech_text(answer_text),
            # The source sequence numbers, not the excerpt numbers: these
            # point at the audio.
            cited_segments=[s.seq for s in source_segments],
            confidence=float(data.get("confidence") or 0.0),
            note=str(data.get("note") or "").strip(),
            retrieved=segments,
            used_llm=True,
        )

    # The model could not produce a valid grounded answer. Abstaining is
    # strictly better than shipping an unvalidated answer.
    log.warning(
        "grounded answer failed validation, abstaining",
        extra={"error": last_error[:200], "question_length": len(question)},
    )
    return GroundedAnswer(
        covered=False,
        answer=NO_ANSWER_MESSAGE,
        note=f"The answer could not be verified against the lecture ({last_error[:120]}).",
        retrieved=segments,
        used_llm=False,
    )


def format_citations(cited_segments: list[int], segments: list[RetrievedSegment]) -> str:
    """Render a citation list for the UI."""
    by_seq = {s.seq: s for s in segments}
    from ..pipeline.segment import format_timestamp

    parts: list[str] = []
    for seq in cited_segments:
        seg = by_seq.get(seq)
        if seg is None:
            continue
        parts.append(f"{format_timestamp(seg.start_seconds)}")
    return ", ".join(parts)


def strip_citations(text: str) -> str:
    """Remove ``[1]``-style citation markers from spoken text.

    The answer is spoken aloud, so bracketed markers would be read out.
    """
    return re.sub(r"\s*\[\d+\]", "", text).strip()


#: The longest a spoken answer may be, in words. The prompt asks for
#: about forty; this is the safety net that catches a model which
#: ignores that and starts lecturing instead of answering.
MAX_ANSWER_WORDS = 60

#: Emoji, pictographs and the joiners/formatting characters that only
#: ever appear with them. Everything here is invisible to a voice and
#: noise on screen, so it goes.
_EMOJI = re.compile(
    "[\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\u20e3"
    "\U0001f000-\U0001faff]"
)

#: Markdown that survives into spoken text. Handled as pairs first so
#: that underscores inside identifiers (``max_tokens``) and asterisks
#: used as multiplication are left alone.
_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?!\w)")
_MARKDOWN_CODE = re.compile(r"`([^`\n]*)`")
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")
_MARKDOWN_NOISE = re.compile(
    r"^[ \t]{0,3}#{1,6}(?=[ \t]|$)[ \t]*|[ \t]*\*{3,}[ \t]*$", re.MULTILINE
)
#: Leading list markers. Numbered items are left alone: "2. The layer
#: does X" is a point the lecturer made, not a bullet to drop.
_BULLET = re.compile(r"^[ \t]{0,3}(?:[-\u2022\u2023\u25e6\u2013])[ \t]+", re.MULTILINE)

#: A filler word the model opens with, followed by the punctuation that
#: marks it as a filler rather than the sentence's first word.
_LEADING_FILLER = re.compile(
    r"^(?:sure|surely|okay|ok|well|right|actually|of course|great question|"
    r"good question|nice question|\u0905\u091a\u094d\u091b\u093e|"
    r"\u0939\u093e\u0901|\u091c\u0940|\u0ab8\u0abe\u0ab0\u0ac1\u0aae)"
    r"[,!:\u0964\u0965]+[ \t]*",
    re.IGNORECASE,
)


def _cap_words(text: str, max_words: int) -> str:
    """Cut an over-long answer down, at a sentence boundary if there is one."""
    words = text.split()
    if len(words) <= max_words:
        return text
    head = " ".join(words[:max_words])
    endings = list(re.finditer(r"[.!?!\u0964\u0965][\"')\]]?", head))
    if endings and endings[-1].start() > len(head) // 3:
        return head[: endings[-1].end()].strip()
    return head.rstrip(" ,;:-") + "."


#: A sentence that reports the model's own work instead of answering.
#: These are what a student sees as "the tutor thinking out loud", so
#: they are dropped from the front of an answer -- only while at least
#: one real sentence is left behind.
_NARRATION = re.compile(
    r"^(?:the student (?:asked|wants|said|is asking)|"
    r"let me|i (?:should|need to|will|'ll|must|don't need to|do not need to|have to)|"
    r"i'm going to|i am going to|should i|first,? i|"
    r"(?:okay|ok)[,!\s])",
    re.IGNORECASE,
)


def _drop_leading_narration(text: str) -> str:
    """Remove sentences that narrate instead of answering."""
    sentences = re.split(r"(?<=[.!?!\u0964\u0965])[\"')\]]?\s+", text.strip())
    if len(sentences) < 2:
        return text
    kept = [s for s in sentences if s]
    index = 0
    while index < len(kept) and index < 3 and _NARRATION.match(kept[index].strip()):
        index += 1
    if index == 0:
        return text
    rest = kept[index:]
    if not rest:
        # Every sentence was narration. The original is no worse than
        # an empty answer, so it is what goes back.
        return text
    return " ".join(rest).strip()


def clean_speech_text(text: str, *, max_words: int = MAX_ANSWER_WORDS) -> str:
    """Make a model answer safe to show and to speak.

    The answer travels two ways -- into the chat transcript and into a
    voice -- and in neither does a citation marker, a heading, an emoji
    or a forty-line preamble belong. This strips what a voice would trip
    over and caps what a student would have to read, without ever
    emptying the answer: an answer cleaned to nothing is worse than the
    one it started as.
    """
    original = text
    if not text or not text.strip():
        return text

    out = strip_citations(text)
    out = _MARKDOWN_BOLD.sub(r"\1", out)
    out = _MARKDOWN_ITALIC.sub(r"\1", out)
    out = _MARKDOWN_CODE.sub(r"\1", out)
    out = _MARKDOWN_LINK.sub(r"\1", out)
    out = _MARKDOWN_NOISE.sub("", out)
    out = _BULLET.sub("", out)
    out = out.replace("**", "")
    out = _EMOJI.sub(" ", out)
    out = re.sub(r"[ \t]*\n[ \t]*", " ", out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    out = _LEADING_FILLER.sub("", out, count=1)
    out = _drop_leading_narration(out)
    out = out.strip()
    # Dropping a filler can leave a sentence starting lower-case.
    if out[:1].islower():
        out = out[:1].upper() + out[1:]

    if not out:
        # Everything the model wrote was markup or emoji. The original
        # is at least readable, so hand that back rather than nothing.
        return original.strip()
    return _cap_words(out, max_words)
