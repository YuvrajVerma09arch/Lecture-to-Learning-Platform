"""Ingest tests: upload validation, transcoding, and silence-aware chunking.

These tests are **mocked** -- they never call the Gnani API. They do use
real ffmpeg, because the chunking behaviour depends on real audio.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.pipeline import ingest


def _make_audio(path: Path, *, seconds: float = 3.0, freq: int = 440) -> Path:
    """Create a real audio file with ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "16000", "-ac", "1", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class TestUploadValidation:
    def test_rejects_missing_file(self, tmp_path: Path):
        with pytest.raises(ingest.IngestError, match="could not be read"):
            ingest.validate_upload(tmp_path / "nope.mp3")

    def test_rejects_unsupported_extension(self, tmp_path: Path):
        path = tmp_path / "notes.txt"
        path.write_text("hello")
        with pytest.raises(ingest.IngestError, match="Unsupported file type"):
            ingest.validate_upload(path)

    def test_rejects_file_with_no_audio_track(self, tmp_path: Path):
        path = tmp_path / "silent.mp3"
        path.write_bytes(b"not really audio" * 100)
        with pytest.raises(ingest.IngestError):
            ingest.validate_upload(path)

    def test_accepts_a_real_audio_file(self, tmp_path: Path):
        path = _make_audio(tmp_path / "tone.mp3", seconds=2.0)
        media = ingest.validate_upload(path)
        assert media.duration_seconds == pytest.approx(2.0, abs=0.3)
        assert media.channels == 1
        assert media.size_bytes > 0

    def test_content_hash_is_stable_and_content_addressed(self, tmp_path: Path):
        a = _make_audio(tmp_path / "a.mp3", seconds=1.0)
        b = _make_audio(tmp_path / "b.mp3", seconds=1.0)
        # Same command, different files, but deterministic encoding means
        # identical content hashes.
        assert ingest.content_hash(a) == ingest.content_hash(b)

        c = _make_audio(tmp_path / "c.mp3", seconds=2.0, freq=880)
        assert ingest.content_hash(c) != ingest.content_hash(a)


class TestChunkPlanning:
    def test_small_file_is_one_chunk(self):
        plan = ingest.plan_chunks(
            total_duration=60.0,
            total_size_bytes=1_000_000,
            target_bytes=8_000_000,
            max_bytes=10 * 1024 * 1024,
            silences=[],
        )
        assert plan == [(0.0, 60.0, False, None)]

    def test_large_file_is_split(self):
        # 10 minutes at 100 KB/s = 60 MB, target 8 MB -> ~8 chunks.
        plan = ingest.plan_chunks(
            total_duration=600.0,
            total_size_bytes=60_000_000,
            target_bytes=8_000_000,
            max_bytes=10 * 1024 * 1024,
            silences=[],
        )
        assert len(plan) > 1
        # Chunks must be contiguous and cover the whole duration.
        assert plan[0][0] == 0.0
        assert plan[-1][1] == 600.0
        for (_, end, _, _), (next_start, _, _, _) in zip(plan, plan[1:], strict=False):
            assert end == pytest.approx(next_start)

    def test_never_exceeds_the_job_file_limit(self):
        # A 10-hour recording at 4 KB/s is 144 MB, so an 8 MB target would
        # imply 18 chunks -- but the byte budget is the binding constraint
        # and the 100-file cap is the floor on chunk duration.
        plan = ingest.plan_chunks(
            total_duration=36_000.0,
            total_size_bytes=144_000_000,
            target_bytes=8_000_000,
            max_bytes=10 * 1024 * 1024,
            silences=[],
        )
        assert len(plan) <= ingest.MAX_FILES_PER_JOB

    def test_boundaries_move_to_silence_when_one_is_nearby(self):
        # 40 MB over 300 s = 133 KB/s, so an 8 MB target is a 60 s chunk.
        # Put a silence at 60 s, right on the boundary.
        silences = [ingest.Silence(59.0, 61.0), ingest.Silence(160.0, 161.5)]
        plan = ingest.plan_chunks(
            total_duration=300.0,
            total_size_bytes=40_000_000,
            target_bytes=8_000_000,
            max_bytes=10 * 1024 * 1024,
            silences=silences,
        )
        assert plan[0][2] is True
        assert plan[0][1] == pytest.approx(60.0)

    def test_a_late_silence_is_rejected_when_it_would_overshoot_the_budget(self):
        # A silence at 80 s would make the first chunk 80 s long. With
        # 133 KB/s that is ~10.6 MB, over the 10 MB hard limit, so the
        # planner must not use it.
        silences = [ingest.Silence(79.0, 81.0)]
        plan = ingest.plan_chunks(
            total_duration=300.0,
            total_size_bytes=40_000_000,
            target_bytes=8_000_000,
            max_bytes=10 * 1024 * 1024,
            silences=silences,
        )
        assert plan[0][1] == pytest.approx(60.0)
        assert plan[0][2] is False

    def test_cut_point_prefers_the_longest_silence_when_equally_close(self):
        silences = [ingest.Silence(78.0, 79.0), ingest.Silence(79.5, 81.5)]
        cut, at_silence, duration = ingest._best_cut_point(silences, target=80.0, window=5.0)
        assert at_silence is True
        # 79.5-81.5 is longer and its midpoint (80.5) is close to 80.0.
        assert duration == pytest.approx(2.0)

    def test_cut_point_respects_the_latest_allowed_bound(self):
        cut, at_silence, _ = ingest._best_cut_point(
            [ingest.Silence(74.0, 76.0)], target=75.0, window=10.0, latest_allowed=70.0
        )
        assert at_silence is False
        assert cut == 75.0

    def test_no_silence_nearby_falls_back_to_the_target(self):
        cut, at_silence, _ = ingest._best_cut_point(
            [ingest.Silence(0.0, 1.0)], target=100.0, window=5.0
        )
        assert at_silence is False
        assert cut == 100.0

    def test_no_silences_at_all(self):
        cut, at_silence, _ = ingest._best_cut_point([], target=50.0, window=5.0)
        assert at_silence is False
        assert cut == 50.0


