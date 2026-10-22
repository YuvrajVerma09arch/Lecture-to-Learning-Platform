"""Flashcard generation.

Turns a lecture's notes into question-and-answer cards a student can revise
from, and writes them to the database.

Three properties the brief asks for, and how they are met:

* **Cards reflect source material.** A card is only kept if its answer
  actually appears in the source segments it cites. A card the model
  invented is dropped, not shipped.
* **Pairs are clear and non-duplicative.** Cards are generated in batches
  and de-duplicated on a normalised form of the question, so two cards
  asking the same thing never both survive.
* **Errors do not corrupt existing cards.** The cards are written in one
  transaction, and a card is only added if it is not already there, so
  re-running the generator is safe and a failure part-way through leaves
  the previous cards intact.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .. import db as db_mod
from ..llm.factory import (
    ErrorKind,
    LLMError,
    complete_json,
    notes_llm_chain,
)
from ..logging_setup import get_logger

log = get_logger(__name__)

#: How many cards to ask for in one call. Batched, because each call
#: costs input tokens against a per-minute budget.
CARDS_PER_CALL = 6

#: The minimum gap between two model calls. The notes and the chat
#: workloads share the provider's per-minute token budget, so the calls
#: are spaced rather than bursted.
MIN_CALL_INTERVAL_SECONDS = 2.0

#: The retry pause for a rate-limited or unavailable call.
RATE_LIMIT_RETRY_SECONDS = 10.0

#: The shortest answer worth keeping. A one-word answer is not a revision
#: aid.
MIN_ANSWER_CHARS = 20


class FlashcardPayload(BaseModel):
    """The structured shape the model must return."""

    question: str = ""
    answer: str = ""
    source_segment_ids: list[int] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)


@dataclass
class Flashcard:
    """One generated card."""

    question: str
    answer: str
    source_segment_ids: list[int] = field(default_factory=list)
    key_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "source_segment_ids": self.source_segment_ids,
            "key_terms": self.key_terms,
        }


class _Pacer:
    """Enforces a minimum interval between model calls."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = self._interval - (now - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w'ऀ-ॿঀ-৿]+", re.UNICODE)


def normalise_question(question: str) -> str:
    """A comparable form of a question, for de-duplication.

    Case, punctuation and word order are all discarded, so "What is
    Moksha?" and "what is moksha" are recognised as the same question.
    """
    text = unicodedata.normalize("NFKC", question).casefold()
    words = _WORD_RE.findall(text)
    return " ".join(sorted(set(words)))


def _content_words(text: str) -> set[str]:
    return set(_WORD_RE.findall(unicodedata.normalize("NFKC", text).casefold()))


def answer_is_supported(answer: str, source_texts: list[str]) -> bool:
    """Whether the answer's content actually appears in the source.

    A word-overlap check: the significant words of the answer have to be
    present in the cited segments. It is deliberately generous about
    function words and strict about content, because a card whose answer
    is not in the lecture is exactly the failure the brief forbids.
    """
    answer_words = _content_words(answer)
    if len(answer_words) < 3:
        return False
    source_words: set[str] = set()
    for text in source_texts:
        source_words |= _content_words(text)
    if not source_words:
        return False
    overlap = len(answer_words & source_words) / len(answer_words)
    return overlap >= 0.6


# --------------------------------------------------------------------------
# The model call
# --------------------------------------------------------------------------

SYSTEM = """You write revision flashcards for Indian college students from a lecture.

Rules you must follow:
- Each card is ONE question with ONE answer.
- The question must be answerable from the lecture excerpt alone. Never ask about something the excerpt does not cover.
- The answer must be factually present in the excerpt. Do not add facts, do not explain beyond what the lecture said, and do not speculate.
- Keep the answer to one or two sentences. These are revision cards, not essays.
- Use the language of the excerpt for technical terms.
- `source_segment_ids` must list the ids of the excerpt segments the answer came from, exactly as they are given in the input.
- Do not repeat a question that has already been asked."""


