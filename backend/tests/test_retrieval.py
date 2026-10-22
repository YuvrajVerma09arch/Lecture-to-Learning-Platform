"""Retrieval tests.

The FTS5 behaviour against Devanagari, Gujarati and romanised Hinglish was
verified during Phase 0 against real text; these tests keep that verified
behaviour from regressing.
"""

from __future__ import annotations

import pytest

from app import db as db_mod
from app.retrieval import fts, rerank

from .conftest import FakeLLM


@pytest.fixture
def seeded_db():
    """A database with a small multilingual corpus."""
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        conn.execute(
            """INSERT INTO lectures (id, title, original_filename, content_hash,
                                    audio_seconds, language_code, status, created_at, updated_at)
               VALUES ('lec1','Test','t.mp3','hash1',60.0,'hi-IN','ready',0,0)"""
        )
        rows = [
            (0, 0.0, 10.0, "छात्र जब समय पर पानी लेता है तो वह समझ जाता है", 0),
            (1, 10.0, 20.0, "વિદ્યાર્થી જ્યારે સમય પર પાણી લે છે તે સમજાય છે", 0),
            (2, 20.0, 30.0, "lecture me binary tree ka explain kiya tha", 0),
            (3, 30.0, 40.0, "लेक्चर में बाइनरी ट्री समझाया गया था", 0),
            (4, 40.0, 50.0, "the physical layer carries raw bits over the wire", 0),
            (5, 50.0, 60.0, "ये वाला diagram देखिए जिसमें data flow दिखाया गया है", 1),
            (6, 60.0, 70.0, "समय पर पानी पीना स्वास्थ्य के लिए अच्छा है", 0),
        ]
        for seq, start, end, text, visual in rows:
            conn.execute(
                """INSERT INTO segments (lecture_id, seq, start_seconds, end_seconds,
                                         text, speaker_id, visual_dependent)
                   VALUES ('lec1',?,?,?,?,?,?)""",
                (seq, start, end, text, 1, visual),
            )
        conn.commit()
        yield conn


class TestTokenisation:
    def test_drops_stopwords(self):
        terms = fts.tokenize("what is the physical layer")
        assert "physical" in terms
        assert "the" not in terms
        assert "what" not in terms

    def test_keeps_indic_words(self):
        terms = fts.tokenize("पानी कैसे लेते हैं")
        assert "पानी" in terms
        assert "कैसे" in terms

    def test_drops_single_characters(self):
        assert fts.tokenize("a b cd") == ["cd"]

    def test_quotes_terms_so_punctuation_is_safe(self):
        # A query full of FTS5 operators must not become a syntax error.
        match = fts.build_match_query('what is this "diagram" AND OR NOT')
        assert '"' in match
        assert " OR " in match


class TestLexicalSearch:
    def test_finds_a_devanagari_segment(self, seeded_db):
        results = fts.lexical_search(seeded_db, "पानी कैसे लेते हैं")
        assert results
        assert "पानी" in results[0].text

    def test_finds_a_gujarati_segment(self, seeded_db):
        results = fts.lexical_search(seeded_db, "પાણી લે છે")
        assert results
        assert "પાણી" in results[0].text

    def test_finds_a_romanised_hinglish_segment(self, seeded_db):
        results = fts.lexical_search(seeded_db, "binary tree explain")
        assert results
        assert "binary" in results[0].text.lower()

    def test_finds_a_devanagari_english_mix(self, seeded_db):
        results = fts.lexical_search(seeded_db, "बाइनरी ट्री")
        assert results
        assert "बाइनरी" in results[0].text

    def test_finds_an_english_segment(self, seeded_db):
        results = fts.lexical_search(seeded_db, "physical layer raw bits")
        assert results
        assert "physical layer" in results[0].text

    def test_no_match_returns_nothing(self, seeded_db):
        assert fts.lexical_search(seeded_db, "photosynthesis mitochondria") == []

    def test_punctuation_does_not_break_the_query(self, seeded_db):
        # A malformed MATCH would raise; it must degrade to no results.
        assert fts.lexical_search(seeded_db, '"') == []

    def test_can_be_scoped_to_one_lecture(self, seeded_db):
        scoped = fts.lexical_search(seeded_db, "पानी", lecture_id="lec1")
        assert scoped
        other = fts.lexical_search(seeded_db, "पानी", lecture_id="lec-other")
        assert other == []

    def test_results_are_ordered_by_relevance(self, seeded_db):
        results = fts.lexical_search(seeded_db, "पानी")
        assert len(results) >= 2
        assert results[0].score >= results[1].score

    def test_visual_flag_is_carried_through(self, seeded_db):
        results = fts.lexical_search(seeded_db, "diagram data flow")
        assert results
        assert results[0].visual_dependent is True


