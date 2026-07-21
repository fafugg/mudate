import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from geocoding_tasks import run_geocode
from scheduler import get_scheduler_status, setup_scheduler
from schemas import (
    CreateSessionRequest,
    ErrorResponse,
    GeocodeResponse,
    OkResponse,
    RunCreatedResponse,
    RunResponse,
    SchedulerResponse,
    SessionResponse,
    UpdateHouseRequest,
    UpdateSessionRequest,
    UserResponse,
)
from scrapers import run_scrape, make_run
from storage import DB_PATH, _now, atomic_update, read_db
from config import settings

# In-memory run status store (keyed by run_id)
runs: Dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    setup_scheduler(runs)
    yield


app = FastAPI(title="Mudate", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Users & sessions ─────────────────────────────────────────────────────────

@app.get("/api/users/{username}", response_model=UserResponse)
async def get_user(username: str):
    """Devuelve los datos del usuario y sus sesiones ordenadas por última ejecución."""
    username = _validate_username(username)
    db = read_db()
    is_new = username not in db["users"]
    user_data = db["users"].get(username, {"sessions": {}})
    sessions = []
    for sid, session in user_data["sessions"].items():
        active = sum(
            1
            for hid in session.get("house_ids", [])
            if db["houses"].get(hid, {}).get("status") == "active"
        )
        sessions.append({**session, "active_count": active})
    sessions.sort(
        key=lambda s: s.get("last_executed") or s["created_at"], reverse=True
    )
    return {"username": username, "is_new": is_new, "sessions": sessions}


@app.post("/api/users/{username}/sessions", status_code=201)
async def create_session(username: str, body: CreateSessionRequest):
    """Crea una nueva sesión de búsqueda para el usuario."""
    username = _validate_username(username)
    if body.search_engine not in settings.valid_engines:
        raise HTTPException(400, "search_engine must be 'zonaprop', 'argenprop', 'mercadolibre', or 'remax'")

    session_id = str(uuid.uuid4())
    now = _now()
    session = {
        "id": session_id,
        "created_at": now,
        "last_executed": None,
        "search_engine": body.search_engine,
        "search_filter": body.search_filter,
        "label": body.label or f"{body.search_engine.capitalize()} — {body.search_filter[:50]}",
        "house_ids": [],
    }

    def update(db: dict):
        db["users"].setdefault(username, {"sessions": {}})
        db["users"][username]["sessions"][session_id] = session

    atomic_update(update)
    return session


@app.get("/api/users/{username}/sessions/{session_id}", response_model=SessionResponse)
async def get_session(username: str, session_id: str):
    """Devuelve una sesión con sus propiedades asociadas."""
    username = _validate_username(username)
    db = read_db()
    session = _session_or_404(db, username, session_id)
    houses = [db["houses"][hid] for hid in session["house_ids"] if hid in db["houses"]]
    return {**session, "houses": houses}


@app.delete("/api/users/{username}/sessions/{session_id}", response_model=OkResponse)
async def delete_session(username: str, session_id: str):
    """Elimina una sesión y todas sus propiedades asociadas."""
    username = _validate_username(username)
    def update(db: dict):
        session = _session_or_404(db, username, session_id)
        for hid in session.get("house_ids", []):
            db["houses"].pop(hid, None)
        del db["users"][username]["sessions"][session_id]
    atomic_update(update)
    return {"ok": True}


@app.put("/api/users/{username}/sessions/{session_id}", response_model=OkResponse)
async def update_session(username: str, session_id: str, body: UpdateSessionRequest):
    """Actualiza el filtro de búsqueda o etiqueta de una sesión."""
    username = _validate_username(username)
    def update(db: dict):
        session = _session_or_404(db, username, session_id)
        if body.search_filter is not None:
            session["search_filter"] = body.search_filter
        if body.label is not None:
            session["label"] = body.label

    atomic_update(update)
    return {"ok": True}


@app.post("/api/users/{username}/sessions/{session_id}/run", response_model=RunCreatedResponse)
async def run_session(
    username: str, session_id: str, background_tasks: BackgroundTasks
):
    """Lanza un scraping en background para la sesión indicada."""
    username = _validate_username(username)
    db = read_db()
    session = _session_or_404(db, username, session_id)

    # Check if a run is already active for this session
    for r in runs.values():
        if r.get("session_id") == session_id and r.get("status") == "running":
            return {"run_id": r["id"], "already_running": True}

    _prune_runs(runs)
    run_id = str(uuid.uuid4())
    runs[run_id] = make_run(run_id, session_id=session_id, triggered_by="manual")
    background_tasks.add_task(
        run_scrape,
        session=session,
        username=username,
        run_id=run_id,
        runs=runs,
    )
    return {"run_id": run_id}


@app.patch("/api/houses/{house_id}", response_model=OkResponse)
async def update_house(house_id: str, body: UpdateHouseRequest):
    """Actualiza review, notas o dirección manual de una propiedad."""
    def update(db: dict):
        if house_id not in db["houses"]:
            raise HTTPException(404, "House not found")
        if "review" in body.model_fields_set:
            db["houses"][house_id]["review"] = body.review
        if "notes" in body.model_fields_set:
            db["houses"][house_id]["notes"] = body.notes
        if "manual_address" in body.model_fields_set:
            db["houses"][house_id]["manual_address"] = body.manual_address
            db["houses"][house_id]["geocode_failed"] = False
            db["houses"][house_id]["lat"] = None
            db["houses"][house_id]["lng"] = None
    atomic_update(update)
    return {"ok": True}


@app.get("/api/houses/{house_id}", response_model=Dict[str, Any])
async def get_house(house_id: str):
    """Devuelve los datos completos de una propiedad."""
    db = read_db()
    house = db["houses"].get(house_id)
    if not house:
        raise HTTPException(404, "House not found")
    return house


@app.post("/api/houses/{house_id}/geocode", response_model=GeocodeResponse)
async def geocode_house(house_id: str, background_tasks: BackgroundTasks):
    """Lanza la geocodificación de una propiedad individual."""
    db = read_db()
    house = db["houses"].get(house_id)
    if not house:
        raise HTTPException(404, "House not found")
    address = house.get("manual_address") or house.get("address")
    if not address:
        return {"run_id": None, "already_done": True}

    _prune_runs(runs)
    run_id = str(uuid.uuid4())
    runs[run_id] = make_run(run_id, total=1, message="Geocodificando…", triggered_by="geocode_single")
    background_tasks.add_task(run_geocode, house_ids=[house_id], run_id=run_id, runs=runs)
    return {"run_id": run_id, "already_done": False}


@app.get("/api/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str):
    """Devuelve el estado de un scraping o geocodificación en curso."""
    if run_id not in runs:
        raise HTTPException(404, "Run not found")
    return runs[run_id]


@app.delete("/api/runs/{run_id}", response_model=OkResponse)
async def cancel_run(run_id: str):
    """Cancela un scraping o geocodificación en curso."""
    if run_id not in runs:
        raise HTTPException(404, "Run not found")
    run = runs[run_id]
    if run.get("status") == "running":
        run["cancelled"] = True
        run["status"] = "cancelled"
        run["message"] = "Cancelado por el usuario."
        run["finished_at"] = _now()
    return {"ok": True}


# ── Geocoding ────────────────────────────────────────────────────────────────

@app.post("/api/users/{username}/sessions/{session_id}/geocode", response_model=GeocodeResponse)
async def geocode_session(
    username: str, session_id: str, background_tasks: BackgroundTasks,
    force: bool = False,
):
    """Lanza la geocodificación en background para todas las propiedades de la sesión."""
    username = _validate_username(username)
    db = read_db()
    session = _session_or_404(db, username, session_id)

    # Check if a geocode run is already active for this session
    for r in runs.values():
        if r.get("session_id") == session_id and r.get("triggered_by") == "geocode" and r.get("status") == "running":
            return {"run_id": r["id"], "already_running": True, "already_done": False}

    needs_api: List[str] = []
    for hid in session.get("house_ids", []):
        house = db["houses"].get(hid)
        if not house or not (house.get("manual_address") or house.get("address")):
            continue
        if house.get("lat") is not None:
            continue  # already geocoded
        if house.get("geocode_failed") and not force:
            continue  # previously failed, skip unless forced
        needs_api.append(hid)

    if not needs_api:
        return {"run_id": None, "already_done": True}

    _prune_runs(runs)
    run_id = str(uuid.uuid4())
    runs[run_id] = make_run(
        run_id,
        session_id=session_id,
        total=len(needs_api),
        message="Geocodificando direcciones…",
        triggered_by="geocode",
    )
    background_tasks.add_task(run_geocode, house_ids=needs_api, run_id=run_id, runs=runs)
    return {"run_id": run_id, "already_done": False}


# ── DB export ────────────────────────────────────────────────────────────────

@app.get("/api/admin/export-db")
async def export_db() -> FileResponse:
    """Descarga una copia de respaldo de la base de datos."""
    if not os.path.exists(DB_PATH):
        raise HTTPException(404, "Database file not found")
    return FileResponse(
        DB_PATH,
        media_type="application/json",
        filename="db-backup.json",
    )


@app.post("/api/admin/import-db", response_model=OkResponse)
async def import_db(file: UploadFile = File(...)):
    """Importa un respaldo JSON reemplazando la base de datos actual."""
    # 1. Filename check
    if file.filename != "db-backup.json":
        raise HTTPException(
            400,
            f"El archivo debe llamarse 'db-backup.json' (recibido: '{file.filename}'). "
            "Renombrá el archivo antes de importar.",
        )

    content = await file.read()

    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, "File too large (max 50 MB)")

    # 2. JSON validity check
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON inválido: {e}")

    if not isinstance(data, dict):
        raise HTTPException(400, "El archivo no contiene un objeto JSON válido.")

    # 3. Required keys check
    required = {"users", "houses"}
    missing = required - set(data.keys())
    if missing:
        raise HTTPException(
            400,
            f"Faltan claves requeridas en el archivo: {', '.join(sorted(missing))}. "
            "¿Es realmente un backup de Mudate?",
        )

    # 4. Atomic replace
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(DB_PATH), delete=False
    ) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, DB_PATH)

    return {"ok": True}


