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
#
# The rule is deliberately asymmetric. A false positive costs a segment an
# "unclear — needs the original visual" marker, which is a small cost. A
# false negative lets the model invent an equation the teacher only
# pointed at, which is the failure the brief explicitly forbids. So the
# detector leans towards flagging.
#
# Three tiers, from strongest signal to weakest:
#
#   Tier 1 -- an explicit reference to something on a surface: "on the
#             screen", "as shown in the diagram", "written here".
#   Tier 2 -- the deictic construction: "ये वाला", "आ વાલો", "this one".
#             In a lecture this almost always points at a board or slide.
#   Tier 3 -- a locative pointer ("here", "in this", "below") next to a
#             visual noun ("diagram", "चित्र", "સૂત્ર").
#
# A bare mention of a visual noun is NOT enough: a teacher who says the
# equation out loud has provided it in the audio, and flagging that would
# make the notes useless in a maths lecture.

# Tier 1: an explicit surface reference.
#
# Note on word boundaries: Python's ``\b`` is defined in terms of ``\w``,
# which does not include Indic combining marks. A pattern like ``\bઆમાં\b``
# therefore never matches, because the anusvara at the end is not a word
# character. The Indic alternatives below deliberately use no boundary.
_SURFACE_RE = re.compile(
    r"(on|upon|over|at)\s+the\s+(screen|board|slide|wall|blackboard|chart)"
    r"|\bas\s+(shown|you\s+can\s+see|displayed)"
    r"|\bwritten\s+(here|there|on\s+the\s+board)"
    r"|\blook\s+at\s+(this|that|the)"
    r"|\bhere\s+on\s+the\b"
    r"|\bthe\s+(diagram|graph|figure|image|table)\s+(below|above|on\s+the\s+left|on\s+the\s+right)"
    r"|\bshowing\s+(here|this|that)\b"
    # A demonstrative in front of a visual noun is a point at something.
    r"|\b(this|that)\s+(diagram|graph|figure|image|slide|equation|formula|chart)\b"
    # Hindi
    r"|(स्क्रीन|बोर्ड|स्लाइड|पर्दे)\s+(पर|में)"
    r"|(नीचे|ऊपर|यहाँ)\s+(दिया|दिए|लिखा|लिखे|बनाया|दिखाया|दिखाता)"
    r"|(दिखाया|दिखाता|देखिए|देखो)"
    r"|(रखा|लिखा|बनाया)\s+(है|हैं|था)"
    r"|इस(में|मे|का|की|के)"
    r"|इस\s*(चित्र|ग्राफ|आकृति|सूत्र|समीकरण|डायग्राम|पट)"
    # Gujarati
    r"|(સ્ક્રીન|બોર્ડ|સ્લાઇડ)\s+(પર|માં)"
    r"|(નીચે|ઉપર|અહીં)\s+(આપેલ|આપેલું|લખેલ|લખેલું|બનાવેલ|દેખાવ્યું)"
    r"|(દેખાવ્યું|દેખો)"
    r"|(રખેલું|લખેલું|બનાવેલું)\s+(છે|છું|હતું)"
    r"|આમાં"
    r"|એમાં"
    r"|આ\s*(ચિત્ર|ગ્રાફ|આકૃતિ|સૂત્ર|સ્લાઇડ)",
    re.IGNORECASE,
)

# Tier 2: the deictic construction, in both scripts.
_DEICTIC_RE = re.compile(
    r"(ये|इस|यह|उस|आ|इन|उन|एक)\s*(वाला|वाली|वाले|वालों|वालू)"
    r"|(આ|એ|તે|તેઓ)\s*(વાલ|વાલો|વાલી|વાલું)"
    r"|\bthis\s+one\b"
    r"|\bthat\s+one\b"
    r"|\bthe\s+one\s+(i|you)\s+(am|are)\s+(showing|pointing)\b",
    re.IGNORECASE,
)

# A demonstrative directly in front of a visual noun, in any combination of
# scripts. Teachers mix scripts freely ("इस diagram"), so the demonstrative
# set and the noun set are combined rather than paired per language.
_DEMONSTRATIVE_NOUN_RE = re.compile(
    r"(\bthis\b|\bthat\b|ये|इस|यह|उस|इन|આ|એ|તે)\s*"
    r"(diagram|graph|figure|image|slide|equation|formula|chart|"
    r"चित्र|ग्राफ|आकृति|सूत्र|समीकरण|डायग्राम|पट|स्लाइड|"
    r"ચિત્ર|ગ્રાફ|આકૃતિ|સૂત્ર|સ્લાઇડ)",
    re.IGNORECASE,
)

# Tier 3: a locative pointer, used only alongside a visual noun.
_LOCATIVE_RE = re.compile(
    r"(\bhere\b|\bthere\b|\bbelow\b|\babove\b|\bin\s+this\b|\bin\s+that\b"
    r"|इसमें|इसका|यहाँ|वहाँ|नीचे|ऊपर"
    r"|અહીં|ત્યાં|નીચે|ઉપર)",
    re.IGNORECASE,
)

# Nouns that name a visual artefact, in all three languages.
_VISUAL_NOUN_RE = re.compile(
    r"(diagram|graph|figure|image|picture|photo|chart|table|slide|screen|board|"
    r"animation|video|"
    r"चित्र|ग्राफ|आकृति|तस्वीर|सूत्र|समीकरण|पट|स्लाइड|डायग्राम"
    r"|ચિત્ર|ગ્રાફ|આકૃતિ|તસ્વીર|સૂત્ર|સ્લાઇડ)",
    re.IGNORECASE,
)

# An equation or formula is only visual if it is also pointed at: a teacher
# who says it aloud has given it to us in the audio.
_EQUATION_RE = re.compile(r"(equation|formula|समीकरण|સૂત્ર)", re.IGNORECASE)


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
    """True when the text refers to something only visible, not audible.

    See the tier comments above for the reasoning. The short version: we
    would rather mark a segment as needing the original visual material
    than let the model invent what the teacher pointed at.
    """
    if not text:
        return False

    # Tier 1: an explicit reference to a surface or a pointing instruction.
    if _SURFACE_RE.search(text):
        return True

    # A demonstrative in front of a visual noun, in any script mix.
    if _DEMONSTRATIVE_NOUN_RE.search(text):
        return True

    # Tier 2: the deictic construction on its own.
    if _DEICTIC_RE.search(text):
        return True

    # Tier 3: a locative pointer next to a visual noun.
    if _LOCATIVE_RE.search(text) and _VISUAL_NOUN_RE.search(text):
        return True

    # An equation or formula named with a pointer is visual; named on its
    # own it may have been spoken aloud, which the audio does carry.
    if _EQUATION_RE.search(text) and _DEICTIC_RE.search(text):
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
