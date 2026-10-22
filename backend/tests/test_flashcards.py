"""Flashcard generation tests.

**These are mocked.** The LLM is replaced with a fake, so no provider is
called. What is being tested is the part that is ours: the grounding
check, the de-duplication, and the promise that a failure never corrupts
the cards that already exist.
"""

from __future__ import annotations

import json

import pytest

from app import db as db_mod
from app.llm.factory import LLMError
from app.pipeline import flashcards as cards_mod

from .conftest import FakeLLM


def _card(question: str, answer: str, ids: list[int], terms: list[str] | None = None):
    return {
        "question": question,
        "answer": answer,
        "source_segment_ids": ids,
        "key_terms": terms or [],
    }


@pytest.fixture
def lecture():
    """A lecture with source segments and a topic."""
    with db_mod.get_conn() as conn:
        conn.execute(
            """INSERT INTO lectures (id, title, original_filename, content_hash,
                                    audio_seconds, language_code, status, created_at, updated_at)
               VALUES ('lec_fc','Flashcard Lecture','t.mp3','h',60.0,'te-IN','ready',0,0)"""
        )
        texts = [
            "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
            "వివేకానంద సాంగ్స్ అయిపోకుండా తేడాని తెలుసుకుంటూ.",
            "అజ్ఞానం వల్ల పురుషుడు ప్రకృతితో గుర్తించుకుంటాడు.",
        ]
        for seq, text in enumerate(texts):
            conn.execute(
                """INSERT INTO segments (lecture_id, seq, start_seconds, end_seconds,
                                         text, speaker_id, visual_dependent, topic_seq)
                   VALUES ('lec_fc',?,?,?,?,1,0,0)""",
                (seq, seq * 20.0, (seq + 1) * 20.0, text),
            )
        conn.execute(
            "INSERT INTO note_topics (lecture_id, seq, start_seconds, end_seconds, "
            "text, visual_dependent, heading, explanation) "
            "VALUES ('lec_fc',0,0.0,60.0,'topic text',0,'Karma and Jnana Yoga','They differ.')"
        )
        conn.commit()
    return "lec_fc"


class TestGrounding:
    def test_an_answer_in_the_source_is_supported(self):
        source = ["కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది."]
        assert cards_mod.answer_is_supported(
            "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది", source
        )

    def test_an_invented_answer_is_not_supported(self):
        source = ["కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది."]
        assert not cards_mod.answer_is_supported(
            "The Battle of Hastings was fought in 1066 near Normandy.", source
        )

    def test_a_very_short_answer_is_not_supported(self):
        assert not cards_mod.answer_is_supported("Yes", ["some source text here"])


class TestDeduplication:
    def test_questions_are_normalised_for_comparison(self):
        assert cards_mod.normalise_question("What is Moksha?") == cards_mod.normalise_question(
            "what is moksha"
        )

    def test_different_questions_stay_different(self):
        assert cards_mod.normalise_question("What is Moksha?") != cards_mod.normalise_question(
            "What is Karma Yoga?"
        )


