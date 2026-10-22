"""Semantic retrieval over the lecture corpus.

The lexical stage alone cannot serve the product's core requirement: a
student asks in Hindi, Hinglish or Gujarati about a lecture that may be
in any Indian language. Measured against a Telugu lecture, a Telugu
question retrieved the right segment and an English or Hinglish
question retrieved nothing at all. Word overlap only helps when the
question shares a script with the lecture.

So the corpus is embedded as well, and the two stages are combined.
Embeddings are multilingual, so an English question matches a Telugu
passage and a Hinglish question matches both.

Cost and complexity are kept down deliberately:

* Embeddings are computed once per lecture, in one batched call, and
  stored in SQLite as raw float32. There is no vector database.
* The corpus is small -- a lecture is tens of segments -- so the search
  is a brute-force cosine over a few hundred vectors, in microseconds.
* If the embedding provider is unavailable, the lexical stage still
  works and the product degrades rather than failing.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

#: The embedding model. ``gemini-embedding-2`` is multilingual and has
#: its own quota bucket, separate from the generation quota that
#: Workload A shares.
EMBEDDING_MODEL = "gemini-embedding-2"

#: The vector width. 768 keeps the storage small and the search fast;
#: the model supports a larger width but there is nothing to gain here.
EMBEDDING_DIMENSIONS = 768

#: How many texts to embed in one request.
BATCH_SIZE = 32


class EmbeddingError(Exception):
    """An embedding call failed."""


@dataclass
class EmbeddedSegment:
    """A segment with its vector, ready for scoring."""

    segment_id: int
    lecture_id: str
    seq: int
    start_seconds: float
    end_seconds: float
    text: str
    visual_dependent: bool
    vector: list[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "lecture_id": self.lecture_id,
            "seq": self.seq,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "visual_dependent": self.visual_dependent,
        }


# --------------------------------------------------------------------------
# The provider call
# --------------------------------------------------------------------------


async def embed_texts(
    texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
) -> list[list[float]]:
    """Embed a list of texts.

    Uses the Gemini batch embedding endpoint directly rather than the
    LiveKit plugin: the plugin has no embedding support, and this is
    the only place in the codebase that needs embeddings.
    """
    if not texts:
        return []

    settings = get_settings()
    if not settings.has_google():
        raise EmbeddingError("GOOGLE_API_KEY is not configured")

    import aiohttp

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{EMBEDDING_MODEL}:batchEmbedContents"
    )
    headers = {"Content-Type": "application/json", "x-goog-api-key": settings.google_api_key}

    out: list[list[float]] = []
    timeout = aiohttp.ClientTimeout(connect=15, total=120)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            payload = {
                "requests": [
                    {
                        "model": f"models/{EMBEDDING_MODEL}",
                        "content": {"parts": [{"text": t}]},
                        "taskType": task_type,
                        "outputDimensionality": EMBEDDING_DIMENSIONS,
                    }
                    for t in batch
                ]
            }
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise EmbeddingError(f"Embedding request failed ({resp.status}): {body[:200]}")
                data = await resp.json()
            for item in data.get("embeddings", []):
                values = item.get("values")
                if not values:
                    raise EmbeddingError("The embedding response had no values")
                out.append([float(v) for v in values])

    if len(out) != len(texts):
        raise EmbeddingError(
            f"Expected {len(texts)} embeddings, got {len(out)}"
        )
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    """The cosine similarity of two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS segment_embeddings (
    segment_id  INTEGER PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dims        INTEGER NOT NULL,
    vector      BLOB NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def store_embeddings(
    conn: sqlite3.Connection,
    *,
    segment_ids: list[int],
    vectors: list[list[float]],
    model: str = EMBEDDING_MODEL,
) -> int:
    """Store one vector per segment, replacing any previous ones."""
    ensure_schema(conn)
    for segment_id, vector in zip(segment_ids, vectors, strict=False):
        conn.execute(
            "INSERT INTO segment_embeddings (segment_id, model, dims, vector) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(segment_id) DO UPDATE SET "
            "model=excluded.model, dims=excluded.dims, vector=excluded.vector",
            (segment_id, model, len(vector), _pack(vector)),
        )
    return len(segment_ids)


def load_embeddings(
    conn: sqlite3.Connection, *, lecture_id: str | None = None
) -> list[EmbeddedSegment]:
    """Load every embedded segment, with its vector."""
    ensure_schema(conn)
    sql = """
        SELECT s.id, s.lecture_id, s.seq, s.start_seconds, s.end_seconds,
               s.text, s.visual_dependent, e.vector
        FROM segment_embeddings e
        JOIN segments s ON s.id = e.segment_id
    """
    params: list[Any] = []
    if lecture_id is not None:
        sql += " WHERE s.lecture_id = ?"
        params.append(lecture_id)
    rows = conn.execute(sql, params).fetchall()
    return [
        EmbeddedSegment(
            segment_id=r["id"],
            lecture_id=r["lecture_id"],
            seq=r["seq"],
            start_seconds=float(r["start_seconds"]),
            end_seconds=float(r["end_seconds"]),
            text=r["text"],
            visual_dependent=bool(r["visual_dependent"]),
            vector=_unpack(r["vector"]),
        )
        for r in rows
    ]


def semantic_search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    lecture_id: str | None = None,
    limit: int = 20,
) -> list[tuple[EmbeddedSegment, float]]:
    """Rank the corpus by cosine similarity to the query."""
    corpus = load_embeddings(conn, lecture_id=lecture_id)
    if not corpus:
        return []
    scored = [(seg, cosine(query_vector, seg.vector)) for seg in corpus]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------


async def index_lecture(
    conn: sqlite3.Connection, *, lecture_id: str
) -> int:
    """Embed every segment of a lecture and store the vectors.

    Called once, after segmentation. A failure here is not fatal: the
    lexical stage still works, so the lecture stays usable.
    """
    rows = conn.execute(
        "SELECT id, text FROM segments WHERE lecture_id=? ORDER BY seq", (lecture_id,)
    ).fetchall()
    if not rows:
        return 0

    try:
        vectors = await embed_texts(
            [r["text"] for r in rows], task_type="RETRIEVAL_DOCUMENT"
        )
    except EmbeddingError as exc:
        log.warning(
            "could not embed the lecture, lexical retrieval only",
            extra={"lecture_id": lecture_id, "error": str(exc)[:200]},
        )
        return 0

    count = store_embeddings(
        conn, segment_ids=[r["id"] for r in rows], vectors=vectors
    )
    conn.commit()
    log.info(
        "lecture indexed",
        extra={"lecture_id": lecture_id, "segments": count, "model": EMBEDDING_MODEL},
    )
    return count


async def embed_question(question: str) -> list[float] | None:
    """Embed a query. Returns None when the provider is unavailable."""
    try:
        vectors = await embed_texts([question], task_type="RETRIEVAL_QUERY")
    except EmbeddingError as exc:
        log.warning(
            "could not embed the question, lexical retrieval only",
            extra={"error": str(exc)[:200]},
        )
        return None
    return vectors[0] if vectors else None
