"""Transcript normalisation and topic segmentation.

Two jobs here:

1. **Normalise** the raw transcript. The batch API's ``full_transcript`` is a
   flat concatenation with no punctuation and no capitalisation, and the
   per-segment text is similarly bare. We restore sentence boundaries and
   capitalisation *without changing factual meaning* -- the brief is
   explicit that normalisation must not alter content.

2. **Segment** the transcript into timestamped topics. The segments are
   both the unit of note generation and the RAG corpus, so each one keeps
   its timestamps and can be traced back to the source audio.

Grounding: a segment that refers to something the teacher could see but the
audio cannot carry ("ye wala equation", "is diagram") is flagged
``visual_dependent``. Those segments are the ones most likely to produce a
fabricated equation, so the flag propagates into note generation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

# --- Grounding: references to visual material -----------------------------
# These are the phrases where a teacher points at something the audio cannot
# carry. Matching is deliberately broad: a false positive costs a segment an
# "unclear" marker, a false negative risks a fabricated equation.
_VISUAL_PATTERNS = [
    # Hindi
    r"ये वाला", r"ये वाली", r"इस वाला", r"इस वाली", r"यह वाला", r"यह वाली",
    r"उस वाला", r"उस वाली", r"इसमें", r"इसमे", r"नीचे दिया", r"नीचे लिखा",
    r"ऊपर दिया", r"यहाँ दिया", r"यहाँ लिखा", r"दिखाया", r"देखिए", r"देखो",
    r"रखा है", r"लिखा है", r"बनाया है",
    # Gujarati
    r"આ વાલ", r"આ વાલો", r"આ વાલી", r"એ વાલ", r"એ વાલો", r"એ વાલી",
    r"આમાં", r"આમે", r"નીચે આપેલ", r"નીચે લખેલ", r"ઉપર આપેલ",
    r"દેખાવ્યું", r"દેખો", r"રખેલું", r"લખેલું", r"બનાવેલું",
    # English
    r"\bthis (?:one|diagram|graph|equation|formula|figure|image|slide|table)\b",
    r"\bthat (?:diagram|graph|equation|formula|figure|image|slide|table)\b",
    r"\bas (?:shown|you can see|you can see here)\b",
    r"\bon the (?:screen|board|slide|board here)\b",
    r"\bhere on the (?:screen|board|slide)\b",
    r"\bthe (?:diagram|graph|equation|formula|figure) (?:below|above|on the)\b",
    r"\bwritten (?:here|there|on the board)\b",
    r"\blook at (?:this|the) (?:diagram|graph|equation|figure)\b",
    r"\bshowing (?:here|this)\b",
]
_VISUAL_RE = re.compile("|".join(_VISUAL_PATTERNS), re.IGNORECASE)

# A segment that only *mentions* these is not necessarily visual-dependent;
# this second pass requires some semantic context.
_VISUAL_CONTEXT_RE = re.compile(
    r"(equation|formula|diagram|graph|figure|table|slide|screen|board|chart|"
    r"image|photo|picture|animation|video)",
    re.IGNORECASE,
)


@dataclass
class Segment:
    """A timestamped topic segment."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str
    speaker_id: int | None = None
    visual_dependent: bool = False
    # How the segment was grouped, for debugging and for the notes writer.
    topic_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "visual_dependent": self.visual_dependent,
        }


