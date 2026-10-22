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
import unicodedata
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
    #: The absolute cosine similarity from the semantic stage, when it
    #: ran. This is the value the abstention gate uses: a *relative*
    #: score always makes the best candidate look good, even when
    #: nothing in the corpus is relevant.
    semantic_score: float | None = None
    #: The 1-based position of this segment in the prompt's excerpt
    #: list. The model cites these numbers, so they are what a citation
    #: has to be validated against -- the source ``seq`` is a different
    #: number entirely.
    excerpt_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lecture_id": self.lecture_id,
            "seq": self.seq,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "score": round(self.score, 4),
            "semantic_score": (
                None if self.semantic_score is None else round(self.semantic_score, 4)
            ),
            "excerpt_index": self.excerpt_index,
            "visual_dependent": self.visual_dependent,
        }


def _is_word_char(ch: str) -> bool:
    """True for characters that belong inside a word.

    This has to include combining marks. Python's ``\\w`` does not: it
    matches ``પ`` but not the vowel sign ``ા``, so a regex-based split
    breaks ``પાણી`` into fragments. SQLite's ``unicode61`` tokenizer keeps
    the marks, so a query split by Python would never match the index.

    Verified against real text: ``પાણી``, ``पानी``, ``बाइनरी`` and ``ટ્રી``
    all survive as single tokens.
    """
    return ch.isalnum() or unicodedata.category(ch) in ("Mn", "Mc", "Me")


def tokenize(query: str) -> list[str]:
    """Split a query into terms FTS5 can use.

    FTS5's query syntax treats many characters as operators, so we quote
    each term. Words are split on whitespace and stripped of punctuation,
    which reproduces ``unicode61``'s token boundaries -- including the
    combining marks that a plain ``\\w`` split would destroy.
    """
    out: list[str] = []
    for raw in re.split(r"\s+", query):
        term = "".join(ch for ch in raw if _is_word_char(ch) or ch == "'")
        if len(term) < 2:
            continue
        if term.lower() in _STOPWORDS:
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
                # The strongest semantic score of the merged parts: the
                # abstention gate reads it, and losing it here would make
                # a merged result look like a lexical-only one.
                if seg.semantic_score is not None:
                    prev.semantic_score = max(
                        prev.semantic_score or 0.0, seg.semantic_score
                    )
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
                semantic_score=seg.semantic_score,
                excerpt_index=seg.excerpt_index,
            )
        )
    return merged


#: The absolute cosine similarity a match must reach to be considered
#: relevant.
#:
#: Measured against a Telugu lecture: a question the lecture covers
#: scored 0.698-0.709, and a question about baking a cake, the FIFA
#: World Cup, or a quadratic derivative scored 0.440-0.518. 0.60 sits
#: cleanly between the two groups.
SEMANTIC_RELEVANT_THRESHOLD = 0.60


def looks_answerable(
    segments: list[RetrievedSegment], *, min_score: float = DEFAULT_MIN_SCORE
) -> bool:
    """Whether retrieval found anything that plausibly answers the question.

    This is the gate that prevents fabrication. A question the lecture
    does not cover retrieves only weak matches, and we answer "not
    covered" instead of asking the model to guess.

    When the semantic stage ran, its *absolute* similarity decides: a
    relative score always makes the best candidate look good, even when
    nothing in the corpus is relevant.
    """
    if not segments:
        return False

    semantic = [s.semantic_score for s in segments if s.semantic_score is not None]
    if semantic:
        return max(semantic) >= SEMANTIC_RELEVANT_THRESHOLD

    return any(s.score >= min_score for s in segments)


#: How much the two retrieval stages are weighted when they are combined.
#: The semantic stage is the one that survives a change of script, so it
#: carries more weight; the lexical stage is exact on vocabulary the
#: student shares with the lecture.
SEMANTIC_WEIGHT = 0.7
LEXICAL_WEIGHT = 0.3


