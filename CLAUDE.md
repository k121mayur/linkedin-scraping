# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- AUTO-MANAGED: project-description -->
## Overview

A **prompt-driven LinkedIn job extraction engine**. A user types a plain-English request
("Extract NGO sector junior accountant roles in India, max 100 jobs"); the engine parses intent
into a structured search plan, drives LinkedIn via Playwright, scores each job for relevance with
an LLM, persists to SQLite, and exports to xlsx/csv/json. A Flask web UI streams live progress
over Server-Sent Events.

Key features:
- Natural-language prompt → structured `SearchPlan` (LLM, with heuristic fallback)
- Self-broadening search loop ("self-refinement") when results run dry
- Pluggable LLM layer (OpenAI / Ollama / Anthropic) over raw `urllib`, no SDK
- Graceful degradation: any LLM/Playwright/JSON failure falls back to heuristics — the run never hard-fails
- SQLite persistence with cross-run dedup; xlsx/csv/json export

<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: build-commands -->
## Build & Development Commands

```bash
# Setup (Python 3.10+)
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# Run the web app (defaults to DRY_RUN=true — safe, no LinkedIn/LLM calls)
python app.py                       # serves http://localhost:5000
DRY_RUN=false python app.py         # real scrape — needs LinkedIn creds + LLM key in .env

# Production (Docker)
docker compose build && docker compose up

# Production (Raspberry Pi + Cloudflare Tunnel)
sudo ./run.sh                       # idempotent installer
```

**There is no test suite** — do not invent test commands. Verify changes by running the app in
`DRY_RUN=true` (the full pipeline runs against mock data) and exercising the HTTP API.

<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: architecture -->
## Architecture

The pipeline is a linear transform chain, each stage in its own `engine/` module:

```
prompt → prompt_parser.parse() → SearchPlan dict
       → search_strategy.build_queue() → ordered [SearchItem(query, location)]
       → self_refinement.run() drives the loop:
            linkedin_client.search()      → job cards
            relevance.filter_relevant()   → cards scored on title/company (cheap, no page nav)
            job_extractor.detail_pass()   → relevant cards enriched with full descriptions
            database.upsert_job()          → SQLite persistence + dedup by linkedin_job_id
       → exporter.export_*()              → xlsx / csv / json
```

Directory layout:
```
app.py                  Flask routes + SSE; spawns each scrape in a daemon thread
config/__init__.py      ALL env vars read once here (single source of truth)
config/ai_config.py     LLM prompt templates + AI constants (AI_BATCH_SIZE=10)
engine/
  prompt_parser.py      prompt → SearchPlan (LLM, _heuristic_parse fallback)
  search_strategy.py    builds primary + relaxed (broadening) SearchItem queues
  self_refinement.py    ORCHESTRATOR — generator, yields Progress, loops to max_jobs/max_attempts(40)
  linkedin_client.py    Playwright driver (module-global _browser/_context/_page singletons)
  email_verifier.py     Gmail IMAP client — fetches LinkedIn email-PIN codes for automated login
  job_extractor.py      detail_pass() enriches relevant cards with full descriptions
  relevance.py          batched LLM 0.0–1.0 scoring, _keyword_score fallback
  database.py           SQLite: search_runs, jobs, search_attempts; auto-inits on import
  exporter.py           xlsx (pandas/openpyxl) / csv / json; EXPORT_COLUMNS list canonical
  llm_client.py         pluggable LLM over urllib: openai / ollama / anthropic
templates/index.html    web UI (prompt form + live progress bar via EventSource)
data/                   auto-created; jobs.db + exports (git-ignored)
_archive/               superseded standalone scripts — reference only, NOT live code
```

Key files to read first: `app.py` → `engine/self_refinement.py` → `config/__init__.py`.

HTTP endpoints (in `app.py`): `GET /`, `POST /scrape` (→ run_id), `GET /stream/<run_id>` (SSE),
`GET /download/<run_id>/<fmt>`, `GET /runs/<run_id>`, `GET /health`.

<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: conventions -->
## Code Conventions

- **Python, snake_case** for functions/variables; `from __future__ import annotations` at the top of engine modules; type hints used throughout.
- **Pure-stdlib HTTP** (`urllib`) — deliberately no `requests`/`openai`/`anthropic` SDK. Keep new outbound calls consistent.
- **Centralized config**: add new env knobs in `config/__init__.py`, not via scattered `os.getenv` calls.
- **Defensive boundaries**: every external boundary (LLM, Playwright, JSON parse) is wrapped in try/except with a heuristic or empty fallback. Preserve this "never crash the run" posture.
- **Data shape**: stages pass plain `dict`s, except the `SearchItem` and `Progress` dataclasses. The canonical job-id key is `linkedin_job_id` once a card is enriched (`job_id` on raw cards).

