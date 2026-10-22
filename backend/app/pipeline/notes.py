"""Note generation.

Two passes:

1. **Per segment.** Each timestamped topic segment is turned into a small
   structured note: a heading, a student-friendly explanation, key terms,
   and any equations stated *in speech*. This is a cheap model on a short
   input, so there are many calls, but each is independent -- a failure in
   one segment does not lose the others or the transcript.

2. **Merge.** All the segment notes go to a stronger model that holds the
   whole lecture in view and produces one coherent document with topic
   headings, subheadings, key terms, equations, and a summary.

The grounding rule is enforced in three places:

* Segments already flagged ``visual_dependent`` get an explicit instruction
  not to reconstruct the missing artefact.
* The prompt forbids inventing equations that were not spoken.
* After generation, a validation pass checks that every cited segment id
  exists, and that a note touching a visual-dependent segment carries an
  uncertainty marker. A note that fails is repaired or dropped, never
  shipped as-is.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..llm.factory import ErrorKind, LLMError, complete_json, notes_llm_chain
from ..logging_setup import get_logger
from .segment import Segment, format_timestamp

log = get_logger(__name__)

# Delay between a failed model call and the next attempt. Kept as a module
# constant so tests can shorten it.
RETRY_DELAY_SECONDS = 0.5

# Pause before retrying a rate-limited or unavailable call. The budget is
# per minute, so the retry has to land in a later window.
RATE_LIMIT_RETRY_SECONDS = 10.0

# How much of each segment's transcript goes into the merge prompt. The
# merge call is the largest request in the pipeline, and both providers
# enforce a tokens-per-minute budget that a 413 will blow.
MERGE_TRANSCRIPT_CHARS = 600

# Minimum gap between two model calls in the notes workload.
#
# Both providers we can use enforce a tokens-per-minute budget, and the
# free tiers are tight. Firing eight calls at once exhausted the budget
# and made the *merge* call -- which is the largest -- fail with a 413.
# Pacing the calls lets the window refill. The notes workload is a batch
# job, so a couple of seconds of spacing costs nothing that matters.
MIN_CALL_INTERVAL_SECONDS = 2.0


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


@dataclass
class SegmentNote:
    """Notes for one transcript segment."""

    segment_index: int
    heading: str
    explanation: str
    key_terms: list[str] = field(default_factory=list)
    equations: list[str] = field(default_factory=list)
    visual_dependent: bool = False
    uncertainty_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NotesDocument:
    """The merged, human-facing notes."""

    title: str
    summary: str
    sections: list[dict[str, Any]]
    key_terms: list[str]
    created_at: float

    def to_markdown(self) -> str:
        """Render as Markdown for the notes reader screen."""
        lines: list[str] = [f"# {self.title}", ""]
        if self.summary:
            lines += ["## Summary", "", self.summary, ""]

        for section in self.sections:
            heading = section.get("heading", "Untitled")
            lines.append(f"## {heading}")
            lines.append("")
            if section.get("start_seconds") is not None:
                lines.append(
                    f"*Source: {format_timestamp(float(section['start_seconds']))}"
                    f" – {format_timestamp(float(section.get('end_seconds') or 0))}*"
                )
                lines.append("")
            if section.get("visual_dependent"):
                note = section.get("uncertainty_note") or (
                    "This part refers to material shown on screen or on the board, "
                    "which is not available in the audio."
                )
                lines += [f"> **Needs the original visual material.** {note}", ""]

            body = section.get("explanation", "").strip()
            if body:
                lines += [body, ""]

            if section.get("subheadings"):
                for sub in section["subheadings"]:
                    lines.append(f"### {sub.get('heading', '')}")
                    lines.append("")
                    if sub.get("body"):
                        lines += [sub["body"].strip(), ""]
                    if sub.get("equations"):
                        lines.append("**Equations**")
                        lines.append("")
                        for eq in sub["equations"]:
                            lines.append(f"- `{eq}`")
                        lines.append("")

            if section.get("key_terms"):
                terms = ", ".join(section["key_terms"])
                lines.append(f"**Key terms:** {terms}")
                lines.append("")

            if section.get("equations"):
                lines.append("**Equations mentioned**")
                lines.append("")
                for eq in section["equations"]:
                    lines.append(f"- `{eq}`")
                lines.append("")

        if self.key_terms:
            lines += ["## Key terms", ""]
            for term in self.key_terms:
                lines.append(f"- {term}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"


class SegmentNotePayload(BaseModel):
    """The structured shape the model must return for one segment."""

    heading: str = ""
    explanation: str = ""
    key_terms: list[str] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)
    uncertainty_note: str = ""


class SubheadingPayload(BaseModel):
    heading: str = ""
    body: str = ""
    equations: list[str] = Field(default_factory=list)


class SectionPayload(BaseModel):
    heading: str = ""
    explanation: str = ""
    subheadings: list[SubheadingPayload] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)
    segment_indexes: list[int] = Field(default_factory=list)
    visual_dependent: bool = False
    uncertainty_note: str = ""


class MergedDocumentPayload(BaseModel):
    """The structured shape the model must return for the merged document."""

    title: str = ""
    summary: str = ""
    sections: list[SectionPayload] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)

SEGMENT_SYSTEM = """You turn a slice of a classroom lecture transcript into study notes for Indian college students.

