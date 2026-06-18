# Prompt-Based LinkedIn Extraction Engine — Plan

## 1. Current State Assessment

The repo has 4 scraper scripts in varying states of decay:

| File | Status | Notes |
|------|--------|-------|
| `final_scrapping_script.py` | **Active** (referenced by `run.sh`, `Dockerfile`, `docker-compose.yml`) | Flask + Gunicorn + Playwright. Keyword+location form, xlsx export, SSE progress. Hardcoded XPath. Max 50. `ensure_credentials()` returns placeholder strings. |
| `main.py` | Dead | Network-page scraper, never wired into the app |
| `seleniu_way.py` | Dead, security bug | Email + password pasted as `By.ID` selector values |
| `backup - local scrapping.py` | Dead | Older local-only Playwright script |

Other artefacts:
- `config.py` + `config/ai_config.py` declare env-driven config; live script doesn't actually read them
- `requirements.txt` lists `openai`, `transformers`, `torch`, `scikit-learn` — none are used
- `AI_SCRAPER_PLAN.md` (existing) is a high-level vision doc with no implementation detail
- `templates/index.html` has a 2-field form (keyword + location) and SSE progress
- `downloaded_source_initial.html` is a 1.3 MB sample of a logged-in jobs page
- `linkedin_jobs.xlsx` shows what the old extractor produces (12 rows, no apply URL, no sector, no relevance)

What the user wants:
1. **Natural-language prompt** instead of two form fields, e.g. "Extract the Jobs in NGO sector for accountant role or jr level financial roles"
2. **Configurable N** (e.g. 50, 100) with hard stop at N
3. **Prompt-driven relevance filtering** — extract skills/sector/experience/role from the prompt, score each candidate
4. **Database** for persistence + dedup + analytics
5. **Self-refinement loop** — change locations / broaden queries / drop filters when count < N
6. **Bypass LinkedIn limits** — throttle, headless rotation, session reuse, location rotation, public+logged-in hybrid

## 2. Architecture Overview

```
                       ┌──────────────────────┐
   User prompt ───────▶│  Prompt Parser (LLM) │
                       └──────────┬───────────┘
                                  │ SearchPlan
                                  │  {role, sector, experience, locations[], max_jobs, filters}
                                  ▼
                       ┌──────────────────────┐
                       │ Search Strategy      │  ← rotates locations, builds query variants
                       └──────────┬───────────┘
                                  │ [(query, location), ...]
                                  ▼
                       ┌──────────────────────┐
                       │ Self-Refinement Loop │ ◀──┐
                       │  for each (q,loc):   │    │
                       │   1. LinkedIn client │    │
                       │   2. Job extractor   │    │ feedback:
                       │   3. Relevance filter│────┘ refine_query / next_location / relax
                       │   4. DB upsert       │
                       │   5. Check N reached │
                       └──────────┬───────────┘
                                  │ Final result set
                                  ▼
                       ┌──────────────────────┐
                       │ Exporter             │  → xlsx, csv, json, SSE progress events
                       └──────────────────────┘
```

All state lives in SQLite (`data/jobs.db`).

## 3. File Structure

```
linkedin-scraping/
├── PLAN.md                   # this file
├── TASKS.md                  # execution checklist
├── README.md                 # updated
├── requirements.txt          # slimmed
├── .env.example              # updated
├── Dockerfile                # updated (gunicorn → app:app)
├── docker-compose.yml        # updated
├── run.sh                    # updated (gunicorn entrypoint)
├── config.py                 # reworked
├── config/
│   └── ai_config.py          # reworked (LLM provider, model, prompt templates)
├── app.py                    # NEW: Flask app + routes + SSE
├── engine/                   # NEW package
│   ├── __init__.py
│   ├── database.py           # SQLite schema + DAL
│   ├── llm_client.py         # pluggable LLM (OpenAI / Anthropic / Ollama)
│   ├── prompt_parser.py      # prompt → SearchPlan
│   ├── search_strategy.py    # location rotation + query expansion
│   ├── linkedin_client.py    # Playwright auth + search
│   ├── job_extractor.py      # raw HTML → job dict
│   ├── relevance.py          # LLM-scored relevance filter
│   ├── self_refinement.py    # the orchestrator loop
│   └── exporter.py           # xlsx/csv/json
├── templates/
│   └── index.html            # reworked: single prompt textarea + N
├── static/                   # existing
├── _archive/                 # moved dead scripts
│   ├── main.py
│   ├── seleniu_way.py
│   └── backup - local scrapping.py
└── data/                     # runtime (gitignored)
    ├── jobs.db
    └── exports/
```

