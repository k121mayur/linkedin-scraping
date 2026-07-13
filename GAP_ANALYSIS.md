# GAP_ANALYSIS.md

**Date:** 2026-07-13
**Scope:** Gap analysis of the "Grant & Donor Discovery Module" spec against the existing `grants_pipeline` implementation.

---

### 1. Dual-tab UI (Job Search Optimization / Grant & Donor Discovery)
✅ **Already implemented**
- **Location:** `templates/index.html` (lines 198-223).
- **Details:** The UI already features a `.tabbar` with "Jobs" and "Grants" tabs. A vanilla JS controller toggles the `hidden` attribute between the `data-tab-panel="jobs"` and `data-tab-panel="grants"` sections.

### 2. Profile uploader (.txt/.docx drag-and-drop) on the Grant tab
❌ **Not implemented**
- **Details:** The Grants tab in `templates/index.html` is currently a structural clone of the Jobs tab; it only has a `<textarea>` for a short prompt. There is no file input, drag-and-drop zone, or frontend JS to read uploaded files.
- **Smallest fix:** Add a `<input type="file" accept=".txt,.docx">` and drop-zone UI to the Grants form in `index.html`, and update the frontend JS to read the file and send its content to the `/scrape` endpoint.

### 3. Target quota input field (default 10) and a halt condition tied to it
⚠️ **Partially implemented**
- **Location:** `config/__init__.py` (line 59), `engine/grants_pipeline.py` (line 285).
- **Details:** The quota input field exists (reusing the Jobs UI), and the halt condition is correctly implemented (`if len(collected) >= max_posts:` breaks the loop). However, the default in `MAX_GRANT_POSTS_DEFAULT` is `50`, and the UI presets are 50/100/250, rather than the spec's `10`.
- **Smallest fix:** Change `MAX_GRANT_POSTS_DEFAULT = 10` in `config/__init__.py`, and update the HTML presets in the Grants panel to `10/25/50`.

### 4. Document ingestion: docx/txt → clean markdown, with a >100-character validation check
❌ **Not implemented**
- **Details:** The backend `/scrape` endpoint only expects a short `prompt` string. There is no logic to extract text from `.docx` files or validate that the uploaded profile meets a 100-character minimum.
- **Smallest fix:** Add `python-docx` to `requirements.txt`. In `app.py`, update the `/scrape` endpoint to accept a `profile_text` field, parse it (if sent as a file/binary), validate `len(profile_text) > 100`, and pass it to the grants pipeline.

### 5. Phase I search abstraction: frontier LLM turning the prompt into 3–5 broad search terms
⚠️ **Partially implemented**
- **Location:** `engine/grants_pipeline.py` -> `plan_keywords()` and `config/ai_config.py` -> `GRANT_KEYWORDS_TEMPLATE`.
- **Details:** The abstraction exists exactly as described: the LLM is used to generate search phrases. However, `GRANT_KEYWORDS_TEMPLATE` currently asks for "5-8 short search phrases" instead of "3-5 broad search terms".
- **Smallest fix:** Edit `GRANT_KEYWORDS_TEMPLATE` to explicitly request "3-5 broad search terms" instead of 5-8.

### 6. Playwright post search hitting LinkedIn's content search feed (not job listings)
✅ **Already implemented**
- **Location:** `engine/linkedin_client.py` -> `search_posts()` (approx line 600+).
- **Details:** The Playwright driver specifically navigates to `https://www.linkedin.com/search/results/content/?keywords=...&sortBy="date_posted"` to scrape posts rather than the standard jobs feed.

### 7. Phase II scoring: does the current analyze_post() call receive an organization profile as part of its evaluation payload?
⚠️ **Partially implemented**
- **Location:** `engine/grants_pipeline.py` -> `analyze_post()` and `config/ai_config.py` -> `GRANT_ANALYSIS_TEMPLATE`.
- **Details:** `analyze_post()` is fully wired to score relevance, but it currently only receives the user's short `prompt`. It does not accept or pass a long organization profile document to the LLM. 
- **Smallest fix:** Update `analyze_post(..., prompt: str, profile: str)` to take the profile text, and inject it into a modified `GRANT_ANALYSIS_TEMPLATE` alongside the short prompt.

### 8. The exact relevance threshold used for grants
⚠️ **Partially implemented**
- **Location:** `config/__init__.py` (line 65).
- **Details:** It correctly uses a completely distinct variable: `GRANT_RELEVANCE_THRESHOLD`. However, it is currently set to `0.5`, whereas the spec requires `0.7`.
- **Smallest fix:** Change `GRANT_RELEVANCE_THRESHOLD = 0.7` in `config/__init__.py` (and `.env.example`).

### 9. Excel export columns — does the existing export already cover opportunity URL, score, and rationale?
✅ **Already implemented**
- **Location:** `engine/grants_exporter.py` -> `GRANT_EXPORT_COLUMNS` and `engine/database.py` (grants schema).
- **Details:** The export is a superset of these requirements. It includes `application_link`, `post_url`, `relevance_score`, and `relevance_reason`, along with ~19 other extracted fields.

### 10. Edge cases from the spec's mitigation table
- **Scarcity loop trap (no results found):** ⚠️ *Partially implemented.* The code loops through keywords and handles 0 results gracefully by continuing, but doesn't explicitly abort early if *all* keywords yield zero results. (Fix: add a check to stop if multiple keywords return 0 posts).
- **JSON schema corruption:** ✅ *Already implemented.* `engine/llm_client.py` -> `_extract_json()` has robust markdown stripping and JSON block hunting. If it still fails, `analyze_post()` degrades to a heuristic keyword analysis safely.
- **False-positive trap:** ⚠️ *Partially implemented.* The `GRANT_ANALYSIS_TEMPLATE` instructs the LLM to look for "real, actionable FUNDING", but does not explicitly warn against "grant-writing services" or "success announcements". (Fix: tune the system prompt).
- **Rate limiting:** ✅ *Already implemented.* The `linkedin_client.py` uses random sleep backoffs, and `MAX_POST_SEARCH_PAGES` caps pagination depth.
- **Corrupted media uploads:** ❌ *Not implemented.* (Fix: wrap the future `.docx` parsing in a `try/except` block that returns a 400 error message to the UI if the file is unreadable).

---

### Potential Relevance-Scoring Conflicts
**Will adding profile-based scoring conflict with existing behavior?**
No. The Jobs pipeline and Grants pipeline are structurally isolated. The Jobs pipeline relies on `engine/relevance.py` and `PROMPT_RELEVANCE_TEMPLATE`. The Grants pipeline relies on `engine/grants_pipeline.py` and `GRANT_ANALYSIS_TEMPLATE`. Modifying `analyze_post()` in the Grants pipeline to accept a long `profile` string will have zero impact on the Jobs pipeline.
