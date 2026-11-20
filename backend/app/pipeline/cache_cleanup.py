"""TTL pruning of the content-addressed ingest cache.

A work directory is removed only when its name is a valid source-hash
prefix, no lecture or transcript references that hash, no in-flight job
claims it, and every file inside is older than the TTL. The checks are
deliberately conservative: keeping a cache directory costs disk, deleting
one that is still in use costs a lecture.
"""

from __future__ import annotations

import re
import shutil
import time
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_DIR = re.compile(r"^[0-9a-f]{16}$")


def _prefixes(conn: Any, query: str) -> set[str]:
    result: set[str] = set()
    for row in conn.execute(query):
        value = row[0] or ""
        if _HASH.fullmatch(value):
            result.add(value[:16])
    return result


def prune_ingest_cache(
    conn: Any,
    *,
    now: float | None = None,
    ttl_days: float | None = None,
    active: set[str] | None = None,
) -> list[str]:
    """Delete expired, unreferenced cache directories; return their names.

    ``active`` lets the caller pass prefixes belonging to jobs that have not
    written their lecture row yet. ``now`` and ``ttl_days`` exist for
    deterministic tests. Never raises for a transient filesystem error.
    """
    settings = get_settings()
    root = settings.cache_dir / "ingest"
    if not root.is_dir():
        return []

    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    referenced = _prefixes(conn, "SELECT DISTINCT content_hash FROM lectures")
    referenced |= _prefixes(conn, "SELECT DISTINCT source_hash FROM transcripts")
    # A lecture row is inserted before its work dir is written, so a job in
    # progress is covered by its non-settled status even in another process.
    referenced |= _prefixes(
        conn,
        "SELECT DISTINCT content_hash FROM lectures WHERE status NOT IN "
        "('ready', 'failed')",
    )
    protected = referenced | (active or set())
    cutoff = (time.time() if now is None else now) - float(ttl) * 86400

    removed: list[str] = []
    try:
        base = root.resolve()
        for child in root.iterdir():
            name = child.name
            if not _DIR.fullmatch(name) or not child.is_dir() or child.is_symlink():
                continue
            target = child.resolve()
            if target == base or not target.is_relative_to(base):
                continue
            if name in protected:
                continue
            try:
                newest = max(
                    (
                        path.stat().st_mtime
                        for path in child.rglob("*")
                        if not path.is_symlink()
                    ),
                    default=child.stat().st_mtime,
                )
            except OSError:
                continue
            if newest > cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                removed.append(name)
    except OSError as exc:
        log.warning("ingest cache prune interrupted", extra={"error": str(exc)[:200]})
    if removed:
        log.info("ingest cache pruned", extra={"directories": len(removed)})
    return removed