def format_timestamp(seconds: float) -> str:
    """``[MM:SS]`` or ``[HH:MM:SS]`` for inline citation."""
    total = int(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def is_visual_dependent(text: str) -> bool:
    """True when the text refers to something only visible, not audible."""
    if not text:
        return False
    if _VISUAL_RE.search(text):
        # Require a visual noun nearby, so "this one" alone is not enough
        # but "this equation" is.
        if _VISUAL_CONTEXT_RE.search(text):
            return True
        # Hindi/Gujarati patterns already name the artefact implicitly.
        if re.search(r"(वाला|वाली|वाले|वालीं|વાલ|વાલો|વાલી)", text, re.IGNORECASE):
            return True
    return False


# --- Normalisation -------------------------------------------------------

_SENTENCE_END = re.compile(r"([.!?।॥])\s+")


def normalise_text(text: str) -> str:
    """Restore sentence boundaries and capitalisation.

    This is deliberately conservative. We add a full stop at the end of a
    run of text if it lacks terminal punctuation, capitalise the first
    letter, and collapse repeated whitespace. We never substitute words,
    merge or split sentences on content grounds, or "fix" what looks like a
    misrecognition -- that is the LLM's job during note generation, and
    doing it here would silently alter the transcript.
    """
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    # Collapse a run of spaces that the API sometimes leaves between words.
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)

    # Capitalise the first alphabetic character.
    for i, ch in enumerate(cleaned):
        if ch.isalpha():
            if ch.islower():
                cleaned = cleaned[:i] + ch.upper() + cleaned[i + 1 :]
            break

    # Ensure terminal punctuation so the text reads as a sentence.
    if cleaned[-1] not in ".!?।॥\"'":
        cleaned += "."

    return cleaned


def normalise_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise each segment's text in place-safe fashion."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        item = dict(seg)
        item["text"] = normalise_text(seg.get("text", ""))
        out.append(item)
    return out


# --- Segmentation ---------------------------------------------------------


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def segment_transcript(
    segments: list[dict[str, Any]],
    *,
    target_seconds: float = 180.0,
    min_seconds: float = 45.0,
    max_seconds: float = 300.0,
    max_words: int = 420,
) -> list[Segment]:
    """Group transcript segments into topic segments.

    The grouping respects the underlying segment boundaries -- we never
    split a segment that Gnani produced, because a segment boundary is the
    only place we know a speaker finished a thought.

    A new group starts when any of these hold:
      * the group has reached ``target_seconds`` of speech, and
      * the current segment starts a new sentence (capitalised), or
      * the group has exceeded ``max_seconds``.

    A group is also closed when it would exceed ``max_words``.

    Groups shorter than ``min_seconds`` are merged forward, so we do not
    produce a scatter of two-second "topics".
    """
    if not segments:
        return []

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_start = segments[0].get("start_seconds", 0.0)
    current_words = 0

    for seg in segments:
        text = (seg.get("text") or "").strip()
        seg_start = float(seg.get("start_seconds") or 0.0)
        seg_end = float(seg.get("end_seconds") or 0.0)
        words = _word_count(text)

        if not current:
            current = [seg]
            current_start = seg_start
            current_words = words
            continue

        span = seg_end - current_start
        starts_sentence = bool(text) and text[0].isupper()

        should_close = (
            span >= target_seconds
            and (starts_sentence or span >= max_seconds)
        ) or current_words + words > max_words

        if should_close:
            groups.append(current)
            current = [seg]
            current_start = seg_start
            current_words = words
        else:
            current.append(seg)
            current_words += words

    if current:
        groups.append(current)

    # Merge groups that are too short to be a topic.
    merged: list[list[dict[str, Any]]] = []
    for group in groups:
        span = float(group[-1].get("end_seconds") or 0) - float(group[0].get("start_seconds") or 0)
        if merged and span < min_seconds:
            merged[-1].extend(group)
        else:
            merged.append(group)

    out: list[Segment] = []
    for i, group in enumerate(merged):
        text = " ".join((s.get("text") or "").strip() for s in group if (s.get("text") or "").strip())
        text = normalise_text(text)
        # A group can span multiple speakers; record the first speaker and
        # flag the group if the speaker changes, which usually means a
        # student interjected.
        speakers = {s.get("speaker_id") for s in group if s.get("speaker_id") is not None}
        out.append(
            Segment(
                index=i,
                start_seconds=round(float(group[0].get("start_seconds") or 0.0), 3),
                end_seconds=round(float(group[-1].get("end_seconds") or 0.0), 3),
                text=text,
                speaker_id=next(iter(speakers)) if len(speakers) == 1 else None,
                visual_dependent=is_visual_dependent(text),
            )
        )

    log.info(
        "transcript segmented",
        extra={
            "raw_segment_count": len(segments),
            "topic_count": len(out),
            "visual_dependent_count": sum(1 for s in out if s.visual_dependent),
            "total_words": sum(_word_count(s.text) for s in out),
        },
    )
    return out
