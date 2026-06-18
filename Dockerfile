FROM mcr.microsoft.com/playwright/python:v1.48.0-focal

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=5000

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-5000} --worker-class sync --threads 4 --timeout 360 app:app"]
