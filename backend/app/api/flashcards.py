"""Flashcards and spaced repetition.

The scheduling is deliberately simple and deterministic: an SM-2 style
interval, with a fixed ease per card, so a test can assert the exact
next due date. The brief asks for "basic spaced repetition", and
anything cleverer would be harder to trust than to use.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db as db_mod
from ..logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/flashcards", tags=["flashcards"])

#: The starting ease factor, as in SM-2.
DEFAULT_EASE = 2.5
#: The first interval, in days.
FIRST_INTERVAL_DAYS = 1.0
#: The second interval, in days.
SECOND_INTERVAL_DAYS = 6.0


class ReviewRequest(BaseModel):
    #: The grade the student gave themselves, as in SM-2.
    #: 0-2 means they got it wrong, 3-5 means they got it right.
    quality: int = Field(ge=0, le=5)


def schedule(repetitions: int, ease: float, quality: int, interval_days: float) -> tuple[int, float, float]:
    """The next review state.

    Returns ``(repetitions, ease, interval_days)``. Pure, so it can be
    tested against a fake clock.

    The rule is SM-2's, with one simplification: a wrong answer resets
    the repetitions and the interval, which is what a student revising
    for an exam needs.
    """
    if quality < 3:
        return 0, max(ease - 0.2, 1.3), FIRST_INTERVAL_DAYS

    ease = max(ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)), 1.3)
    repetitions += 1
    if repetitions == 1:
        interval = FIRST_INTERVAL_DAYS
    elif repetitions == 2:
        interval = SECOND_INTERVAL_DAYS
    else:
        interval = round(interval_days * ease, 2)
    return repetitions, round(ease, 2), interval


@router.get("")
async def list_cards(lecture_id: str | None = None, due_only: bool = False) -> dict[str, Any]:
    with db_mod.get_conn() as conn:
        sql = "SELECT * FROM flashcards"
        params: list[Any] = []
        if lecture_id:
            sql += " WHERE lecture_id=?"
            params.append(lecture_id)
        sql += " ORDER BY id"
        cards = conn.execute(sql, params).fetchall()

        reviews = {
            r["flashcard_id"]: r
            for r in conn.execute("SELECT * FROM flashcard_reviews").fetchall()
        }

    now = time.time()
    out: list[dict[str, Any]] = []
    for card in cards:
        review = reviews.get(card["id"])
        due_at = review["due_at"] if review else now
        out.append(
            {
                "id": card["id"],
                "lecture_id": card["lecture_id"],
                "question": card["question"],
                "answer": card["answer"],
                "due_at": due_at,
                "is_due": due_at <= now,
                "repetitions": review["repetitions"] if review else 0,
                "ease": review["ease"] if review else DEFAULT_EASE,
            }
        )

    if due_only:
        out = [c for c in out if c["is_due"]]
    return {"cards": out, "count": len(out)}


@router.post("/{card_id}/review")
async def review_card(card_id: str, request: ReviewRequest) -> dict[str, Any]:
    """Record a review and schedule the next one."""
    with db_mod.get_conn() as conn:
        card = conn.execute(
            "SELECT * FROM flashcards WHERE id=?", (card_id,)
        ).fetchone()
        if card is None:
            raise HTTPException(status_code=404, detail="No such flashcard")

        review = conn.execute(
            "SELECT * FROM flashcard_reviews WHERE flashcard_id=?", (card_id,)
        ).fetchone()

        repetitions = review["repetitions"] if review else 0
        ease = review["ease"] if review else DEFAULT_EASE
        interval = review["interval_days"] if review else 0.0

        new_reps, new_ease, new_interval = schedule(
            repetitions, ease, request.quality, interval
        )
        due_at = time.time() + new_interval * 24 * 3600

        conn.execute(
            """INSERT INTO flashcard_reviews
                 (flashcard_id, ease, interval_days, repetitions, lapses, due_at, last_reviewed_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(flashcard_id) DO UPDATE SET
                 ease=excluded.ease, interval_days=excluded.interval_days,
                 repetitions=excluded.repetitions,
                 lapses=excluded.lapses + (CASE WHEN ? < 3 THEN 1 ELSE 0 END),
                 due_at=excluded.due_at, last_reviewed_at=excluded.last_reviewed_at""",
            (
                card_id,
                new_ease,
                new_interval,
                new_reps,
                review["lapses"] if review else 0,
                due_at,
                time.time(),
                request.quality,
            ),
        )
        conn.commit()

    return {
        "id": card_id,
        "repetitions": new_reps,
        "ease": new_ease,
        "interval_days": new_interval,
        "due_at": due_at,
    }
