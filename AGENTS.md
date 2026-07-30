# Mudate — Project Context for AI Sessions

## User's Analysis Request

Analyze the architecture, folder structure, infraestructure, files, understand the app, the objective, the functions and features, and everything into your memory to better complete future tasks.

---

## AI Analysis Response

### Overview

Mudate is a **self-hosted web app** for tracking and comparing real estate listings from Argentine portals (Zonaprop, Argenprop, MercadoLibre, Remax). It scrapes listings, tracks price history, geocodes addresses onto an interactive map, and lets users review/filter/export properties.

### Architecture

```
Browser (Alpine.js + Tailwind CDN + Leaflet)
      ↕ HTTP REST
FastAPI (main.py)
  ├── Scheduler (APScheduler — daily 08:00 ART)
  ├── Background Tasks (scrape / geocode)
  ├── scrapers/ (Playwright + stealth)
  │   ├── base.py          — ABC + shared utils (coerce, parse_price)
  │   ├── factory.py       — engine → scraper class
  │   ├── runner.py        — run_scrape() lifecycle
  │   ├── persistence.py   — merge listings into db.json
  │   ├── zonaprop.py      — Zonaprop scraper
  │   ├── argenprop.py     — Argenprop scraper
  │   ├── mercadolibre.py  — MercadoLibre scraper
  │   └── remax.py         — Remax scraper
  ├── geocoder.py          — Nominatim cascade → OpenCage fallback
  ├── geocoding_tasks.py   — concurrent geocode runner
  ├── storage.py           — atomic JSON read/write (filelock + temp+replace)
  ├── config.py            — all env vars & constants
  └── schemas.py           — Pydantic request/response models
      ↕
db.json (single-file JSON database)
```

### Folder Structure

```
mudate/
├── backend/
│   ├── main.py            — FastAPI app, all routes, lifespan
│   ├── storage.py         — read_db() & atomic_update()
│   ├── geocoder.py        — Nominatim (pipelined rate limiter) → OpenCage
│   ├── geocoding_tasks.py — async concurrency for batch geocoding
│   ├── scheduler.py       — daily 08:00 ART auto-refresh
│   ├── config.py          — Settings dataclass (env-driven)
│   ├── schemas.py         — HouseDict, request/response Pydantic models
│   ├── requirements.txt   — fastapi, uvicorn, playwright, apscheduler, etc.
│   ├── scrapers/          — scraper package (see above)
│   └── tests/             — standalone diagnostic test scripts
├── frontend/
│   ├── index.html         — SPA (Alpine.js + Tailwind + Leaflet)
│   ├── app.js             — Alpine component (state, methods, filters, etc.)
│   ├── map.js             — map & geocoding methods (spread into app)
│   ├── constants.js       — REVIEW_OPTIONS, PIN_COLORS, api() helper
│   └── dark.css           — dark mode overrides
├── Dockerfile             — multi-stage (python:3.11-slim + playwright chromium)
├── docker-compose.yml     — single service, mounts ./data for persistence
├── scripts/
│   └── reimage_argenprop.py — one-off script to re-scrape Argenprop images
├── README.md
├── DOCS.md
└── AGENTS.md              — This file
```

### Key Features

| Feature | Detail |
|---|---|
| **Multi-engine scraping** | Zonaprop, Argenprop, MercadoLibre, Remax — Playwright with stealth anti-detection |
| **Cloudflare bypass** | Persistent browser profiles; separate profiles for headless vs headed runs |
| **Price history** | Every price change recorded per property; % change shown in table |
| **Geocoding** | Nominatim (pipelined rate limiter) → OpenCage fallback; manual address override |
| **Interactive map** | Leaflet + OSM; color-coded pins by review status; fly-to on click |
| **Review system** | A revisar / En duda / Interesante / Descartada / Contactar |
| **Filter & sort** | By review, type, status, price range, address, real estate, notes, provider, price changes |
| **Bulk actions** | Multi-select with batch review + compare (2-5 properties side by side) |
| **Export CSV** | Respects active filters; includes BOM for Excel |
| **Export/Import DB** | Full db.json backup & restore via admin UI |
| **Auto-refresh** | APScheduler re-scrapes all sessions daily at 08:00 ART |
| **Dark mode** | CSS overrides with localStorage persistence |
| **Column visibility** | Toggleable table columns (persisted to localStorage) |

### Database Schema (HouseDict)

Single JSON file (`db.json`) with `{ users, houses }`:

- **users[username].sessions[id]** — search config + `house_ids[]`
- **houses[id]** — canonical property record with: `search_engine_id`, `type`, `price`, `currency`, `address`, `lat/lng`, `images[]`, `previous_prices[]`, `review`, `notes`, `status` (active/removed), timestamps

