# syntax=docker/dockerfile:1
# Slim Python base (~45 MB) + Chromium-only Playwright install (~350 MB total
# download) instead of the 2 GB+ mcr.microsoft.com/playwright image that ships
# all three browsers. On a slow connection this is the difference between
# ~40 minutes and a few minutes — and it only happens once: every later build
# reuses the cached layers and finishes in seconds.
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000 \
    DRY_RUN=false \
    PLAYWRIGHT_HEADLESS=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Python dependencies first (layer caching). The BuildKit cache mount keeps
# downloaded wheels across builds, so a requirements.txt tweak doesn't
# re-download pandas & co.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 60 -r requirements.txt

# Chromium only (no firefox/webkit) + the exact OS libs it needs.
# This layer is cached until requirements.txt changes — code edits never rerun it.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy application code last — day-to-day rebuilds only redo this layer.
COPY . .

# Data dir for jobs.db + exports (also a sensible volume mount point).
RUN mkdir -p /app/data/exports
VOLUME ["/app/data"]

EXPOSE 5000

# Health check hits the app's /health route (urlopen raises on any non-2xx).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

# Single worker (in-memory progress/stop state is shared across threads within
# one process) + threads for the SSE stream alongside the background scrape.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-5000} --workers 1 --worker-class gthread --threads 8 --timeout 1800 app:app"]