class TestGeneration:
    async def test_a_well_formed_reply_produces_cards(self, lecture):
        llm = FakeLLM(
            default=[
                _card(
                    "What is the difference between Karma Yoga and Jnana Yoga?",
                    "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
                    [1],
                )
            ]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
        assert len(cards) == 1
        assert cards[0].source_segment_ids == [1]

    async def test_duplicates_are_removed(self, lecture):
        llm = FakeLLM(
            default=[
                _card("What is Moksha?", "అజ్ఞానం వల్ల పురుషుడు ప్రకృతితో గుర్తించుకుంటాడు.", [3]),
                _card("what is moksha", "అజ్ఞానం వల్ల పురుషుడు ప్రకృతితో గుర్తించుకుంటాడు.", [3]),
            ]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
        assert len(cards) == 1

    async def test_a_card_whose_answer_is_not_in_the_source_is_dropped(self, lecture):
        llm = FakeLLM(
            default=[
                _card(
                    "Who won the Battle of Hastings?",
                    "William the Conqueror won it in 1066 at Hastings.",
                    [1],
                )
            ]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
        assert cards == []

    async def test_a_card_citing_nothing_is_dropped(self, lecture):
        llm = FakeLLM(default=[_card("What is Moksha?", "అజ్ఞానం వల్ల పురుషుడు గుర్తించుకుంటాడు.", [])])
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
        assert cards == []

    async def test_a_card_citing_an_unknown_segment_is_dropped(self, lecture):
        llm = FakeLLM(
            default=[_card("What is Moksha?", "అజ్ఞానం వల్ల పురుషుడు గుర్తించుకుంటాడు.", [999])]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
        assert cards == []

    async def test_malformed_output_is_retried_and_then_reported(self, lecture):
        llm = FakeLLM(responses=["not json at all", "still not json"], default="nope")
        with db_mod.get_conn() as conn:
            with pytest.raises(LLMError):
                await cards_mod.generate_flashcards(conn, lecture, llm=llm, batch_size=6)
        assert len(llm.calls) >= 2

    async def test_a_quota_error_switches_to_the_fallback(self, lecture):
        class QuotaLLM(FakeLLM):
            def chat(self, *, chat_ctx, **kwargs):
                raise LLMError("429 quota", kind="quota")

        fallback = FakeLLM(
            default=[
                _card(
                    "What is the difference between Karma Yoga and Jnana Yoga?",
                    "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
                    [1],
                )
            ]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(
                conn, lecture, chain=[QuotaLLM(), fallback]
            )
        assert len(cards) == 1
        assert fallback.calls, "the fallback provider should have been used"


class TestStorage:
    async def test_cards_are_stored_with_their_source_segments(self, lecture):
        llm = FakeLLM(
            default=[
                _card(
                    "What is the difference between Karma Yoga and Jnana Yoga?",
                    "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
                    [1],
                )
            ]
        )
        with db_mod.get_conn() as conn:
            cards = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
            added = cards_mod.store_flashcards(conn, lecture, cards)
            assert added == 1
            row = conn.execute(
                "SELECT * FROM flashcards WHERE lecture_id=?", (lecture,)
            ).fetchone()
            assert row is not None
            assert json.loads(row["segment_ids"]) == [1]

    async def test_running_twice_does_not_duplicate(self, lecture):
        llm = FakeLLM(
            default=[
                _card(
                    "What is the difference between Karma Yoga and Jnana Yoga?",
                    "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
                    [1],
                )
            ]
        )
        with db_mod.get_conn() as conn:
            first = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
            cards_mod.store_flashcards(conn, lecture, first)
            second = await cards_mod.generate_flashcards(conn, lecture, llm=llm)
            added = cards_mod.store_flashcards(conn, lecture, second)
            count = conn.execute(
                "SELECT COUNT(*) FROM flashcards WHERE lecture_id=?", (lecture,)
            ).fetchone()[0]
        assert added == 0
        assert count == 1

    async def test_a_failure_part_way_through_leaves_the_existing_cards(
        self, lecture
    ):
        """The brief requires that a generation failure does not corrupt
        the cards that already exist."""
        # One good card is stored first.
        good = cards_mod.Flashcard(
            question="What is Moksha?",
            answer="అజ్ఞానం వల్ల పురుషుడు ప్రకృతితో గుర్తించుకుంటాడు.",
            source_segment_ids=[3],
        )
        with db_mod.get_conn() as conn:
            cards_mod.store_flashcards(conn, lecture, [good])
            before = conn.execute(
                "SELECT COUNT(*) FROM flashcards WHERE lecture_id=?", (lecture,)
            ).fetchone()[0]

            # A generation that fails part-way through.
            class ExplodingLLM(FakeLLM):
                def __init__(self):
                    super().__init__()
                    self.calls = 0

                def chat(self, *, chat_ctx, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return super().chat(chat_ctx=chat_ctx, **kwargs)
                    raise LLMError("the provider went away", kind="unknown")

            llm = ExplodingLLM()
            with pytest.raises(LLMError):
                await cards_mod.generate_flashcards(conn, lecture, llm=llm)

            after = conn.execute(
                "SELECT COUNT(*) FROM flashcards WHERE lecture_id=?", (lecture,)
            ).fetchone()[0]
        assert before == 1
        assert after == 1
