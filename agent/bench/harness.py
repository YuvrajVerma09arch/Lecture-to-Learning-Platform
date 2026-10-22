"""The benchmark runner and its statistics.

The statistics are the point of the harness, so they are defined
explicitly rather than left to a library:

* **p50** is the median: the middle of the distribution, so one slow
  turn does not move it.
* **p95** is the 95th percentile by the *nearest-rank* method: the
  value at index ``ceil(0.95 * n) - 1`` of the sorted samples. The
  nearest-rank method always returns a value that was actually
  observed, which matters when the sample count is small.
* A failed turn is **counted and excluded from the latency figures**,
  but it is reported. A run where half the turns failed is not a fast
  run.

Every figure is labelled with the conditions it was measured under, so
a number is never quoted without the model, the region and the sample
count that produced it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_setup import configure_logging, get_logger

from .driver import TurnRecord, run_turn
from .questions import Question, all_questions, summary

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile by the nearest-rank method.

    The nearest-rank method returns a value that was actually observed:
    the sample at index ``ceil(q * n) - 1`` of the sorted list. That
    matters with a small sample, where an interpolating percentile would
    report a number that was never measured.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil(q * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


def summarise(values: list[float]) -> dict[str, float | int | None]:
    """The headline figures for a list of measurements."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "min": None, "max": None}
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.50) or 0.0, 3),
        "p95": round(percentile(values, 0.95) or 0.0, 3),
        "mean": round(statistics.fmean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass
class RunConditions:
    """What the numbers were measured under."""

    lecture_id: str = ""
    livekit_region: str = "US"
    livekit_url: str = ""
    stt_model: str = "gnani-prisma-v2.5 (streaming)"
    tts_model: str = "timbre-v2.5"
    llm_notes: str = "gemini-3.5-flash"
    llm_chat: str = "qwen/qwen3.8-27b"
    turn_detector: str = "turn-detector-v1-mini (local)"
    concurrency: int = 1
    scripted: bool = True
    warmup_turns_excluded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lecture_id": self.lecture_id,
            "livekit_region": self.livekit_region,
            "livekit_url": self.livekit_url,
            "stt_model": self.stt_model,
            "tts_model": self.tts_model,
            "llm_notes": self.llm_notes,
            "llm_chat": self.llm_chat,
            "turn_detector": self.turn_detector,
            "concurrency": self.concurrency,
            "scripted": self.scripted,
            "warmup_turns_excluded": self.warmup_turns_excluded,
        }


#: The phrases a declining answer is allowed to contain. A decline is a
#: pass; a confident answer to a question the lecture never covered is
#: the fabrication the brief forbids.
DECLINE_PHRASES = (
    "not in this lecture",
    "does not cover",
    "isn't covered",
    "not covered",
    "did not cover",
    "lecture does not",
    "not in the lecture",
)


