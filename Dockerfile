# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11.9-slim AS base

# Install system dependencies required by Playwright / Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its OS-level dependencies via Playwright
RUN playwright install chromium --with-deps

# Copy application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create the data directory (overridden by a volume in docker-compose)
RUN mkdir -p /data

# ── Runtime config ─────────────────────────────────────────────────────────────
ENV DB_PATH=/data/db.json \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Use $PORT so Railway (and similar platforms) can inject their own port;
# fall back to 8000 for local Docker usage.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/api/scheduler || exit 1

CMD ["sh", "-c", "cd /app/backend && uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