def combine_scores(
    lexical: list[RetrievedSegment], semantic: list[Any]
) -> list[RetrievedSegment]:
    """Merge the two retrieval stages.

    ``semantic`` is a list of ``(EmbeddedSegment, cosine)`` pairs.

    The ranking score is a weighted blend of the two stages, with the
    semantic stage's similarity *normalised within the result set* so a
    narrow score range does not swamp the lexical stage. The absolute
    cosine is kept separately in ``semantic_score``, because that -- not
    the ranking score -- is what decides whether the corpus covers the
    question at all.
    """
    if not semantic:
        return lexical

    by_id = {s.id: s for s in lexical}
    out: list[RetrievedSegment] = []

    semantic_scores = [score for _, score in semantic]
    max_semantic = max(semantic_scores) if semantic_scores else 0.0
    min_semantic = min(semantic_scores) if semantic_scores else 0.0
    span_semantic = (max_semantic - min_semantic) or 1.0

    for seg, raw_score in semantic:
        norm_semantic = (raw_score - min_semantic) / span_semantic
        lex = by_id.get(seg.segment_id)
        if lex is not None:
            combined = SEMANTIC_WEIGHT * norm_semantic + LEXICAL_WEIGHT * lex.score
        else:
            combined = SEMANTIC_WEIGHT * norm_semantic
        out.append(
            RetrievedSegment(
                id=seg.segment_id,
                lecture_id=seg.lecture_id,
                seq=seg.seq,
                start_seconds=seg.start_seconds,
                end_seconds=seg.end_seconds,
                text=seg.text,
                score=max(0.0, min(1.0, combined)),
                visual_dependent=seg.visual_dependent,
                semantic_score=raw_score,
            )
        )

    # Keep any lexical-only hits that the semantic stage missed.
    seen = {s.id for s in out}
    for seg in lexical:
        if seg.id not in seen:
            out.append(seg)

    out.sort(key=lambda s: s.score, reverse=True)
    return out


#: The character budget for the retrieved context.
#:
#: The provider's limit is expressed in tokens per minute, and Indic
#: text costs roughly 1.6 characters per token -- much worse than
#: English. Measured: Groq's limit for the chat model is 7000 input
#: tokens per minute, and five three-thousand-character topic segments
#: came to 11,080 tokens and was rejected. A budget of 3,000
#: characters is about 1,900 tokens, which leaves room for the answer
#: and for a second call inside the same minute.
CONTEXT_CHAR_BUDGET = 3000


def format_context(
    segments: list[RetrievedSegment], *, char_budget: int = CONTEXT_CHAR_BUDGET
) -> str:
    """Render retrieved segments for a prompt, with timestamps.

    The segments are added in rank order until the character budget is
    spent, so the most relevant material is always the first thing the
    model sees. A segment longer than the whole budget is truncated
    rather than dropped: a partial passage is more useful than none,
    and the model is told the passage is cut short.
    """
    from ..pipeline.segment import format_timestamp

    blocks: list[str] = []
    used = 0
    for i, seg in enumerate(segments, start=1):
        ts = (
            f"{format_timestamp(seg.start_seconds)}-"
            f"{format_timestamp(seg.end_seconds)}"
        )
        header = f"[{i}] (segment {seg.seq}, {ts})\n"
        remaining = char_budget - used - len(header)
        if remaining <= 100:
            break

        # The model cites the excerpt number, so the segment has to carry
        # it for a citation to be checked.
        seg.excerpt_index = i

        text = seg.text
        if len(text) > remaining:
            text = text[:remaining].rstrip() + " ... (passage truncated)"

        block = header + text
        if seg.visual_dependent:
            block += (
                "\n(NOTE: this part refers to visual material that is not in the "
                "audio, so do not reconstruct it.)"
            )
        blocks.append(block)
        used += len(block) + 2

    return "\n\n".join(blocks)


async def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    lecture_id: str | None = None,
    limit: int = 20,
) -> list[RetrievedSegment]:
    """Search with both stages and combine them.

    The lexical stage alone only works when the question shares a
    script with the lecture. The semantic stage is what lets a student
    ask in Hinglish about a lecture in another Indian language, so it
    carries the larger weight. If the embedding provider is unavailable,
    the lexical result is returned unchanged.
    """
    from . import embeddings as emb_mod

    lexical = lexical_search(conn, query, lecture_id=lecture_id, limit=limit)

    query_vector = await emb_mod.embed_question(query)
    if query_vector is None:
        return lexical

    semantic = emb_mod.semantic_search(
        conn, query_vector, lecture_id=lecture_id, limit=limit
    )
    if not semantic:
        return lexical

    return combine_scores(lexical, semantic)