# ── Clear geo data ───────────────────────────────────────────────────────────

@app.delete("/api/users/{username}/sessions/{session_id}/geodata", response_model=OkResponse)
async def clear_geodata(username: str, session_id: str):
    """Borra las coordenadas geográficas de todas las propiedades de la sesión."""
    username = _validate_username(username)
    def update(db: dict):
        session = _session_or_404(db, username, session_id)
        for hid in session.get("house_ids", []):
            if hid in db["houses"]:
                db["houses"][hid]["lat"] = None
                db["houses"][hid]["lng"] = None
                db["houses"][hid]["geocode_failed"] = False
    atomic_update(update)
    return {"ok": True}


# ── Scheduler status ─────────────────────────────────────────────────────────

@app.get("/api/scheduler", response_model=SchedulerResponse)
async def scheduler_status():
    """Devuelve el estado del programador de tareas."""
    return get_scheduler_status()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _prune_runs(runs: dict, max_age_hours: int = settings.run_prune_hours) -> None:
    """Remove finished runs older than max_age_hours to prevent unbounded growth."""
    from datetime import timedelta
    from datetime import timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    stale = [
        rid for rid, r in runs.items()
        if r.get("status") != "running" and r.get("finished_at")
        and datetime.fromisoformat(r["finished_at"].rstrip("Z")) < cutoff
    ]
    for rid in stale:
        del runs[rid]

def _validate_username(username: str) -> str:
    username = username.lower()
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", username):
        raise HTTPException(
            400, "Username can only contain letters, numbers, _ and - (max 32 chars)"
        )
    return username


def _session_or_404(db: dict, username: str, session_id: str) -> dict:
    user = db.get("users", {}).get(username)
    if not user:
        raise HTTPException(404, f"User '{username}' not found")
    session = user.get("sessions", {}).get(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return session


# ── Static files (frontend) ───────────────────────────────────────────────────

_frontend = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
