# Grants Extraction — What It Is, How It Works

This document explains the **Grants tab** of the Tamuku portal in simple language:
what was built, why, and exactly what happens from the moment you click
**Start extraction** to the moment you download the Excel file.

---

## 1. What is this feature?

NGOs constantly need funding. Funders (foundations, CSR teams, fellowship
programs) often announce their grants **as ordinary LinkedIn posts** — not on
job boards, not on grant portals. These posts are easy to miss.

The Grants tab automates finding them. You upload an **organisation profile**
(.txt or .docx) and type a plain-English request like:

> "Grant funding opportunities for NGOs working in education across India"

…and the engine searches LinkedIn posts, reads each post carefully (including
**images** and **linked websites**), pulls out the important facts (funder,
deadline, amount, eligibility, how to apply), removes duplicates, and gives you
a clean Excel/CSV/JSON file.

---

## 2. The flow, step by step

```
Your prompt + (Optional) Organisation Profile
   │
   ▼
① Keyword planning (AI)
   │  "education NGO grants India" → 5-8 real search phrases
   ▼
② LinkedIn posts search (browser automation)
   │  Content search + Posts filter, newest first, page by page
   ▼
③ Duplicate check
   │  Already seen this post (or this exact text)? → skip
   ▼
④ Enrichment
   │  a. Images in the post  → AI reads the text inside them (OCR)
   │  b. Links in the post   → the external website is fetched and read
   ▼
⑤ AI analysis
   │  "Is this a real funding opportunity?" + extract all the facts
   ▼
⑥ Save + live progress
   │  Stored in the database; the counter on screen ticks up 1, 2, 3…
   ▼
⑦ Download
      Excel / CSV / JSON with one row per opportunity
```

### ① Keyword planning — `engine/grants_pipeline.py → plan_keywords()`

Your one sentence is sent to the AI, which expands it into 5–8 short search
phrases that funders actually use in posts, e.g. *"call for proposals NGO"*,
*"CSR funding education"*, *"grants for nonprofits India"*.
If the AI is unreachable, a built-in default list of NGO-funding phrases is
used instead — the run never fails because of this step.

### ② LinkedIn posts search — `engine/linkedin_client.py → search_posts()`

For each keyword, the automated browser (Playwright, logged into your LinkedIn
account) opens LinkedIn's **content search** — this is the same thing as
searching and clicking the **"Posts" filter** — sorted by most recent.

For every post on the page it collects:

| Collected | Meaning |
|---|---|
| Post URN | LinkedIn's unique ID for the post (`urn:li:activity:…`) |
| Post URL | A permanent, clickable link to the post |
| Full text | Every "…see more" is clicked first, so nothing is cut off |
| Author + profile link | Who posted it (person or organization) |
| Posted date | LinkedIn's relative stamp, e.g. "2w" (2 weeks ago) |
| Image URLs | Any pictures attached to the post |

It scrolls to load lazily-rendered posts and moves through result pages until
enough posts are gathered or the keyword runs dry.

### ③ Duplicate handling — two locks

Duplicates are ignored using **two** independent keys:

1. **Post URN** — the same post can never be saved twice (it's a `UNIQUE`
   column in the database).
2. **Content hash** — the post text is normalized (lowercased, extra spaces
   removed) and hashed with SHA-256. If someone **reposts the same
   announcement as a new post**, the text hash matches and it's skipped too.

### ④ Enrichment — the "advanced" part

**Images (OCR):** Many funding announcements are posted as a poster/flyer
image with the important details *only in the picture*. Each attached image is
downloaded through the logged-in browser and sent to the AI's **vision** input
(`engine/llm_client.py → chat_vision()`), which transcribes all readable text —
title, deadline, amounts, links, emails. That text joins the analysis.

**External websites:** If the post text contains a link (including LinkedIn's
`lnkd.in` short links, which are followed to the real site), the engine fetches
that page, strips the HTML down to readable text, and includes it in the
analysis. So if the post says *"details on our website"*, the details still end
up in your Excel.

Both steps are best-effort: if an image can't be read or a site can't be
reached, the run simply continues with what it has.

### ⑤ AI analysis — `analyze_post()`

