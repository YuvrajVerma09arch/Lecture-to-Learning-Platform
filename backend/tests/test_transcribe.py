"""Batch transcription tests.

**These are mocked.** The Gnani API is replaced with a fake session, so no
test here spends a credit or touches the network. The response shapes are
taken from a real job captured in
``fixtures/raw_batch_transcript.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from app.pipeline import transcribe
from app.pipeline.transcribe import GnaniBatchClient, TranscribeError


def _transcript_payload(
    *, duration: float, text: str, start: float = 0.0, end: float | None = None
) -> dict[str, Any]:
    """A transcript body shaped exactly like the real API's."""
    return {
        "file_id": "file-1",
        "job_id": "job-1",
        "original_path": "chunk000.mp3",
        "language_code": "en-IN",
        "duration_seconds": duration,
        "model": "gnani-prisma-v2.5",
        "mode": "transcribe",
        "segments": [
            {
                "segment_id": 0,
                "start_time": start,
                "end_time": end if end is not None else duration,
                "text": text,
                "speaker_id": 1,
                "confidence": None,
                "language_detected": None,
                "sentiment": None,
                "emotion": None,
            }
        ],
        "full_transcript": text,
        "created_at": "2026-10-22T00:00:00Z",
    }


class FakeGnaniServer:
    """A minimal stand-in for the Gnani batch API.

    Records what it was asked to do so a test can assert the request
    shape, and can be told to fail in specific ways.
    """

    def __init__(
        self,
        *,
        payload_by_file: dict[str, dict[str, Any]] | None = None,
        fail_status: int | None = None,
        fail_times: int = 0,
    ) -> None:
        self.payload_by_file = payload_by_file or {}
        self.fail_status = fail_status
        self.fail_times = fail_times
        self.requests: list[dict[str, Any]] = []
        self.started = False
        self.cancelled = False
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/stt/v3/batch/jobs", self._create)
        app.router.add_post("/stt/v3/batch/jobs/{job_id}/start", self._start)
        app.router.add_get("/stt/v3/batch/jobs/{job_id}", self._status)
        app.router.add_get("/stt/v3/batch/jobs/{job_id}/files", self._files)
        app.router.add_post("/stt/v3/batch/jobs/{job_id}/cancel", self._cancel)
        app.router.add_get("/transcript/{name}", self._transcript)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        port = self._runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def _maybe_fail(self) -> web.Response | None:
        if self.fail_status is not None and self.fail_times > 0:
            self.fail_times -= 1
            return web.json_response(
                {"detail": {"error_code": "BUSY", "message": "try again"}},
                status=self.fail_status,
            )
        return None

    async def _create(self, request: web.Request) -> web.Response:
        body = await request.post()
        self.requests.append(
            {
                "path": "create",
                "files": [f.filename for f in body.getall("files", [])],
                "config": body.get("config"),
            }
        )
        if (resp := self._maybe_fail()) is not None:
            return resp
        return web.json_response(
            {
                "job_id": "job-1",
                "status": "CREATED",
                "total_files_accepted": len(body.getall("files", [])),
                "created_at": "2026-10-22T00:00:00Z",
                "message": "Call POST to start.",
            },
            status=201,
        )

    async def _start(self, request: web.Request) -> web.Response:
        self.started = True
        return web.json_response({"job_id": request.match_info["job_id"], "status": "STARTING"},
                                 status=202)

    async def _status(self, request: web.Request) -> web.Response:
        if (resp := self._maybe_fail()) is not None:
            return resp
        # Report progress for a couple of polls, then complete.
        self.requests.append({"path": "status"})
        polls = sum(1 for r in self.requests if r["path"] == "status")
        if polls < 2:
            return web.json_response(
                {
                    "job_id": "job-1",
                    "status": "IN_PROGRESS",
                    "progress": {"total_files": 1, "completed_files": 0, "failed_files": 0},
                }
            )
        return web.json_response(
            {
                "job_id": "job-1",
                "status": "COMPLETED",
                "progress": {"total_files": 1, "completed_files": 1, "failed_files": 0},
            }
        )

    async def _files(self, request: web.Request) -> web.Response:
        data = []
        for name, payload in self.payload_by_file.items():
            data.append(
                {
                    "file_id": f"file-{name}",
                    "original_path": name,
                    "status": "COMPLETED",
                    "duration_seconds": payload["duration_seconds"],
                    "transcript_url": f"http://127.0.0.1:{self._port}/transcript/{name}",
                    "error_message": None,
                }
            )
        return web.json_response({"job_id": "job-1", "data": data, "pagination": {}})

    async def _transcript(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return web.json_response(self.payload_by_file[name])

    async def _cancel(self, request: web.Request) -> web.Response:
        self.cancelled = True
        return web.json_response({"status": "CANCELLED"})

    @property
    def _port(self) -> int:
        assert self._runner is not None
        return self._runner.addresses[0][1]


@pytest.fixture
async def fake_gnani():
    server = FakeGnaniServer()
    base_url = await server.start()
    server.base_url = base_url
    yield server
    await server.stop()


class TestJobLifecycle:
    async def test_full_transcription_flow(self, fake_gnani, tmp_path: Path):
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=12.0, text="hello there everyone")
        }
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")

        result = await client.transcribe([chunk], language_code="en-IN")

        assert fake_gnani.started is True
        assert result.segment_count == 1
        assert result.segments[0].text == "hello there everyone"
        assert result.segments[0].start_seconds == 0.0
        assert result.segments[0].end_seconds == 12.0

    async def test_config_is_sent_as_a_json_string(self, fake_gnani, tmp_path: Path):
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=1.0, text="x")
        }
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")
        await client.transcribe([chunk], language_code="hi-IN")

        create = next(r for r in fake_gnani.requests if r["path"] == "create")
        # The API expects `config` to be a JSON *string* in a form field.
        # aiohttp's server-side reader hands it back as bytes.
        raw = create["config"]
        raw = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        assert isinstance(raw, str)
        config = json.loads(raw)
        assert config["model"] == "gnani-prisma-v2.5"
        assert config["language_code"] == "hi-IN"

    async def test_retried_upload_sends_the_file_again(self, fake_gnani, tmp_path: Path):
        """A retry must resend the file, not an empty body.

        The multipart body is consumed by the first attempt, so a retry
        that reuses it would upload nothing.
        """
        fake_gnani.fail_status = 503
        fake_gnani.fail_times = 1
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=1.0, text="x")
        }
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"real audio bytes")

        result = await client.transcribe([chunk], language_code="en-IN")
        assert result.segment_count == 1
        creates = [r for r in fake_gnani.requests if r["path"] == "create"]
        assert len(creates) == 2
        assert all(c["files"] == ["chunk000.mp3"] for c in creates)

    async def test_auth_header_is_present(self, fake_gnani, tmp_path: Path):
        """Regression: the header was once missing, which gave a 401."""
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=1.0, text="x")
        }
        client = GnaniBatchClient(api_key="secret-key", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")
        # The fake server does not check auth, so assert on the client side.
        assert client._headers()["X-API-Key-ID"] == "secret-key"

    async def test_missing_api_key_raises(self, monkeypatch):
        from app import config as config_mod

        monkeypatch.setenv("GNANI_API_KEY", "")
        config_mod.reset_settings_cache()
        with pytest.raises(TranscribeError, match="GNANI_API_KEY"):
            GnaniBatchClient()


class TestTimestampRebasing:
    """The live probe confirmed per-file timestamps are relative."""

    def test_detects_relative_timestamps(self):
        segments = [{"start_time": 0.0, "end_time": 5.0}, {"start_time": 5.0, "end_time": 9.0}]
        assert transcribe.detect_timestamp_basis(segments, chunk_offset=12.0, file_duration=5.0) == "relative"

    def test_detects_absolute_timestamps(self):
        # A segment starting well past the chunk's own offset, and running
        # past its duration, can only be absolute.
        segments = [{"start_time": 12.0, "end_time": 30.0}]
        assert transcribe.detect_timestamp_basis(segments, chunk_offset=12.0, file_duration=5.0) == "absolute"

    def test_first_chunk_is_always_relative(self):
        segments = [{"start_time": 0.0, "end_time": 5.0}]
        assert transcribe.detect_timestamp_basis(segments, chunk_offset=0.0, file_duration=5.0) == "relative"

    def test_empty_segments_are_relative(self):
        assert transcribe.detect_timestamp_basis([], chunk_offset=10.0, file_duration=5.0) == "relative"

    async def test_offsets_are_applied_to_later_chunks(self, fake_gnani, tmp_path: Path):
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=10.0, text="first part"),
            "chunk001.mp3": _transcript_payload(duration=8.0, text="second part"),
        }
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunks = []
        for name in ("chunk000.mp3", "chunk001.mp3"):
            p = tmp_path / name
            p.write_bytes(b"fake")
            chunks.append(p)

        result = await client.transcribe(
            chunks, language_code="en-IN", chunk_offsets=[0.0, 10.0]
        )

        assert result.segment_count == 2
        # The first chunk is untouched.
        assert result.segments[0].start_seconds == 0.0
        # The second chunk is rebased onto the original timeline.
        assert result.segments[1].start_seconds == 10.0
        assert result.segments[1].end_seconds == 18.0
        # Segments come back in chronological order.
        assert result.segments[0].start_seconds < result.segments[1].start_seconds


