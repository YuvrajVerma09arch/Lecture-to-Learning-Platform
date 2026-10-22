"""Note generation tests.

**These are mocked.** The LLM is replaced with a fake that returns
canned responses, so no test calls Gemini. The fake records every prompt,
which lets a test assert what the model was actually shown -- in
particular that a visual-dependent segment carries the instruction not to
invent the missing material.
"""

from __future__ import annotations

import pytest

from app.llm.factory import LLMError
from app.pipeline import notes as notes_mod
from app.pipeline.segment import Segment

from .conftest import FakeLLM


def _segment(index: int = 0, *, text: str = "The physical layer carries raw bits.",
             visual: bool = False) -> Segment:
    return Segment(
        index=index,
        start_seconds=float(index * 60),
        end_seconds=float(index * 60 + 45),
        text=text,
        visual_dependent=visual,
    )


VALID_NOTE = {
    "heading": "The Physical Layer",
    "explanation": "The physical layer transmits raw bits over the medium, "
                    "defining the electrical and mechanical properties.",
    "key_terms": ["physical layer", "raw bits"],
    "equations": [],
    "uncertainty_note": "",
}


def _prompt_text(llm: FakeLLM, index: int = 0) -> str:
    """The text actually shown to the model, from the recorded call."""
    ctx = llm.calls[index]["chat_ctx"]
    return "\n".join(
        item.text_content or "" for item in ctx.items
    )


class TestSegmentNoteGeneration:
    async def test_generates_a_valid_note(self):
        llm = FakeLLM(responses=[VALID_NOTE])
        note = await notes_mod.generate_segment_note(llm, _segment())
        assert note.heading == "The Physical Layer"
        assert "raw bits" in note.explanation
        assert note.key_terms == ["physical layer", "raw bits"]

    async def test_retries_on_malformed_json(self):
        llm = FakeLLM(responses=["this is not json at all", VALID_NOTE])
        note = await notes_mod.generate_segment_note(llm, _segment())
        assert note.heading == "The Physical Layer"
        assert len(llm.calls) == 2

    async def test_retries_on_an_empty_explanation(self):
        bad = dict(VALID_NOTE, explanation="")
        llm = FakeLLM(responses=[bad, VALID_NOTE])
        note = await notes_mod.generate_segment_note(llm, _segment())
        assert note.explanation

    async def test_raises_after_exhausting_attempts(self):
        llm = FakeLLM(responses=["nope", "still nope", "nope again"], default="nope")
        with pytest.raises(LLMError, match="Could not produce a valid note"):
            await notes_mod.generate_segment_note(llm, _segment(), max_attempts=3)

    async def test_visual_segment_prompt_forbids_invention(self):
        llm = FakeLLM(responses=[dict(VALID_NOTE, uncertainty_note="The diagram is missing.")])
        segment = _segment(text="ये वाला diagram देखिए", visual=True)
        await notes_mod.generate_segment_note(llm, segment)

        rendered = _prompt_text(llm)
        assert "MUST NOT reconstruct" in rendered
        assert "uncertainty_note" in rendered
        # The segment's own text is included, so the model can see what
        # was actually said.
        assert "ये वाला diagram देखिए" in rendered

    async def test_visual_note_without_an_uncertainty_note_is_rejected(self):
        """A visual-dependent segment must carry an explicit uncertainty."""
        bad = dict(VALID_NOTE, uncertainty_note="")
        llm = FakeLLM(responses=[bad, dict(VALID_NOTE, uncertainty_note="Missing.")])
        note = await notes_mod.generate_segment_note(llm, _segment(visual=True))
        assert note.uncertainty_note

    async def test_a_visual_note_on_a_plain_segment_upgrades_the_flag(self):
        """If the model spots a visual reference we missed, keep the flag."""
        llm = FakeLLM(responses=[dict(VALID_NOTE, uncertainty_note="Referenced a board.")])
        note = await notes_mod.generate_segment_note(llm, _segment(visual=False))
        assert note.visual_dependent is True