## 4. Component Details

### 4.1 Prompt Parser (`engine/prompt_parser.py`)
- Input: free-text prompt + max_jobs
- LLM call returns JSON:
  ```json
  {
    "role_keywords": ["accountant", "junior accountant", "financial analyst"],
    "sector": "NGO",
    "sector_keywords": ["NGO", "non-profit", "nonprofit", "charity", "foundation"],
    "experience_level": "junior",
    "experience_keywords": ["junior", "entry level", "fresher", "0-2 years"],
    "locations": ["India", "Bangalore", "Mumbai", "Delhi", "Remote"],
    "exclude_keywords": ["senior", "manager", "director"],
    "max_jobs": 100
  }
  ```
- If the LLM call fails or user is in `--dry-run`, fall back to a heuristic parser
- Uses low temperature (0.1) and JSON-mode / structured output

### 4.2 Search Strategy (`engine/search_strategy.py`)
- Takes the `SearchPlan` and produces an ordered queue of `(query, location)` pairs
- Combinations: each role_keyword × each location × (with_sector / without_sector)
- Order:
  1. Most specific → most broad
  2. User-specified location first, then alternatives
  3. Add "Remote" / "India" / "Anywhere" as fallbacks
- Yields in batches; lets the orchestrator stop early

### 4.3 LinkedIn Client (`engine/linkedin_client.py`)
- Single Playwright browser instance, lazy-init, single page
- Auth: read storage state from `playwright_auth.json`; on miss, log in with creds from env and persist
- Public-search fallback: works logged-out, returns ~40 results per page, but with less detail
- Throttle: 2-4s random sleep between page actions
- Headless: configurable, default true
- Returns: raw page HTML + parsed job-card list (title, company, location, posted, link, snippet, job_id from URL)

### 4.4 Job Extractor (`engine/job_extractor.py`)
- Two paths:
  1. **Search-results path** (fast, batch): parse job cards from search HTML
  2. **Job-detail path** (slow, per-job): click into a job, parse full description
- For 100-job target: detail path is the bottleneck. Mitigations:
  - Visit each detail page in parallel (3-5 concurrent tabs) — capped to avoid LinkedIn rate-limits
  - Cache detail results by `job_id` in DB so we never re-fetch
  - Backoff: exponential on 429 / captcha

### 4.5 Relevance Filter (`engine/relevance.py`)
- LLM call per batch (not per job) — pack 5-10 jobs per call to save tokens
- Returns per-job score 0-1 + reason
- Threshold default 0.65 (configurable in `config/ai_config.py`)
- If LLM disabled: keyword-match fallback using the plan's role/sector keywords

### 4.6 Self-Refinement (`engine/self_refinement.py`)
State machine across `(query, location)` attempts:

```
SEED → EXTRACT → RELEVANCE_FILTER → DECIDE
                                    │
                                    ├─ count >= N        → DONE
                                    ├─ queue not empty  → SEED (next combo)
                                    ├─ queue empty
                                    │   ├─ try broader query (drop sector/experience)
                                    │   └─ try wider location set (country → world)
                                    └─ retries exhausted → DONE (partial)
```

Telemetry: every attempt logged in `search_attempts` table with reason. Surfaced to UI as live progress.

### 4.7 Database (`engine/database.py`)
SQLite with three tables (see §5). WAL mode. Connection-per-thread.

## 5. Database Schema

```sql
CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_job_id     TEXT UNIQUE NOT NULL,
    title               TEXT,
    company             TEXT,
    company_url         TEXT,
    location            TEXT,
    posted_date         TEXT,
    apply_url           TEXT,
    description         TEXT,
    sector              TEXT,
    experience_level    TEXT,
    relevance_score     REAL,
    relevance_reason    TEXT,
    prompt              TEXT,
    search_run_id       INTEGER NOT NULL,
    scraped_at          TEXT NOT NULL,
    raw_json            TEXT,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
);

CREATE INDEX idx_jobs_run ON jobs(search_run_id);
CREATE INDEX idx_jobs_relevance ON jobs(search_run_id, relevance_score DESC);

CREATE TABLE search_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt              TEXT NOT NULL,
    parsed_plan_json    TEXT NOT NULL,
    max_jobs            INTEGER NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,         -- running | completed | partial | failed
    jobs_found          INTEGER DEFAULT 0,
    error_message       TEXT
);

CREATE TABLE search_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    search_run_id       INTEGER NOT NULL,
    query               TEXT NOT NULL,
    location            TEXT NOT NULL,
    cards_extracted     INTEGER,
    jobs_relevant       INTEGER,
    refinement_action   TEXT,                  -- seed | broaden_query | widen_location | drop_filter
    error               TEXT,
    attempted_at        TEXT NOT NULL,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
);

CREATE INDEX idx_attempts_run ON search_attempts(search_run_id);
```