class TestAbstention:
    def test_empty_retrieval_is_not_answerable(self):
        assert fts.looks_answerable([]) is False

    def test_weak_match_is_not_answerable(self, seeded_db):
        results = fts.lexical_search(seeded_db, "माइटोकॉन्ड्रिया")
        assert fts.looks_answerable(results) is False

    def test_a_real_match_is_answerable(self, seeded_db):
        results = fts.lexical_search(seeded_db, "पानी")
        assert fts.looks_answerable(results) is True


class TestMerging:
    def test_merges_adjacent_segments(self, seeded_db):
        results = fts.lexical_search(seeded_db, "पानी")
        merged = fts.merge_adjacent(results)
        # Adjacent segments in the same lecture become one block.
        assert len(merged) <= len(results)
        for seg in merged:
            assert seg.text

    def test_does_not_merge_across_a_gap(self):
        a = fts.RetrievedSegment(
            id=1, lecture_id="l", seq=0, start_seconds=0, end_seconds=5,
            text="one", score=1.0,
        )
        b = fts.RetrievedSegment(
            id=2, lecture_id="l", seq=5, start_seconds=60, end_seconds=65,
            text="two", score=1.0,
        )
        merged = fts.merge_adjacent([a, b])
        assert len(merged) == 2


class TestGroundedAnswering:
    def _segments(self):
        return [
            fts.RetrievedSegment(
                id=1, lecture_id="l", seq=0, start_seconds=0, end_seconds=10,
                text="The physical layer carries raw bits.", score=1.0,
            )
        ]

    async def test_answers_with_a_citation(self):
        llm = FakeLLM(
            responses=[
                {
                    "covered": True,
                    "answer": "It carries raw bits over the wire.",
                    "cited_segments": [0],
                    "confidence": 0.9,
                }
            ]
        )
        result = await rerank.answer_grounded("what does the physical layer do?", self._segments(), llm=llm)
        assert result.covered is True
        assert "raw bits" in result.answer
        assert result.cited_segments == [0]

    async def test_abstains_without_calling_the_model(self):
        llm = FakeLLM()
        result = await rerank.answer_grounded("unrelated question", [], llm=llm)
        assert result.covered is False
        assert result.used_llm is False
        assert llm.calls == []

    async def test_rejects_a_citation_to_a_nonexistent_segment(self):
        llm = FakeLLM(
            responses=[
                {
                    "covered": True,
                    "answer": "An answer.",
                    "cited_segments": [99],
                    "confidence": 0.9,
                },
                {
                    "covered": True,
                    "answer": "An answer.",
                    "cited_segments": [0],
                    "confidence": 0.9,
                },
            ]
        )
        result = await rerank.answer_grounded("question", self._segments(), llm=llm)
        assert result.cited_segments == [0]

    async def test_abstains_when_the_model_cannot_be_validated(self):
        llm = FakeLLM(responses=[{"covered": True, "answer": "x", "cited_segments": [99]}])
        result = await rerank.answer_grounded("question", self._segments(), llm=llm)
        assert result.covered is False
        assert result.used_llm is False

    async def test_the_prompt_forbids_outside_knowledge(self):
        llm = FakeLLM(
            responses=[
                {"covered": True, "answer": "a", "cited_segments": [0], "confidence": 0.5}
            ]
        )
        await rerank.answer_grounded("question", self._segments(), llm=llm)
        rendered = "\n".join(item.text_content or "" for item in llm.calls[0]["chat_ctx"].items)
        assert "ONLY" in rendered
        assert "Do not guess" in rendered

    def test_citation_markers_are_stripped_for_speech(self):
        assert rerank.strip_citations("It carries bits [1] and more [2].") == "It carries bits and more."
