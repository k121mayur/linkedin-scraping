# LinkedIn Scraping Service

Automated LinkedIn job scraper built with Flask, Playwright, and pandas. The service runs both locally and inside containers, exposes a simple UI, and automatically solves LinkedIn email-based verification challenges via Gmail IMAP.

## Requirements

- Python 3.11+ (for local execution outside Docker)
- Google IMAP access and an app password for the configured Gmail address
- Playwright Chromium dependencies (already present in the provided Docker image)
- Docker + Docker Compose (optional but recommended for deployment)
- Gunicorn 22+ (installed via `requirements.txt`)

## Environment Variables

Duplicate `.env.example` (or create `.env`) with the following keys:

```
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=super-secret
LINKEDIN_AUTH_FILE=playwright_auth.json
DEFAULT_KEYWORD=Software Engineer
DEFAULT_LOCATION=India
PLAYWRIGHT_HEADLESS=true
FLASK_DEBUG=false
PORT=5000

GMAIL_USERNAME=shgplusplus@gmail.com
GMAIL_APP_PASSWORD=app-password-generated-via-google
GMAIL_IMAP_HOST=imap.gmail.com
GMAIL_IMAP_PORT=993
GMAIL_IMAP_FOLDER=INBOX
GMAIL_VERIFICATION_SENDER=security-noreply@linkedin.com
GMAIL_POLL_INTERVAL=8
GMAIL_POLL_TIMEOUT=180
```

> Keep Gmail secrets private; the scraper needs IMAP access to fetch verification codes automatically.

## Running Locally (Python)

```bash
python -m venv .venv
source .venv/Scripts/activate  # PowerShell: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
python final_scrapping_script.py
```

Playwright browsers are bundled in the Docker base image. For local runs install Chromium once:

```bash
playwright install chromium
```

## Running with Docker

```bash
docker compose build
docker compose up
```

The service listens on port `5000` by default (`http://localhost:5000`). Override by exporting `PORT` (Compose and the Dockerfile run `gunicorn -b 0.0.0.0:${PORT:-5000}`), e.g. `PORT=8080 docker compose up`.

## Production (Render / Gunicorn)

- The container now starts via Gunicorn: `gunicorn -b 0.0.0.0:${PORT:-5000} --threads 4 --timeout 360 final_scrapping_script:app`.
- Set Render’s start command to `gunicorn -b 0.0.0.0:$PORT --threads 4 --timeout 360 final_scrapping_script:app` (Render injects `PORT` at runtime; never hard-code it).
- Ensure `.env` or Render environment variables include the Gmail and LinkedIn credentials; do not commit secrets.

## Troubleshooting

- **Verification loops**: confirm IMAP settings and that Gmail labels/deliverability still include LinkedIn emails. Logs will show “Waiting for LinkedIn verification email…” if the code cannot be fetched.
- **Playwright browser crashes**: increase `shm_size` in Docker (`docker-compose.yml` already uses `1gb`). For self-hosted deployments, ensure the host has 1 GB+ shared memory.
- **LinkedIn rate limiting**: consider rotating headless/headful mode, or throttle scraping frequency; repeated verification prompts indicate suspicious automation activity.

## Code Overview

- `final_scrapping_script.py` – Flask routes, scraping logic, verification helpers, and Gunicorn app factory.
- `config.py` – loads environment variables and defaults.
- `templates/index.html` – simple UI for kicking off a scrape.
- `docker-compose.yml` / `Dockerfile` – container build and runtime configuration (Gunicorn-based).

Contributions, bug reports, and pull requests are welcome!