Deduplication: `linkedin_job_id` is the natural key (parsed from job URL, e.g. `.../view/1234567890/`). Re-runs of the same prompt skip already-seen jobs.

## 6. Bypassing LinkedIn Limits

| Limit type | Counter |
|-----------|---------|
| Login throttle / captcha | Persist `playwright_auth.json`; reuse across runs; only re-login when cookie invalid |
| Per-page action rate | Random sleep 2-4s between scrolls/clicks; mouse-move on focus |
| Aggressive logged-in DOM | Prefer public guest search endpoint for the broad pass; switch to logged-in only for jobs that need full description |
| 1000-result cap per search | Rotate `(query, location)` pairs — same role across N locations = N × ~40 results |
| 429 / too-many-requests | Exponential backoff (5s → 10s → 20s → 60s); after 3 fails in a row, switch to public search |
| DOM structure changes | Selector abstraction in `linkedin_client.py` with fallback lists; XPaths are versioned |
| IP-based throttling | Out of scope (would need proxy pool); document the limitation |
| li_at cookie expiration | Detect by 401/redirect to login, re-auth, persist new state |

Backwards-compatible with the existing Gmail IMAP verification helper (we'll port that into the auth flow in case a 2FA challenge is raised).

## 7. Self-Refinement Loop

Pseudocode for `engine/self_refinement.py`:

```python
def run(plan: SearchPlan, run_id: int) -> List[Job]:
    queue = build_queue(plan)            # [(query, location, refinement_action)]
    out: List[Job] = []
    attempts = 0
    max_attempts = len(queue) * 3        # safety bound

    while len(out) < plan.max_jobs and attempts < max_attempts:
        attempts += 1
        if not queue:
            # exhaust base combos → try wider strategies
            queue = build_relaxed_queue(plan, out, attempts_so_far=attempts)
            if not queue:
                break

        query, location, action = queue.pop(0)
        log_attempt(run_id, query, location, action)
        cards = client.search(query, location, page=0)
        if not cards:
            backoff()
            continue

        jobs = extractor.detail_pass(cards, limit=plan.max_jobs - len(out))
        scored = relevance.filter(jobs, plan)
        new_jobs = [j for j in scored if j.relevance_score >= plan.threshold and j.job_id not in seen]
        db.upsert_many(new_jobs, run_id=run_id, prompt=plan.original_prompt)
        out.extend(new_jobs)

        yield Progress(run_id, len(out), plan.max_jobs, attempt=attempts)

    return out
```

## 8. UI Changes (`templates/index.html`)

- Single large `<textarea>` for the prompt
- Number input for "How many jobs? (default 100, max 500)"
- Optional: location hint, sector hint, experience level
- Live SSE progress showing: parsed plan, current (query, location), jobs collected, attempts
- Download button (xlsx/csv/json) appears on completion
- "View in database" link → `/runs/{id}` (JSON dump of run metadata)

## 9. Deployment

- `Dockerfile` unchanged except `gunicorn ... app:app`
- `docker-compose.yml` adds a `data/` volume
- `run.sh` references `app:app`
- `.env.example` adds: `OPENAI_API_KEY`, `OPENAI_MODEL`, `LLM_PROVIDER=openai|ollama|anthropic`, `OLLAMA_BASE_URL`, `RELEVANCE_THRESHOLD`, `DRY_RUN=true|false`, `MAX_DETAIL_CONCURRENCY=3`
- Backwards-compat env vars (`LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`, `GMAIL_*`) retained

## 10. Risks / Open Questions

1. **LLM cost**: per-prompt + per-batch relevance calls. For 100 jobs with 10/batch = 10 relevance calls + 1 parse call. ~$0.05-0.20 per run on OpenAI gpt-4o-mini.
2. **LinkedIn blocking**: even with all mitigations, sustained scraping risks account flag. The `--dry-run` mode + cap on `max_jobs` mitigate this.
3. **Schema drift**: LinkedIn DOM changes weekly. Selector abstraction with fallback lists will need maintenance; document the maintenance cost.
4. **Verification emails**: existing Gmail-IMAP code path declared in `config.py` but never implemented — we'll port it (T12) for safety.

## 11. Out of Scope (this iteration)

- Proxy rotation / IP rotation
- Multi-account pool
- Local LLM by default (supported but not the default)
- Real-time streaming results (results still batch into xlsx at the end)
- User accounts / multi-tenant
