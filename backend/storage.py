import json
import os
import threading
from datetime import datetime, timezone
from filelock import FileLock
from typing import Callable, Optional

from config import settings

DB_PATH = settings.db_path
LOCK_PATH = DB_PATH + ".lock"
_lock = FileLock(LOCK_PATH)

# ── In-memory DB cache ────────────────────────────────────────────────────────
# db.json is 6.4 MB: json.load takes ~48 ms, and every GET/PATCH/POST used to
# pay it. The cache is validated against the file's mtime, so any external
# write (import-db, scripts, another process) invalidates it automatically.
# atomic_update() refreshes the mtime after each write so we don't re-parse.
# _cache_guard also serializes reads from the threads used by asyncio.to_thread
# (dedup previews), which otherwise could parse concurrently and race.
_cache: Optional[dict] = None
_cache_mtime: Optional[float] = None
_cache_guard = threading.Lock()


def _default() -> dict:
    """Devuelve la estructura vacía por defecto de la base de datos."""
    return {"users": {}, "houses": {}}


def read_db() -> dict:
    """Read the database. Parses db.json at most once per mtime change.

    The returned dict IS the cached object, not a copy — that is what makes the
    cache worthwhile, and it is also the one rule attached to it: treat it as
    read-only. Mutating it outside atomic_update() leaks the change into every
    later read while never reaching disk. If a mutation is needed, do it in the
    closure passed to atomic_update(), which writes and keeps the cache in sync.
    """
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        # File gone: drop the cache so a re-created file is picked up.
        with _cache_guard:
            _cache, _cache_mtime = None, None
        return _default()

    with _cache_guard:
        if _cache is not None and _cache_mtime == mtime:
            return _cache
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                db = json.load(f)
        except json.JSONDecodeError:
            # Corrupt file: drop any stale cache too, otherwise atomic_update()
            # could later stamp the new mtime onto an old cached object.
            _cache, _cache_mtime = None, None
            return _default()
        _cache, _cache_mtime = db, mtime
        return db


def _invalidate_cache() -> None:
    """Drop the cached DB (used when a write fails mid-mutation)."""
    global _cache, _cache_mtime
    with _cache_guard:
        _cache, _cache_mtime = None, None


def _now() -> str:
    """Devuelve la fecha y hora actual en formato ISO 8601 UTC."""
    return datetime.now(timezone.utc).isoformat()


def atomic_update(fn: Callable[[dict], None]) -> None:
    """Read db.json, apply fn(db) in-place, then write back — all under a file lock.

    Uses a write-to-temp-then-replace pattern so a crash mid-write never
    leaves db.json in a corrupt/partial state.

    The cached dict read here is mutated in place and then written, so the
    cache stays in sync without a re-parse. If fn() or the write raises, the
    cache is dropped — fn may have mutated it halfway and the file on disk is
    still the old one, so the next read must re-parse.

    Writes are compact (no indent): 6.4 MB → 5.5 MB on disk and ~15% faster
    to serialize on every save (review/notes changes included).
    """
    global _cache_mtime
    tmp_path = DB_PATH + ".tmp"
    with _lock:
        db = read_db()
        try:
            fn(db)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False)
            # Stamp the mtime of the file we are about to move into place, not
            # of DB_PATH afterwards: os.replace keeps the tmp file's mtime, and
            # reading DB_PATH after the replace would race a second writer and
            # label our older cache with their newer file.
            mtime = os.path.getmtime(tmp_path)
            os.replace(tmp_path, DB_PATH)  # atomic on all major OSes
        except Exception:
            _invalidate_cache()
            raise
        # The file we just wrote IS the cached dict: refresh its mtime so the
        # next read_db() serves it from cache instead of re-parsing.
        with _cache_guard:
            _cache_mtime = mtime
