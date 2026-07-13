# CONTEXT.md — LinkedIn Scraper Deep-Dive

> **Generated:** 2026-07-13 · **Scope:** read-only code investigation, no modifications  
> **Purpose:** Build accurate context for upcoming feature work  

---

## 1. Architecture Map

### Corrected Assembly Line (what the code actually does)

The README's diagram (§2) is largely accurate for the **Jobs** pipeline but **omits an entire second pipeline** (Grants) and several cross-cutting concerns that were added after the README was written.

```
                                 ┌─────────────────┐
                                 │  Login (session) │
                                 │  login.html      │
                                 └────────┬────────┘
                                          │ session cookie
                                          ▼
                              ┌───────── app.py ─────────┐
                              │  POST /scrape             │
                              │  { prompt, max_jobs, mode }│
                              │   mode = "jobs" | "grants" │
                              └──────┬──────────┬─────────┘
                                     │ daemon   │ daemon
                                     │ thread   │ thread
                          ┌──────────▼──┐    ┌──▼──────────────┐
                          │  JOBS PATH  │    │   GRANTS PATH   │
                          │             │    │                  │
                          │ prompt_parser│    │ plan_keywords() │
                          │   .parse()  │    │   (LLM/default) │
                          │      │      │    │       │          │
                          │      ▼      │    │       ▼          │
                          │ search_     │    │ linkedin_client  │
                          │ strategy    │    │  .search_posts() │
                          │ .build_     │    │       │          │
                          │  queue()    │    │       ▼          │
                          │      │      │    │ image OCR (vision│
                          │      ▼      │    │   LLM) + external│
                          │ self_refine │    │   site fetch     │
                          │ ment.run()  │    │       │          │
                          │  ┌─ loop ─┐ │    │       ▼          │
                          │  │li_srch │ │    │ analyze_post()   │
                          │  │relevnce│ │    │   (LLM/heuristic)│
                          │  │detail  │ │    │       │          │
                          │  │db.upsrt│ │    │       ▼          │
                          │  └────────┘ │    │ db.upsert_grant()│
                          │      │      │    │       │          │
                          │      ▼      │    │       ▼          │
                          │ exporter    │    │ grants_exporter  │
                          └─────────────┘    └─────────────────┘
                                   │              │
                                   ▼              ▼
                            queue.Queue  ──► SSE /stream/<run_id>
                                   │
                                   ▼
                          /download/<run_id>/<fmt>
                          (xlsx | csv | json)
```

### Key architectural additions the README doesn't mention

| Feature | Where | Notes |
|---------|-------|-------|
| **Grants pipeline** | `engine/grants_pipeline.py`, `engine/grants_exporter.py` | Full second pipeline for LinkedIn-post grant scraping with image OCR + external site fetch |
| **Vision LLM** | `engine/llm_client.py` `chat_vision()` | Multimodal image reading for all three providers |
| **Authentication system** | `app.py`, `templates/login.html`, `engine/database.py` users table, `engine/mailer.py` | Hardcoded admin + SQLite-stored users with password hashing |
| **User management API** | `app.py` `/admin/users` endpoints | CRUD for users by admin role |
| **Welcome email** | `engine/mailer.py` | SMTP welcome email when admin creates a user |
| **Cooperative stop** | `app.py` `/stop/<run_id>`, `threading.Event` | User can halt a running scrape; everything collected stays |
| **CLI entry point** | `cli.py` | Headless terminal-driven pipeline with file export |
| **Browser-thread dispatcher** | `engine/linkedin_client.py` lines 963-1021 | ALL Playwright work is serialized onto a single persistent daemon thread; **the old "module globals not thread-safe" caveat is gone** |

---

## 2. Module Reference Table