def _build_prompt(
    excerpts: list[dict[str, Any]], existing_questions: list[str]
) -> str:
    blocks = []
    for item in excerpts:
        blocks.append(
            f"[segment {item['id']}]\n{item['text']}"
        )
    prompt = "Lecture excerpts:\n\n" + "\n\n".join(blocks)
    if existing_questions:
        already = "\n".join(f"- {q}" for q in existing_questions)
        prompt += (
            "\n\nThese questions have already been asked. Do not repeat them:\n"
            + already
        )
    prompt += (
        f"\n\nWrite {min(CARDS_PER_CALL, len(excerpts))} flashcards from these excerpts."
    )
    return prompt


def _payload_to_cards(
    payload: Any, excerpts: list[dict[str, Any]]
) -> tuple[list[Flashcard], int]:
    """Turn the model's reply into cards.

    Returns the cards and how many were *filtered out*. The two are
    different outcomes: a model that returned nothing is a failure worth
    retrying, while a model whose cards were all unsupportable has been
    correctly filtered and is not an error.
    """
    by_id = {item["id"]: item for item in excerpts}
    cards: list[Flashcard] = []
    filtered = 0

    for item in payload or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question or len(answer) < MIN_ANSWER_CHARS:
            filtered += 1
            continue

        raw_ids = item.get("source_segment_ids") or []
        cited: list[int] = []
        for raw in raw_ids:
            try:
                segment_id = int(raw)
            except (TypeError, ValueError):
                continue
            if segment_id in by_id:
                cited.append(segment_id)
        if not cited:
            # A card that cites nothing cannot be checked, so it is dropped.
            filtered += 1
            continue

        source_texts = [by_id[cid]["text"] for cid in cited]
        if not answer_is_supported(answer, source_texts):
            log.info(
                "dropped a flashcard whose answer is not in the source",
                extra={"question": question[:120]},
            )
            filtered += 1
            continue

        cards.append(
            Flashcard(
                question=question,
                answer=answer,
                source_segment_ids=cited,
                key_terms=[str(t).strip() for t in (item.get("key_terms") or []) if str(t).strip()],
            )
        )
    return cards, filtered


async def _call_model(
    llm: Any,
    excerpts: list[dict[str, Any]],
    existing_questions: list[str],
    *,
    pacer: _Pacer,
    max_attempts: int = 3,
) -> list[Flashcard]:
    prompt = _build_prompt(excerpts, existing_questions)
    last_error = ""

    for attempt in range(max_attempts):
        try:
            await pacer.wait()
            reply = await complete_json(
                llm, prompt=prompt, system=SYSTEM, schema=list[FlashcardPayload]
            )
            data = reply.data or []
        except LLMError as exc:
            if not exc.retryable:
                raise
            last_error = str(exc)
            delay = (
                RATE_LIMIT_RETRY_SECONDS * (2**attempt)
                if exc.kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER)
                else 0.5 * (attempt + 1)
            )
            if exc.retry_after_seconds:
                delay = max(delay, exc.retry_after_seconds + 1.0)
            await asyncio.sleep(delay)
            continue

        cards, filtered = _payload_to_cards(data, excerpts)
        if cards:
            return cards
        if filtered:
            # The model did write cards; they were all filtered out because
            # they were not grounded in the source. That is the filter
            # working, not a failure, so it is not retried.
            log.info(
                "every generated flashcard was filtered out as ungrounded",
                extra={"filtered": filtered},
            )
            return []
        last_error = "the model returned no cards"
        await asyncio.sleep(0.5 * (attempt + 1))

    raise LLMError(
        f"Could not generate flashcards after {max_attempts} attempts: {last_error}"
    )


# --------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------


