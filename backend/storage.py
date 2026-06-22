import json
import os
from datetime import datetime, timezone
from filelock import FileLock
from typing import Callable

_BASE = os.path.dirname(__file__)
DB_PATH = os.environ.get("DB_PATH", os.path.join(_BASE, "..", "db.json"))
LOCK_PATH = DB_PATH + ".lock"
_lock = FileLock(LOCK_PATH)


def _default() -> dict:
    """Devuelve la estructura vacía por defecto de la base de datos."""
    return {"users": {}, "houses": {}}


def read_db() -> dict:
    """Read and return the database. Safe to call without the lock (reads are non-destructive)."""
    if not os.path.exists(DB_PATH):
        return _default()
    with open(DB_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return _default()


def _now() -> str:
    """Devuelve la fecha y hora actual en formato ISO 8601 UTC."""
    return datetime.now(timezone.utc).isoformat()


def atomic_update(fn: Callable[[dict], None]) -> None:
    """Read db.json, apply fn(db) in-place, then write back — all under a file lock.

    Uses a write-to-temp-then-replace pattern so a crash mid-write never
    leaves db.json in a corrupt/partial state.
    """
    tmp_path = DB_PATH + ".tmp"
    with _lock:
        db = read_db()
        fn(db)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DB_PATH)  # atomic on all major OSes