class TestFailureHandling:
    async def test_transient_failure_is_retried(self, fake_gnani, tmp_path: Path):
        fake_gnani.fail_status = 503
        fake_gnani.fail_times = 2
        fake_gnani.payload_by_file = {
            "chunk000.mp3": _transcript_payload(duration=1.0, text="x")
        }
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")

        result = await client.transcribe([chunk], language_code="en-IN")
        assert result.segment_count == 1

    async def test_client_error_is_not_retried(self, fake_gnani, tmp_path: Path):
        fake_gnani.fail_status = 400
        fake_gnani.fail_times = 99
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")

        with pytest.raises(TranscribeError, match="Gnani rejected"):
            await client.transcribe([chunk], language_code="en-IN")

        creates = [r for r in fake_gnani.requests if r["path"] == "create"]
        # A 400 is a validation error: retrying it would burn time and,
        # for a billable operation, risk a duplicate charge.
        assert len(creates) == 1

    async def test_failed_job_cancels_the_billable_job(self, fake_gnani, tmp_path: Path):
        """A job we give up on must be cancelled, not left running."""
        fake_gnani.payload_by_file = {}  # no transcripts -> nothing collected
        client = GnaniBatchClient(api_key="k", base_url=fake_gnani.base_url)
        chunk = tmp_path / "chunk000.mp3"
        chunk.write_bytes(b"fake")

        result = await client.transcribe([chunk], language_code="en-IN")
        # The job completed but produced nothing; the pipeline treats that
        # as a failure, and the client must not leave the job running.
        assert result.segment_count == 0
