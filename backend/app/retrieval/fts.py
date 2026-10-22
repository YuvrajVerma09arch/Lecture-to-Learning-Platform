"""Retrieval over the lecture corpus.

Two stages:

1. **Lexical (FTS5).** SQLite's built-in full-text index over the segment
   text. The ``unicode61`` tokenizer was verified against real Devanagari,
   Gujarati and romanised Hinglish text during Phase 0 -- it matches
   ``पानी``, ``પાણી``, ``बाइनरी`` and ``binary`` correctly.

2. **Rerank.** The lexical stage is recall-oriented; the reranker (an LLM)
   picks the segments that actually answer the question and produces the
   grounded answer.

Why lexical first rather than embeddings: it needs no second service, costs
no credits, is deterministic, and a student's Hinglish question usually
shares vocabulary with a Hinglish lecture. Embeddings are a pluggable
third stage if measurement shows recall is too low.

The crucial behaviour is the **abstention path**. If retrieval finds
nothing that plausibly answers the question, the system must say the
lecture does not cover it -- not call the model and hope.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

# Below this score the retrieved text is not considered to be about the
# question, and the system abstains rather than guessing.
DEFAULT_MIN_SCORE = 0.05

# Words that carry no retrieval signal in a student question.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "do", "does",
    "did", "can", "could", "would", "should", "what", "which", "who", "how",
    "when", "where", "why", "in", "on", "at", "of", "to", "for", "and", "or",
    "but", "if", "then", "than", "that", "this", "these", "those", "it", "its",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "kya", "ka", "ki", "ke", "ko", "hai", "hain", "ho", "tha", "thi", "please", "explain", "tell", "sir", "ma'am", "mam", "teacher",
    "samjhao", "samjha", "batao", "bata", "kaise", "kyun",
}


@dataclass
class RetrievedSegment:
    """One candidate segment, with its score and provenance."""

    id: int
    lecture_id: str
    seq: int
    start_seconds: float
    end_seconds: float
    text: str
    score: float
    visual_dependent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lecture_id": self.lecture_id,
            "seq": self.seq,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "score": round(self.score, 4),
            "visual_dependent": self.visual_dependent,
        }


def tokenize(query: str) -> list[str]:
    """Split a query into terms FTS5 can use.

    FTS5's query syntax treats many characters as operators, so we quote
    each term. Devanagari and Gujarati are split on whitespace, which is
    what unicode61 does internally anyway.
    """
    terms = re.findall(r"[\w'ऀ-ॿ]+", query, flags=re.UNICODE)
    out: list[str] = []
    for term in terms:
        low = term.lower()
        if len(low) < 2 or low in _STOPWORDS:
            continue
        out.append(term)
    return out


def build_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression.

    Each term is quoted, so punctuation in the query cannot be interpreted
    as FTS5 syntax. Terms are OR-ed: we want recall from the lexical stage
    and let the reranker handle precision.
    """
    terms = tokenize(query)
    if not terms:
        return ""
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return " OR ".join(quoted)


def lexical_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    lecture_id: str | None = None,
    limit: int = 20,
) -> list[RetrievedSegment]:
    """Run the lexical stage.

    Returns candidates ordered by relevance. FTS5's ``bm25()`` returns a
    value where *lower is better* and it is negative for matches, so the
    score is normalised into a positive relevance number.
    """
    match = build_match_query(query)
    if not match:
        return []

    sql = """
        SELECT s.id, s.lecture_id, s.seq, s.start_seconds, s.end_seconds,
               s.text, s.visual_dependent,
               bm25(segments_fts) AS rank
        FROM segments_fts
        JOIN segments s ON s.id = segments_fts.rowid
        WHERE segments_fts MATCH ?
    """
    params: list[Any] = [match]
    if lecture_id is not None:
        sql += " AND s.lecture_id = ?"
        params.append(lecture_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # A malformed MATCH expression should degrade to "no results", not
        # take down the request.
        log.warning("fts query failed", extra={"error": str(exc)[:200]})
        return []

    if not rows:
        return []

    # bm25 is negative; the most negative is the best match. Normalise
    # against the best row so the best candidate scores 1.0.
    best = min(float(r["rank"]) for r in rows)
    out: list[RetrievedSegment] = []
    for r in rows:
        rank = float(r["rank"])
        score = (rank / best) if best != 0 else 1.0
        out.append(
            RetrievedSegment(
                id=int(r["id"]),
                lecture_id=r["lecture_id"],
                seq=int(r["seq"]),
                start_seconds=float(r["start_seconds"]),
                end_seconds=float(r["end_seconds"]),
                text=r["text"],
                score=max(0.0, min(1.0, score)),
                visual_dependent=bool(r["visual_dependent"]),
            )
        )
    return out


def merge_adjacent(
    segments: list[RetrievedSegment], *, max_gap_seconds: float = 12.0
) -> list[RetrievedSegment]:
    """Merge segments that are adjacent in the transcript.

    A question often spans two neighbouring segments; returning them as one
    block gives the model the context it needs.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s.lecture_id, s.seq))
    merged: list[RetrievedSegment] = []
    for seg in ordered:
        if merged:
            prev = merged[-1]
            if (
                prev.lecture_id == seg.lecture_id
                and seg.seq == prev.seq + 1
                and seg.start_seconds - prev.end_seconds <= max_gap_seconds
            ):
                prev.text = f"{prev.text} {seg.text}".strip()
                prev.end_seconds = seg.end_seconds
                prev.score = max(prev.score, seg.score)
                prev.visual_dependent = prev.visual_dependent or seg.visual_dependent
                continue
        merged.append(
            RetrievedSegment(
                id=seg.id,
                lecture_id=seg.lecture_id,
                seq=seg.seq,
                start_seconds=seg.start_seconds,
                end_seconds=seg.end_seconds,
                text=seg.text,
                score=seg.score,
                visual_dependent=seg.visual_dependent,
            )
        )
    return merged


def looks_answerable(
    segments: list[RetrievedSegment], *, min_score: float = DEFAULT_MIN_SCORE
) -> bool:
    """Whether retrieval found anything that plausibly answers the question.

    This is the gate that prevents fabrication. A question the lecture does
    not cover retrieves only weak matches, and we answer "not covered"
    instead of asking the model to guess.
    """
    if not segments:
        return False
    return any(s.score >= min_score for s in segments)


def format_context(segments: list[RetrievedSegment]) -> str:
    """Render retrieved segments for a prompt, with timestamps for citation."""
    from ..pipeline.segment import format_timestamp

    blocks: list[str] = []
    for i, seg in enumerate(segments, start=1):
        ts = (
            f"{format_timestamp(seg.start_seconds)}-"
            f"{format_timestamp(seg.end_seconds)}"
        )
        block = f"[{i}] (segment {seg.seq}, {ts})\n{seg.text}"
        if seg.visual_dependent:
            block += (
                "\n(NOTE: this part refers to visual material that is not in the "
                "audio, so do not reconstruct it.)"
            )
        blocks.append(block)
    return "\n\n".join(blocks)