### `app.py` — Flask web server + SSE
[app.py](file:///e:/Silicon%20Mango/linkedin-scraping/app.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | HTTP entry point — routes, auth, SSE streaming, background thread dispatch |
| **Public interface** | Routes: `GET /`, `POST /login`, `POST /logout`, `GET /admin/users`, `POST /admin/users`, `DELETE /admin/users/<id>`, `POST /scrape`, `POST /stop/<run_id>`, `GET /stream/<run_id>`, `GET /download/<run_id>/<fmt>`, `GET /runs/<run_id>`, `GET /health` |
| **Data shapes** | `/scrape` accepts `{"prompt": str, "max_jobs": int, "mode": "jobs"|"grants"}`, returns `{"run_id": int, "status": "started", "mode": str}` |
| **Side effects** | Spawns daemon threads per scrape; maintains in-memory `_progress_queues` (dict[int, queue.Queue]) and `_stop_events` (dict[int, threading.Event]) |
| **Error handling** | `_scrape_worker` catches all exceptions and pushes `{"error": str}` to the queue, followed by `None` sentinel. SSE generator times out after 300s with a timeout error. |
| **Dependencies** | `flask`, `werkzeug.security`, `config`, `engine.prompt_parser`, `engine.self_refinement`, `engine.grants_pipeline`, `engine.database`, `engine.exporter`, `engine.grants_exporter`, `engine.mailer` |

---

### `config/__init__.py` — Centralized configuration
[config/__init__.py](file:///e:/Silicon%20Mango/linkedin-scraping/config/__init__.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Single source of truth for all env vars; reads `.env` via `python-dotenv` at import time |
| **Precedence** | `os.getenv()` with hardcoded defaults → `.env` file (loaded by `load_dotenv()` at line 7, which writes to `os.environ` if the var isn't already set) → real environment variables **win** over `.env` |
| **Side effects** | Creates `data/` and `data/exports/` directories on import |
| **Notable** | `DRY_RUN` defaults to **`false`** (line 46) — contradicts README §8 which says "defaults to true" |
| **Not present** | No runtime override mechanism beyond env vars — no CLI flags or API params for config knobs |

---

### `config/ai_config.py` — LLM prompt templates
[config/ai_config.py](file:///e:/Silicon%20Mango/linkedin-scraping/config/ai_config.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Prompt templates for: prompt parsing, relevance scoring, grant keyword planning, grant analysis, image OCR; plus constants (`AI_BATCH_SIZE=20`) |
| **Dependencies** | `config` (for `LLM_PROVIDER`, `RELEVANCE_THRESHOLD`); `os` imported but unused |

---

### `engine/prompt_parser.py` — Prompt → SearchPlan
[engine/prompt_parser.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/prompt_parser.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Parse natural-language prompt into a structured dict (`SearchPlan`) |
| **Public interface** | `parse(prompt: str, max_jobs: int | None = None) -> dict` |
| **Data shapes (output)** | `{"role_keywords": [str], "sector": str, "sector_keywords": [str], "experience_level": str, "experience_keywords": [str], "skills": [str], "locations": [str], "exclude_keywords": [str], "max_jobs": int}` |
| **Error handling** | LLM failure → `_heuristic_parse()` fallback (keyword-based). `DRY_RUN=true` → always heuristic. Never raises to caller. |
| **Dependencies** | `config.DRY_RUN`, `config.MAX_JOBS_DEFAULT`, `config.ai_config.PROMPT_PARSER_TEMPLATE`, `engine.llm_client.chat_json` |

---

### `engine/search_strategy.py` — Query queue builder
[engine/search_strategy.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/search_strategy.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Build ordered list of `SearchItem(query, location, action)` from a SearchPlan |
| **Public interface** | `build_queue(plan) -> list[SearchItem]`, `build_relaxed_queue(plan, attempts) -> list[SearchItem]`, `build_all(plan) -> list[SearchItem]` |
| **Data shapes** | `SearchItem` dataclass: `query: str, location: str, action: str` (one of `"seed"`, `"broaden_query"`, `"widen_location"`, `"relax_filters"`) |
| **Broadening logic** | `build_relaxed_queue` is NOT a progressive escalation across calls — it generates ALL strategies up to the given attempt threshold in ONE call (≤3: role×loc, ≤6: fallback locs, ≤9: no loc, always: single-word broad + experience keywords). The caller increments `relaxed_attempts` and re-calls each time the queue empties. |
| **Dependencies** | None (pure logic) |

---

### `engine/self_refinement.py` — Jobs orchestrator
[engine/self_refinement.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/self_refinement.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Main jobs pipeline loop — search, score, enrich, persist, yield progress |
| **Public interface** | `run(prompt: str, parsed_plan: dict, max_jobs: int, run_id=None, should_stop=None) -> Generator[Progress, None, list[dict]]` |
| **Data shapes** | Yields `Progress` dataclass: `{run_id, collected, target, attempts, current_query, current_location, status, error}`. Returns `list[dict]` of job dicts. |
| **Hard cap** | `max_attempts = 40` — fixed constant, never more than 40 search+score cycles |
| **Side effects** | DB writes (via `db.upsert_job`, `db.log_attempt`, `db.finish_run`), Playwright browser navigation (via `li_search`, `get_job_detail`), stdout logging |
| **Error handling** | Search failures: logged, attempt recorded with error, `continue` to next item. Individual job enrichment failures: silently skipped (get_job_detail never returns None per its contract). |
| **Notable** | Browser is deliberately NOT closed at end of run — warm session reused for next run. `li_close()` is never called from the live pipeline. |
| **Dependencies** | `config.FETCH_DETAILS`, `config.JOBS_PER_PAGE`, `engine.database`, `engine.search_strategy`, `engine.linkedin_client`, `engine.relevance` |

---

### `engine/linkedin_client.py` — Playwright browser driver
[engine/linkedin_client.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/linkedin_client.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | All browser automation: auth, job search, job detail, post search, image download |
| **Public interface** | `search(query, location, limit) -> list[dict]`, `get_job_detail(url_or_id) -> dict`, `search_posts(keyword, limit) -> list[dict]`, `fetch_image_b64(url) -> str`, `close()`, `canonical_view_url(job_id) -> str` |
| **Thread model** | ALL public functions proxy through `_on_browser_thread()` → a single persistent daemon thread (`_browser_loop`) that runs all Playwright sync API calls. This **solves** the thread-safety problem described in CLAUDE.md — concurrent Flask threads can call `search()` but they serialize through the task queue. |
| **Data shapes (job card)** | `{"job_id": str, "title": str, "company": str, "location": str, "posted": str, "link": str, "snippet": str}` |
| **Data shapes (job detail)** | `{"description": str, "apply_url": str, "posted_date": str, "company_url": str}` |
| **Data shapes (post card)** | `{"post_urn": str, "post_url": str, "text": str, "author": str, "author_url": str, "posted": str, "image_urls": [str]}` |
| **Side effects** | Global singletons `_pw`, `_browser`, `_context`, `_page`; reads/writes `playwright_auth.json`; Gmail IMAP polling for email-PIN verification |
| **Error handling** | Auth failure → empty list returned (no jobs). Detail failure → fallback dict with canonical URL. No exceptions escape public functions. |
| **Dependencies** | `config.*`, `engine.email_verifier` |

---

### `engine/llm_client.py` — Pluggable LLM layer
[engine/llm_client.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/llm_client.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Raw HTTP calls to OpenAI / Ollama / Anthropic; JSON extraction from responses |
| **Public interface** | `chat_text(prompt, system, temperature) -> str`, `chat_json(prompt, system, temperature) -> dict|list`, `chat_vision(prompt, images_b64, temperature) -> str` |
| **Provider dispatch** | `_PROVIDERS = {"openai": ..., "ollama": ..., "anthropic": ...}` selected by `LLM_PROVIDER` config constant. Unsupported provider → `ValueError` raised. |
| **Retry logic** | `_urlopen_json()` retries 3x on status codes `{429, 500, 502, 503, 504, 520-524, 530}` + `URLError`/`TimeoutError`, with `2*(i+1)` second backoff |
| **JSON extraction** | `_extract_json()`: strips markdown fences, tries direct parse, then scans for first balanced `{...}` or `[...]` block. Raises `ValueError` if no JSON found. |
| **Error handling** | **Does NOT catch exceptions itself** — callers must wrap in try/except. This is the key boundary: `chat_json` can raise on network errors, JSON parse failures, etc. |
| **Notable** | Ollama: sets `"think": False` to suppress gemma4 reasoning; appends JSON-only system instruction. Ollama key only sent if `_is_real_key()`. All providers send browser-like `User-Agent`. |
| **Dependencies** | `config.LLM_PROVIDER`, `config.LLM_API_KEY`, `config.LLM_BASE_URL`, `config.LLM_MODEL`; stdlib `urllib`, `json` |

---

### `engine/relevance.py` — Relevance scoring
[engine/relevance.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/relevance.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Score jobs 0.0–1.0 and filter above threshold |
| **Public interface** | `filter_relevant(jobs: list[dict], plan: dict, threshold: float | None) -> list[dict]`, `_keyword_score(job: dict, plan: dict) -> float` (nominally private but imported by `self_refinement` for pre-ranking) |
| **Scoring paths** | `DRY_RUN=true` → always `_keyword_score`. Live: tries LLM batches (`_batch_jobs`), on exception falls back to keyword scoring. Within `_batch_jobs`, if a specific job has no LLM score, `_keyword_score` is used as per-job fallback. |
| **Keyword scoring formula** | Role in title: 0.75. Role in body: 0.55. Partial word overlap: 0.35×fraction. Sector bonus: +0.10. Skills bonus: +0.12×fraction. Experience bonus: +0.08. Exclude in title: −0.50. Exclude in body: −0.20. Clamped [0,1]. |
| **Error handling** | Outer try/except in `filter_relevant` catches `_batch_jobs` failure → keyword fallback for all jobs. Inner batch loop: exception per batch → empty result for that batch, jobs get keyword fallback. **Graceful degradation confirmed.** |
| **Dependencies** | `config.DRY_RUN`, `config.RELEVANCE_THRESHOLD`, `config.ai_config.*`, `engine.llm_client.chat_json` |

---

### `engine/database.py` — SQLite persistence
[engine/database.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/database.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Schema management, CRUD for runs/jobs/grants/users/attempts |
| **Public interface** | `init_db()`, `create_run()`, `finish_run()`, `get_run()`, `get_run_jobs()`, `upsert_job()`, `upsert_many()`, `seen_job_ids()`, `log_attempt()`, `upsert_grant()`, `seen_grant_keys()`, `get_run_grants()`, `add_user()`, `get_user_by_email()`, `list_users()`, `delete_user()` |
| **Thread safety** | `threading.local()` for connection objects; WAL mode enabled. |
| **Auto-init** | `init_db()` called at module level (line 357) — importing the module creates the DB |
| **Migration** | In-place `ALTER TABLE` for `run_type` column on `search_runs` (line 133-136), wrapped in try/except for idempotency |
| **Dependencies** | `config.DATA_DIR` |

---

### `engine/job_extractor.py` — Job detail enrichment
[engine/job_extractor.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/job_extractor.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Enrich job cards with full descriptions from detail pages |
| **Public interface** | `extract_cards(cards) -> list[dict]` (identity pass-through), `detail_pass(cards, limit, seen_ids) -> list[dict]` |
| **Status** | **`detail_pass` is NOT called by the active pipeline.** `self_refinement.run()` calls `get_job_detail()` inline per card (line 179). `extract_cards` is a no-op. This module is effectively dead code in the live pipeline. |
| **Notable** | Imports `MAX_DETAIL_CONCURRENCY` from config but never uses it for actual concurrency (no threading/async in this module). |
| **Dependencies** | `config.MAX_DETAIL_CONCURRENCY`, `engine.linkedin_client.get_job_detail` |

---

### `engine/exporter.py` — Jobs export
[engine/exporter.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/exporter.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Export job run results to xlsx/csv/json |
| **Public interface** | `export_json(run_id) -> str`, `export_csv(run_id) -> str`, `export_xlsx_bytes(run_id) -> bytes`, `export_to_files(run_id, formats, out_dir) -> list[str]` |
| **Data shapes** | `EXPORT_COLUMNS`: `linkedin_job_id, title, company, company_url, location, posted_date, job_url, apply_url, description, sector, experience_level, relevance_score, relevance_reason` |
| **Dependencies** | `pandas`, `openpyxl`, `engine.database` |

---

### `engine/grants_pipeline.py` — Grants orchestrator
[engine/grants_pipeline.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/grants_pipeline.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Search LinkedIn posts for grant/funding opportunities, enrich with image OCR + external site text, analyze with LLM |
| **Public interface** | `run(prompt, max_posts, run_id, should_stop) -> Generator[Progress, None, list[dict]]`, `plan_keywords(prompt) -> list[str]`, `analyze_post(...)`, `read_post_images(...)`, `read_external_sites(...)` |
| **Dedup** | Dual keying: post URN (UNIQUE in DB) + content hash (SHA-256 of normalized text, catches reposts) |
| **Error handling** | Keyword planning LLM failure → default keyword list. Image OCR failure → empty string, continue. External site fetch failure → empty string, continue. Analysis LLM failure → keyword heuristic fallback. **Graceful degradation confirmed.** |
| **Dependencies** | `config.*`, `config.ai_config.*`, `engine.database`, `engine.linkedin_client`, `engine.llm_client`, `engine.self_refinement.Progress` |

---

### `engine/grants_exporter.py` — Grants export
[engine/grants_exporter.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/grants_exporter.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Export grant run results to xlsx/csv/json |
| **Public interface** | `export_grants_json(run_id)`, `export_grants_csv(run_id)`, `export_grants_xlsx_bytes(run_id)` |
| **Columns** | 23 columns including `opportunity_title`, `funder`, `deadline`, `grant_amount`, `eligibility`, etc. |

---

### `engine/email_verifier.py` — Gmail IMAP verification code fetcher
[engine/email_verifier.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/email_verifier.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Poll Gmail IMAP for LinkedIn's 6-digit email verification PIN |
| **Public interface** | `gmail_configured() -> bool`, `fetch_verification_code(after_epoch, timeout, poll_interval, already_used) -> str | None` |
| **Status** | **LIVE AND WIRED** — called from `linkedin_client._handle_email_challenge()` during login |
| **Error handling** | IMAP login failure → `None`. Search/fetch failure → `None`. Timeout → `None`. Never raises. |
| **Dependencies** | `config.GMAIL_*` |

---

### `engine/mailer.py` — Outbound email
[engine/mailer.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/mailer.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Send welcome emails to new users with their credentials |
| **Public interface** | `mail_configured() -> bool`, `send_welcome_email(name, email, password) -> bool` |
| **Error handling** | All SMTP failures caught → returns `False`, never blocks user creation |
| **Dependencies** | `config.GMAIL_USERNAME`, `config.GMAIL_APP_PASSWORD`, `config.SMTP_*`, `config.APP_BASE_URL` |

---

### `cli.py` — Terminal entry point
[cli.py](file:///e:/Silicon%20Mango/linkedin-scraping/cli.py)

| Aspect | Detail |
|--------|--------|
| **Purpose** | Run the jobs pipeline headless from the command line, with file export |
| **Public interface** | `main(argv) -> int` (exit code) |
| **Notable** | Forces `PLAYWRIGHT_HEADLESS=true` before importing config. Does NOT support the grants pipeline (only jobs). Does NOT pass `should_stop` to the pipeline (no cooperative stop from CLI). |
| **Dependencies** | `engine.prompt_parser`, `engine.self_refinement`, `engine.exporter`, `config` |

---

## 3. Data Model Reference

### Actual DB Schema (from [database.py](file:///e:/Silicon%20Mango/linkedin-scraping/engine/database.py) lines 17-113)

#### `search_runs`
| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | INTEGER | PK AUTOINCREMENT | |
| prompt | TEXT | NOT NULL | |
| parsed_plan_json | TEXT | nullable | Was `NOT NULL` in PLAN.md; code uses `None` when called from `app.py` |
| max_jobs | INTEGER | NOT NULL | |
| started_at | TEXT | NOT NULL | ISO datetime UTC |
| finished_at | TEXT | nullable | |
| status | TEXT | NOT NULL DEFAULT 'running' | values: `running`, `completed`, `partial`, `stopped` |
| jobs_found | INTEGER | DEFAULT 0 | |
| error_message | TEXT | nullable | |
| run_type | TEXT | NOT NULL DEFAULT 'jobs' | Added via migration; values: `jobs`, `grants` |

#### `jobs`
| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | INTEGER | PK AUTOINCREMENT | |
| linkedin_job_id | TEXT | **UNIQUE NOT NULL** | Natural dedup key |
| title | TEXT | nullable | |
| company | TEXT | nullable | |
| company_url | TEXT | nullable | |
| location | TEXT | nullable | |
| posted_date | TEXT | nullable | |
| apply_url | TEXT | nullable | Always populated by pipeline |
| description | TEXT | nullable | |
| sector | TEXT | nullable | Populated by plan, not by individual job |
| experience_level | TEXT | nullable | Same |
| relevance_score | REAL | nullable | |
| relevance_reason | TEXT | nullable | |
| prompt | TEXT | nullable | |
| search_run_id | INTEGER | NOT NULL, FK → search_runs.id | |
| scraped_at | TEXT | NOT NULL | ISO datetime UTC |
| raw_json | TEXT | nullable | Full job dict as JSON |

**Indexes:** `idx_jobs_run(search_run_id)`, `idx_jobs_relevance(search_run_id, relevance_score DESC)`

#### `search_attempts`
| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | INTEGER | PK AUTOINCREMENT | |
| search_run_id | INTEGER | NOT NULL, FK | |
| query | TEXT | NOT NULL | |
| location | TEXT | NOT NULL | |
| cards_extracted | INTEGER | nullable | |
| jobs_relevant | INTEGER | nullable | |
| refinement_action | TEXT | nullable | seed, broaden_query, widen_location, relax_filters, grant_posts |
| error | TEXT | nullable | |
| attempted_at | TEXT | NOT NULL | |

**Index:** `idx_attempts_run(search_run_id)`

#### `grants`
| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | INTEGER | PK AUTOINCREMENT | |
| post_urn | TEXT | **UNIQUE NOT NULL** | |
| content_hash | TEXT | nullable | SHA-256 of normalized text |
| post_url | TEXT | | |
| author | TEXT | | |
| author_url | TEXT | | |
| posted_date | TEXT | | Relative, e.g. "2w" |
| posted_date_normalized | TEXT | | ISO date, computed |
| opportunity_title | TEXT | | |
| funder | TEXT | | |
| summary | TEXT | | |
| deadline | TEXT | | |
| grant_amount | TEXT | | |
| eligibility | TEXT | | |
| focus_areas | TEXT | | |
| geography | TEXT | | |
| how_to_apply | TEXT | | |
| application_link | TEXT | | |
| external_links | TEXT | | Comma-separated URLs |
| contact_email | TEXT | | |
| post_text | TEXT | | Raw post text |
| image_text | TEXT | | OCR output |
| external_site_summary | TEXT | | Fetched site text |
| relevance_score | REAL | | |
| relevance_reason | TEXT | | |
| keyword | TEXT | | Which search phrase found it |
| prompt | TEXT | | |
| search_run_id | INTEGER | NOT NULL, FK | |
| scraped_at | TEXT | NOT NULL | |
| raw_json | TEXT | | |

**Indexes:** `idx_grants_run(search_run_id)`, `idx_grants_hash(content_hash)`

#### `users`
| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| id | INTEGER | PK AUTOINCREMENT | |
| name | TEXT | NOT NULL | |
| email | TEXT | **UNIQUE NOT NULL** | |
| password_hash | TEXT | NOT NULL | werkzeug `generate_password_hash` |
| created_at | TEXT | NOT NULL | |

### Shape of a "job" dict as it flows through the pipeline

```json
{
  // From _extract_cards (search page DOM):
  "job_id": "4123456789",
  "title": "Junior Accountant",
  "company": "UNICEF",
  "location": "New Delhi, India",
  "posted": "",
  "link": "https://www.linkedin.com/jobs/view/4123456789",
  "snippet": "",

  // Added by relevance scoring:
  "relevance_score": 0.82,
  "relevance_reason": "exact role match in title...",

  // Added by get_job_detail (detail page):
  "description": "Full job description text...",
  "apply_url": "https://www.linkedin.com/jobs/view/4123456789",
  "posted_date": "2 weeks ago",
  "company_url": "https://www.linkedin.com/company/unicef",

  // Synthesized by self_refinement:
  "linkedin_job_id": "4123456789",

  // From the search plan (not per-job):
  "sector": null,
  "experience_level": null
}
```

> **Note:** `sector` and `experience_level` are columns in the DB but are **never populated per-job** by the pipeline — `job.get("sector")` will return `None`. The plan has a `sector` field but it's not copied onto individual job dicts.

---

## 4. Concurrency & State Model

### Browser thread safety — the REAL story

The README (§11) and CLAUDE.md both say:

> "the browser is a shared global…only one scrape at a time is safe"

**This was true historically but is no longer accurate.** The code has a proper solution at [linkedin_client.py:963-1001](file:///e:/Silicon%20Mango/linkedin-scraping/engine/linkedin_client.py#L963-L1001):

- All public functions (`search`, `get_job_detail`, `search_posts`, `fetch_image_b64`, `close`) proxy through `_on_browser_thread()`.
- `_on_browser_thread` serializes all calls through a single persistent daemon thread (`_browser_loop`) via a `queue.Queue` of `(fn, args, kwargs, {done: Event, result/error})` tuples.
- A `_dispatch_lock` (threading.Lock) protects thread startup.
- The `_browser_loop` runs an infinite `while True: _task_q.get()` — it processes one call at a time.

**What happens if `/scrape` is hit twice concurrently?**

1. Both Flask daemon threads call `search()` etc. through `_on_browser_thread`.
2. Calls serialize on the single browser thread's task queue.
3. **The two pipeline generators will interleave at the browser-call granularity**, not at the run level. Thread A's `search()` might run, then Thread B's `search()`, then Thread A's `get_job_detail()`.
4. This means the single browser/page instance will **navigate away from Thread A's search results to handle Thread B's request**, corrupting both runs.

**Conclusion:** The browser thread serialization prevents crashes but does NOT prevent logical corruption from concurrent runs. There is **no run-level lock or guard** — the "one scrape at a time" constraint is enforced only by social convention and the fact that the UI naturally serializes (one user, one button).

### SSE/Progress flow — traced end to end

1. `POST /scrape` → `_scrape_worker` starts in a daemon thread.
2. `_scrape_worker` creates a fresh `queue.Queue`, stores it in `_progress_queues[run_id]`.
3. The pipeline generator (`self_refinement.run()` or `grants_pipeline.run()`) yields `Progress` dataclasses.
4. `_scrape_worker` iterates the generator, pushing each `Progress` onto the queue.
5. On exception: pushes `{"error": str}`. Finally: pushes `None` sentinel, cleans up.
6. `GET /stream/<run_id>` → `generate()` pops from the queue (blocking with 300s timeout).
7. Each item is serialized as `data: <json>\n\n` SSE event.
8. `None` sentinel → emits `{"status": "done"}` and returns.

**What if the background thread throws?** The exception is caught by `_scrape_worker`'s try/except (line 169), an error dict is pushed, then `None` sentinel. The SSE stream receives the error event and terminates cleanly. **The SSE connection does not hang** — this is handled correctly.

**What if the SSE client disconnects?** The Flask generator keeps running (draining the queue) until it returns or the Python garbage collector collects it. Queue items accumulate but are bounded by the pipeline's natural pace. The background thread is unaware of the disconnection and continues to completion.

---

## 5. README vs. Reality — Discrepancy List

### Critical Discrepancies

| # | README Claim | Reality | Files |
|---|---|---|---|
| **D1** | §8, §9 table: "`DRY_RUN` defaults to **true**" (stated twice, once in §8 and once in the config table) | `DRY_RUN` defaults to **`false`** (`os.getenv("DRY_RUN", "false")` at [config/__init__.py:46](file:///e:/Silicon%20Mango/linkedin-scraping/config/__init__.py#L46)). CLAUDE.md correctly notes this. The `.env.example` has `DRY_RUN=true` but `.env` has `DRY_RUN=false`. | config/\_\_init\_\_.py |
| **D2** | §9 table: `RELEVANCE_THRESHOLD` default is `0.65` | Default is **`0.6`** in code ([config/__init__.py:43](file:///e:/Silicon%20Mango/linkedin-scraping/config/__init__.py#L43)). The `.env` file has `0.6`, `.env.example` has `0.65`. | config/\_\_init\_\_.py |
| **D3** | §9: "Gmail settings…not currently wired into the live code (it was deferred — see docs/TASKS.md T14)" | **Gmail IMAP IS live and wired.** `email_verifier.py` is imported and called by `linkedin_client._handle_email_challenge()` during login ([linkedin_client.py:161-168](file:///e:/Silicon%20Mango/linkedin-scraping/engine/linkedin_client.py#L161-L168)). T14 in TASKS.md says "cancelled" (`[-]`), but the feature was implemented anyway. | engine/email_verifier.py, engine/linkedin_client.py |
| **D4** | Assembly-line diagram shows `job_extractor.py` as a pipeline stage | `job_extractor.py` is **not called by the active pipeline**. `self_refinement.run()` calls `get_job_detail()` directly, inline. `detail_pass()` is never called. The module is dead code. | engine/self_refinement.py, engine/job_extractor.py |
| **D5** | §11 endpoint table shows 8 endpoints; no mention of auth | The app has **16+ routes** including `/login`, `/logout`, `/admin/users` (GET, POST, DELETE), `/stop/<run_id>`. Every route except `/login`, `/health` requires session authentication. | app.py |
| **D6** | §11: "the browser is shared globally and isn't thread-safe, the app is designed to run one scrape at a time" | The browser IS shared but is now marshalled through a **single dedicated thread** with a task queue dispatcher ([linkedin_client.py:963-1001](file:///e:/Silicon%20Mango/linkedin-scraping/engine/linkedin_client.py#L963-L1001)), making it crash-safe but still logically single-run. | engine/linkedin_client.py |
| **D7** | No mention of grants/posts pipeline | The codebase has a complete grants pipeline (`grants_pipeline.py`, `grants_exporter.py`), dedicated DB table, UI tab, and API support (`mode: "grants"` in `/scrape`). The `docs/GRANTS_FEATURE.md` documents it but the README doesn't. | engine/grants_pipeline.py, engine/grants_exporter.py, app.py |
| **D8** | No mention of user authentication/management | Full auth system with hardcoded admin, SQLite users table, session-based access, admin user management API, welcome email. | app.py, templates/login.html, engine/mailer.py |
| **D9** | No mention of CLI entry point | `cli.py` provides headless terminal-driven scraping with file export. | cli.py |
| **D10** | §7: "By default this runs in **dry-run mode**" | Default is real scraping (DRY_RUN=false in code). See D1. | config/\_\_init\_\_.py |
| **D11** | §4 file structure omits many files | Missing: `cli.py`, `engine/grants_pipeline.py`, `engine/grants_exporter.py`, `engine/email_verifier.py`, `engine/mailer.py`, `templates/login.html`, `.dockerignore` | — |

### Minor / Non-Critical Discrepancies

| # | Area | Detail |
|---|---|---|
| D12 | §9 env table | Missing ~20 env vars actually defined in config: `JOBS_PER_PAGE`, `MAX_SEARCH_PAGES`, `SCROLL_PASSES`, `PAGE_NAV_TIMEOUT`, `FETCH_DETAILS`, `LINKEDIN_JOB_VIEW_URL`, all `GRANT_*` vars, `ADMIN_*`, `SECRET_KEY`, `SMTP_*`, `APP_BASE_URL` |
| D13 | §9 | Table says "max of 500 jobs" — no max enforcement exists in code, only default of 100. The HTML presets go to 250. |
| D14 | TASKS.md T14 | Marked `[-]` (cancelled), but the feature is fully implemented and live |
| D15 | docs/PLAN.md §10 | "Real-time streaming results (results still batch into xlsx at the end)" listed as out-of-scope — but SSE streaming IS implemented |
| D16 | docs/PLAN.md §11 | "User accounts / multi-tenant" listed as out-of-scope — but user auth IS implemented |
| D17 | PLAN.md schema | `parsed_plan_json` declared `NOT NULL` — code passes `None` when called from `app.py` (only `cli.py` / self_refinement passes the plan) |
| D18 | CLAUDE.md §Patterns | Says "Calls `li_close()` at the end of each run" — code comment (self_refinement.py:223-225) explicitly says browser is NOT closed |
| D19 | `requirements.txt` | `werkzeug` is not listed but is required (used for password hashing via `werkzeug.security` imported by `app.py`); it comes transitively via `Flask` but is an implicit dependency |

---

## 6. Archive & Docs Analysis

### `_archive/` — What was tried and why it was abandoned

| File | What it was | Why abandoned |
|------|-------------|---------------|
| `final_scrapping_script.py` (242 lines) | The **original active script** — Flask+Playwright with keyword/location form, jobs_store dict, `SimpleQueue` for SSE, `ensure_credentials()` returning hardcoded placeholders, Gmail IMAP handler, xlsx export. Was the live `app.py` before the rewrite. | Replaced by the prompt-driven engine. Same problem, different approach: 2-field form vs. natural language prompt, no LLM scoring, no self-refinement, no database, hardcoded max 50. |
| `main.py` (87 lines) | Network-page scraper — logs in, navigates to `/mynetwork/`, scrapes connection names from "discover-entity-list". **Hardcoded credentials** on line 7-8. | Not a job scraper at all — different purpose (people/connections). Never wired into the web app. |
| `seleniu_way.py` (112 lines) | Selenium-based job scraper with **security bug**: line 26 uses email as `By.ID` selector, line 31 uses password as `By.ID` selector (treating credential values as element IDs). | Broken code (wrong `By.ID` usage). Replaced by Playwright approach. |
| `backup - local scrapping.py` (167 lines) | Local Playwright script with argparse, `ensure_credentials()` reading from env, auth file reuse, search by keyword/location, xlsx export. Closest ancestor to the current `linkedin_client.py`. | Superseded by the modular engine architecture. |

**Key takeaway:** All four scripts solved "search LinkedIn for jobs" with Playwright/Selenium, none had LLM integration, none had self-refinement, and none had a database. The archive represents the "V1 static-keyword scraper" that the current prompt-driven engine replaced entirely.

### `docs/` — What's planned/deferred but not wired

| Doc | Planned feature | Status in code |
|-----|-----------------|----------------|
| TASKS.md T0.2 | "Confirm LLM provider with user" | `[ ]` todo — never done, Ollama is just the default |
| TASKS.md T14 | Gmail IMAP verification | `[-]` marked cancelled BUT fully implemented |
| TASKS.md T24 | Real-run smoke test (DRY_RUN=false) | `[ ]` todo — never formally completed |
| TASKS.md T25 | Document DRY_RUN mode in README | `[ ]` todo — README does document it, but with wrong defaults |
| PLAN.md §10 #3 | "Schema drift maintenance" | Acknowledged risk, no automated solution |
| PLAN.md §11 | Proxy/IP rotation, multi-account, local LLM default, user accounts | User accounts done; rest still out of scope |
| AI_SCRAPER_PLAN.md | "user profiles or preferences", "user feedback mechanism for learning", "dashboard features" | None implemented — this is a very early vision doc, mostly superseded by PLAN.md |
| superpowers plans | Fixed-height layout + Jobs/Grants tabs | Fully implemented in index.html |

---

## 7. Extension Readiness Notes

### Natural seams / extension points

1. **LLM provider dispatch** (`llm_client.py` `_PROVIDERS` / `_VISION_PROVIDERS` dicts): Adding a new provider is straightforward — write a `_call_newprovider()` function, add it to the dict. Clean and decoupled.

2. **Relevance scoring** (`relevance.py`): `_keyword_score()` is a pure function with no side effects — a new scoring strategy could replace it or run alongside it. `filter_relevant()` orchestrates both LLM and keyword paths; a new strategy could be swapped at this level. However, the function is not pluggable via config — you'd need to modify the module directly.

3. **Pipeline dispatch** (`app.py` `_scrape_worker` line 160-166): Already dispatches `mode == "grants"` vs `mode == "jobs"` with a simple if/else. Adding a third mode is a 5-line change.

4. **Export columns** (`exporter.py` `EXPORT_COLUMNS`, `grants_exporter.py` `GRANT_EXPORT_COLUMNS`): Adding a column means adding it to the list and ensuring the DB column and job dict populate it.

5. **Config registration** (`config/__init__.py`): Adding a new knob is one line of `os.getenv(...)`. Clean and centralized.

6. **DB schema** (`database.py` `SCHEMA`): Uses `CREATE TABLE IF NOT EXISTS` — new tables can be added to the schema string. Migrations are done via try/except `ALTER TABLE` (see `run_type` example).

7. **Search broadening** (`search_strategy.py` `build_relaxed_queue`): Each broadening tier is an `if attempts <= N` block — new strategies can be added as new blocks.

### Constraints that would resist clean extension

| Constraint | Impact | Severity |
|-----------|--------|----------|
| **Single browser instance** | Can't run two pipelines concurrently (jobs + grants simultaneously). Each `_on_browser_thread` call blocks until the browser is free. If the grants pipeline is doing image fetch while a jobs pipeline needs to search, one waits for the other. | 🔴 High |
| **SQLite single-file DB** | No concurrent write performance (WAL helps but still one writer at a time). No built-in replication. Adequate for single-user/team use but won't scale to multi-tenant. | 🟡 Medium |
| **No run-level lock** | If two users click "Start" on the web UI simultaneously, both runs will start and interleave on the browser thread, corrupting results. The UI doesn't prevent this. | 🔴 High |
| **Module-global config** | All config is read once at import time (module-level `os.getenv`). You can't have different `RELEVANCE_THRESHOLD` for different runs without env manipulation before import. | 🟡 Medium |
| **In-memory progress state** | `_progress_queues` and `_stop_events` are Python dicts in the Flask process. With `gunicorn --workers 1` this is fine, but won't survive a process restart or scale to multiple workers. | 🟡 Medium (Docker CMD already pins `--workers 1`) |
| **No test suite** | Any refactor is verified manually via DRY_RUN. Adding tests would need mocking for the LLM and browser layers. | 🟡 Medium |
| **Tightly coupled Progress dataclass** | Both pipelines (jobs + grants) use the same `Progress` from `self_refinement.py`. Grants pipeline imports it. A third pipeline would need to use it too or refactor it to a shared location. | 🟢 Low |
| **Session-based auth only** | No API tokens, no OAuth. CLI has no auth concept. Adding API-key auth would require a new auth path. | 🟢 Low |

### Conventions to match for new code

1. **Naming**: `snake_case` everywhere. Modules in `engine/`. Files named for their single responsibility.
2. **Imports**: `from __future__ import annotations` at top of engine modules. Config imported from `config` or `config.ai_config`, never from scattered `os.getenv`.
3. **Error handling**: Every external boundary (LLM, Playwright, JSON parse) wrapped in try/except. Fallback to heuristic or empty. **Never crash the run.**
4. **Config registration**: New env vars go in `config/__init__.py` with a default value. Add to `.env.example`.
5. **Logging**: `_log(msg)` pattern (module-level function writing to stdout with a `[module_name]` prefix), with `UnicodeEncodeError` protection for Windows.
6. **Data passing**: Plain `dict`s for jobs/grants. `dataclass` only for `SearchItem` and `Progress`.
7. **DB operations**: Thread-local connections via `_conn()`. Manual SQL, no ORM. Commit after every write operation.
8. **LLM calls**: Always through `llm_client.chat_json()` / `chat_text()` / `chat_vision()`. Never call urllib directly for LLM.
9. **Browser calls**: Always through the public functions in `linkedin_client` (which proxy to the browser thread). Never access `_page` directly from outside.
10. **Pipeline pattern**: Generator that yields `Progress` dataclasses, consumed by `_scrape_worker` → queue → SSE. A new pipeline should follow this exact pattern.

---

## Appendix A: Full File Inventory

### Root
| File | What it contains |
|------|-----------------|
| `app.py` (305 lines) | Flask web server with auth, route handlers, SSE streaming, background thread dispatch |
| `cli.py` (99 lines) | Headless terminal entry point for jobs pipeline |
| `CLAUDE.md` (166 lines) | AI assistant context: overview, conventions, detected patterns, git insights |
| `README.md` (518 lines) | User-facing documentation (significantly outdated — see §5) |
| `requirements.txt` (9 lines) | Flask, playwright==1.48.0, pandas, openpyxl, python-dotenv, gunicorn |
| `.env.example` (53 lines) | Template with all env vars and safe defaults |
| `.env` (57 lines) | Live config — real credentials, DRY_RUN=false |
| `.gitignore` (39 lines) | Ignores .env, auth JSON, data/, venv/, __pycache__ |
| `.dockerignore` (692 bytes) | Docker build exclusions |
| `Dockerfile` (46 lines) | Python 3.11-slim, chromium only, gunicorn CMD |
| `docker-compose.yml` (15 lines) | Single service, .env, data volume, shm_size 1gb |
| `run.sh` (393 lines) | Raspberry Pi deployment: apt packages, venv, Cloudflare tunnel, systemd services |
| `playwright_auth.json` (7447 bytes) | Saved browser session (storage state) |

### `config/`
| File | What it contains |
|------|-----------------|
| `__init__.py` (83 lines) | All env vars read once: paths, LinkedIn creds, Playwright knobs, LLM settings, relevance, DRY_RUN, Gmail, grants, auth, SMTP, Flask |
| `ai_config.py` (127 lines) | LLM prompt templates (parse, relevance, grants keywords, grant analysis, image OCR) + AI constants |

### `engine/`
| File | What it contains |
|------|-----------------|
| `__init__.py` (2 lines) | Empty package marker |
| `prompt_parser.py` (188 lines) | Natural-language → SearchPlan dict; LLM with heuristic fallback |
| `search_strategy.py` (87 lines) | SearchItem queue builder with broadening tiers |
| `self_refinement.py` (234 lines) | Jobs orchestrator: search→score→enrich→persist loop, Progress generator |
| `linkedin_client.py` (1111 lines) | Playwright: auth, job search, job detail, post search, image fetch, browser-thread dispatcher |
| `job_extractor.py` (45 lines) | Dead code — `detail_pass` not called by active pipeline |
| `relevance.py` (154 lines) | Batched LLM scoring + keyword fallback |
| `database.py` (358 lines) | SQLite: 5 tables (search_runs, jobs, search_attempts, grants, users), CRUD, auto-init |
| `exporter.py` (100 lines) | Jobs export: xlsx/csv/json + file export for CLI |
| `grants_pipeline.py` (390 lines) | Grants orchestrator: post search → image OCR → site fetch → LLM analysis → persist |
| `grants_exporter.py` (49 lines) | Grants export: xlsx/csv/json |
| `llm_client.py` (299 lines) | Pluggable LLM: OpenAI/Ollama/Anthropic chat + vision, retry, JSON extraction |
| `email_verifier.py` (146 lines) | Gmail IMAP: poll for LinkedIn 6-digit verification codes |
| `mailer.py` (48 lines) | SMTP: send welcome emails for new user accounts |

### `templates/`
| File | What it contains |
|------|-----------------|
| `index.html` (1485 lines) | Main web UI: Tamuku-branded, Jobs/Grants tabs, prompt textarea, example chips, progress grid, SSE, downloads, user management panel, admin controls |
| `login.html` (218 lines) | Login page: email/password form, Tamuku branding |

### `static/`
| File | What it contains |
|------|-----------------|
| `tamuku-logo.png` (115 KB) | Tamuku brand logo |

### `docs/`
| File | What it contains |
|------|-----------------|
| `PLAN.md` (312 lines) | Original architecture plan — partially outdated (see discrepancies) |
| `TASKS.md` (63 lines) | Implementation checklist — mostly done, some items inaccurately marked |
| `AI_SCRAPER_PLAN.md` (49 lines) | Very early vision doc — high-level, no implementation detail, largely superseded |
| `GRANTS_FEATURE.md` (195 lines) | Grants feature documentation — accurate and comprehensive |
| `superpowers/plans/` | Detailed implementation plan for fixed-height layout + tabs |
| `superpowers/specs/` | Design spec for the same |

### `_archive/`
| File | What it contains |
|------|-----------------|
| `final_scrapping_script.py` (242 lines) | Original Flask+Playwright scraper — keyword/location form, no LLM, no DB |
| `main.py` (87 lines) | Network-page people scraper — different purpose entirely |
| `seleniu_way.py` (112 lines) | Selenium job scraper with security bug (credentials as element IDs) |
| `backup - local scrapping.py` (167 lines) | CLI Playwright scraper — closest ancestor to current code |

### `data/`
| File | What it contains |
|------|-----------------|
| `jobs.db` (1.2 MB) | Live SQLite database |
| `exports/*.csv,*.json,*.xlsx` | Sample export files from previous runs |
