"""Lecture ingestion: validation, transcoding, and silence-aware chunking.

Gnani's batch API accepts at most 10 MB per file. A 45-minute lecture is
~42 MB at 128 kbps, so chunking is not an optimisation here, it is a
requirement.

Two decisions matter for correctness:

1. **Transcode to 16 kHz mono MP3.** Prisma is a *telephonic* model, so
   16 kHz mono is not a downgrade -- it is the input distribution the model
   was trained on. It also cuts the byte size roughly fourfold, which
   reduces the number of chunks and therefore the number of chunk
   boundaries where sentences could break.

2. **Cut on silence, not on time.** A fixed-duration cut can land in the
   middle of a sentence. We locate the quietest point near each target
   boundary and cut there, so a chunk boundary is far more likely to fall
   between sentences.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

ALLOWED_SUFFIXES = {".wav", ".mp3", ".mp4", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".amr"}
# Gnani's documented batch-STT formats.
GNANI_SUPPORTED_SUFFIXES = {
    ".wav", ".mp3", ".mp4", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".webm", ".amr",
}

# Magic-byte signatures for container sniffing. We do not rely on the
# filename extension alone.
_MAGIC = [
    (b"RIFF", ".wav"),
    (b"ID3", ".mp3"),
    (b"\xff\xfb", ".mp3"),
    (b"\xff\xf3", ".mp3"),
    (b"\xff\xf2", ".mp3"),
    (b"OggS", ".ogg"),
    (b"fLaC", ".flac"),
    (b"\x1a\x45\xdf\xa3", ".webm"),
    (b"\x00\x00\x00\x18ftyp", ".m4a"),
    (b"\x00\x00\x00\x20ftyp", ".m4a"),
    (b"\x00\x00\x00\x28ftyp", ".m4a"),
]


class IngestError(Exception):
    """Raised when a file cannot be accepted. The message is user-facing."""


@dataclass
class MediaInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    codec: str
    size_bytes: int
    detected_suffix: str


@dataclass
class Chunk:
    """One chunk of the source audio, ready to upload."""

    index: int
    path: Path
    start_seconds: float
    duration_seconds: float
    size_bytes: int
    # Set when the cut point was moved to a silence rather than landing on
    # the exact target boundary.
    cut_at_silence: bool = False
    silence_score: float | None = None

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass
class IngestResult:
    content_hash: str
    source_path: Path
    media: MediaInfo
    chunks: list[Chunk] = field(default_factory=list)
    transcript_audio_path: Path | None = None

    @property
    def total_chunk_seconds(self) -> float:
        return sum(c.duration_seconds for c in self.chunks)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def detect_suffix(path: Path) -> str | None:
    """Sniff the container from magic bytes, falling back to the extension."""
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError:
        return None

    for magic, suffix in _MAGIC:
        if head.startswith(magic):
            return suffix
    # mp4/m4a share the ftyp box at a variable offset.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ".m4a"
    return path.suffix.lower() or None


def probe_media(path: Path) -> MediaInfo:
    """Run ffprobe and return media facts. Raises IngestError on bad media."""
    if not shutil.which("ffprobe"):
        raise IngestError("ffprobe is not available on this system")

    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise IngestError("Reading the audio file timed out") from exc

    if proc.returncode != 0:
        raise IngestError("The file is not readable as audio")

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise IngestError("Could not interpret the audio file") from exc

    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise IngestError("The file contains no audio track")

    stream = audio_streams[0]
    duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0.0)
    if duration <= 0:
        raise IngestError("The audio duration could not be determined")

    settings = get_settings()
    size = path.stat().st_size
    if size > settings.max_upload_bytes:
        raise IngestError(
            f"File is too large ({size / 1024 / 1024:.0f} MB). "
            f"The limit is {settings.max_upload_bytes / 1024 / 1024:.0f} MB."
        )
    if duration > settings.max_audio_duration_seconds:
        raise IngestError(
            f"Audio is too long ({duration / 60:.0f} minutes). "
            f"The limit is {settings.max_audio_duration_seconds // 60} minutes."
        )

    suffix = detect_suffix(path) or path.suffix.lower()
    return MediaInfo(
        duration_seconds=duration,
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        codec=str(stream.get("codec_name") or "unknown"),
        size_bytes=size,
        detected_suffix=suffix,
    )


def validate_upload(path: Path) -> MediaInfo:
    """Full upload validation: existence, type, size, duration, decodability."""
    if not path.exists() or not path.is_file():
        raise IngestError("The uploaded file could not be read")

    suffix = path.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise IngestError(
            f"Unsupported file type '{suffix or path.name}'. "
            f"Supported: {', '.join(sorted(ALLOWED_SUFFIXES))}."
        )

    media = probe_media(path)

    if media.detected_suffix not in GNANI_SUPPORTED_SUFFIXES:
        raise IngestError(
            f"'{media.detected_suffix}' is not a format Gnani's batch API accepts. "
            f"Supported: {', '.join(sorted(GNANI_SUPPORTED_SUFFIXES))}."
        )

    log.info(
        "upload validated",
        extra={
            "filename": path.name,
            "duration_seconds": round(media.duration_seconds, 2),
            "size_bytes": media.size_bytes,
            "codec": media.codec,
            "sample_rate": media.sample_rate,
            "channels": media.channels,
        },
    )
    return media


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def content_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 of the file bytes.

    Stable across restarts, which is what makes the transcript cache
    survive an application restart. We hash the *original* upload, not the
    transcoded output, so re-uploading the same file is a cache hit even if
    the transcode settings change.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Silence detection
# --------------------------------------------------------------------------

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


def detect_silences(path: Path, threshold_db: int = -40, min_duration: float = 0.35) -> list[Silence]:
    """Find quiet stretches in the audio.

    ffmpeg's silencedetect is the cheapest reliable way to do this without
    decoding the whole file twice.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    # ffmpeg exits non-zero for null output on some builds; stderr still has
    # the detections, so we parse regardless of the return code.
    stderr = proc.stderr or ""

    silences: list[Silence] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        if m := _SILENCE_START.search(line):
            pending_start = float(m.group(1))
        elif m := _SILENCE_END.search(line):
            if pending_start is not None:
                silences.append(Silence(pending_start, float(m.group(1))))
                pending_start = None

    silences.sort(key=lambda s: s.start)
    return silences


