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
            linkedin_client.get_job_detail() → per-job inline enrichment (full description)
            database.upsert_job()          → SQLite persistence + dedup by linkedin_job_id
       → exporter.export_*()              → xlsx / csv / json
```

Directory layout:
```
app.py                  Flask routes + SSE; spawns each scrape in a daemon thread
config/__init__.py      ALL env vars read once here (single source of truth)
config/ai_config.py     LLM prompt templates + AI constants (AI_BATCH_SIZE=20)
engine/
  prompt_parser.py      prompt → SearchPlan (LLM, _heuristic_parse fallback)
  search_strategy.py    builds primary + relaxed (broadening) SearchItem queues
  self_refinement.py    ORCHESTRATOR — generator, yields Progress, loops to max_jobs/max_attempts(40)
  linkedin_client.py    Playwright driver (module-global _pw/_browser/_context/_page singletons)
  email_verifier.py     Gmail IMAP client — fetches LinkedIn email-PIN codes for automated login
  job_extractor.py      detail_pass() enriches relevant cards with full descriptions
  relevance.py          batched LLM 0.0–1.0 scoring, _keyword_score fallback
  database.py           SQLite: search_runs, jobs, search_attempts; auto-inits on import
  exporter.py           xlsx (pandas/openpyxl) / csv / json; EXPORT_COLUMNS list canonical
  llm_client.py         pluggable LLM over urllib: openai / ollama / anthropic
templates/index.html    web UI — Tamuku-branded light design system (cream/peach/orange accent); masthead with logo + status pill; centered hero (eyebrow/h1/p) above a white action panel; prompt textarea + India-focused example chips + 50/100/250 presets; live 3-stat progress grid (Collected/Target/Passes) with shimmer bar via EventSource + download links on completion; Poppins + Inter fonts; vanilla JS only; responsive (920px/560px breakpoints); accessible (skip-link, aria-live, prefers-reduced-motion)
data/                   auto-created; jobs.db + exports (git-ignored)
_archive/               superseded standalone scripts — reference only, NOT live code
```

Key files to read first: `app.py` → `engine/self_refinement.py` → `config/__init__.py`.

HTTP endpoints (in `app.py`): `GET /`, `POST /scrape` (→ run_id), `GET /stream/<run_id>` (SSE),
`GET /download/<run_id>/<fmt>` (filename derived from prompt slug via `_download_stem()`), `GET /runs/<run_id>`, `GET /health`.

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

- **Generator-based orchestration**: `self_refinement.run()` is a generator that `yield`s `Progress` dataclasses (consumed by the Flask SSE stream) and `return`s the final job list. Yields once at the start of each attempt (to refresh the "Searching: …" line) and once per collected job (so the UI counter increments 1, 2, 3, … not in batch jumps). Calls `li_close()` at the end of each run to release the Playwright browser.
- **Two-phase scoring**: relevance is scored on card data first (cheap, title/company only); `linkedin_client.get_job_detail()` fetches the full description inline for each job that passes the threshold, one at a time. Gated by `FETCH_DETAILS` env var. (`job_extractor.detail_pass` is no longer called by the active pipeline.)
- **LLM + heuristic fallback pair**: `prompt_parser` and `relevance` each have an LLM path and a keyword/regex fallback (`_heuristic_parse`, `_keyword_score`) selected on `DRY_RUN` or LLM failure. Relevance scoring judges on both title and description (not title/company only).
- **Provider dispatch map**: `llm_client` selects `_call_openai`/`_call_ollama`/`_call_anthropic` from a dict keyed on `LLM_PROVIDER`.
- **Conditional x-api-key + browser UA**: Ollama calls only attach `x-api-key` when `_is_real_key(LLM_API_KEY)` returns true (Silicon Mango endpoint is keyless by default); all providers send a browser-like `_USER_AGENT` to avoid HTTP 403 from the reverse proxy.
- **Transient-error retry**: `_urlopen_json()` retries up to 3 times with exponential backoff (`2*(attempt+1)s`) on status codes `{429, 500, 502, 503, 504, 520–524, 530}` (covers Cloudflare tunnel errors).
- **Thinking disabled for gemma4**: `_call_ollama` sets `"think": False` in the request body to suppress gemma4's reasoning phase, avoiding ~90s latency per relevance/parse batch.
- **Robust JSON extraction**: `llm_client._extract_json()` strips markdown fences and scans for the first balanced `{...}` or `[...]` block before raising, tolerating model prose around JSON. Ollama callers also receive an appended JSON-only system instruction (Ollama lacks `response_format`).
- **Background-thread + queue + SSE**: `POST /scrape` returns immediately; work runs in a daemon thread pushing `Progress` onto a per-run `queue.Queue` drained by `GET /stream/<run_id>`.
- **Thread-local SQLite** with WAL mode; dedup by unique `linkedin_job_id` via `seen_job_ids()`.
- **Automated email-PIN login**: `linkedin_client` detects LinkedIn's email-verification challenge and delegates to `email_verifier.fetch_verification_code()` (Gmail IMAP) when `GMAIL_*` vars are configured.
- **Saved-session reuse**: `_ensure_auth()` tries to restore auth from `AUTH_FILE_PATH` (Playwright storage state) before doing a full login; saves state on successful login.
- **Paginated card extraction**: `search()` loops up to `MAX_SEARCH_PAGES`, calling `_wait_for_cards()` → `_scroll_to_load()` → `_extract_cards()` (DOM evaluation keyed on `/jobs/view/<id>` anchors). Stops early when no new cards appear.
- **Canonical job links**: `canonical_view_url(job_id)` is the single source of truth for clickable job URLs, using the `LINKEDIN_JOB_VIEW_URL` config knob.
- **Progressive broadening via `SearchItem.action`**: each item in the search queue carries an `action` tag (`seed` | `broaden_query` | `widen_location` | `relax_filters`); `build_relaxed_queue(plan, attempts)` escalates strategies across attempt thresholds (0-3, 0-6, 0-9, then broad single-word).
- **Prompt-derived download filenames**: `_download_stem(run_id)` in `app.py` slugifies the run's prompt (lowercase, non-alphanumeric → `_`, capped at 60 chars) for `Content-Disposition` filenames on `GET /download/<run_id>/<fmt>`; falls back to `run_{run_id}` when no prompt is stored.

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