@dataclass
class BenchmarkResult:
    """The whole run."""

    conditions: RunConditions = field(default_factory=RunConditions)
    records: list[TurnRecord] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    # -- the figures ----------------------------------------------------

    def _latencies(self, predicate=None) -> list[float]:
        out: list[float] = []
        for record in self.records:
            if not record.ok:
                continue
            if predicate is not None and not predicate(record):
                continue
            value = record.time_to_first_audio
            if value is not None:
                out.append(value)
        return out

    @property
    def failures(self) -> list[TurnRecord]:
        return [r for r in self.records if not r.ok]

    @property
    def overall(self) -> dict[str, Any]:
        return summarise(self._latencies())

    @property
    def by_language(self) -> dict[str, Any]:
        out: dict[str, list[float]] = {}
        for record in self.records:
            if not record.ok:
                continue
            value = record.time_to_first_audio
            if value is None:
                continue
            out.setdefault(record.language, []).append(value)
        return {lang: summarise(values) for lang, values in sorted(out.items())}

    @property
    def by_case(self) -> dict[str, Any]:
        """Covered versus not-covered."""
        groups: dict[str, list[float]] = {"covered": [], "not_covered": []}
        for record in self.records:
            if not record.ok:
                continue
            value = record.time_to_first_audio
            if value is None:
                continue
            groups["covered" if record.expected_covered else "not_covered"].append(value)
        return {name: summarise(values) for name, values in groups.items()}

    @property
    def answer_durations(self) -> dict[str, Any]:
        return summarise(
            [r.answer_duration for r in self.records if r.ok and r.answer_duration is not None]
        )

    def abstention_accuracy(self) -> dict[str, Any]:
        """How the tutor handled the questions the lecture does not cover."""
        declined_correctly = 0
        answered_incorrectly = 0
        expected = 0
        for record in self.records:
            if record.expected_covered or not record.ok:
                continue
            expected += 1
            text = (record.agent_text or "").lower()
            if any(phrase in text for phrase in DECLINE_PHRASES):
                declined_correctly += 1
            else:
                answered_incorrectly += 1
        return {
            "expected_to_decline": expected,
            "declined_correctly": declined_correctly,
            "answered_incorrectly": answered_incorrectly,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "conditions": self.conditions.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(self.finished_at - self.started_at, 2),
            "overall": self.overall,
            "by_language": self.by_language,
            "by_case": self.by_case,
            "answer_duration": self.answer_durations,
            "abstention": self.abstention_accuracy(),
            "failures": [r.to_dict() for r in self.failures],
            "records": [r.to_dict() for r in self.records],
        }


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


async def run_benchmark(
    *,
    lecture_id: str,
    api_url: str,
    livekit_url: str,
    questions: list[Question] | None = None,
    cache_dir: Path | None = None,
    warmup: int = 1,
    on_progress: Any = None,
) -> BenchmarkResult:
    """Run the questions and collect the measurements."""
    settings = get_settings()
    result = BenchmarkResult()
    result.conditions = RunConditions(
        lecture_id=lecture_id,
        livekit_url=livekit_url,
        llm_notes=settings.llm_model_notes,
        llm_chat=settings.llm_model_chat,
        warmup_turns_excluded=warmup,
    )
    result.started_at = time.time()

    questions = questions if questions is not None else all_questions()

    # The first turn is a warm-up: the first call pays for a cold
    # connection and a cold model. It is run, and then excluded.
    for index, question in enumerate(questions):
        if on_progress is not None:
            on_progress(index, len(questions), question)
        record = await run_turn(
            question,
            lecture_id=lecture_id,
            api_url=api_url,
            livekit_url=livekit_url,
            cache_dir=cache_dir,
        )
        if index < warmup:
            log.info(
                "warm-up turn complete, excluded from the figures",
                extra={"question": question.text[:80], "ok": record.ok},
            )
            continue
        result.records.append(record)

    result.finished_at = time.time()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the voice latency benchmark against LiveKit Cloud."
    )
    parser.add_argument("--lecture-id", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--livekit-url", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0, help="run only the first N questions")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args()

    configure_logging("INFO")
    settings = get_settings()
    livekit_url = args.livekit_url or settings.livekit_url
    if not livekit_url:
        print("LIVEKIT_URL is not set", file=sys.stderr)
        return 1

    questions = all_questions()
    if args.limit:
        questions = questions[: args.limit]

    print("Benchmark plan")
    print(f"  lecture      : {args.lecture_id}")
    print(f"  livekit      : {livekit_url}")
    print(f"  api          : {args.api_url}")
    print(f"  questions    : {len(questions)}")
    print(f"  breakdown    : {summary()}")
    if args.dry_run:
        print("\nDry run: nothing was executed.")
        return 0

    cache = settings.cache_dir / "bench" / "questions"

    def progress(index: int, total: int, question: Question) -> None:
        print(f"  [{index + 1}/{total}] {question.language} {question.text[:60]}")

    result = asyncio.run(
        run_benchmark(
            lecture_id=args.lecture_id,
            api_url=args.api_url,
            livekit_url=livekit_url,
            questions=questions,
            cache_dir=cache,
            on_progress=progress,
        )
    )

    payload = result.to_dict()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\nWrote {args.out}")

    from .report import render

    print()
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
