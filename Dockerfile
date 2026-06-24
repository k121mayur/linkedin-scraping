# Playwright's official Python image ships Chromium + all OS deps preinstalled.
# Keep this tag in lockstep with the pinned `playwright==1.48.0` in
# requirements.txt — a mismatch makes the browser fail to launch at runtime.
FROM mcr.microsoft.com/playwright/python:v1.48.0-focal

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000 \
    DRY_RUN=false \
    PLAYWRIGHT_HEADLESS=true

# Install Python dependencies first (layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Guarantee the Chromium build matching the pinned Playwright client is present,
# even if the base image drifts. No-op/fast when already baked into the image.
RUN python -m playwright install chromium

# Copy application code.
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