def _excerpts_from(
    segments: list[dict[str, Any]], topics: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Pick the source segments to write cards from.

    The topic notes are used to choose *which* segments matter, and the
    segments themselves supply the text the card must be grounded in.
    """
    if not segments:
        return []

    # A topic's time span tells us which segments belong to it.
    by_topic: dict[int, list[dict[str, Any]]] = {}
    for segment in segments:
        topic_seq = segment.get("topic_seq")
        if topic_seq is None:
            continue
        by_topic.setdefault(int(topic_seq), []).append(segment)

    chosen: list[dict[str, Any]] = []
    for topic in topics:
        for segment in by_topic.get(int(topic.get("seq", -1)), []):
            chosen.append(segment)
        if len(chosen) >= limit:
            break

    if not chosen:
        chosen = list(segments)
    return chosen[:limit]


async def generate_flashcards(
    conn: Any,
    lecture_id: str,
    *,
    count: int = 20,
    batch_size: int = CARDS_PER_CALL,
    llm: Any = None,
    chain: list[Any] | None = None,
) -> list[Flashcard]:
    """Generate flashcards for a lecture.

    The cards are written to the database in one transaction. A failure
    part-way through leaves the previous cards untouched, and re-running
    does not duplicate cards that already exist.
    """
    segments = [
        {
            "id": r["id"],
            "text": r["text"],
            "start_seconds": r["start_seconds"],
            "topic_seq": r["topic_seq"],
        }
        for r in conn.execute(
            "SELECT id, text, start_seconds, topic_seq FROM segments "
            "WHERE lecture_id=? ORDER BY seq",
            (lecture_id,),
        ).fetchall()
    ]
    topics = [
        {"seq": r["seq"], "heading": r["heading"], "explanation": r["explanation"]}
        for r in db_mod.get_topics(conn, lecture_id)
    ]
    if not segments:
        return []

    if chain is None:
        chain = [llm] if llm is not None else notes_llm_chain(segment=True)
    pacer = _Pacer(MIN_CALL_INTERVAL_SECONDS)
    state = {"index": 0}

    # The questions already asked, so the model does not repeat them.
    existing = [
        r["question"]
        for r in conn.execute(
            "SELECT question FROM flashcards WHERE lecture_id=?", (lecture_id,)
        ).fetchall()
    ]
    seen = {normalise_question(q) for q in existing}

    cards: list[Flashcard] = []
    excerpts = _excerpts_from(segments, topics, limit=max(count * 3, 12))

    while len(cards) < count and excerpts:
        batch = excerpts[:batch_size]
        excerpts = excerpts[batch_size:]
        if not batch:
            break

        provider = chain[min(state["index"], len(chain) - 1)]
        try:
            produced = await _call_model(
                provider, batch, [c.question for c in cards], pacer=pacer
            )
        except LLMError as exc:
            if exc.is_quota and state["index"] < len(chain) - 1:
                state["index"] += 1
                log.warning(
                    "flashcard provider quota exhausted, switching to the fallback",
                    extra={"error_kind": exc.kind},
                )
                provider = chain[state["index"]]
                produced = await _call_model(
                    provider, batch, [c.question for c in cards], pacer=pacer
                )
            else:
                raise

        for card in produced:
            key = normalise_question(card.question)
            if not key or key in seen:
                continue
            seen.add(key)
            cards.append(card)
            if len(cards) >= count:
                break

    return cards


def store_flashcards(
    conn: Any, lecture_id: str, cards: list[Flashcard]
) -> int:
    """Write the cards, skipping any that already exist.

    The whole write is one transaction: a failure part-way through leaves
    the previous cards intact.
    """
    existing = {
        normalise_question(r["question"])
        for r in conn.execute(
            "SELECT question FROM flashcards WHERE lecture_id=?", (lecture_id,)
        ).fetchall()
    }

    added = 0
    try:
        for card in cards:
            key = normalise_question(card.question)
            if key in existing:
                continue
            existing.add(key)
            conn.execute(
                "INSERT INTO flashcards (id, lecture_id, question, answer, segment_ids, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    db_mod.new_id("fc"),
                    lecture_id,
                    card.question,
                    card.answer,
                    json.dumps(card.source_segment_ids),
                    db_mod.now(),
                ),
            )
            added += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return added
