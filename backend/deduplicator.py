import logging
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from storage import _now, atomic_update, read_db

logger = logging.getLogger(__name__)

_ADDRESS_NOISE = {
    "calle", "calles", "av", "avenida", "avda", "pje", "pasaje",
    "de", "del", "la", "las", "los", "y", "e", "al",
    "nro", "num", "numero", "nº", "no",
    "piso", "dpto", "departamento", "oficina", "local", "unidad",
    "torre", "casa", "ph", "lote", "manzana", "bloque",
    "esq", "esquina", "entre", "int", "interior",
    "sur", "norte", "este", "oeste",
    "cp", "codigo", "postal", "código",
    "arg", "argentina", "bsas", "buenos", "aires", "caba",
    "gba", "zona", "partido", "provincia", "pcia",
}

_SIMILARITY_THRESHOLD = 0.75
_PRICE_TOLERANCE = 0.10
_M2_TOLERANCE = 0.10


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[\u0300-\u036f]", "", s)
    s = re.sub(r"[^a-z0-9\sáéíóúñü]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = [t for t in s.split() if t not in _ADDRESS_NOISE and len(t) > 1]
    return " ".join(tokens)


def _token_set(s: str) -> set:
    return set(_normalize(s).split())


def _address_similarity(a: str, b: str) -> float:
    ta = _token_set(a or "")
    tb = _token_set(b or "")
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    if not inter:
        return 0.0
    has_nonnumeric = any(not t.isdigit() for t in inter)
    if not has_nonnumeric:
        return 0.0
    return len(inter) / len(union) if union else 0.0


def _within_tolerance(a: Optional[float], b: Optional[float], pct: float) -> bool:
    if a is None or b is None or a == 0:
        return False
    return abs(a - b) / a <= pct


def _engine_rank(engine: str, sources: List[dict]) -> int:
    for i, s in enumerate(sources):
        if s.get("engine") == engine:
            return i
    return len(sources)


def _pick_keep(houses: List[dict], sources: List[dict]) -> dict:
    return min(houses, key=lambda h: _engine_rank(h.get("search_engine", ""), sources))


_REVIEW_PRIORITY = {
    "contactar": 0,
    "interesante": 1,
    "en_duda": 2,
    "": 3,
    None: 3,
    "descartada": 4,
    "duplicado": 5,
}


def _merge_metadata(keep: dict, removed: dict) -> None:
    keep_review = keep.get("review") or ""
    removed_review = removed.get("review") or ""
    rp_keep = _REVIEW_PRIORITY.get(keep_review, 3)
    rp_removed = _REVIEW_PRIORITY.get(removed_review, 3)
    if rp_removed < rp_keep:
        keep["review"] = removed_review

    kept_notes = (keep.get("notes") or "").strip()
    removed_notes = (removed.get("notes") or "").strip()
    if removed_notes:
        if kept_notes:
            keep["notes"] = kept_notes + "\n---\n" + removed_notes
        else:
            keep["notes"] = removed_notes


def find_duplicates(session: dict, username: str) -> List[dict]:
    db = read_db()
    house_ids = session.get("house_ids", [])
    houses = []
    for hid in house_ids:
        h = db["houses"].get(hid)
        if h and h.get("status") == "active" and h.get("review") != "duplicado":
            houses.append((hid, h))

    raw = [h for _, h in houses]
    sources = session.get("search_sources", [])

    n = len(raw)
    matched: Dict[int, set] = {i: {i} for i in range(n)}

    for i in range(n):
        for j in range(i + 1, n):
            hi, hj = raw[i], raw[j]
            if hi.get("search_engine") == hj.get("search_engine"):
                continue

            addr_i = hi.get("manual_address") or hi.get("address") or ""
            addr_j = hj.get("manual_address") or hj.get("address") or ""
            if not addr_i or not addr_j:
                continue
            if _address_similarity(addr_i, addr_j) < _SIMILARITY_THRESHOLD:
                continue

            if not _within_tolerance(hi.get("price"), hj.get("price"), _PRICE_TOLERANCE):
                continue

            if not _within_tolerance(
                hi.get("total_m2") or hi.get("covered_m2"),
                hj.get("total_m2") or hj.get("covered_m2"),
                _M2_TOLERANCE,
            ):
                continue

            matched[i].add(j)
            matched[j].add(i)

    visited = [False] * n
    groups = []
    for i in range(n):
        if visited[i]:
            continue
        cluster = set()
        stack = [i]
        while stack:
            idx = stack.pop()
            if visited[idx]:
                continue
            visited[idx] = True
            cluster.add(idx)
            for neighbor in matched[idx]:
                if not visited[neighbor]:
                    stack.append(neighbor)
        if len(cluster) >= 2:
            cluster_houses = [raw[idx] for idx in cluster]
            keep = _pick_keep(cluster_houses, sources)
            removed = [h for h in cluster_houses if h["internal_id"] != keep["internal_id"]]
            groups.append({
                "keep_id": keep["internal_id"],
                "keep_engine": keep.get("search_engine", ""),
                "houses": cluster_houses,
            })

    return groups


def apply_dedup(session: dict, username: str, groups: List[dict]) -> int:
    marked_count = 0
    now = _now()

    def update(db: dict):
        nonlocal marked_count
        session_id = session["id"]
        for group in groups:
            keep_id = group["keep_id"]
            for h in group["houses"]:
                hid = h["internal_id"]
                if hid == keep_id:
                    continue
                keep_house = db["houses"].get(keep_id)
                remove_house = db["houses"].get(hid)
                if keep_house and remove_house:
                    _merge_metadata(keep_house, remove_house)
                    remove_house["review"] = "duplicado"
                    remove_house["last_updated"] = now
                    keep_house["last_updated"] = now
                    marked_count += 1

        db["users"][username]["sessions"][session_id]["last_executed"] = now

    atomic_update(update)
    return marked_count
