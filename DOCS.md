# Mudate — Documentation

## Table of Contents

1. [Why Mudate?](#why-mudate)
2. [Getting Started](#getting-started)
3. [Usability Guide](#usability-guide)
4. [Integrations](#integrations)
5. [Architecture](#architecture)
6. [Folder Structure](#folder-structure)
7. [Main Functions](#main-functions)
8. [Testing](#testing)

---

## Why Mudate?

Finding a home is not a single moment — it's a process that can stretch over weeks or months. Existing real estate sites like Zonaprop and Argenprop are built for sellers and for the first step of that journey: discovery. Once you've found a listing you like, you're on your own. There's no way to compare two properties side by side, no place to leave yourself a note about why you liked or disliked something, no way to track whether a price dropped since you last checked, and no way to see all the properties you're considering on a map at once.

Mudate solves the entire lifecycle:

- **Discovery** — scrape listings in bulk from a filtered search URL, without clicking through pages manually.
- **Tracking** — every price change is recorded automatically. You always know whether a property got cheaper or more expensive since you first saw it.
- **Organization** — mark properties as *Interesting*, *Contact*, *Unsure*, or *Discard*. Leave notes. Filter by any combination of criteria. Hide removed listings.
- **Comparison** — sort and filter by price, size, rooms, price-per-m², neighbourhood, and more in a single table.
- **Mapping** — see every property you're considering on an interactive map with one click.
- **Persistence** — your data survives browser refreshes, new tabs, and sessions. Everything is saved server-side.

Mudate is designed to be self-hosted: you control your data, and nothing is sent to any third-party analytics service.

It supports four Argentine real estate portals: **Zonaprop**, **Argenprop**, **MercadoLibre**, and **Remax**. Each session can combine multiple search engines, and properties found across different engines are automatically deduplicated.

---

## Getting Started

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine (Linux), **or** Python 3.11+.

---

### Option A — Docker (recommended)

```bash
git clone https://github.com/YOUR_USERNAME/mudate.git
cd mudate
docker compose up
```

Open **http://localhost:8010**.

Data is stored in `./data/db.json` on your machine and survives container restarts.

**Useful commands:**

```bash
docker compose up -d          # run in the background
docker compose logs -f        # follow logs
docker compose down           # stop
docker compose up --build     # rebuild after a code change
```

---

### Option B — Manual (Python 3.11+)

```bash
git clone https://github.com/YOUR_USERNAME/mudate.git
cd mudate/backend

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install --with-deps chromium   # also installs the distro's libraries

uvicorn main:app --host 0.0.0.0 --port 8010 --reload
```

> `--with-deps` is what resolves the system package names for you. Installing
> manually is error-prone — on Debian 11/12 and Raspberry Pi OS the ALSA
> library is `libasound2`, while Ubuntu 24.04+ and Debian 13 call it
> `libasound2t64`.

Open **http://localhost:8010**.

---

### Configuration

All settings are passed as environment variables.

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `../db.json` | Path to the JSON database file |
| `OPENCAGE_API_KEY` | *(empty)* | Optional. Improves geocoding accuracy when Nominatim fails. Free tier: 2,500 req/day. Get one at [opencagedata.com](https://opencagedata.com) |
| `PLAYWRIGHT_HEADLESS` | *(auto)* | `1` forces headless, `0` forces headed. Zonaprop defaults to **headed** mode unless Docker is detected (`/.dockerenv`), because a real window bypasses Cloudflare more reliably. On a machine without a display — Raspberry Pi, NAS, VPS — set `PLAYWRIGHT_HEADLESS=1` in the process environment, otherwise Chromium tries to open a window and exits |

**With Docker**, create a `.env` file next to `docker-compose.yml`:

```env
OPENCAGE_API_KEY=your_key_here
```

**Manually**, prefix the command:

```bash
OPENCAGE_API_KEY=your_key_here uvicorn main:app --host 0.0.0.0 --port 8010
```

---

## Usability Guide

### 1. Log in

Enter any username on the login screen. No password is required — usernames exist only to separate data between multiple people sharing the same instance. If the username doesn't exist yet, you'll be prompted to create it.

### 2. Create a search

Click **Nueva búsqueda** and paste the path portion of a Zonaprop, Argenprop, MercadoLibre, or Remax search URL — the part after the domain. For example:

```
/inmuebles-venta-palermo-capital-federal-argentina.html
```

You can also give the search a custom label to identify it later.

### 3. Run the scraper

Click **Actualizar**. The scraper runs in the background and populates the table with all listings that match your filter. A progress bar shows how many pages have been processed.

You can cancel a run mid-way and the results scraped so far will be saved.

### 4. Review listings

| Action | How |
|---|---|
| Change review status | Use the dropdown on each row (*A revisar*, *Interesante*, *En duda*, *Contactar*, *Descartada*) |
| Leave a note | Open the detail panel (🔍 icon) and type in the Notes field |
| See price history | Open the detail panel — previous prices are listed with timestamps |
| View photos | Open the detail panel and click any image, or press ← → to browse |
| See the original listing | Click the ↗ icon on any row |

### 5. Filter and sort

- **Revisión** — filter by review status.
- **Tipo** — filter by property type (house, apartment, PH, etc.).
- **Estado** — show active listings, removed listings, or all.
- **Precio máx.** — hide listings above a price threshold.
- **Dirección** — fuzzy text search on the address field.
- **Inmobiliaria** — fuzzy text search on the real estate agency name.
- Click any column header to sort.

On mobile, extra filters collapse under a **Filtros** toggle button. The Tabla/Mapa switch is always visible.

### 6. Map view

Click **Mapa**. Addresses are geocoded automatically the first time you open map view. A pin appears for each property that has a known location. Click a pin to open the detail panel.

If an address fails to geocode, you can enter a corrected address manually in the detail panel and click **Geocodificar** to retry.

### 7. Export

Open the **⋯** menu and choose **Exportar CSV**. The export respects the currently active filters.

### 8. Daily auto-refresh

The scheduler re-scrapes every session for every user every morning at **08:00 Argentina time**. New listings are added, price changes are recorded, and listings that disappeared from the search results are marked as *Removida*.

---

## Integrations

### Zonaprop

- **URL:** `https://www.zonaprop.com.ar`
- **Method:** Playwright (Chromium, stealth mode) — the site is a Next.js app that requires JavaScript execution.
- **Scraping strategy:** Listing cards are extracted from the search results pages via a JavaScript evaluation of the DOM. Detail pages are loaded individually to extract full data (images, description, specs). Detail pages are skipped for listings already in the database — only card-level data (price, size) is re-fetched to detect price changes.
- **Anti-bot notes:** Zonaprop uses Cloudflare. The scraper uses a persistent browser profile so that Cloudflare cookies (`cf_clearance`) survive between runs. Headed mode (local) and headless mode (Docker) use **separate browser profiles** to prevent a Docker run from poisoning the local profile with bot-flagged cookies. The profile in use is `~/.mudate_browser` when headed and `~/.mudate_browser_headless` when headless.
- **Blocked detection:** every Zonaprop navigation — the initial search page, pagination **and each detail page** — is checked for a Cloudflare interstitial or a block status (`403`, `429`, `503`, …). When one is found the scraper raises `ScrapeBlockedError` instead of silently returning zero listings, and the run banner prints the profile directory to delete (`rm -rf <profile>`). A block status is authoritative and needs no page inspection; otherwise the page must not look like a genuine results page (listing cards, `__PRELOADED_STATE__` or a title naming the site) before any marker is considered, so listing text is never mistaken for a challenge. Verified against a live Zonaprop fetch, including a real Spanish-language 403 challenge.
  - **Detail pages are checked in two stages.** First the HTTP status, right after navigation — before the selector waits, which would otherwise burn up to 8 s per blocked card. Second, only when the extraction comes back with no property content, a confirmation pass over the page. A page that yields property data is never scanned for markers, so a listing's own description can never trigger a false positive.
  - **Blocks on detail pages are counted, not fatal.** One blocked detail page only costs that listing's address, images and description: its card-level data still reaches `db.json`, and the details are refetched on the next run. The run aborts with `ScrapeBlockedError` only when the blocks are systemic — a total block (`blocked == attempted`), or at least 3 blocks covering at least 50 % of the detail attempts. That is the same "half the evidence" rule used by the pagination and persistence safeguards (`_should_abort_on_detail_blocks()`).

### Argenprop

- **URL:** `https://www.argenprop.com`
- **Method:** Playwright (Chromium, stealth mode).
- **Scraping strategy:** Similar to Zonaprop — search pages are paginated, detail pages are loaded for new listings only. DOM-based extraction pulls property specs, images, and agent info.

### MercadoLibre

- **URL:** `https://inmuebles.mercadolibre.com.ar`
- **Method:** Playwright (Chromium, stealth mode).
- **Scraping strategy:** Click-based pagination through search results. Listing cards are parsed from the DOM. Detail pages are loaded for new listings to extract full specs, images, and agent contact info.

### Remax

- **URL:** `https://www.remax.com.ar`
- **Method:** Direct HTTP requests to the Remax API (`api-ar.redremax.com`), supplemented by Playwright for listing details.
- **Scraping strategy:** Uses the internal Remax API for search results (no Cloudflare issues). Detail pages are scraped via Playwright for full property data.

### Nominatim (OpenStreetMap)

- **URL:** `https://nominatim.openstreetmap.org`
- **Purpose:** Primary geocoding service. Resolves Argentine street addresses to latitude/longitude coordinates.
- **Rate limit:** 1 request per second (enforced by policy). The geocoder uses a **pipelined rate limiter** — the scheduling lock is held only long enough to claim a send-time slot, then released before the HTTP call. This allows concurrent geocoding tasks to pipeline their 1.1 s waits, nearly doubling throughput compared to a naive lock-held-for-entire-call approach.
- **Cascade:** Up to 3 progressively simplified variants of the address are tried (full address → strip street number → first two comma-parts only).
- **No API key required.**

### OpenCage

- **URL:** `https://api.opencagedata.com`
- **Purpose:** Fallback geocoding service used when all Nominatim attempts fail. OpenCage aggregates multiple sources and has better coverage for incomplete or ambiguous addresses.
- **Rate limit:** Free tier: 2,500 requests/day, 1 request/second.
- **Requires:** `OPENCAGE_API_KEY` environment variable. When not set, this service is skipped.

---

## Architecture

```
┌──────────────────────────────────────────────┐
│                  Browser                     │
│          (Alpine.js + Tailwind + Leaflet)     │
└─────────────────────┬────────────────────────┘
                      │ HTTP / REST
┌─────────────────────▼────────────────────────┐
│               FastAPI  (main.py)             │
│                                              │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │  Scheduler  │  │    Background tasks   │  │
│  │ (08:00 ART) │  │  scrape / geocode     │  │
│  └──────┬──────┘  └──────────┬───────────┘  │
│         │                    │               │
│  ┌──────▼────────────────────▼───────────┐  │
│  │            scrapers/                  │  │
│  │  zonaprop.py    argenprop.py          │  │
│  │  mercadolibre.py  remax.py           │  │
│  │       base.py / factory.py            │  │
│  └───────────────────────────────────────┘  │
│                                              │
│  ┌───────────────────────────────────────┐  │
│  │            geocoder.py               │  │
│  │  Nominatim (cascade) → OpenCage      │  │
│  └───────────────────────────────────────┘  │
│                                              │
│  ┌───────────────────────────────────────┐  │
│  │  deduplicator.py  same_engine_dedup  │  │
│  │  cross-engine & reactivation dedup   │  │
│  └───────────────────────────────────────┘  │
│                                              │
│  ┌───────────────────────────────────────┐  │
│  │            storage.py                │  │
│  │   atomic read/write  •  filelock     │  │
│  └──────────────────┬────────────────────┘  │
└─────────────────────┼────────────────────────┘
                      │
              ┌───────▼───────┐
              │   db.json     │
              └───────────────┘
```

### Key design decisions

**Single-file JSON database.** All data lives in one `db.json` file. This keeps deployment trivial (no database server to manage) and makes backup/restore a single file copy. Writes are protected by a file lock (`filelock`) and use a write-to-temp-then-`os.replace` pattern so a crash mid-write never corrupts the file.

**No build step.** The frontend is served as static files (Alpine.js + Tailwind via CDN). No npm, no bundler, no compilation. FastAPI serves it as a static file mount.

**In-memory run state.** Scrape and geocode job progress is stored in a Python dict (`runs`) in the FastAPI process memory, not in the database. The frontend polls `/api/runs/{run_id}` every second to update the progress bar. This state is lost on server restart, but jobs that were running will simply stop and their results (up to the point of cancellation) will already be persisted.

**House deduplication across sessions.** A house scraped in session A and re-found in session B is stored only once. This means review status, notes, geocoordinates, and price history are shared across sessions for the same property — you don't lose your work if you create a new search with overlapping results.

---

## Folder Structure

```
mudate/
│
├── backend/
│   ├── main.py              # FastAPI app: all API routes + background task launchers
│   ├── config.py            # Centralized settings (env vars & constants)
│   ├── schemas.py           # Pydantic request/response models
│   ├── storage.py           # Atomic JSON read/write with filelock
│   ├── deduplicator.py      # Cross-engine fuzzy deduplication
│   ├── same_engine_dedup.py # Same-engine reactivation detection
│   ├── geocoder.py          # Address → lat/lng (Nominatim cascade + OpenCage fallback)
│   ├── geocoding_tasks.py   # Concurrent batch geocoding with Semaphore
│   ├── scheduler.py         # APScheduler: daily 08:00 ART refresh of all sessions
│   ├── requirements.txt     # Python dependencies
│   │
│   ├── scrapers/
│   │   ├── __init__.py      # Re-exports: run_scrape, make_run, get_scraper
│   │   ├── base.py          # BaseScraper ABC + shared utils (coerce, parse_price)
│   │   ├── factory.py       # Engine name → scraper class mapping
│   │   ├── runner.py        # run_scrape() orchestration + make_run()
│   │   ├── persistence.py   # Merge listings into db.json (dedup + price history)
│   │   ├── zonaprop.py      # Zonaprop scraper (Playwright, stealth)
│   │   ├── argenprop.py     # Argenprop scraper (Playwright, stealth)
│   │   ├── mercadolibre.py  # MercadoLibre scraper (Playwright, click pagination)
│   │   └── remax.py         # Remax scraper (API + Playwright detail)
│   │
│   └── tests/
│       ├── test_cloudflare.py       # Diagnose Cloudflare blocks / profile poisoning
│       ├── test_block_detection.py  # Unit tests for block detection (offline)
│       ├── test_detail.py           # Smoke-test Zonaprop detail page extractor
│       ├── test_images.py           # Debug Zonaprop image extraction failures
│       ├── test_pagination.py       # Validate Zonaprop click-based pagination
│       ├── test_suggested.py        # Inspect Zonaprop suggested listings behaviour
│       ├── test_argenprop.py        # Diagnostic tests for Argenprop scraper
│       ├── test_argenprop_pagination.py # Argenprop pagination validation
│       ├── test_mercadolibre.py     # Diagnostic tests for MercadoLibre scraper
│       ├── test_remax.py            # Diagnostic tests for Remax scraper
│       ├── test_persistence.py      # Multi-engine persistence behavior
│       └── test_pagination_all.py   # Pagination verification for all engines
│
├── frontend/
│   ├── index.html           # SPA shell (Alpine.js + Tailwind CDN + Leaflet)
│   ├── app.js               # Alpine component (state, methods, filters)
│   ├── map.js               # Leaflet map & geocoding methods
│   ├── constants.js         # Review options, pin colors, api() helper
│   └── dark.css             # Dark mode overrides
│
├── scripts/
│   └── reimage_argenprop.py # One-off script to re-scrape Argenprop images
│
├── data/                    # Created automatically — holds db.json (git-ignored)
│   └── db.json
│
├── Dockerfile
├── docker-compose.yml
├── README.md
├── DOCS.md
└── AGENTS.md                # Project context for AI sessions
```

---

## Main Functions

### Backend — `storage.py`

#### `read_db() → dict`
Reads and returns the full database from `db.json`. Safe to call without holding the file lock (reads are non-destructive). Returns a default empty structure `{"users": {}, "houses": {}}` if the file doesn't exist or is corrupted.

#### `atomic_update(fn: Callable[[dict], None]) → None`
The only way to write to the database. Acquires the file lock, reads the current state, calls `fn(db)` to mutate it in-place, writes to a `.tmp` file, then atomically replaces `db.json`. A crash mid-write never leaves the database in a corrupt state.

---

### Backend — `config.py`

#### `settings` (Settings dataclass)
Centralized configuration singleton. All environment variables and magic constants live here:
- `db_path` — path to `db.json` (env: `DB_PATH`, default: `../db.json`)
- `opencage_api_key` — OpenCage API key (env: `OPENCAGE_API_KEY`)
- `geocode_batch_size` — max concurrent geocoding tasks (default: 10)
- `nominatim_interval` — seconds between Nominatim sends (default: 1.1)
- `scheduler_hour` / `scheduler_minute` — daily auto-refresh time (default: 08:00 ART)
- `valid_engines` — tuple of supported engine names: `("zonaprop", "argenprop", "mercadolibre", "remax")`
- `max_upload_mb` — max import file size (default: 50)
- `run_prune_hours` — max age for finished runs in memory (default: 24)

---

### Backend — `geocoder.py`

#### `geocode(address: str) → Tuple[Optional[float], Optional[float]]`
Public entry point. Resolves an Argentine address string to `(lat, lng)`. Tries up to three progressively simplified address variants against Nominatim, then falls back to OpenCage if configured. Returns `(None, None)` if all sources fail.

#### `_nominatim_rl(query: str) → Optional[Tuple[float, float]]`
Rate-limited Nominatim call. Acquires a scheduling semaphore only long enough to claim the next permitted send-time slot (advancing `_nom_next_send` by 1.1 s), then releases it before making the HTTP call. This pipelining allows multiple concurrent geocoding tasks to overlap their HTTP waits without violating Nominatim's 1 req/s policy.

#### `_variants(address: str) → List[str]`
Generates up to three progressively simpler versions of an address: full cleaned address → street number stripped → first two comma-separated parts only. This improves geocoding hit rate for addresses with apartment numbers or noisy suffixes.

---

### Backend — `scrapers/runner.py`

#### `run_scrape(session, username, run_id, runs) → None`
Top-level background task for a scrape run. Supports multiple search sources — runs each engine's scraper sequentially (to avoid Playwright resource conflicts). Updates `runs[run_id]` with progress, status, and errors throughout. On completion, calls `persist_listings()` and then detects same-engine reactivation duplicates.

#### `make_run(run_id, ...) → dict`
Creates a new run status dict with id, status, progress, timestamps, and error list.

#### `mark_cancelled(runs, run_id) → None`
Marks a run as cancelled by setting status, message, and finished_at.

### Backend — `scrapers/persistence.py`

#### `persist_listings(listings, session, username) → None`
Merges a list of scraped listing dicts into the database in a single `atomic_update`. For each listing:
- If a house with the same `search_engine_id` or URL already exists in **any** of the user's sessions, it is updated in-place (price history is appended if the price changed).
- If it's new, a fresh house record is created.
- Houses that were in the session previously but absent from the current scrape are marked as `status: "removed"`.
- **Partial scrape safeguard:** If the scraper returned far fewer listings than the session previously had (likely Cloudflare block), removal is skipped to prevent mass data loss.

#### `_merge(house, listing, now) → None`
Updates a house record's mutable fields (price, images, specs, etc.) from a fresh listing dict without touching user-set fields (review, notes, manual_address, lat, lng).

### Backend — `scrapers/factory.py`

#### `get_scraper(engine: str) → BaseScraper`
Returns the correct scraper instance for the given engine name (`"zonaprop"`, `"argenprop"`, `"mercadolibre"`, or `"remax"`). Raises `ValueError` for unknown engines.

---

### Backend — `deduplicator.py`

#### `find_duplicates(session, username) → List[dict]`
Finds groups of properties across different search engines that are likely the same listing, using fuzzy address matching (Jaccard similarity ≥ 0.75), price tolerance (±10%), and square-meter tolerance (±10%). Returns groups of duplicate houses, each with a designated `keep_id` (the property from the highest-priority engine).

#### `apply_dedup(session, username, groups) → int`
Marks duplicate houses as `review: "duplicado"` and merges their metadata (notes, review status) into the kept house. Returns the number of houses marked as duplicates.

---

### Backend — `same_engine_dedup.py`

#### `find_same_engine_duplicates(session, username, run_started_at=None) → List[dict]`
Detects reactivation duplicates: cases where a real estate agent deactivated a listing and re-created it as "new" to bump it to the top. Finds pairs of houses from the **same** search engine where one was removed and one was newly added, matching on address similarity, price tolerance, and square-meter tolerance. When `run_started_at` is provided, only considers houses changed during that specific scrape run.

#### `apply_same_engine_dedup(session, username, groups) → int`
Merges selected same-engine duplicate groups. For each group: updates the old house with the new house's mutable fields, records price changes in `previous_prices`, transfers the `search_engine_id`, and deletes the new house from the database. Returns the number of houses merged.

---

### Backend — `main.py`

#### `POST /api/users/{username}/sessions/{session_id}/run`
Launches a scrape as a FastAPI background task. Returns immediately with a `run_id`. Rejects duplicate runs for the same session (returns `already_running: true`).

#### `GET /api/runs/{run_id}`
Returns current run state: `status`, `progress`, `total`, `message`, `errors`. Polled by the frontend every second while a run is active.

#### `DELETE /api/runs/{run_id}`
Cancels an active run by setting `runs[run_id]["cancelled"] = True`. The scraper checks this flag between pages and stops cleanly.

#### `PATCH /api/houses/{house_id}`
Updates user-set fields on a house: `review`, `notes`, and/or `manual_address`. Setting a `manual_address` clears the existing geocode result so it can be re-geocoded with the corrected address.

#### `POST /api/users/{username}/sessions/{session_id}/geocode`
Launches geocoding for all un-geocoded houses in a session as a background task. Accepts a `force=true` query parameter to retry previously failed addresses.

#### `POST /api/users/{username}/sessions/{session_id}/deduplicate/preview`
Finds groups of cross-engine duplicates in the session using fuzzy address matching. Returns the groups without modifying anything.

#### `POST /api/users/{username}/sessions/{session_id}/deduplicate/apply`
Marks cross-engine duplicates as `review: "duplicado"` and merges their metadata into the kept house. Optionally accepts `selected_groups` (list of group indices) to only deduplicate specific groups.

#### `POST /api/users/{username}/sessions/{session_id}/same-engine-dedup/preview`
Detects same-engine reactivation duplicates (agent deactivated + re-listed). Returns pairs without modifying anything.

#### `POST /api/users/{username}/sessions/{session_id}/same-engine-dedup/apply`
Merges same-engine reactivation duplicates: keeps the original house, transfers data from the re-listed house, and deletes the duplicate. Optionally accepts `selected_groups`.

#### `_run_geocode(house_ids, run_id, runs) → None`
Geocodes a list of house IDs concurrently (up to 10 at a time via `asyncio.Semaphore`). Each task calls `geocoder.geocode()` and saves the result with `atomic_update`. The Nominatim rate limiter inside the geocoder ensures the 1 req/s policy is respected globally across all concurrent tasks.

---

### Frontend — `app.js` (Alpine.js)

Main Alpine component. Source is split across `app.js` (core logic), `map.js` (Leaflet methods), and `constants.js` (review options, pin colors, `api()` helper). These are merged at runtime into a single Alpine component.

#### `selectSession(sessionId)`
Loads a session from the API, sets `this.houses`, resets all filter state and pagination, and switches the screen to `'session'`. Called when opening a session from the list or after creating a new one.

#### `goBack()`
Returns to the session list. Refreshes the session list from the API, resets all filter state, and stops any active geocoding poll.

#### `saveReview(house, value)`
Updates `house.review` in local state immediately (optimistic update), then sends a `PATCH /api/houses/{id}` request in the background.

#### `saveNotes(house)`
Debounced. Sends a `PATCH /api/houses/{id}` with the current `house.notes` value.

#### `triggerRun()`
Calls `POST /run` for the current session and starts polling the run status via `startPolling()`.

#### `startPolling()` / `stopPolling()`
Polls `GET /api/runs/{run_id}` every second while a run is active. Updates `runStatus` which drives the progress bar. Stops automatically when `status` is `done`, `error`, or `cancelled`.

#### `initMap()`
Initialises the Leaflet map (first call) or resizes and re-renders pins (subsequent calls). Uses `clip-path` instead of `overflow: hidden` for rounded corners to avoid clipping Leaflet's zoom animations.

#### `renderPins()`
Clears and re-renders all map pins for the filtered house list. Uses `L.circleMarker` with colour-coded fills by review status. Clicking a pin opens the detail panel.

#### `geocode(address, houseId)`
Calls `POST /api/houses/{id}/geocode` to re-geocode a single house, then polls until the run completes and refreshes the house data.

#### `openLightbox(images, index)` / `lightboxNext()` / `lightboxPrev()`
Opens the image lightbox at a given index. Arrow keys navigate images. If no lightbox is open but a detail panel is open, arrow keys open the lightbox from the first or last image.

#### `get filteredHouses()`
Computed getter. Applies all active filters (review, type, status, max price, address, real estate) and then the active sort. Fuzzy text matching uses Unicode NFD normalisation (strips accents, lowercases) so searching for "Belgrano" matches "Bélgrano".

#### `get activeExtraFilters()`
Returns the count of non-default extra filters (type, status, max price, address, real estate). Used to show a badge on the mobile Filtros toggle button.

#### `exportCSV()`
Serialises `filteredHouses` to a CSV string and triggers a browser download. Respects all active filters — what you see in the table is what you get in the file.

---

## Testing

The tests in `backend/tests/` are **standalone diagnostic scripts**, not a pytest suite. They require a live network connection and a real Chromium browser (via Playwright). They are designed to be run manually when debugging scraper issues — not as part of a CI pipeline.

All scripts should be run from the `backend/` directory:

```bash
cd backend
source .venv/bin/activate
```

---

### `test_cloudflare.py` — Diagnose Cloudflare blocks

Checks whether the local browser profile can successfully reach Zonaprop without being blocked by Cloudflare.

**Run:**
```bash
python tests/test_cloudflare.py
```

**What it checks:**
1. `_HEADLESS` flag and environment (Docker vs local)
2. Browser profile directory — existence, size, cookie files
3. HTTP response status from a test Zonaprop URL
4. Whether a Cloudflare challenge page is detected (English and Spanish variants)
5. Whether `[data-posting-type]` listing cards are found in the DOM
6. Cloudflare cookies (`cf_clearance`, `__cf_bm`, etc.)

**Common fix:** If the profile is poisoned (a Docker/headless run stored bot-flagged cookies), delete the profile the run is actually using — the script prints the exact path in its verdict:
```bash
rm -rf ~/.mudate_browser           # headed run (local)
rm -rf ~/.mudate_browser_headless  # headless run / Docker
```
A blocked run also prints this command in the run banner.

---

### `test_block_detection.py` — Unit tests for block detection

Covers the logic that decides whether a Zonaprop page is a Cloudflare challenge rather than a results page. **Runs fully offline**: no browser is launched and no network request is made, because detection is factored out of navigation into the pure function `_looks_like_challenge()`.

**Run (from `backend/`):**
```bash
python -m tests.test_block_detection
python tests/test_block_detection.py   # equivalent
```

**What it covers:**
1. Block statuses (`403`, `429`, `503`, …) are always treated as blocked, and win even when the page cannot be inspected at all
2. English and Spanish challenge titles (`Just a moment…`, `Un momento…`, `Attention Required!`)
3. Challenge text markers (`verifying you are human`, `verifica que eres un humano`, and the Spanish 403 wording observed live on Zonaprop)
4. Challenge **markup** markers (`_cf_chl_opt`, `cf-browser-verification`) — these live in a `<script>`, so they are matched against the markup, never `innerText`
5. The false-positive guard — a page recognised as a results page is never flagged, and `un momento` matches the **title** only, never listing text
6. `_blocked_error()` produces a message containing the profile path and the `rm -rf` command
7. `_raise_if_blocked()` raises `ScrapeBlockedError` on a block, and stays silent on a healthy page, a malformed payload, or when the page cannot be inspected
8. `_scrape_detail()` checks the status **before** waiting on selectors, re-raises instead of swallowing blocks, and keeps the original "log and return `{}`" contract for genuine navigation errors
9. `_is_usable_detail()` gates the marker pass — empty extractions, per-field values, and blank/empty values that must not count as content
10. `_should_abort_on_detail_blocks()` — complete blocks always abort, isolated blocks are noise, `>= 3` blocks **and** `>= 50 %` escalates
11. The wiring through `scrape_search()` against a stubbed browser: the counter, threshold and escalation all run for real, the blocked listing still comes back with its card-level data, and tabs are still closed by the `finally` when the detail call raises

---

### `test_detail.py` — Smoke-test detail page extraction

Loads two real Zonaprop property URLs through the same `_scrape_detail()` function used in production and prints all extracted fields in a formatted table.

**Run:**
```bash
python tests/test_detail.py
```

Useful when a field (price, specs, images) is returning `None` for a specific listing — add the URL to the `URLS` list at the top of the file to inspect it.

---

### `test_images.py` — Debug image extraction

Deep-inspects why a listing returns fewer images than expected. For each URL it shows:
- How many images the BeautifulSoup extractor returns
- How many images the JavaScript extractor (same as production) returns
- All photo-like arrays found in `__NEXT_DATA__` (Zonaprop embeds all page data in a Next.js JSON blob)
- What the DOM gallery portal contains (`<img>` tags, `srcset`, lazy-load attributes)

**Run:**
```bash
python tests/test_images.py
```

Add the problematic URL to `URLS_PROBLEM` at the top of the file before running.

---

### `test_pagination.py` — Validate pagination

Tests that the scraper can navigate from page 1 to page 2 using Zonaprop's click-based pagination. Prints the cards found on each page and the structure of pagination links/buttons in the DOM.

**Run:**
```bash
python tests/test_pagination.py "/casas-ph-venta-san-isidro-300000-310001-dolar.html"
```

The argument is the search filter path (same format used when creating a session).

---

### `test_suggested.py` — Inspect suggested listings

Examines the "suggested" or "related" listings that Zonaprop injects into search results pages (listings that don't strictly match the filter). Useful for understanding how many results on a given page are genuine matches vs. suggestions.

**Run:**
```bash
python tests/test_suggested.py
```

---

### `test_argenprop.py` — Diagnostic tests for Argenprop

Runs a suite of diagnostic tests against the Argenprop scraper, covering listing extraction, detail page parsing, and pagination.

**Run:**
```bash
python tests/test_argenprop.py
```

---

### `test_argenprop_pagination.py` — Argenprop pagination validation

Validates that the Argenprop scraper correctly paginates through search results, testing URL format, page navigation, and listing extraction across multiple pages.

**Run:**
```bash
python tests/test_argenprop_pagination.py
```

---

### `test_mercadolibre.py` — Diagnostic tests for MercadoLibre

Runs diagnostic tests against the MercadoLibre scraper, covering click-based pagination, listing card parsing, and detail page extraction.

**Run:**
```bash
python tests/test_mercadolibre.py
```

---

### `test_remax.py` — Diagnostic tests for Remax

Runs diagnostic tests against the Remax scraper, covering API-based search, image URL construction, listing detail extraction, and URL format handling.

**Run:**
```bash
python tests/test_remax.py
```

---

### `test_persistence.py` — Multi-engine persistence behavior

Tests the persistence layer's behavior when scraping multiple engines in the same session: deduplication across engines, price history tracking, and the "removed" marking logic.

**Run:**
```bash
python -m tests.test_persistence
```

---

### `test_pagination_all.py` — Pagination verification for all engines

Runs pagination tests across all four scrapers (Zonaprop, Argenprop, MercadoLibre, Remax) to verify that each engine correctly navigates through multiple pages of results.

**Run:**
```bash
python tests/test_pagination_all.py
```

---

### When to run which test

| Symptom | Test to run |
|---|---|
| Scraper returns 0 results | `test_cloudflare.py` |
| Run banner reports "Zonaprop bloqueado" (HTTP 403) | `test_cloudflare.py` to diagnose the profile, `test_block_detection.py` for the detection logic |
| A specific Zonaprop listing is missing fields | `test_detail.py` |
| A listing shows only 1 image | `test_images.py` |
| Only page 1 is scraped (Zonaprop) | `test_pagination.py` |
| Result count seems higher than expected | `test_suggested.py` |
| Argenprop scraper not working | `test_argenprop.py` |
| Argenprop only scrapes page 1 | `test_argenprop_pagination.py` |
| MercadoLibre scraper not working | `test_mercadolibre.py` |
| Remax scraper not working | `test_remax.py` |
| Deduplication or price history issues | `test_persistence.py` |
| Any engine's pagination broken | `test_pagination_all.py` |
