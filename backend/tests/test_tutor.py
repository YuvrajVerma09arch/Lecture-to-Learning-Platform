"""Voice tutor tests.

**These are mocked.** No room is opened, no websocket is connected, and no
provider is called. What is tested is the part of the tutor that is ours:
which lecture a session is grounded in, which voice speaks which
language, what the agent is told to do, and that the lookup refuses to
answer when the lecture does not cover the question.
"""

from __future__ import annotations

import time

import pytest

from agent import tutor
from app.retrieval import fts


class TestRoomNaming:
    def test_the_lecture_id_comes_from_the_room(self):
        assert tutor.lecture_id_from_room("lecture-lec_abc123") == "lec_abc123"

    def test_a_room_without_the_prefix_has_no_lecture(self):
        assert tutor.lecture_id_from_room("some-other-room") is None

    def test_an_empty_room_has_no_lecture(self):
        assert tutor.lecture_id_from_room("") is None

    def test_the_prefix_alone_has_no_lecture(self):
        assert tutor.lecture_id_from_room("lecture-") is None


class TestVoiceSelection:
    @pytest.mark.parametrize(
        ("language", "voice"),
        [
            ("hi-IN", "Nalini"),
            ("gu-IN", "Falak"),
            ("en-IN", "Kaveri"),
            ("hi-en", "Poorvi"),
        ],
    )
    def test_each_language_gets_its_own_voice(self, language, voice):
        assert tutor.voice_for_language(language) == voice

    def test_an_unknown_language_gets_the_fallback(self):
        assert tutor.voice_for_language("xx-XX") == tutor.FALLBACK_VOICE

    def test_no_language_gets_the_fallback(self):
        assert tutor.voice_for_language(None) == tutor.FALLBACK_VOICE


class TestLanguageSelection:
    def test_the_room_metadata_wins(self):
        assert (
            tutor.language_for_room("lecture-1", {"language": "gu-IN"}) == "gu-IN"
        )

    def test_the_metadata_key_is_flexible(self):
        assert (
            tutor.language_for_room("lecture-1", {"language_code": "te-IN"}) == "te-IN"
        )

    def test_without_metadata_it_falls_back(self):
        assert tutor.language_for_room("lecture-1", None) == tutor.DEFAULT_LANGUAGE


class TestInstructions:
    def test_the_instructions_require_the_lecture_to_be_searched(self):
        text = tutor.INSTRUCTIONS.lower()
        assert "only from the lecture" in text
        assert "search tool" in text

    def test_the_instructions_require_declining_when_not_covered(self):
        assert "not in this lecture" in tutor.INSTRUCTIONS.lower()

    def test_the_instructions_require_the_student_language(self):
        assert "language the student used" in tutor.INSTRUCTIONS.lower()

    def test_the_instructions_require_a_short_spoken_answer(self):
        assert "two or four sentences" in tutor.INSTRUCTIONS.lower()

    def test_the_instructions_forbid_reconstructing_visual_material(self):
        assert "never reconstruct" in tutor.INSTRUCTIONS.lower()


class TestTurnHandling:
    def test_the_turn_detector_is_the_local_core_one(self):
        handling = tutor.build_turn_handling()
        detector = handling["turn_detection"]
        assert type(detector).__name__ == "TurnDetector"
        # The deprecated plugin is a different class in a different module.
        assert type(detector).__module__.startswith("livekit.agents.inference")

    def test_the_endpointing_is_not_premature(self):
        handling = tutor.build_turn_handling()
        endpointing = handling["endpointing"]
        # A pause-heavy Hinglish question must not be answered half-way.
        assert endpointing["min_delay"] >= 0.5
        assert endpointing["max_delay"] > endpointing["min_delay"]

    def test_interruptions_are_enabled_and_recoverable(self):
        interruption = tutor.build_turn_handling()["interruption"]
        assert interruption["enabled"] is True
        assert interruption["resume_false_interruption"] is True


class TestTimings:
    def test_the_durations_are_derived_from_the_markers(self):
        timings = tutor.TurnTimings(
            end_of_speech=100.0,
            final_transcript=101.0,
            retrieval_start=101.1,
            retrieval_end=101.3,
            llm_first_token=101.8,
            llm_complete=102.5,
            tts_first_byte=103.0,
        )
        durations = timings.durations()
        assert durations["transcription_delay_s"] == pytest.approx(1.0)
        assert durations["retrieval_s"] == pytest.approx(0.2)
        assert durations["llm_ttft_s"] == pytest.approx(0.5)
        assert durations["tts_ttfb_s"] == pytest.approx(0.5)
        # End to voice: from the end of the student's speech to the first
        # playable audio.
        assert durations["end_to_voice_s"] == pytest.approx(3.0)

    def test_a_missing_marker_makes_its_duration_unknown(self):
        timings = tutor.TurnTimings(end_of_speech=100.0)
        assert timings.durations()["transcription_delay_s"] is None
        assert timings.durations()["end_to_voice_s"] is None

    def test_resetting_a_turn_clears_the_markers(self):
        state = tutor.TutorState()
        state.timings.final_transcript = time.monotonic()
        state.question = "a question"
        state.reset_turn()
        assert state.timings.final_transcript is None
        assert state.question == ""