<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: patterns -->
## Detected Patterns

- **Generator-based orchestration**: `self_refinement.run()` is a generator that `yield`s `Progress` dataclasses (consumed by the Flask SSE stream) and `return`s the final job list.
- **Two-phase scoring**: relevance is scored on card data first (cheap, title/company only); `detail_pass` fetches full descriptions only for jobs that pass the threshold. Gated by `FETCH_DETAILS` env var.
- **LLM + heuristic fallback pair**: `prompt_parser` and `relevance` each have an LLM path and a keyword/regex fallback (`_heuristic_parse`, `_keyword_score`) selected on `DRY_RUN` or LLM failure.
- **Provider dispatch map**: `llm_client` selects `_call_openai`/`_call_ollama`/`_call_anthropic` from a dict keyed on `LLM_PROVIDER`.
- **Conditional x-api-key**: Ollama calls only attach `x-api-key` when `_is_real_key(LLM_API_KEY)` returns true — the Silicon Mango endpoint is keyless by default.
- **Robust JSON extraction**: `llm_client._extract_json()` strips markdown fences and scans for the first balanced `{...}` or `[...]` block before raising, tolerating model prose around JSON.
- **Background-thread + queue + SSE**: `POST /scrape` returns immediately; work runs in a daemon thread pushing `Progress` onto a per-run `queue.Queue` drained by `GET /stream/<run_id>`.
- **Thread-local SQLite** with WAL mode; dedup by unique `linkedin_job_id` via `seen_job_ids()`.
- **Automated email-PIN login**: `linkedin_client` detects LinkedIn's email-verification challenge and delegates to `email_verifier.fetch_verification_code()` (Gmail IMAP) when `GMAIL_*` vars are configured.

<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: git-insights -->
## Git Insights

- The project was rebuilt from standalone Selenium/Playwright scripts (now in `_archive/`) into the prompt-driven engine — see commit "Prompt based scrapping". Original design notes live in `PLAN.md`, `TASKS.md`, `AI_SCRAPER_PLAN.md`.
- Deployment evolved toward Raspberry Pi + Cloudflare Tunnel (`run.sh`, multiple "Updated run.sh" commits) after earlier Render deployment attempts.

<!-- END AUTO-MANAGED -->

<!-- MANUAL -->
## Custom Notes — Critical gotchas

These are hand-written and never auto-modified.

- **`DRY_RUN` defaults to `false`** (see `config/__init__.py`) — real scraping is the default. Set
  `DRY_RUN=true` explicitly to use mock data: `linkedin_client.search` returns mock cards,
  `get_job_detail` returns mock descriptions, and relevance uses keyword matching with zero network
  calls to LinkedIn or the LLM. Most "why is it returning fake jobs" confusion used to trace back to
  this; now the opposite confusion can occur if `.env` credentials are missing.

- **The SQLite DB auto-initializes on import** of `engine/database.py` (the module ends with a bare
  `init_db()` call). Importing the module creates `data/jobs.db`.

- **Playwright state is module-global** (`_browser`, `_page` singletons in
  `engine/linkedin_client.py`) and **not thread-safe**. The Flask app runs each scrape in its own
  daemon thread, so concurrent runs would race on the shared browser. Treat single-run-at-a-time as
  the supported model.

- **Default LLM provider** is `ollama` against `https://ollama.siliconmango.in` (auth via
  `x-api-key` header), model `gemma4:31b`. Switch via `LLM_PROVIDER` / `LLM_MODEL` in `.env`.

- **Self-refinement broadening order** when the primary queue is exhausted before reaching the
  target (`search_strategy.build_relaxed_queue`): drop sector keywords → widen locations → drop
  location → single broad keyword.

- **Gmail IMAP verification** (`GMAIL_*` env vars) is **live and wired** via
  `engine/email_verifier.py`. When LinkedIn presents an email-PIN challenge during login,
  `linkedin_client` calls `fetch_verification_code()` which polls the configured Gmail inbox
  (IMAP SSL) and submits the 6-digit code automatically. Requires `GMAIL_USERNAME` and
  `GMAIL_APP_PASSWORD` to be set; if absent, the challenge falls through to manual handling.

<!-- END MANUAL -->