Everything gathered for one post — **post text + image text + website text** —
along with your **organisation profile** (if uploaded), is sent to the AI with
one strict instruction: decide whether this is a *real, actionable* funding
opportunity, score how relevant it is to your prompt and profile (0.0–1.0), and
extract only facts that are **actually stated** (no guessing, no invented
deadlines).

The AI is also given explicit rules to score 0.0 if the post is from an entity
*seeking* funds, a gratitude/past-award announcement, or a grant-writing service.
Posts that aren't funding opportunities, or score below the threshold (default 0.7),
are dropped.

If the AI fails mid-run, a keyword-based fallback still detects funding terms
and extracts emails/deadlines/links with patterns — the run never crashes.

### ⑥ Saving + live progress

Each accepted opportunity is written immediately to the `grants` table in
SQLite. The web page shows live progress (Collected / Target / Passes) through
the same streaming mechanism as the Jobs tab.

To prevent infinite loops when results are scarce, the grants run has a hard
**8-minute wall-clock timeout**. If it hits this cap, it stops gracefully and
saves everything collected so far. The **Stop** button works the same way:
everything collected so far stays saved and downloadable.

### ⑦ Download — the Excel columns

`engine/grants_exporter.py` produces one row per opportunity:

| Column | What it tells you |
|---|---|
| `opportunity_title` | Short name of the opportunity |
| `funder` | Who is giving the money |
| `summary` | 2–3 sentence factual summary |
| `deadline` | Application deadline, exactly as stated |
| `grant_amount` | Amount/range, exactly as stated |
| `eligibility` | Who can apply |
| `focus_areas` | Sectors/themes funded (education, health, …) |
| `geography` | Countries/regions covered |
| `how_to_apply` | Application instructions |
| `application_link` | The URL to apply / learn more |
| `contact_email` | Contact email, if given |
| `post_url` | Permanent link to the original LinkedIn post |
| `author`, `author_url` | Who posted it, with profile link |
| `posted_date` | LinkedIn's stamp ("2w") |
| `posted_date_normalized` | The same, converted to a real date (e.g. 2026-06-21) |
| `external_links` | All external URLs found in the post |
| `relevance_score`, `relevance_reason` | How well it matches your prompt, and why |
| `keyword` | Which search phrase found it |
| `post_text`, `image_text` | The raw evidence everything was extracted from |
| `scraped_at` | When it was collected |

Empty cells mean the information genuinely wasn't stated anywhere — the AI is
told not to invent anything.

---

## 3. Where the code lives

| File | Role |
|---|---|
| `engine/grants_pipeline.py` | The orchestrator — runs steps ①–⑥ |
| `engine/linkedin_client.py` | `search_posts()`, image download (browser automation) |
| `engine/llm_client.py` | AI calls, including `chat_vision()` for reading images |
| `config/ai_config.py` | The exact AI prompt templates used |
| `engine/database.py` | `grants` table + duplicate checks |
| `engine/grants_exporter.py` | Excel / CSV / JSON export |
| `app.py` | `/scrape` with `mode: "grants"`, live stream, downloads |
| `templates/index.html` | The Grants tab UI |

## 4. Settings (in `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GRANT_ANALYZE_IMAGES` | `true` | Read text inside post images |
| `GRANT_FOLLOW_LINKS` | `true` | Fetch external websites linked in posts |
| `GRANT_MAX_IMAGES_PER_POST` | `2` | Images analyzed per post |
| `GRANT_MAX_LINKS_PER_POST` | `2` | External sites fetched per post |
| `GRANT_RELEVANCE_THRESHOLD` | `0.7` | Minimum score to keep a post |
| `MAX_GRANT_POSTS_DEFAULT` | `10` | Default target quota for grants |
| `MAX_POST_SEARCH_PAGES` | `5` | Search pages per keyword |

## 5. Good to know

- **DRY_RUN=true** runs the whole pipeline against mock posts with zero
  LinkedIn/AI calls — useful for testing the UI and exports safely.
- Image reading requires the configured AI model to support images (vision).
  If it doesn't, the run continues without image text.
- LinkedIn changes its page structure now and then. The extractor uses several
  fallback selectors, but if a run suddenly returns zero posts, the selectors
  in `search_posts()` are the first place to look.
- One scrape at a time is the supported model (the browser session is shared).