class TestSplitting:
    def test_split_produces_chunks_under_the_target(self, tmp_path: Path):
        source = _make_audio(tmp_path / "long.mp3", seconds=12.0)
        # 12 s at 16 kHz mono 32 kbps is ~48 KB, so a 20 KB target forces
        # several chunks.
        chunks = ingest.split_audio(
            source, tmp_path / "chunks", target_bytes=20_000
        )
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.size_bytes < 100_000
            assert chunk.duration_seconds > 0
            assert chunk.path.exists()

    def test_chunk_offsets_are_monotonic_and_contiguous(self, tmp_path: Path):
        source = _make_audio(tmp_path / "long.mp3", seconds=10.0)
        chunks = ingest.split_audio(source, tmp_path / "chunks", target_bytes=20_000)
        for i, chunk in enumerate(chunks):
            assert chunk.index == i
            if i:
                assert chunk.start_seconds >= chunks[i - 1].start_seconds
        # Total covered duration should roughly equal the source duration.
        total = sum(c.duration_seconds for c in chunks)
        assert total == pytest.approx(10.0, abs=1.5)

    def test_chunks_are_16k_mono_mp3(self, tmp_path: Path):
        source = _make_audio(tmp_path / "long.mp3", seconds=4.0)
        chunks = ingest.split_audio(source, tmp_path / "chunks", target_bytes=8_000_000)
        assert len(chunks) == 1
        media = ingest.probe_media(chunks[0].path)
        assert media.sample_rate == 16000
        assert media.channels == 1
        assert media.detected_suffix == ".mp3"


class TestTranscode:
    def test_transcode_produces_16k_mono(self, tmp_path: Path):
        source = _make_audio(tmp_path / "src.wav", seconds=2.0)
        out = ingest.transcode_for_gnani(source, tmp_path / "out")
        media = ingest.probe_media(out)
        assert media.sample_rate == 16000
        assert media.channels == 1

    def test_transcode_drops_video_streams(self, tmp_path: Path):
        # An mp4 with a video track must become audio-only.
        source = tmp_path / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
            check=True,
            capture_output=True,
        )
        out = ingest.transcode_for_gnani(source, tmp_path / "out")
        assert out.suffix == ".mp3"
        media = ingest.probe_media(out)
        assert media.duration_seconds == pytest.approx(2.0, abs=0.3)


class TestSilenceDetection:
    def test_finds_silence_in_a_sparse_file(self, tmp_path: Path):
        # 2s tone, 2s silence, 2s tone.
        path = tmp_path / "gaps.mp3"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
             "-ar", "16000", "-ac", "1", str(path)],
            check=True,
            capture_output=True,
        )
        silences = ingest.detect_silences(path, threshold_db=-40, min_duration=0.3)
        assert silences, "expected at least one detected silence"
        # The gap is around the 2s mark.
        assert any(1.5 <= s.start <= 2.5 for s in silences)
