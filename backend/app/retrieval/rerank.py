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

from ..llm.factory import LLMError, complete_json
from ..logging_setup import get_logger
from .fts import RetrievedSegment, format_context

log = get_logger(__name__)

# Delay between a failed model call and the next attempt.
RETRY_DELAY_SECONDS = 0.5

class GroundedAnswerPayload(BaseModel):
    """The structured shape the model must return for a grounded answer."""

    covered: bool = False
    answer: str = ""
    cited_segments: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    note: str = ""

ANSWER_SYSTEM = """You answer a student's question using ONLY the lecture excerpts provided.

Rules you must follow:
- Use only what the excerpts say. If the answer is not in the excerpts, set `covered` to false and write a short, friendly message that the lecture does not cover this topic. Do not guess, and do not fill the gap with general knowledge.
- Answer in the language the student asked in (Hindi, Hinglish, Gujarati or English). Keep the teacher's technical vocabulary.
- Keep it concise: this is a spoken answer, so two to four sentences.
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
    """Reject answers that cite segments that do not exist."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["model did not return a JSON object"]
    cited = data.get("cited_segments") or []
    valid = {s.seq for s in segments}
    for c in cited:
        try:
            seq = int(c)
        except (TypeError, ValueError):
            problems.append(f"cited segment {c!r} is not an integer")
            continue
        if seq not in valid:
            problems.append(f"cited segment {seq} does not exist in the excerpts")
    if data.get("covered") and not cited:
        problems.append("answer claims coverage but cites nothing")
    return problems


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
            last_error = str(exc)
            await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
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

        return GroundedAnswer(
            covered=covered,
            answer=answer_text,
            cited_segments=[int(c) for c in (data.get("cited_segments") or [])],
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
