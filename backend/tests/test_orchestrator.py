"""End-to-end pipeline tests, with every remote call mocked.

These are the tests that matter most for the brief's two hard guarantees:

* A downstream failure must not destroy a finished transcript.
* Identical audio must never be transcribed twice.

Nothing here touches the Gnani API or the LLM providers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app import db as db_mod
from app.pipeline import orchestrator
from app.pipeline import transcribe as transcribe_mod

from .conftest import FakeLLM


def _make_audio(path: Path, *, seconds: float = 4.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ar", "16000", "-ac", "1", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class FakeBatchClient:
    """Stands in for :class:`GnaniBatchClient`."""

    instances: list[FakeBatchClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.transcribe_error: Exception | None = None
        FakeBatchClient.instances.append(self)

    async def transcribe(self, files, **kwargs):
        self.calls.append({"files": [str(f) for f in files], "kwargs": kwargs})
        if self.transcribe_error is not None:
            raise self.transcribe_error
        segments = []
        for i, _file in enumerate(files):
            start = kwargs.get("chunk_offsets", [0.0] * len(files))[i]
            segments.append(
                transcribe_mod.TranscriptSegment(
                    index=len(segments),
                    start_seconds=start,
                    end_seconds=start + 4.0,
                    text=f"Segment number {i} discusses the topic of networks.",
                    speaker_id=1,
                    chunk_index=i,
                )
            )
        return transcribe_mod.TranscriptResult(
            segments=segments,
            full_text=" ".join(s.text for s in segments),
            audio_seconds=4.0 * len(files),
            language_code=kwargs.get("language_code", "en-IN"),
            job_id="fake-job",
        )


@pytest.fixture
def fake_client(monkeypatch):
    FakeBatchClient.instances.clear()
    monkeypatch.setattr(transcribe_mod, "GnaniBatchClient", FakeBatchClient)
    return FakeBatchClient


@pytest.fixture
def fake_llms(monkeypatch):
    """Replace both note-generation models with a fake."""
    note = {
        "heading": "Networks",
        "explanation": "An explanation of the topic that is long enough to pass validation.",
        "key_terms": ["networks"],
        "equations": [],
        "uncertainty_note": "",
    }
    segment_llm = FakeLLM(default=note)
    merge_llm = FakeLLM(
        default={
            "title": "Test Lecture",
            "summary": "A summary of the lecture.",
            "sections": [
                {
                    "heading": "Networks",
                    "explanation": "An explanation long enough to pass validation checks.",
                    "segment_indexes": [0],
                    "key_terms": ["networks"],
                }
            ],
            "key_terms": ["networks"],
        }
    )
    monkeypatch.setattr(
        orchestrator.notes_mod,
        "notes_llm_chain",
        lambda segment=False: [segment_llm if segment else merge_llm],
    )
    return segment_llm, merge_llm


class TestFullPipeline:
    async def test_runs_end_to_end(self, tmp_path, fake_client, fake_llms):
        audio = _make_audio(tmp_path / "lecture.mp3", seconds=4.0)
        result = await orchestrator.run_notes_pipeline(
            audio, title="Test Lecture", language_code="en-IN"
        )

        assert result.status == "ready"
        assert result.notes_document is not None
        assert result.notes_document.title == "Test Lecture"

        with db_mod.get_conn() as conn:
            lecture = conn.execute(
                "SELECT * FROM lectures WHERE id=?", (result.lecture_id,)
            ).fetchone()
            assert lecture["status"] == "ready"
            assert lecture["failed_stage"] is None

            # The transcript is stored, keyed by the cache key (the file
            # hash plus the language and model).
            transcript = conn.execute(
                "SELECT * FROM transcripts WHERE source_hash=?", (lecture["content_hash"],)
            ).fetchone()
            assert transcript is not None
            assert transcript["gnani_job_id"] == "fake-job"

            # The segments are stored and are the RAG corpus.
            segments = conn.execute(
                "SELECT * FROM segments WHERE lecture_id=? ORDER BY seq", (result.lecture_id,)
            ).fetchall()
            assert len(segments) > 0

            # The notes are stored.
            notes_row = conn.execute(
                "SELECT * FROM notes WHERE lecture_id=?", (result.lecture_id,)
            ).fetchone()
            assert notes_row is not None
            assert "Test Lecture" in notes_row["document"]

            # The credit estimate was recorded.
            usage = conn.execute("SELECT * FROM credit_usage").fetchall()
            assert len(usage) == 1
            assert usage[0]["kind"] == "batch_stt"

    async def test_a_cache_hit_skips_transcription(self, tmp_path, fake_client, fake_llms):
        audio = _make_audio(tmp_path / "lecture.mp3", seconds=4.0)

        first = await orchestrator.run_notes_pipeline(
            audio, title="Test Lecture", language_code="en-IN"
        )
        assert len(fake_client.instances) == 1

        second = await orchestrator.run_notes_pipeline(
            audio, title="Test Lecture Again", language_code="en-IN"
        )

        # The second run must not create another Gnani job.
        assert len(fake_client.instances) == 1
        assert second.cached is True
        assert second.credits_estimated == 0.0
        # And it reuses the same transcript.
        assert second.transcript == first.transcript


class TestFailureIsolation:
    async def test_a_note_failure_preserves_the_transcript(self, tmp_path, fake_client, monkeypatch):
        audio = _make_audio(tmp_path / "lecture.mp3", seconds=4.0)

        async def failing_notes(*args, **kwargs):
            raise RuntimeError("the model exploded")

        monkeypatch.setattr(orchestrator.notes_mod, "generate_segment_notes", failing_notes)

        with pytest.raises(orchestrator.PipelineError) as exc:
            await orchestrator.run_notes_pipeline(
                audio, title="Test", language_code="en-IN"
            )
        assert "Note generation failed" in str(exc.value)

        with db_mod.get_conn() as conn:
            # The transcript survived the failure.
            transcript = conn.execute("SELECT * FROM transcripts").fetchone()
            assert transcript is not None
            assert transcript["gnani_job_id"] == "fake-job"

            # The lecture is marked failed, at the note stage.
            lecture = conn.execute("SELECT * FROM lectures").fetchone()
            assert lecture["status"] == "failed"
            assert lecture["failed_stage"] == "note"

            # The transcript stage is recorded as done.
            stage = conn.execute(
                "SELECT status FROM stage_state WHERE stage='transcribe'"
            ).fetchone()
            assert stage["status"] == "done"

    async def test_a_transcription_failure_never_stores_a_partial_transcript(
        self, tmp_path, fake_client, monkeypatch
    ):
        audio = _make_audio(tmp_path / "lecture.mp3", seconds=4.0)

        async def failing_transcribe(*args, **kwargs):
            raise transcribe_mod.TranscribeError("Gnani rejected the request")

        monkeypatch.setattr(
            orchestrator.transcribe_mod.GnaniBatchClient,
            "transcribe",
            failing_transcribe,
        )

        with pytest.raises(orchestrator.PipelineError):
            await orchestrator.run_notes_pipeline(audio, title="T", language_code="en-IN")

        with db_mod.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
            lecture = conn.execute("SELECT * FROM lectures").fetchone()
            assert lecture["status"] == "failed"
            assert lecture["failed_stage"] == "transcribe"


class TestCreditCeiling:
    async def test_refuses_a_job_that_would_exceed_the_ceiling(
        self, tmp_path, fake_client, fake_llms, monkeypatch
    ):
        audio = _make_audio(tmp_path / "lecture.mp3", seconds=4.0)

        # A ceiling of zero means any transcription is refused.
        monkeypatch.setenv("DAILY_CREDIT_CEILING", "0")
        from app import config as config_mod

        config_mod.reset_settings_cache()

        with pytest.raises(orchestrator.PipelineError) as exc:
            await orchestrator.run_notes_pipeline(audio, title="T", language_code="en-IN")
        assert "credit ceiling" in str(exc.value)

        with db_mod.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
