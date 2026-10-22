"""Transcript normalisation, segmentation, and grounding detection."""

from __future__ import annotations

import pytest

from app.pipeline import segment as seg


class TestNormalisation:
    def test_adds_terminal_punctuation(self):
        assert seg.normalise_text("hello there") == "Hello there."

    def test_capitalises_the_first_letter(self):
        assert seg.normalise_text("today we study networks")[0].isupper()

    def test_preserves_existing_punctuation(self):
        assert seg.normalise_text("Is it true?") == "Is it true?"

    def test_collapses_whitespace(self):
        assert seg.normalise_text("  a   b \n c ") == "A b c."

    def test_leaves_an_existing_full_stop_alone(self):
        assert seg.normalise_text("Already done.") == "Already done."

    def test_handles_devanagari_danda(self):
        # The danda is a valid sentence terminator in Devanagari.
        assert seg.normalise_text("यह एक वाक्य है।") == "यह एक वाक्य है।"

    def test_does_not_change_the_words(self):
        original = "the osi model has seven layers"
        out = seg.normalise_text(original)
        assert out.lower().replace(".", "") == original

    def test_empty_input(self):
        assert seg.normalise_text("") == ""


class TestVisualDependency:
    """The grounding rule: audio has no visual channel."""

    @pytest.mark.parametrize(
        "text",
        [
            "ये वाला equation समझाओ",
            "इस diagram में क्या है",
            "यहाँ दिए गए चित्र को देखिए",
            "नीचे दिया गया सूत्र देखो",
            "आ वाला graph बताओ",
            "આ વાલો equation સમજાવો",
            "આમાં શું છે",
            "this equation is important",
            "as shown in the diagram",
            "look at this figure",
            "written here on the board",
        ],
    )
    def test_detects_visual_references(self, text):
        assert seg.is_visual_dependent(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "आज हम नेटवर्क के बारे में पढ़ेंगे",
            "the physical layer carries raw bits",
            "data link layer does framing and error detection",
            "this is a very important topic",
            "students should revise the seven layers",
        ],
    )
    def test_does_not_flag_ordinary_speech(self, text):
        assert seg.is_visual_dependent(text) is False

    def test_empty_text(self):
        assert seg.is_visual_dependent("") is False


class TestSegmentation:
    def _segments(self, count: int, step: float = 5.0) -> list[dict]:
        return [
            {
                "index": i,
                "start_seconds": i * step,
                "end_seconds": (i + 1) * step,
                "text": f"Sentence number {i} about the topic.",
            }
            for i in range(count)
        ]

    def test_groups_into_topics(self):
        result = seg.segment_transcript(self._segments(40))
        assert 0 < len(result) < 40
        assert result[0].index == 0
        assert [s.index for s in result] == list(range(len(result)))

    def test_timestamps_are_monotonic(self):
        result = seg.segment_transcript(self._segments(40))
        for prev, cur in zip(result, result[1:], strict=False):
            assert cur.start_seconds >= prev.end_seconds - 0.001

    def test_never_splits_a_source_segment(self):
        source = self._segments(40)
        result = seg.segment_transcript(source)
        # Every source segment's text must appear in exactly one output.
        joined = " ".join(s.text for s in result)
        for item in source:
            assert item["text"] in joined

    def test_visual_dependency_propagates(self):
        source = self._segments(20)
        source[5]["text"] = "ये वाला diagram देखिए जिसमें data flow दिखाया गया है"
        result = seg.segment_transcript(source)
        assert any(s.visual_dependent for s in result)

    def test_empty_input(self):
        assert seg.segment_transcript([]) == []

    def test_short_groups_are_merged_forward(self):
        # A single very short segment should not become its own topic.
        result = seg.segment_transcript(self._segments(1))
        assert len(result) == 1


class TestTimestampFormatting:
    def test_minutes_and_seconds(self):
        assert seg.format_timestamp(65.4) == "1:05"

    def test_hours(self):
        assert seg.format_timestamp(3725.0) == "1:02:05"

    def test_zero(self):
        assert seg.format_timestamp(0) == "0:00"

    def test_negative_is_clamped(self):
        assert seg.format_timestamp(-5) == "0:00"
