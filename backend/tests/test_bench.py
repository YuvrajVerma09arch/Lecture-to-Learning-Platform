"""Benchmark harness tests.

**These are mocked.** No room is joined and no provider is called. What
is tested is the part of the harness that is ours: the statistics, the
record schema, the question set's coverage, and the report renderer.
"""

from __future__ import annotations

from agent.bench import driver, harness, questions, report


class TestPercentile:
    def test_the_median_of_an_odd_number_of_samples(self):
        assert harness.percentile([3.0, 1.0, 2.0], 0.5) == 2.0

    def test_the_nearest_rank_method_returns_an_observed_value(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        p95 = harness.percentile(values, 0.95)
        assert p95 in values
        # ceil(0.95 * 10) - 1 = 9, the tenth value.
        assert p95 == 10.0

    def test_a_single_sample_is_its_own_percentile(self):
        assert harness.percentile([4.2], 0.5) == 4.2
        assert harness.percentile([4.2], 0.95) == 4.2

    def test_no_samples_has_no_percentile(self):
        assert harness.percentile([], 0.5) is None
        assert harness.percentile([], 0.95) is None

    def test_the_result_does_not_depend_on_the_order(self):
        ordered = [1.0, 2.0, 3.0, 4.0, 5.0]
        shuffled = [3.0, 1.0, 5.0, 2.0, 4.0]
        assert harness.percentile(ordered, 0.95) == harness.percentile(shuffled, 0.95)


class TestSummarise:
    def test_an_empty_list(self):
        stats = harness.summarise([])
        assert stats["n"] == 0
        assert stats["p50"] is None

    def test_the_figures_are_present(self):
        stats = harness.summarise([1.0, 2.0, 3.0])
        assert stats["n"] == 3
        assert stats["p50"] == 2.0
        assert stats["min"] == 1.0
        assert stats["max"] == 3.0
        assert stats["mean"] == 2.0


class TestQuestionSet:
    def test_there_are_at_least_twenty_four_questions(self):
        assert len(questions.all_questions()) >= 24

    def test_at_least_three_questions_the_lecture_does_not_cover(self):
        assert len(questions.NOT_COVERED) >= 3

    def test_some_questions_have_a_pause(self):
        assert any(q.has_pause for q in questions.all_questions())

    def test_every_language_the_questions_use_is_supported(self):
        for question in questions.all_questions():
            assert question.language in questions.LANGUAGES

    def test_the_questions_span_more_than_one_language(self):
        assert len({q.language for q in questions.all_questions()}) >= 4

    def test_a_covered_question_in_another_script_exists(self):
        # The corpus is Telugu, so an English or Hinglish question about it
        # is the cross-lingual case the retrieval has to handle.
        assert any(
            q.expected_covered and q.language != "te-IN" for q in questions.all_questions()
        )

    def test_every_question_records_what_it_tests(self):
        for question in questions.all_questions():
            assert question.note

    def test_the_summary_counts_match_the_set(self):
        stats = questions.summary()
        assert stats["total"] == len(questions.all_questions())
        assert stats["covered"] + stats["not_covered"] == stats["total"]


class TestPauseSplicing:
    def test_a_pause_lengthens_the_audio(self):
        pcm = b"\x01\x02" * 8000  # one second of 16-bit samples
        spliced = driver.splice_pause(pcm, at_seconds=0.5, seconds=1.0)
        assert len(spliced) > len(pcm)

    def test_the_silence_is_actually_there(self):
        pcm = b"\x01\x02" * 8000
        spliced = driver.splice_pause(pcm, at_seconds=0.5, seconds=0.5)
        # The middle of the splice is silent.
        middle = spliced[len(pcm) : len(pcm) + 1000]
        assert middle == b"\x00" * 1000

    def test_no_pause_leaves_the_audio_alone(self):
        pcm = b"\x01\x02" * 8000
        assert driver.splice_pause(pcm, at_seconds=0.5, seconds=0.0) == pcm

    def test_a_past_the_end_is_clamped(self):
        pcm = b"\x01\x02" * 800
        spliced = driver.splice_pause(pcm, at_seconds=100.0, seconds=0.5)
        # The pause lands at the end rather than raising.
        assert len(spliced) > len(pcm)


class TestTurnRecord:
    def _record(self, **kwargs):
        base = {
            "question": "q",
            "language": "te-IN",
            "expected_covered": True,
            "question_end": 100.0,
            "first_audio": 101.5,
            "agent_end": 104.0,
        }
        base.update(kwargs)
        return driver.TurnRecord(**base)

    def test_the_latency_is_from_the_end_of_speech_to_the_first_audio(self):
        assert self._record().time_to_first_audio == 1.5

    def test_the_answer_duration_is_measured(self):
        assert self._record().answer_duration == 2.5

    def test_a_turn_with_no_audio_is_not_ok(self):
        assert self._record(first_audio=None).ok is False

    def test_a_turn_with_an_error_is_not_ok(self):
        assert self._record(error="boom").ok is False

    def test_a_missing_marker_makes_the_latency_unknown(self):
        assert self._record(question_end=None).time_to_first_audio is None


class TestBenchmarkResult:
    def _result(self):
        result = harness.BenchmarkResult()
        for index in range(4):
            result.records.append(
                driver.TurnRecord(
                    question=f"q{index}",
                    language="te-IN" if index % 2 == 0 else "hi-en",
                    expected_covered=index != 3,
                    question_end=100.0,
                    first_audio=100.0 + (index + 1),
                    agent_end=103.0,
                    agent_text="a covered answer" if index != 3 else "a wrong answer",
                )
            )
        return result

    def test_the_failures_are_counted(self):
        result = self._result()
        result.records[0].error = "boom"
        assert len(result.failures) == 1

    def test_a_failed_turn_is_excluded_from_the_latency_figures(self):
        result = self._result()
        result.records[0].error = "boom"
        assert result.overall["n"] == 3

    def test_the_figures_are_broken_down_by_language(self):
        result = self._result()
        by_language = result.by_language
        assert set(by_language) == {"te-IN", "hi-en"}

    def test_a_decline_counts_as_correct(self):
        result = self._result()
        result.records[3].agent_text = "That is not in this lecture."
        stats = result.abstention_accuracy()
        assert stats["expected_to_decline"] == 1
        assert stats["declined_correctly"] == 1
        assert stats["answered_incorrectly"] == 0

    def test_a_confident_answer_to_an_uncovered_question_counts_as_wrong(self):
        result = self._result()
        result.records[3].agent_text = "It was invented in 1066."
        stats = result.abstention_accuracy()
        assert stats["declined_correctly"] == 0
        assert stats["answered_incorrectly"] == 1

    def test_the_conditions_are_recorded(self):
        result = self._result()
        payload = result.to_dict()
        assert payload["conditions"]["livekit_region"] == "US"
        assert payload["conditions"]["scripted"] is True


class TestReport:
    def test_it_renders_the_measured_section(self):
        result = harness.BenchmarkResult()
        result.records.append(
            driver.TurnRecord(
                question="q",
                language="te-IN",
                expected_covered=True,
                question_end=1.0,
                first_audio=2.5,
                agent_end=4.0,
                agent_text="an answer",
            )
        )
        text = report.render(result)
        assert "## MEASURED" in text
        assert "## ESTIMATED" in text
        assert "## MOCKED" in text
        assert "## UNVERIFIED" in text
        assert "p95" in text
        assert "nearest-rank" in text

    def test_it_names_the_configuration_b_and_c_as_not_run(self):
        text = report.render(harness.BenchmarkResult())
        assert "configuration B" in text
        assert "configuration C" in text
        assert "No latency figure is quoted for B or C" in text

    def test_it_lists_the_failures(self):
        result = harness.BenchmarkResult()
        result.records.append(
            driver.TurnRecord(
                question="broken",
                language="te-IN",
                expected_covered=True,
                error="the agent produced no audio",
            )
        )
        text = report.render(result)
        assert "the agent produced no audio" in text