Rules you must follow:
- Explain in clear, simple language a student can revise from. Keep the technical accuracy of the lecture.
- Use the language of the transcript for technical terms. Do not translate a technical term into English if the teacher used the local term.
- `key_terms` are the terms the student would need to know. Use the exact form the teacher used.
- `equations` contains ONLY equations that were actually spoken aloud in this segment, written in plain text (for example `F = ma`). If no equation was spoken, return an empty list.
- NEVER write an equation, diagram, or figure that the teacher referred to but did not speak out. Audio has no visual channel.
- If the teacher refers to something visible ("this equation", "this diagram", "ye wala"), set `uncertainty_note` to a short sentence naming what is missing, and say so in the explanation. Do not guess its content.
- Do not add facts that are not in the segment. Do not add examples the teacher did not give.
- Keep the explanation SHORT: three to five sentences, under 120 words. The note is merged into a larger document later, so detail here is wasted budget."""

MERGE_SYSTEM = """You merge per-segment lecture notes into one coherent study document for Indian college students.

Rules you must follow:
- Produce a flowing document with topic headings and subheadings, not a list of disconnected fragments.
- Group related segments under a shared heading where they belong to the same topic.
- The `explanation` for each section must be a genuine, self-contained explanation a student can revise from -- not a copy of the per-segment text.
- Preserve the teacher's technical vocabulary and the language of the lecture.
- `equations` must contain only equations that were spoken in the lecture. Never invent one.
- `segment_indexes` must list the transcript segment numbers that back the section. Use only numbers that appear in the input.
- If a section rests on material the teacher showed rather than said, set `visual_dependent` to true and explain in `uncertainty_note` what visual information is missing.
- The summary must be a genuine overview of what was taught, not a generic introduction.
- Do not add any fact that is not present in the segment notes."""


def _segment_prompt(segment: Segment) -> str:
    ts = f"[{format_timestamp(segment.start_seconds)} - {format_timestamp(segment.end_seconds)}]"
    visual_note = (
        "\n\nIMPORTANT: the teacher refers here to something visible on screen or on a "
        "board. You MUST NOT reconstruct it. State what is missing in "
        "`uncertainty_note` and in the explanation, and leave `equations` empty "
        "unless the equation was also spoken aloud."
        if segment.visual_dependent
        else ""
    )
    return (
        f"Transcript segment {segment.index} (timestamps {ts}):\n\n"
        f'"""\n{segment.text}\n"""\n'
        f"{visual_note}\n\n"
        "Return the structured note for this segment."
    )


def _validate_segment_note(note: SegmentNote, segment: Segment) -> list[str]:
    """Return a list of problems with a generated note (empty means valid)."""
    problems: list[str] = []
    if not note.heading.strip():
        problems.append("empty heading")
    if not note.explanation.strip():
        problems.append("empty explanation")
    if len(note.explanation) < 20:
        problems.append("explanation too short to be useful")
    if note.visual_dependent and not note.uncertainty_note.strip():
        problems.append("visual-dependent segment without an uncertainty note")
    if note.uncertainty_note and not note.visual_dependent:
        # An uncertainty note on a segment we did not flag: the model may
        # have spotted a visual reference we missed. That is worth keeping.
        note.visual_dependent = True
    return problems


async def generate_segment_note(
    llm: Any,
    segment: Segment,
    *,
    max_attempts: int = 3,
    pacer: _Pacer | None = None,
) -> SegmentNote:
    """Generate and validate the note for one segment.

    Retries on malformed or empty output, with a stricter instruction added
    on each attempt.

    A quota error is *not* retried. Google's free tier allows 20 requests
    per model per day, and its quota error is a 429 -- indistinguishable
    from a rate limit by status code alone. Retrying it burns the rest of
    the daily allowance and still fails, so it propagates immediately and
    the caller switches provider.
    """
    prompt = _segment_prompt(segment)
    last_error = ""
    last_kind = ErrorKind.UNKNOWN

    for attempt in range(max_attempts):
        try:
            if pacer is not None:
                await pacer.wait()
            reply = await complete_json(
                llm, prompt=prompt, system=SEGMENT_SYSTEM, schema=SegmentNotePayload
            )
            data = reply.data or {}
        except LLMError as exc:
            if not exc.retryable:
                raise
            last_error = str(exc)
            last_kind = exc.kind
            # A rate limit or a server error needs a real pause: the
            # budget is per minute, so a short retry lands in the same
            # window that already rejected us. When the provider says how
            # long to wait, that is the authoritative answer -- a
            # per-minute token budget refills on a schedule the caller
            # cannot guess.
            delay = RETRY_DELAY_SECONDS * (attempt + 1)
            if exc.kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER):
                delay = RATE_LIMIT_RETRY_SECONDS * (2**attempt)
            if exc.retry_after_seconds:
                delay = max(delay, exc.retry_after_seconds + 1.0)
            await asyncio.sleep(delay)
            continue

        note = SegmentNote(
            segment_index=segment.index,
            heading=str(data.get("heading") or "").strip(),
            explanation=str(data.get("explanation") or "").strip(),
            key_terms=[str(t).strip() for t in (data.get("key_terms") or []) if str(t).strip()],
            equations=[str(e).strip() for e in (data.get("equations") or []) if str(e).strip()],
            visual_dependent=segment.visual_dependent,
            uncertainty_note=str(data.get("uncertainty_note") or "").strip(),
        )

        problems = _validate_segment_note(note, segment)
        if not problems:
            return note

        last_error = "; ".join(problems)
        log.warning(
            "segment note rejected, retrying",
            extra={"segment_index": segment.index, "attempt": attempt + 1, "problems": last_error},
        )
        await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    raise LLMError(
        f"Could not produce a valid note for segment {segment.index} after "
        f"{max_attempts} attempts: {last_error}",
        kind=last_kind,
    )


async def generate_segment_notes(
    segments: list[Segment],
    *,
    concurrency: int = 1,
    llm: Any = None,
    chain: list[Any] | None = None,
) -> list[SegmentNote]:
    """Generate notes for every segment, concurrently.

    A failure in one segment is recorded and the rest continue -- the merge
    step is told which segments are missing rather than silently losing
    them.

    When the primary provider's quota is exhausted, the remaining segments
    are generated on the fallback provider rather than failing outright.
    """
    if chain is None:
        chain = [llm] if llm is not None else notes_llm_chain(segment=True)
    semaphore = asyncio.Semaphore(concurrency)
    pacer = _Pacer(MIN_CALL_INTERVAL_SECONDS)
    # Index into the provider chain. It only moves forward, and only when
    # a provider fails in a way another provider can fix.
    state = {"index": 0}

    async def one(segment: Segment) -> tuple[int, SegmentNote | None, str | None]:
        async with semaphore:
            # Pick the provider for this call. The chain only advances
            # when a provider fails in a way another provider can fix.
            provider = chain[min(state["index"], len(chain) - 1)]
            try:
                return (
                    segment.index,
                    await generate_segment_note(provider, segment, pacer=pacer),
                    None,
                )
            except LLMError as exc:
                if exc.is_quota and state["index"] < len(chain) - 1:
                    state["index"] += 1
                    log.warning(
                        "notes provider quota exhausted, switching to the fallback",
                        extra={
                            "segment_index": segment.index,
                            "error_kind": exc.kind,
                            "error": str(exc)[:200],
                        },
                    )
                    # Retry this segment on the next provider.
                    try:
                        provider = chain[state["index"]]
                        return (
                            segment.index,
                            await generate_segment_note(provider, segment, pacer=pacer),
                            None,
                        )
                    except LLMError as exc2:
                        return segment.index, None, str(exc2)
                return segment.index, None, str(exc)

    results = await asyncio.gather(*(one(s) for s in segments))
    notes: list[SegmentNote] = []
    for index, note, error in sorted(results, key=lambda r: r[0]):
        if note is not None:
            notes.append(note)
        else:
            log.error(
                "segment note failed",
                extra={"segment_index": index, "error": (error or "")[:200]},
            )
    return notes


def _merge_prompt(segments: list[Segment], notes: list[SegmentNote]) -> str:
    note_by_index = {n.segment_index: n for n in notes}
    blocks: list[str] = []
    for segment in segments:
        note = note_by_index.get(segment.index)
        if note is None:
            continue
        ts = f"{format_timestamp(segment.start_seconds)}-{format_timestamp(segment.end_seconds)}"
        transcript = segment.text
        if len(transcript) > MERGE_TRANSCRIPT_CHARS:
            transcript = transcript[:MERGE_TRANSCRIPT_CHARS] + " ..."
        block = (
            f"### Transcript segment {segment.index} ({ts})\n"
            f"Teacher said: {transcript}\n"
            f"Segment note heading: {note.heading}\n"
            f"Segment note: {note.explanation}"
        )
        if note.key_terms:
            block += f"\nKey terms: {', '.join(note.key_terms)}"
        if note.equations:
            block += f"\nEquations spoken: {'; '.join(note.equations)}"
        if note.visual_dependent:
            block += (
                f"\nNOTE: this segment refers to visual material that is not in the "
                f"audio. The teacher said: {note.uncertainty_note}"
            )
        blocks.append(block)

    return (
        "Merge the following transcript segments and their notes into one coherent "
        "study document.\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn the merged document. Cite the segment numbers you used in "
        "`segment_indexes`."
    )


def _validate_document(
    document: dict[str, Any],
    segments: list[Segment],
    notes: list[SegmentNote],
) -> list[str]:
    """Check the merged document for the failure modes we care about."""
    problems: list[str] = []
    valid_indexes = {s.index for s in segments}

    if not isinstance(document, dict):
        return ["model did not return a JSON object"]

    sections = document.get("sections") or []
    if not sections:
        problems.append("no sections produced")
        return problems

    # The brief requires a lecture summary. An empty one is a defect, not a
    # cosmetic gap, so it is rejected and regenerated.
    if not str(document.get("summary") or "").strip():
        problems.append("no summary produced")

    covered: set[int] = set()
    for i, section in enumerate(sections):
        heading = str(section.get("heading") or "").strip()
        if not heading:
            problems.append(f"section {i} has no heading")
        explanation = str(section.get("explanation") or "").strip()
        if len(explanation) < 20:
            problems.append(f"section {i} explanation too short")

        indexes = section.get("segment_indexes") or []
        if not indexes:
            problems.append(f"section {i} cites no segments")
        for idx in indexes:
            try:
                idx_int = int(idx)
            except (TypeError, ValueError):
                problems.append(f"section {i} has a non-integer segment index")
                continue
            if idx_int not in valid_indexes:
                problems.append(f"section {i} cites unknown segment {idx_int}")
            covered.add(idx_int)

        # A section that rests on a visual-dependent segment must carry the
        # uncertainty marker.
        source_segments = [s for s in segments if s.index in {int(x) for x in indexes if str(x).lstrip("-").isdigit()}]
        if any(s.visual_dependent for s in source_segments):
            if not section.get("visual_dependent"):
                problems.append(
                    f"section {i} rests on visual-dependent material but is not marked"
                )
            if not str(section.get("uncertainty_note") or "").strip():
                problems.append(f"section {i} has no uncertainty note")

    if not problems:
        missing = valid_indexes - covered
        # Some loss is tolerable if a segment note failed upstream; a large
        # loss means the merge dropped content.
        if len(missing) > max(2, len(valid_indexes) * 0.25):
            problems.append(f"{len(missing)} segments were not cited by any section")

    return problems


async def merge_notes(
    segments: list[Segment],
    notes: list[SegmentNote],
    *,
    title: str,
    llm: Any = None,
    max_attempts: int = 3,
    chain: list[Any] | None = None,
) -> NotesDocument:
    """Merge per-segment notes into one coherent document.

    Falls back to another provider if the primary's quota is exhausted.
    """
    if chain is None:
        chain = [llm] if llm is not None else notes_llm_chain(segment=False)
    state = {"index": 0}
    last_error = ""
    pacer = _Pacer(MIN_CALL_INTERVAL_SECONDS)

    for attempt in range(max_attempts):
        model = chain[min(state["index"], len(chain) - 1)]
        prompt = _merge_prompt(segments, notes)
        try:
            await pacer.wait()
            reply = await complete_json(
                model, prompt=prompt, system=MERGE_SYSTEM, schema=MergedDocumentPayload
            )
            data = reply.data or {}
        except LLMError as exc:
            if exc.is_quota and state["index"] < len(chain) - 1:
                state["index"] += 1
                log.warning(
                    "notes provider quota exhausted, switching to the fallback",
                    extra={"error_kind": exc.kind, "error": str(exc)[:200]},
                )
                continue
            if not exc.retryable:
                raise
            last_error = str(exc)
            delay = RETRY_DELAY_SECONDS * (attempt + 1)
            if exc.kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER):
                delay = RATE_LIMIT_RETRY_SECONDS * (2**attempt)
            if exc.retry_after_seconds:
                delay = max(delay, exc.retry_after_seconds + 1.0)
            await asyncio.sleep(delay)
            continue

        problems = _validate_document(data, segments, notes)
        if not problems:
            import time

            return NotesDocument(
                title=str(data.get("title") or title).strip() or title,
                summary=str(data.get("summary") or "").strip(),
                sections=_attach_timestamps(data.get("sections") or [], segments),
                key_terms=[str(t).strip() for t in (data.get("key_terms") or []) if str(t).strip()],
                created_at=time.time(),
            )

        last_error = "; ".join(problems)
        log.warning(
            "merged document rejected, retrying",
            extra={"attempt": attempt + 1, "problems": last_error[:300]},
        )
        # Feed the problems back so the retry can fix them.
        prompt = (
            _merge_prompt(segments, notes)
            + "\n\nYour previous attempt had these problems, which you must fix: "
            + "; ".join(problems)
        )
        await asyncio.sleep(RETRY_DELAY_SECONDS * (attempt + 1))

    raise LLMError(f"Could not merge notes into a valid document: {last_error}")


def _attach_timestamps(
    sections: list[dict[str, Any]], segments: list[Segment]
) -> list[dict[str, Any]]:
    """Give each merged section the time span of the segments it cites.

    The merge model is asked which transcript segments back each section,
    so the timestamps can be derived rather than asked for. Without this,
    a merged document has no links back to the audio -- and the brief
    requires every note to be traceable to its source segment.
    """
    by_index = {s.index: s for s in segments}
    out: list[dict[str, Any]] = []
    for section in sections:
        item = dict(section)
        if item.get("start_seconds") is None:
            cited = []
            for raw in item.get("segment_indexes") or []:
                try:
                    seg = by_index.get(int(raw))
                except (TypeError, ValueError):
                    continue
                if seg is not None:
                    cited.append(seg)
            if cited:
                item["start_seconds"] = min(s.start_seconds for s in cited)
                item["end_seconds"] = max(s.end_seconds for s in cited)
        out.append(item)
    return out


def build_fallback_document(
    segments: list[Segment],
    notes: list[SegmentNote],
    *,
    title: str,
) -> NotesDocument:
    """A last-resort document built from the per-segment notes.

    Used when the merge model keeps failing. It is not pretty, but it is
    complete and correct-by-construction, which beats losing the notes.
    """
    import time

    note_by_index = {n.segment_index: n for n in notes}
    sections: list[dict[str, Any]] = []
    for segment in segments:
        note = note_by_index.get(segment.index)
        if note is None:
            continue
        sections.append(
            {
                "heading": note.heading,
                "explanation": note.explanation,
                "subheadings": [],
                "key_terms": note.key_terms,
                "equations": note.equations,
                "segment_indexes": [segment.index],
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "visual_dependent": note.visual_dependent,
                "uncertainty_note": note.uncertainty_note,
            }
        )

    key_terms: list[str] = []
    seen: set[str] = set()
    for note in notes:
        for term in note.key_terms:
            low = term.lower()
            if low not in seen:
                seen.add(low)
                key_terms.append(term)

    return NotesDocument(
        title=title,
        summary=_derive_summary(sections),
        sections=sections,
        key_terms=key_terms,
        created_at=time.time(),
    )


def _derive_summary(sections: list[dict[str, Any]]) -> str:
    """Build a summary from the section headings and explanations.

    The brief requires a lecture summary. When the merge model cannot
    produce one, an empty summary would leave a required element missing,
    so the degraded path derives one from the material it does have. It
    is mechanical rather than written, and it says so.
    """
    if not sections:
        return ""
    headings = [str(s.get("heading") or "").strip() for s in sections]
    headings = [h for h in headings if h]
    if not headings:
        return ""

    parts: list[str] = []
    for section in sections:
        explanation = str(section.get("explanation") or "").strip()
        if not explanation:
            continue
        # The first sentence of each section, trimmed.
        sentence = explanation.split(". ")[0].strip().rstrip(".")
        if sentence:
            parts.append(sentence)
        if len(parts) >= 4:
            break

    if not parts:
        return "The lecture covers: " + "; ".join(headings) + "."

    return (
        "This lecture covers "
        + ", ".join(headings[:4])
        + ". In summary: "
        + ". ".join(parts)
        + "."
    )
