# Prompt-Driven LinkedIn Job Extraction Engine

Describe what you need in plain English — the engine parses your intent, searches LinkedIn, scores relevance, and exports structured results.

## Quick Start

```bash
# 1. Set up environment
cp .env.example .env
# Edit .env with your LinkedIn credentials and LLM key

# 2. Install dependencies
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 3. Dry-run test (no LinkedIn calls)
DRY_RUN=true python app.py

# 4. Real run
DRY_RUN=false python app.py
```

Open http://localhost:5000 and enter a prompt like:

> Extract NGO sector accountant and junior financial analyst roles in India, max 100 jobs

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LINKEDIN_EMAIL` | — | LinkedIn login email |
| `LINKEDIN_PASSWORD` | — | LinkedIn password |
| `LLM_PROVIDER` | `ollama` | `openai`, `ollama`, or `anthropic` |
| `LLM_API_KEY` | — | API key (x-api-key for Ollama) |
| `LLM_BASE_URL` | `https://ollama.siliconmango.in` | Base URL for Ollama |
| `LLM_MODEL` | `gemma4:31b` | Model name |
| `DRY_RUN` | `true` | Mock mode — no LinkedIn or LLM calls |
| `RELEVANCE_THRESHOLD` | `0.65` | Min score to include a job |
| `MAX_JOBS_DEFAULT` | `100` | Default job target |
| `PORT` | `5000` | Flask port |

## Architecture

```
User Prompt → [LLM Parser] → SearchPlan
  → [Search Strategy] → (query, location) queue
    → [LinkedIn Client] → job cards
      → [Job Extractor] → full detail
        → [Relevance Filter] → scored jobs
          → [Database] → SQLite persistence
            → [Exporter] → xlsx/csv/json
```

## Docker

```bash
docker compose build && docker compose up
```

## API

- `POST /scrape` — `{"prompt": "...", "max_jobs": 100}` → `{"run_id": 1}`
- `GET /stream/<run_id>` — SSE progress stream
- `GET /download/<run_id>/xlsx` — Excel export
- `GET /download/<run_id>/csv` — CSV export
- `GET /download/<run_id>/json` — JSON export
- `GET /runs/<run_id>` — Run metadata + jobs

## Dry-Run Mode

Set `DRY_RUN=true` to test the full pipeline without touching LinkedIn or calling any LLM. All searches return mock data, scores use keyword matching.