class TestMerge:
    def _segments(self) -> list[Segment]:
        return [
            _segment(0, text="The physical layer carries raw bits."),
            _segment(1, text="The data link layer does framing.", visual=False),
        ]

    def _notes(self) -> list[notes_mod.SegmentNote]:
        return [
            notes_mod.SegmentNote(
                segment_index=0,
                heading="Physical Layer",
                explanation="Carries raw bits over the medium.",
            ),
            notes_mod.SegmentNote(
                segment_index=1,
                heading="Data Link Layer",
                explanation="Handles framing and error detection.",
            ),
        ]

    async def test_merges_into_a_document(self):
        llm = FakeLLM(
            responses=[
                {
                    "title": "Networking Basics",
                    "summary": "An introduction to the OSI layers.",
                    "sections": [
                        {
                            "heading": "Lower layers",
                            "explanation": "The physical and data link layers move bits.",
                            "segment_indexes": [0, 1],
                            "key_terms": ["physical layer"],
                        }
                    ],
                    "key_terms": ["physical layer", "data link layer"],
                }
            ]
        )
        doc = await notes_mod.merge_notes(self._segments(), self._notes(), title="T", llm=llm)
        assert doc.title == "Networking Basics"
        assert doc.summary
        assert len(doc.sections) == 1

    async def test_rejects_a_section_citing_an_unknown_segment(self):
        llm = FakeLLM(
            responses=[
                {
                    "title": "T",
                    "summary": "s",
                    "sections": [
                        {
                            "heading": "H",
                            "explanation": "An explanation long enough to pass validation.",
                            "segment_indexes": [99],
                        }
                    ],
                    "key_terms": [],
                },
                {
                    "title": "T",
                    "summary": "s",
                    "sections": [
                        {
                            "heading": "H",
                            "explanation": "An explanation long enough to pass validation.",
                            "segment_indexes": [0, 1],
                        }
                    ],
                    "key_terms": [],
                },
            ]
        )
        doc = await notes_mod.merge_notes(self._segments(), self._notes(), title="T", llm=llm)
        assert doc.sections[0]["segment_indexes"] == [0, 1]

    async def test_rejects_a_visual_section_without_the_marker(self):
        segments = [_segment(0, visual=True)]
        seg_notes = [
            notes_mod.SegmentNote(
                segment_index=0,
                heading="H",
                explanation="Explains the diagram the teacher pointed at.",
                visual_dependent=True,
                uncertainty_note="The diagram is not in the audio.",
            )
        ]
        llm = FakeLLM(
            responses=[
                {
                    "title": "T",
                    "summary": "s",
                    "sections": [
                        {
                            "heading": "H",
                            "explanation": "An explanation long enough to pass validation.",
                            "segment_indexes": [0],
                            "visual_dependent": False,
                        }
                    ],
                    "key_terms": [],
                },
                {
                    "title": "T",
                    "summary": "s",
                    "sections": [
                        {
                            "heading": "H",
                            "explanation": "An explanation long enough to pass validation.",
                            "segment_indexes": [0],
                            "visual_dependent": True,
                            "uncertainty_note": "The diagram is not in the audio.",
                        }
                    ],
                    "key_terms": [],
                },
            ]
        )
        doc = await notes_mod.merge_notes(segments, seg_notes, title="T", llm=llm)
        assert doc.sections[0].get("visual_dependent") is True

    async def test_raises_when_it_cannot_produce_a_valid_document(self):
        llm = FakeLLM(responses=[{"title": "T", "summary": "s", "sections": [], "key_terms": []}])
        with pytest.raises(LLMError, match="Could not merge notes"):
            await notes_mod.merge_notes(self._segments(), self._notes(), title="T", llm=llm)


class TestFallbackDocument:
    def test_builds_a_document_from_segment_notes(self):
        segments = [_segment(0), _segment(1)]
        seg_notes = [
            notes_mod.SegmentNote(
                segment_index=0, heading="A", explanation="First point explained.",
                key_terms=["x"],
            ),
            notes_mod.SegmentNote(
                segment_index=1, heading="B", explanation="Second point explained.",
                key_terms=["x", "y"],
            ),
        ]
        doc = notes_mod.build_fallback_document(segments, seg_notes, title="Fallback")
        assert len(doc.sections) == 2
        # Duplicate key terms are collapsed.
        assert doc.key_terms == ["x", "y"]

    def test_markdown_includes_the_uncertainty_marker(self):
        segments = [_segment(0, visual=True)]
        seg_notes = [
            notes_mod.SegmentNote(
                segment_index=0,
                heading="A",
                explanation="Explains a diagram.",
                visual_dependent=True,
                uncertainty_note="The diagram is not in the audio.",
            )
        ]
        doc = notes_mod.build_fallback_document(segments, seg_notes, title="F")
        md = doc.to_markdown()
        assert "Needs the original visual material" in md
        assert "The diagram is not in the audio." in md
        assert "# F" in md
