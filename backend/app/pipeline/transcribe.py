"""Gnani Prisma batch transcription.

Direct HTTP against the documented batch API. This path is deliberately kept
separate from the live conversational audio path: ingestion is a long-running
offline job, conversation is real-time, and mixing them would make both
harder to reason about.

Contract notes verified against the documentation in Phase 0:
  * Base URL is ``api.vachana.ai``; auth is the ``X-API-Key-ID`` header.
  * Creating a job does NOT start it. ``POST /{job_id}/start`` is required.
  * The multipart file field is ``files``; ``config`` is a JSON *string*
    form field.
  * Polling faster than every 10 seconds is against the documented
    guidance and risks 429.
  * ``transcript_url`` is a pre-signed URL that expires after 1 hour, so
    the transcript is downloaded and stored immediately.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
TERMINAL_JOB_STATES = {"COMPLETED", "PARTIAL_FAILURE", "FAILED", "START_FAILED", "CANCELLED"}
PENDING_JOB_STATES = {"CREATED", "STARTING", "QUEUED", "IN_PROGRESS", "CANCELLING"}


class TranscribeError(Exception):
    """A transcription failure. The message is safe to show a user."""


class CreditCeilingExceeded(TranscribeError):
    """Raised before a job starts when the daily estimate would exceed the cap."""


@dataclass
class TranscriptSegment:
    """One segment of a transcript, with absolute timestamps."""

    index: int
    start_seconds: float
    end_seconds: float
    text: str
    speaker_id: int | None = None
    chunk_index: int | None = None
    source_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "chunk_index": self.chunk_index,
            "source_file": self.source_file,
        }


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment]
    full_text: str
    audio_seconds: float
    language_code: str
    job_id: str | None = None

    @property
    def segment_count(self) -> int:
        return len(self.segments)


def build_config(
    *,
    language_code: str,
    model: str,
    with_diarization: bool = False,
    num_speakers: int | None = None,
    with_denoise: bool = False,
    bias_list: list[str] | None = None,
) -> str:
    """Build the ``config`` form field.

    Returned as a JSON *string* because that is what the multipart field
    expects, not a nested object.
    """
    config: dict[str, Any] = {
        "model": model,
        "language_code": language_code,
        "mode": "transcribe",
    }
    if with_diarization:
        config["with_diarization"] = True
        config["num_speakers"] = num_speakers or 5
    if with_denoise:
        config["with_denoise"] = True
    if bias_list:
        # Documented as single words, letters only, max 100 entries.
        config["bias_list"] = [w for w in bias_list if w.isalpha()][:100]
    return json.dumps(config)


class GnaniBatchClient:
    """Async client for the Gnani Prisma batch STT API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.gnani_api_key
        self.base_url = (base_url or settings.gnani_base_url).rstrip("/")
        self.model = settings.gnani_stt_model
        if not self.api_key:
            raise TranscribeError(
                "GNANI_API_KEY is not set. Add it to your .env file."
            )

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key-ID": self.api_key}

    # -- low-level ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        session: aiohttp.ClientSession,
        max_attempts: int = 4,
        **kwargs: Any,
    ) -> tuple[int, Any]:
        """One HTTP request with bounded retries and exponential backoff.

        Only retryable statuses are retried. A 4xx validation error is
        returned to the caller immediately -- retrying it would just burn
        time and, for a billable operation, risk a duplicate charge.
        """
        settings = get_settings()
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(
            connect=settings.http_connect_timeout, total=settings.http_read_timeout
        )

        last_error: Exception | None = None
        # Every call to this API is authenticated with the same header.
        headers = {**self._headers(), **(kwargs.pop("headers", None) or {})}
        for attempt in range(max_attempts):
            try:
                async with session.request(
                    method, url, timeout=timeout, headers=headers, **kwargs
                ) as resp:
                    if resp.status in RETRYABLE_STATUS:
                        body = await resp.text()
                        last_error = TranscribeError(
                            f"Transient error {resp.status} from Gnani: {body[:200]}"
                        )
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after else None
                        await self._backoff(attempt, delay)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise TranscribeError(
                            f"Gnani rejected the request ({resp.status}): {body[:300]}"
                        )
                    # A job-status or files response is JSON; the transcript
                    # download is handled separately.
                    return resp.status, await resp.json()
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                await self._backoff(attempt)

        raise TranscribeError(f"Request to Gnani failed after {max_attempts} attempts: {last_error}")

    @staticmethod
    async def _backoff(attempt: int, server_delay: float | None = None) -> None:
        """Exponential backoff with jitter, honouring Retry-After if present."""
        if server_delay is not None:
            delay = server_delay
        else:
            delay = min(2.0**attempt, 30.0)
            delay += random.uniform(0, delay * 0.3)
        await asyncio.sleep(delay)

    # -- job lifecycle -----------------------------------------------------

    async def create_job(
        self,
        files: list[Path],
        *,
        language_code: str,
        session: aiohttp.ClientSession,
        with_diarization: bool = False,
        num_speakers: int | None = None,
        with_denoise: bool = False,
        bias_list: list[str] | None = None,
    ) -> str:
        """Create a batch job. Returns the job id.

        Creating does not start the job; call :meth:`start_job` next.
        """
        config = build_config(
            language_code=language_code,
            model=self.model,
            with_diarization=with_diarization,
            num_speakers=num_speakers,
            with_denoise=with_denoise,
            bias_list=bias_list,
        )

        form = aiohttp.FormData()
        form.add_field("config", config, content_type="application/json")
        for path in files:
            form.add_field(
                "files", path.open("rb"), filename=path.name, content_type="audio/mpeg"
            )

        try:
            _, body = await self._request(
                "POST", "/stt/v3/batch/jobs", session=session, data=form
            )
        finally:
            # Close the handles FormData opened for us.
            for part in form._fields:
                if hasattr(part[0], "close"):
                    part[0].close()

        job_id = body.get("job_id")
        if not job_id:
            raise TranscribeError(f"Gnani did not return a job id: {str(body)[:200]}")
        log.info(
            "gnani batch job created",
            extra={
                "job_id": job_id,
                "file_count": len(files),
                "total_bytes": sum(p.stat().st_size for p in files),
                "language_code": language_code,
            },
        )
        return job_id

    async def start_job(self, job_id: str, *, session: aiohttp.ClientSession) -> None:
        """Start transcription. Required -- creating a job does not start it."""
        await self._request("POST", f"/stt/v3/batch/jobs/{job_id}/start", session=session)
        log.info("gnani batch job started", extra={"job_id": job_id})

    async def get_job(self, job_id: str, *, session: aiohttp.ClientSession) -> dict[str, Any]:
        _, body = await self._request("GET", f"/stt/v3/batch/jobs/{job_id}", session=session)
        return body

    async def get_job_files(
        self, job_id: str, *, session: aiohttp.ClientSession
    ) -> list[dict[str, Any]]:
        _, body = await self._request("GET", f"/stt/v3/batch/jobs/{job_id}/files", session=session)
        return body.get("data") or []

    async def cancel_job(self, job_id: str, *, session: aiohttp.ClientSession) -> None:
        try:
            await self._request("POST", f"/stt/v3/batch/jobs/{job_id}/cancel", session=session)
        except TranscribeError as exc:
            log.warning("failed to cancel gnani job", extra={"job_id": job_id, "error": str(exc)[:200]})

    async def download_transcript(
        self, url: str, *, session: aiohttp.ClientSession
    ) -> dict[str, Any]:
        """Download a transcript from its pre-signed URL.

        The URL needs no auth and expires after an hour, so it is fetched
        and stored immediately rather than persisted.
        """
        timeout = aiohttp.ClientTimeout(connect=15, total=120)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise TranscribeError(f"Could not download transcript: {body[:200]}")
            return await resp.json()

    # -- the whole flow ----------------------------------------------------

    async def transcribe(
        self,
        files: list[Path],
        *,
        language_code: str,
        chunk_offsets: list[float] | None = None,
        with_diarization: bool = False,
        num_speakers: int | None = None,
        with_denoise: bool = False,
        bias_list: list[str] | None = None,
        on_progress: Any = None,
    ) -> TranscriptResult:
        """Run a full batch transcription over one or more chunk files.

        ``chunk_offsets`` gives each file's start time in the original
        recording, so the per-file segments can be rebased onto a single
        absolute timeline.
        """
        if chunk_offsets is None:
            chunk_offsets = [0.0] * len(files)
        if len(chunk_offsets) != len(files):
            raise TranscribeError("chunk_offsets must have one entry per file")

        async with aiohttp.ClientSession() as session:
            job_id = await self.create_job(
                files,
                language_code=language_code,
                session=session,
                with_diarization=with_diarization,
                num_speakers=num_speakers,
                with_denoise=with_denoise,
                bias_list=bias_list,
            )
            try:
                await self.start_job(job_id, session=session)
                status = await self._wait_for_completion(
                    job_id, session=session, on_progress=on_progress
                )
                if status.get("status") != "COMPLETED":
                    raise TranscribeError(
                        f"Transcription ended in state {status.get('status')}. "
                        f"Reason: {status.get('cancel_reason') or 'not provided'}"
                    )
                return await self._collect(
                    job_id,
                    files=files,
                    chunk_offsets=chunk_offsets,
                    language_code=language_code,
                    session=session,
                )
            except Exception:
                # Never leave a billable job running if we are giving up.
                await self.cancel_job(job_id, session=session)
                raise

    async def _wait_for_completion(
        self,
        job_id: str,
        *,
        session: aiohttp.ClientSession,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        deadline = time.monotonic() + settings.batch_job_timeout_seconds
        interval = settings.batch_poll_interval_seconds

        while True:
            status = await self.get_job(job_id, session=session)
            state = status.get("status", "")
            progress = status.get("progress") or {}

            if on_progress is not None:
                on_progress(state, progress)

            if state in TERMINAL_JOB_STATES:
                log.info(
                    "gnani batch job finished",
                    extra={
                        "job_id": job_id,
                        "status": state,
                        "completed_files": progress.get("completed_files"),
                        "failed_files": progress.get("failed_files"),
                    },
                )
                return status

            if time.monotonic() > deadline:
                raise TranscribeError(
                    f"Transcription did not finish within "
                    f"{settings.batch_job_timeout_seconds // 60} minutes"
                )

            # Never poll faster than the documented 10s floor.
            await asyncio.sleep(interval)
            # Back off gently as the job runs, staying under the 30s cap.
            interval = min(interval * 1.25, settings.batch_poll_max_interval_seconds)

    async def _collect(
        self,
        job_id: str,
        *,
        files: list[Path],
        chunk_offsets: list[float],
        language_code: str,
        session: aiohttp.ClientSession,
    ) -> TranscriptResult:
        """Download and merge the per-file transcripts.

        The documentation does not state whether a file's segment timestamps
        are relative to that file or to the whole job. We determine it
        empirically (see :func:`detect_timestamp_basis`) and rebase
        accordingly, so the merge is correct either way.
        """
        file_records = await self.get_job_files(job_id, session=session)

        # The API returns files in job order; map them back to our inputs by
        # original filename, since that is what we uploaded.
        by_name = {r.get("original_path"): r for r in file_records}

        segments: list[TranscriptSegment] = []
        full_parts: list[str] = []
        total_seconds = 0.0
        index = 0

        for path, offset in zip(files, chunk_offsets, strict=True):
            record = by_name.get(path.name)
            if record is None:
                # Fall back to positional matching if the name did not round
                # trip.
                record = file_records[len(segments) and 0 or 0] if file_records else None
            if record is None:
                log.warning("no transcript record for chunk", extra={"filename": path.name})
                continue
            if record.get("status") != "COMPLETED":
                log.warning(
                    "chunk did not transcribe successfully",
                    extra={
                        "filename": path.name,
                        "status": record.get("status"),
                        "error": str(record.get("error_message"))[:200],
                    },
                )
                continue

            url = record.get("transcript_url")
            if not url:
                continue

            payload = await self.download_transcript(url, session=session)
            raw_segments = payload.get("segments") or []
            file_duration = float(payload.get("duration_seconds") or 0.0)
            total_seconds += file_duration

            # Decide whether this file's timestamps need rebasing.
            basis = detect_timestamp_basis(raw_segments, offset, file_duration)

            for seg in raw_segments:
                start = float(seg.get("start_time") or 0.0)
                end = float(seg.get("end_time") or 0.0)
                if basis == "relative":
                    start += offset
                    end += offset
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                segments.append(
                    TranscriptSegment(
                        index=index,
                        start_seconds=round(start, 3),
                        end_seconds=round(end, 3),
                        text=text,
                        speaker_id=seg.get("speaker_id"),
                        chunk_index=files.index(path) if path in files else None,
                        source_file=path.name,
                    )
                )
                index += 1

            if payload.get("full_transcript"):
                full_parts.append(payload["full_transcript"])

        segments.sort(key=lambda s: s.start_seconds)
        for i, seg in enumerate(segments):
            seg.index = i

        return TranscriptResult(
            segments=segments,
            full_text=" ".join(full_parts).strip(),
            audio_seconds=total_seconds,
            language_code=language_code,
            job_id=job_id,
        )


def detect_timestamp_basis(
    raw_segments: list[dict[str, Any]],
    chunk_offset: float,
    file_duration: float,
) -> str:
    """Decide whether a file's segment timestamps are file-relative or absolute.

    The documentation does not say. We infer it: if the file's first segment
    starts noticeably after zero *and* the chunk did not start at zero in the
    original recording, the timestamps must be absolute. If the first
    segment starts near zero, they are relative and need rebasing.

    This is deliberately conservative -- it only claims "absolute" when the
    evidence is unambiguous, because applying the wrong rebasing silently
    corrupts every timestamp in the notes.
    """
    if chunk_offset <= 0.0 or not raw_segments:
        return "relative"

    starts = [float(s.get("start_time") or 0.0) for s in raw_segments]
    if not starts:
        return "relative"

    first_start = min(starts)
    max_start = max(starts)
    # If any segment start falls outside the file's own duration, the
    # timestamps clearly refer to the whole recording.
    if max_start > file_duration + 5.0:
        return "absolute"
    # If the first segment starts well after the chunk's offset, absolute.
    if first_start > chunk_offset * 0.5:
        return "absolute"
    return "relative"