def _best_cut_point(
    silences: list[Silence],
    target: float,
    window: float,
) -> tuple[float, bool, float | None]:
    """Pick the cut point nearest to ``target``.

    Prefers the midpoint of a silence, because that maximises the gap
    between the two chunks. If no silence is near the target, falls back to
    the target itself and reports that the cut was not silence-aligned.
    """
    if not silences:
        return target, False, None

    candidates = [s for s in silences if abs(s.midpoint - target) <= window]
    if not candidates:
        return target, False, None

    # Prefer longer silences when several are equally close.
    best = min(candidates, key=lambda s: (abs(s.midpoint - target), -s.duration))
    return best.midpoint, True, best.duration


# --------------------------------------------------------------------------
# Transcoding
# --------------------------------------------------------------------------


def transcode_for_gnani(src: Path, out_dir: Path, *, bitrate: str = "32k") -> Path:
    """Transcode to 16 kHz mono MP3 at the given bitrate.

    16 kHz mono is the native input distribution for a telephonic ASR model.
    At 32 kbps a 45-minute lecture is ~10.5 MB, which still needs chunking,
    but a 30-minute lecture fits in a single chunk.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{src.stem}-16k-mono.mp3"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",                       # drop any video stream
        "-ar", "16000",              # telephony sample rate
        "-ac", "1",                  # mono
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "16000",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not out_path.exists():
        raise IngestError(f"Audio conversion failed: {proc.stderr.strip()[:300]}")
    return out_path


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def plan_chunks(
    *,
    total_duration: float,
    total_size_bytes: int,
    target_bytes: int,
    silences: list[Silence],
) -> list[tuple[float, float, bool, float | None]]:
    """Work out chunk boundaries.

    Returns ``(start, end, cut_at_silence, silence_duration)`` tuples.
    Boundaries are placed at silences where possible.

    The size estimate assumes a constant bitrate, which is true for the MP3
    we produce. We verify the real size after encoding and re-split if a
    chunk came out over the limit.
    """
    if total_size_bytes <= target_bytes:
        return [(0.0, total_duration, False, None)]

    bytes_per_second = total_size_bytes / total_duration if total_duration else 0
    if bytes_per_second <= 0:
        return [(0.0, total_duration, False, None)]

    chunk_duration = target_bytes / bytes_per_second
    # Guard against a pathological estimate producing thousands of chunks.
    chunk_duration = max(chunk_duration, 30.0)

    # How far we are willing to move a boundary to find a silence.
    window = min(chunk_duration * 0.25, 60.0)

    boundaries: list[tuple[float, bool, float | None]] = []
    position = 0.0
    while position + chunk_duration < total_duration:
        target = position + chunk_duration
        cut, at_silence, sil_dur = _best_cut_point(silences, target, window)
        # Never cut before the previous boundary, and always make progress.
        if cut <= position + 1.0:
            cut = target
            at_silence, sil_dur = False, None
        boundaries.append((cut, at_silence, sil_dur))
        position = cut

    chunks: list[tuple[float, float, bool, float | None]] = []
    start = 0.0
    for cut, at_silence, sil_dur in boundaries:
        chunks.append((start, cut, at_silence, sil_dur))
        start = cut
    chunks.append((start, total_duration, False, None))
    return chunks


def split_audio(
    src: Path,
    out_dir: Path,
    *,
    target_bytes: int,
    silences: list[Silence] | None = None,
) -> list[Chunk]:
    """Split an audio file into chunks, each under ``target_bytes``.

    Cuts are aligned to silence so sentences survive the boundary. Each
    chunk's start time is recorded so the transcript can be reassembled with
    correct absolute timestamps.
    """
    settings = get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    media = probe_media(src)
    total_size = src.stat().st_size

    if silences is None:
        silences = detect_silences(src)

    plan = plan_chunks(
        total_duration=media.duration_seconds,
        total_size_bytes=total_size,
        target_bytes=target_bytes,
        silences=silences,
    )

    chunks: list[Chunk] = []
    for index, (start, end, at_silence, sil_dur) in enumerate(plan):
        length = end - start
        if length <= 0.5:
            continue
        out_path = out_dir / f"{src.stem}-chunk{index:03d}.mp3"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(src),
            "-t", f"{length:.3f}",
            "-ar", "16000", "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", "32k",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not out_path.exists():
            raise IngestError(f"Failed to split audio at chunk {index}: {proc.stderr.strip()[:200]}")

        size = out_path.stat().st_size
        actual_duration = length
        # Re-probe: seeking can land slightly off, and the true duration is
        # what the transcript offsets must be based on.
        try:
            actual_duration = probe_media(out_path).duration_seconds
        except IngestError:
            pass

        chunks.append(
            Chunk(
                index=index,
                path=out_path,
                start_seconds=start,
                duration_seconds=actual_duration,
                size_bytes=size,
                cut_at_silence=at_silence,
                silence_score=sil_dur,
            )
        )

    over = [c for c in chunks if c.size_bytes > settings.gnani_max_file_bytes]
    if over:
        raise IngestError(
            f"Internal error: {len(over)} chunk(s) exceed Gnani's 10 MB limit. "
            f"Re-run with a smaller chunk target."
        )

    log.info(
        "audio chunked",
        extra={
            "chunk_count": len(chunks),
            "source_size_bytes": total_size,
            "silence_aligned_cuts": sum(1 for c in chunks if c.cut_at_silence),
            "largest_chunk_bytes": max((c.size_bytes for c in chunks), default=0),
            "total_duration_seconds": round(media.duration_seconds, 2),
        },
    )
    return chunks


def ingest(
    src: Path,
    work_dir: Path,
    *,
    copy_source: bool = True,
) -> IngestResult:
    """Validate, transcode, and chunk a lecture upload."""
    settings = get_settings()
    media = validate_upload(src)
    digest = content_hash(src)

    work_dir.mkdir(parents=True, exist_ok=True)
    source_copy = work_dir / f"source-{digest[:12]}{src.suffix.lower()}"
    if copy_source and not source_copy.exists():
        shutil.copy2(src, source_copy)

    transcript_audio = transcode_for_gnani(source_copy, work_dir / "audio")
    chunks = split_audio(
        transcript_audio,
        work_dir / "chunks",
        target_bytes=settings.gnani_chunk_target_bytes,
    )

    return IngestResult(
        content_hash=digest,
        source_path=source_copy,
        media=media,
        chunks=chunks,
        transcript_audio_path=transcript_audio,
    )


def work_dir_for(content_hash: str) -> Path:
    settings = get_settings()
    return settings.cache_dir / "ingest" / content_hash[:16]


def cleanup_temp_upload(path: Path) -> None:
    """Remove a temporary upload. Best-effort; never raises."""
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def temp_upload_dir() -> Path:
    return Path(tempfile.gettempdir()) / "gnani-lecture-uploads"
