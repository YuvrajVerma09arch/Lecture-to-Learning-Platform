"""The grounded chat API.

This is the text path: the same retrieval and the same grounded
answering the voice tutor uses, so a student can type a question when
the microphone is not available, and the demo can be driven from a
script.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db as db_mod
from ..logging_setup import get_logger
from ..retrieval import fts, rerank

log = get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: How many segments go into the model's context.
#:
#: The answer is spoken aloud, so it needs two or three good segments,
#: not twenty. A large context also breaks the provider's per-minute
#: input-token budget: measured, a five-segment Telugu context was
#: rejected with a 413.
MAX_CONTEXT_SEGMENTS = 5

#: The minimum score for a segment to be considered a match. Calibrated
#: so that a question about something the lecture never mentioned does
#: not reach the model.
MIN_SCORE = 0.15


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    lecture_id: str | None = None
    #: How many candidate segments to retrieve before the reranker picks.
    top_k: int = Field(default=20, ge=1, le=50)


class ChatResponse(BaseModel):
    covered: bool
    answer: str
    cited_segments: list[int] = Field(default_factory=list)
    confidence: float = 0.0
    note: str = ""
    retrieved: list[dict[str, Any]] = Field(default_factory=list)
    used_llm: bool = False
    timings: dict[str, float] = Field(default_factory=dict)


@router.post("", response_model=ChatResponse)
async def ask(request: ChatRequest) -> ChatResponse:
    """Answer a question from the lecture notes.

    When the lecture does not cover the question, this says so rather
    than inventing an answer.
    """
    started = time.monotonic()
    settings_timings: dict[str, float] = {}

    t0 = time.monotonic()
    with db_mod.get_conn() as conn:
        if request.lecture_id:
            lecture = conn.execute(
                "SELECT id FROM lectures WHERE id=?", (request.lecture_id,)
            ).fetchone()
            if lecture is None:
                raise HTTPException(status_code=404, detail="No such lecture")
        candidates = await fts.hybrid_search(
            conn, request.question, lecture_id=request.lecture_id, limit=request.top_k
        )
    settings_timings["retrieval_ms"] = (time.monotonic() - t0) * 1000

    candidates = fts.merge_adjacent(candidates)[:MAX_CONTEXT_SEGMENTS]

    if not fts.looks_answerable(candidates, min_score=MIN_SCORE):
        # Nothing plausibly matches, so the model is not asked at all.
        log.info(
            "abstained without calling the model",
            extra={"question_length": len(request.question)},
        )
        return ChatResponse(
            covered=False,
            answer=rerank.NO_ANSWER_MESSAGE,
            note="No relevant lecture segments were found for this question.",
            retrieved=[],
            used_llm=False,
            timings=settings_timings,
        )

    t0 = time.monotonic()
    result = await rerank.answer_grounded(request.question, candidates)
    settings_timings["answer_ms"] = (time.monotonic() - t0) * 1000
    settings_timings["total_ms"] = (time.monotonic() - started) * 1000

    return ChatResponse(
        covered=result.covered,
        answer=result.answer,
        cited_segments=result.cited_segments,
        confidence=result.confidence,
        note=result.note,
        retrieved=[s.to_dict() for s in result.retrieved],
        used_llm=result.used_llm,
        timings=settings_timings,
    )


@router.get("/search")
async def search(q: str, lecture_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Search the lecture corpus without asking the model.

    Useful for the notes reader's "jump to source" links, and for
    checking retrieval on its own.
    """
    with db_mod.get_conn() as conn:
        results = await fts.hybrid_search(
            conn, q, lecture_id=lecture_id, limit=limit
        )
    return {
        "query": q,
        "results": [r.to_dict() for r in results],
        "answerable": fts.looks_answerable(results, min_score=MIN_SCORE),
    }
