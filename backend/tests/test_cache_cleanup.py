"""TTL pruning of the shared ingest cache, without touching live data."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app.config import get_settings
from app.pipeline import cache_cleanup


def _dir(prefix: str) -> Path:
    return get_settings().cache_dir / "ingest" / prefix


def _seed_dir(prefix: str, *, age_days: float = 0) -> Path:
    path = _dir(prefix)
    path.mkdir(parents=True, exist_ok=True)
    marker = path / "source.mp3"
    marker.write_bytes(b"audio")
    if age_days:
        past = time.time() - age_days * 86400
        os.utime(marker, (past, past))
        os.utime(path, (past, past))
    return path


def _lecture(conn: Any, digest: str, status: str = "ready") -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO lectures (id, title, original_filename, content_hash, "
        "language_code, status, owner_id, created_at, updated_at) "
        "VALUES (?, 'T', 'x.mp3', ?, 'en-IN', ?, 'anonymous', ?, ?)",
        ("lec_" + digest[:16], digest, status, now, now),
    )


def test_expired_unreferenced_directory_is_removed(db_conn: Any):
    path = _seed_dir("a" * 16, age_days=31)
    removed = cache_cleanup.prune_ingest_cache(db_conn, now=time.time())
    assert removed == ["a" * 16]
    assert not path.exists()


def test_referenced_directory_survives(db_conn: Any):
    digest = "b" * 64
    path = _seed_dir(digest[:16], age_days=31)
    _lecture(db_conn, digest)
    assert cache_cleanup.prune_ingest_cache(db_conn, now=time.time()) == []
    assert (path / "source.mp3").read_bytes() == b"audio"


def test_transcript_only_reference_survives(db_conn: Any):
    digest = "c" * 64
    path = _seed_dir(digest[:16], age_days=31)
    now = time.time()
    db_conn.execute(
        "INSERT INTO transcripts (id, content_hash, source_hash, source_path, "
        "language_code, model, audio_seconds, segments_json, full_text, created_at) "
        "VALUES ('tr_c', ?, ?, 'x.mp3', 'en-IN', 'test', 1.0, '[]', '', ?)",
        ("other-hash", digest, now),
    )
    cache_cleanup.prune_ingest_cache(db_conn, now=time.time())
    assert path.exists()


def test_in_flight_and_fresh_directories_survive(db_conn: Any):
    active = _seed_dir("d" * 16, age_days=31)
    unsettled = "e" * 64
    busy = _seed_dir(unsettled[:16], age_days=31)
    _lecture(db_conn, unsettled, status="transcribing")
    fresh = _seed_dir("f" * 16)

    removed = cache_cleanup.prune_ingest_cache(
        db_conn, now=time.time(), active={"d" * 16}
    )
    assert removed == []
    assert active.exists() and busy.exists() and fresh.exists()


def test_malformed_names_and_symlink_escapes_are_ignored(db_conn: Any, tmp_path: Any):
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"keep")
    (_dir("not-a-hash")).mkdir(parents=True, exist_ok=True)
    link = _dir("0" * 16)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=False)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem")

    cache_cleanup.prune_ingest_cache(db_conn, now=time.time() + 90 * 86400)
    assert outside.read_bytes() == b"keep"
    assert _dir("not-a-hash").exists()


def test_missing_cache_root_is_a_no_op(db_conn: Any):
    root = get_settings().cache_dir / "ingest"
    if root.exists():
        root.rmdir()
    assert cache_cleanup.prune_ingest_cache(db_conn) == []
    assert not root.exists()


def test_ttl_comes_from_settings(db_conn: Any, monkeypatch: Any):
    monkeypatch.setenv("CACHE_TTL_DAYS", "1")
    config_mod.reset_settings_cache()
    try:
        old = _seed_dir("1" * 16, age_days=2)
        new = _seed_dir("2" * 16, age_days=0.5)
        removed = cache_cleanup.prune_ingest_cache(db_conn, now=time.time())
        assert removed == ["1" * 16]
        assert not old.exists() and new.exists()
    finally:
        config_mod.reset_settings_cache()


async def test_startup_task_is_cancelled_on_shutdown(monkeypatch: Any):
    monkeypatch.setenv("CACHE_PRUNER_ENABLED", "true")
    config_mod.reset_settings_cache()
    from app.main import create_app

    try:
        with TestClient(create_app()) as client:
            task = client.app.state.cache_pruner_task
            assert not task.done()
        await asyncio.sleep(0)
        assert task.done()
    finally:
        config_mod.reset_settings_cache()
