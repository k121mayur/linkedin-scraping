# Task List — Prompt-Based LinkedIn Extraction Engine

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` cancelled

## Phase 0 — Setup

- [x] T0.1  Save this plan + task list to root (done as part of initial response)
- [ ] T0.2  Confirm LLM provider with user (default: OpenAI gpt-4o-mini; Ollama supported)
- [x] T0.3  Move dead scripts (`main.py`, `seleniu_way.py`, `backup - local scrapping.py`) to `_archive/`

## Phase 1 — Foundation

- [x] T1  Rework `config.py` to actually load env vars; add `LLM_*`, `RELEVANCE_*`, `MAX_JOBS_DEFAULT`, `DRY_RUN` knobs
- [x] T2  Rework `config/ai_config.py` with provider/model/threshold + prompt templates
- [x] T3  Create `engine/` package skeleton (`__init__.py`)
- [x] T4  Implement `engine/database.py` — schema, `upsert_job`, `create_run`, `finish_run`, `log_attempt`, `seen_job_ids`
- [x] T5  Implement `engine/llm_client.py` — OpenAI default, Anthropic + Ollama stubs, JSON-mode helper

## Phase 2 — Core extraction

- [x] T6  Implement `engine/prompt_parser.py` — `parse(prompt, max_jobs) -> SearchPlan` with heuristic fallback
- [x] T7  Implement `engine/search_strategy.py` — `(query, location)` queue builder with broadening
- [x] T8  Implement `engine/linkedin_client.py` — auth via storage state, public + logged-in search, throttle, captcha backoff
- [x] T9  Implement `engine/job_extractor.py` — search-card parse + per-job detail fetch (with concurrency cap + cache)
- [x] T10 Implement `engine/relevance.py` — batched LLM scoring + keyword fallback
- [x] T11 Implement `engine/self_refinement.py` — orchestrator with attempt budget + telemetry

## Phase 3 — Surface

- [x] T12 Implement `engine/exporter.py` — xlsx / csv / json with relevance-sorted rows
- [x] T13 Rewrite `app.py` — `/` GET, `POST /scrape`, `GET /stream/<run_id>` (SSE), `GET /download/<run_id>.<fmt>`, `GET /runs/<run_id>` (JSON)
- [-] T14 Port Gmail IMAP verification helper (low priority — deferred)
- [x] T15 Rewrite `templates/index.html` — prompt textarea + N + live progress + download

## Phase 4 — Plumbing

- [x] T16 Slim `requirements.txt` (drop torch/transformers; keep openai/Flask/playwright/pandas/openpyxl/python-dotenv/gunicorn)
- [x] T17 Update `Dockerfile` to `gunicorn ... app:app`
- [x] T18 Update `docker-compose.yml` with `data/` volume
- [x] T19 Update `run.sh` to point to `app:app`
- [x] T20 Update `README.md` with new prompt-driven flow + env vars
- [x] T21 Create `.env.example`
- [x] T22 Create `data/.gitkeep` and add `data/` to `.gitignore`

## Phase 5 — Validation

- [x] T23 Smoke test: `DRY_RUN=true` end-to-end with a mock prompt — parse → plan → mock extract → mock relevance → DB → export
- [ ] T24 Smoke test: real run with a tiny prompt (`max_jobs=5`, `DRY_RUN=false`) — needs LinkedIn credentials
- [ ] T25 Document known limitations and the `DRY_RUN` mode in README

---

## Execution order (one-by-one)

I'll work top-to-bottom. After each task I'll mark it done in this file **and** in the `todo` tool, then move to the next. If a task blocks (e.g. LinkedIn creds not configured), I'll record the blocker in the response and ask for guidance rather than fabricate a result.

## Notes for the user

- `data/` and `.env` are gitignored
- The first run needs a one-time `playwright_auth.json` — generate it by running with `DRY_RUN=false` once and solving the LinkedIn email/IMAP verification if it appears
- All public-facing endpoints return JSON; only the export endpoint returns the binary
- The dry-run mode is the recommended way to test the orchestration end-to-end before spending LinkedIn trust