Deduplication: houses with same `search_engine_id` or URL across sessions are merged (review/notes/geo preserved).

### API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/users/{username}` | Get user + sessions list |
| POST | `/api/users/{username}/sessions` | Create session |
| GET/DELETE | `/api/users/{username}/sessions/{id}` | Get/delete session + houses |
| PUT | `/api/users/{username}/sessions/{id}` | Update label/sources |
| POST | `/api/users/{username}/sessions/{id}/run` | Launch scrape (background) |
| POST | `/api/users/{username}/sessions/{id}/geocode` | Launch geocoding (background) |
| DELETE | `/api/users/{username}/sessions/{id}/geodata` | Clear geo coordinates |
| GET/PATCH | `/api/houses/{id}` | Get/update review, notes, manual_address |
| POST | `/api/houses/{id}/geocode` | Geocode single house |
| GET/DELETE | `/api/runs/{id}` | Poll/cancel run |
| GET | `/api/scheduler` | Scheduler status |
| GET | `/api/admin/export-db` | Download db.json |
| POST | `/api/admin/import-db` | Upload db.json backup |

### Infrastructure

- **Containerized**: Docker + docker-compose (single service)
- **Railway-ready**: `$PORT` env var, health check, volume support for persistence
- **No build step**: Frontend served as static files by FastAPI
- **Manual alternative**: Python 3.11+ venv with `uvicorn main:app`

### Design Decisions

1. **Single-file JSON DB** — trivial deployment, atomic writes via `filelock` + temp+replace
2. **In-memory run state** — scrape/geocode progress in Python dict (lost on restart, but persisted data survives)
3. **No auth** — just usernames for data separation (single-user/small-group friendly)
4. **Optimistic UI** — review/notes saved optimistically and patched in background
5. **Pipelined Nominatim rate limiter** — lock held only to claim send-time slot, released before HTTP call (~2x throughput)

### Key Backend Files

| File | Responsibility |
|---|---|
| `backend/main.py:51` | FastAPI app initialization, CORS, lifespan events |
| `backend/main.py:63-81` | `GET /api/users/{username}` — fetches user sessions sorted by last_executed |
| `backend/main.py:84-116` | `POST /api/users/{username}/sessions` — creates session with validation |
| `backend/main.py:157-181` | `POST /api/users/{username}/sessions/{id}/run` — launches scrape background task |
| `backend/main.py:184-200` | `PATCH /api/houses/{id}` — updates review, notes, manual_address |
| `backend/main.py:255-294` | `POST /api/users/{username}/sessions/{id}/geocode` — batch geocoding launcher |
| `backend/config.py:13-51` | `Settings` dataclass — all env vars and magic constants |
| `backend/storage.py:36-48` | `atomic_update()` — write-to-temp-then-replace with filelock |
| `backend/scrapers/base.py:17-69` | `BaseScraper` ABC — `launch_browser()`, shared utils |
| `backend/scrapers/runner.py:47-163` | `run_scrape()` — orchestrates multi-source scrape lifecycle |
| `backend/scrapers/persistence.py:13-114` | `persist_listings()` — merges scraped data into db.json, deduplicates, marks removed |
| `backend/geocoder.py:207-242` | `geocode()` — Nominatim cascade → OpenCage fallback |
| `backend/geocoder.py:158-179` | `_nominatim_rl()` — pipelined rate limiter |
| `backend/geocoding_tasks.py:15-67` | `run_geocode()` — concurrent geocoding with Semaphore |

### Key Frontend Files

| File | Responsibility |
|---|---|
| `frontend/constants.js:2-8` | `REVIEW_OPTIONS` — dropdown options for review status |
| `frontend/constants.js:54-66` | `api()` — fetch wrapper used by all frontend requests |
| `frontend/app.js:1-987` | `app()` — main Alpine component with all state & methods |
| `frontend/app.js:74-76` | `isRunning` — computed property for run status |
| `frontend/app.js:106-144` | `filteredHouses` — computed getter applying all filters + sort |
| `frontend/app.js:345-353` | `setView()` — switches between table/map view |
| `frontend/app.js:392-415` | `login()` — handles user login flow |
| `frontend/app.js:417-445` | `selectSession()` — loads session data from API |
| `frontend/app.js:481-516` | `triggerRun()` / `startPolling()` / `pollRun()` — scrape lifecycle |
| `frontend/app.js:841-867` | `exportCsv()` — CSV export with BOM |
| `frontend/map.js:4-173` | `mapMethods` — Leaflet map, pins, geocoding poll |
| `frontend/map.js:19-36` | `initMap()` — initializes Leaflet map |
| `frontend/map.js:38-85` | `renderPins()` — color-coded circle markers by review status |