class TestGrounding:
    async def test_it_declines_when_there_is_no_lecture(self):
        result = await tutor.look_up_in_lecture(None, "what is moksha")
        assert result.covered is False
        assert result.answer == ""

    async def test_it_declines_when_the_retrieval_finds_nothing(self, monkeypatch):
        async def nothing(*args, **kwargs):
            return []

        monkeypatch.setattr(tutor.fts, "hybrid_search", nothing)
        result = await tutor.look_up_in_lecture("lec_1", "how do I bake a cake")
        assert result.covered is False
        assert "does not cover" in result.note.lower()

    async def test_it_returns_the_grounded_answer(self, monkeypatch):
        segment = fts.RetrievedSegment(
            id=1,
            lecture_id="lec_1",
            seq=0,
            start_seconds=0.0,
            end_seconds=40.0,
            text="కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
            score=0.9,
            semantic_score=0.72,
        )

        async def found(*args, **kwargs):
            return [segment]

        async def grounded(question, candidates, **kwargs):
            return tutor.rerank.GroundedAnswer(
                covered=True,
                answer="They are different paths.",
                cited_segments=[0],
                retrieved=candidates,
                used_llm=True,
            )

        monkeypatch.setattr(tutor.fts, "hybrid_search", found)
        monkeypatch.setattr(tutor.rerank, "answer_grounded", grounded)

        result = await tutor.look_up_in_lecture("lec_1", "jnana vs karma yoga")
        assert result.covered is True
        assert result.answer == "They are different paths."
        assert result.cited_segments == [0]

    async def test_it_records_the_retrieval_timings(self, monkeypatch):
        timings = tutor.TurnTimings()

        async def found(*args, **kwargs):
            return []

        monkeypatch.setattr(tutor.fts, "hybrid_search", found)
        await tutor.look_up_in_lecture("lec_1", "question", timings=timings)
        assert timings.retrieval_start is not None
        assert timings.retrieval_end is not None
        assert timings.retrieval_end >= timings.retrieval_start


class TestLookupTool:
    async def test_the_tool_declines_when_the_lecture_does_not_cover_it(self, monkeypatch):
        async def nothing(*args, **kwargs):
            return []

        monkeypatch.setattr(tutor.fts, "hybrid_search", nothing)
        state = tutor.TutorState(lecture_id="lec_1")
        tool = tutor.make_lookup_tool(state)

        result = await tool(query="how do I bake a cake")
        assert result["covered"] is False
        assert result["answer"] == ""

    async def test_the_tool_returns_the_answer_when_the_lecture_covers_it(
        self, monkeypatch
    ):
        segment = fts.RetrievedSegment(
            id=1,
            lecture_id="lec_1",
            seq=0,
            start_seconds=0.0,
            end_seconds=40.0,
            text="some lecture text",
            score=0.9,
            semantic_score=0.72,
        )

        async def found(*args, **kwargs):
            return [segment]

        async def grounded(question, candidates, **kwargs):
            return tutor.rerank.GroundedAnswer(
                covered=True,
                answer="The answer.",
                cited_segments=[0],
                retrieved=candidates,
                used_llm=True,
            )

        monkeypatch.setattr(tutor.fts, "hybrid_search", found)
        monkeypatch.setattr(tutor.rerank, "answer_grounded", grounded)

        state = tutor.TutorState(lecture_id="lec_1")
        tool = tutor.make_lookup_tool(state)
        result = await tool(query="what is covered")

        assert result["covered"] is True
        assert result["answer"] == "The answer."
        assert result["cited_segments"] == [0]
        # The question is remembered, so the answer can be logged.
        assert state.question == "what is covered"


class TestSessionConstruction:
    def test_the_stt_is_the_tuned_gnani_one(self):
        stt = tutor.build_stt("hi-IN")
        assert type(stt).__name__ == "TunedGnaniSTT"

    def test_the_tts_uses_the_websocket_transport(self):
        tts = tutor.build_tts("hi-IN")
        assert type(tts).__name__ == "TTS"
        # The plugin's stream() always speaks the WebSocket, which is what
        # makes sentence-level incremental synthesis possible.
        assert hasattr(tts, "stream")

    def test_the_tts_voice_follows_the_language(self):
        assert tutor.build_tts("gu-IN")._opts.voice == "Falak"
        assert tutor.build_tts("hi-IN")._opts.voice == "Nalini"
