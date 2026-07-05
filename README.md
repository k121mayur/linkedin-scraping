# Prompt-Driven LinkedIn Job Scraper

> Describe the jobs you want in **plain English** — the app figures out what to search for on
> LinkedIn, collects the matching jobs, ranks them by how well they fit your request, and lets
> you download the results as Excel, CSV, or JSON.

This README is written for someone who **did not build this project** and wants to understand
it, run it, and improve it. No prior knowledge of the codebase is assumed. Where a technical
term shows up, it's explained in plain language, and there's a [Glossary](#glossary) at the end.

---

## Table of contents

1. [What this project does (in plain English)](#1-what-this-project-does-in-plain-english)
2. [How it works — the big picture](#2-how-it-works--the-big-picture)
3. [The tech it's built with](#3-the-tech-its-built-with)
4. [Project structure — what every file does](#4-project-structure--what-every-file-does)
5. [Before you start (prerequisites)](#5-before-you-start-prerequisites)
6. [Step-by-step setup](#6-step-by-step-setup)
7. [Running the app](#7-running-the-app)
8. [Understanding DRY_RUN (read this!)](#8-understanding-dry_run-read-this)
9. [Configuration — the `.env` file explained](#9-configuration--the-env-file-explained)
10. [Using the web page](#10-using-the-web-page)
11. [The API endpoints](#11-the-api-endpoints)
12. [Where your data is stored](#12-where-your-data-is-stored)
13. [Deploying it for real (Docker / Raspberry Pi)](#13-deploying-it-for-real-docker--raspberry-pi)
14. [Troubleshooting](#14-troubleshooting)
15. [Ideas for upgrading the project](#15-ideas-for-upgrading-the-project)
16. [Glossary](#glossary)

---

## 1. What this project does (in plain English)

Normally, finding jobs on LinkedIn means typing keywords into a search box, scrolling through
pages, opening each job, reading it, and deciding if it's relevant. This project automates all
of that.

You type something like:

> *"Extract NGO sector accountant and junior financial analyst roles in India, max 100 jobs"*

and the app:

1. **Reads your sentence** and works out the important details — the job titles
   (*accountant, financial analyst*), the industry (*NGO / non-profit*), the experience level
   (*junior*), and the locations (*India*).
2. **Searches LinkedIn** for those jobs (using an automated browser).
3. **Opens each job** and reads the full description.
4. **Scores each job** from 0.0 to 1.0 for how well it matches what you asked for, and throws
   away the ones that score too low.
5. **Saves the good ones** to a small database on your computer.
6. **Lets you download** the results as an Excel spreadsheet, CSV, or JSON file.

While it's working, a **live progress bar** on the web page shows you how many jobs it has found
so far and what it's currently searching for.

---

## 2. How it works — the big picture

Think of the app as an **assembly line**. Your prompt goes in one end, and a clean list of jobs
comes out the other. Each station on the line does one job and hands its result to the next:

```
  Your prompt ("NGO accountant jobs in India")
        │
        ▼
  ┌─────────────────────┐
  │ 1. Prompt Parser     │  Turns your sentence into a structured "search plan"
  │  prompt_parser.py    │  (job titles, sector, experience level, locations)
  └─────────────────────┘
        │
        ▼
  ┌─────────────────────┐
  │ 2. Search Strategy   │  Builds an ordered to-do list of searches to run,
  │  search_strategy.py  │  e.g. ("accountant" in "Mumbai"), ("accountant" in "Delhi")...
  └─────────────────────┘
        │
        ▼
  ┌─────────────────────┐
  │ 3. The Orchestrator  │  The "manager" that runs the loop below until it has
  │  self_refinement.py  │  enough jobs (or runs out of things to try)
  └─────────────────────┘
        │  for each search in the to-do list:
        │
        ├──►  LinkedIn Client (linkedin_client.py)  — opens a browser, searches, grabs job cards
        ├──►  Job Extractor  (job_extractor.py)     — opens each job, reads the full description
        ├──►  Relevance      (relevance.py)         — scores each job 0.0–1.0, keeps the good ones
        └──►  Database       (database.py)          — saves jobs, avoids duplicates
        │
        ▼
  ┌─────────────────────┐
  │ 4. Exporter          │  Turns saved jobs into Excel / CSV / JSON files to download
  │  exporter.py         │
  └─────────────────────┘
```

Two ideas make this smarter than a plain scraper:

- **AI understanding.** Steps 1 and 3-scoring use an **LLM** (a large language model — the same
  kind of AI behind ChatGPT) to *understand* your request and judge job relevance, instead of
  just matching exact words. If the AI is unavailable, the app **falls back to simple keyword
  matching** so it still works.

- **Self-refinement** (this is the clever part, in [`engine/self_refinement.py`](engine/self_refinement.py)).
  If the app runs out of searches before reaching your target number of jobs, it doesn't give up
  — it **broadens** the search step by step: first it drops the industry filter, then it widens
  the locations, then it removes the location entirely, and finally it tries a single broad
  keyword. This is why it's called the *self-refinement* engine — it adjusts its own strategy to
  find more results.

---

## 3. The tech it's built with

You don't need to be an expert in these, but it helps to know what each one is:

| Tool | What it is | Why this project uses it |
|------|-----------|--------------------------|
| **Python** | A popular, readable programming language | The whole app is written in Python |
| **Flask** | A lightweight Python library for building websites/APIs | Powers the web page and the `/scrape` endpoints |
| **Playwright** | A tool that controls a real web browser automatically | Logs into LinkedIn and clicks/scrolls like a human would |
| **SQLite** | A tiny database that lives in a single file | Stores the jobs it finds (`data/jobs.db`) |
| **pandas + openpyxl** | Python libraries for spreadsheets | Create the downloadable Excel file |
| **An LLM** | An AI model (OpenAI, Anthropic Claude, or a self-hosted Ollama) | Understands your prompt and scores job relevance |
| **Server-Sent Events (SSE)** | A way for the server to push live updates to the browser | Drives the live progress bar |
| **Docker** | A way to package and run the app anywhere | Optional, for production deployment |
| **Cloudflare Tunnel** | Exposes a home server to the internet securely | Optional, used by the Raspberry Pi setup |

> **Note:** This project deliberately avoids heavyweight SDKs. All web/AI calls are made using
> Python's built-in `urllib`, keeping the dependency list short.

---

## 4. Project structure — what every file does

```
linkedin-scraping/
│
├── app.py                  ← The web server. Start here. Defines all the web pages/endpoints.
│
├── config/
│   ├── __init__.py         ← ALL settings live here (reads your .env file). One place for every knob.
│   └── ai_config.py        ← The AI prompt templates + AI-related constants
│
├── engine/                 ← The "assembly line" — the core logic, one file per station
│   ├── prompt_parser.py    ← Station 1: turns your sentence into a structured search plan
│   ├── search_strategy.py  ← Station 2: builds the ordered list of searches to run
│   ├── self_refinement.py  ← The manager: runs the whole loop, broadens search when needed
│   ├── linkedin_client.py  ← Drives the browser: logs in, searches, reads job pages (Playwright)
│   ├── job_extractor.py    ← Opens each job and pulls out the full description
│   ├── relevance.py        ← Scores each job 0.0–1.0 and filters out poor matches
│   ├── database.py         ← Saves everything to SQLite; handles de-duplication
│   ├── exporter.py         ← Builds the Excel / CSV / JSON download files
│   └── llm_client.py       ← Talks to the AI (OpenAI / Ollama / Anthropic)
│
├── templates/
│   └── index.html          ← The web page you see in your browser (the form + progress bar)
│
├── static/                 ← Images used by the web page (logo)
│
├── data/                   ← Created automatically. Holds the database + exports. (git-ignored)
│
├── requirements.txt        ← The list of Python libraries the app needs
├── .env.example            ← A template for your secret settings — copy it to ".env"
├── Dockerfile              ← Instructions to build the app as a Docker container
├── docker-compose.yml      ← One-command Docker run config
├── run.sh                  ← Automated installer for a Raspberry Pi + Cloudflare Tunnel
│
├── _archive/               ← OLD, unused scraper scripts. Kept for reference only — ignore these.
├── docs/                   ← Design notes and feature docs (PLAN.md, TASKS.md, AI_SCRAPER_PLAN.md, …)
└── CLAUDE.md               ← Notes for the Claude AI coding assistant
```

The most important files to read first, in order: **`app.py`** → **`engine/self_refinement.py`**
→ **`config/__init__.py`**.

---

## 5. Before you start (prerequisites)

You need these installed on your computer:

1. **Python 3.10 or newer.** Check by running `python --version` (or `python3 --version`).
   If you don't have it, download it from [python.org](https://www.python.org/downloads/).
2. **pip** — Python's package installer. It comes with Python.
3. *(Optional, only for real scraping)* **A LinkedIn account** — email and password.
4. *(Optional, only for real AI scoring)* **Access to an LLM** — either an OpenAI/Anthropic API
   key, or the team's self-hosted Ollama server.

> **Good news:** You can run the **entire app without any of the optional items** by using
> *dry-run mode* (explained in [section 8](#8-understanding-dry_run-read-this)). This is the
> recommended way to first see the app working.

---

## 6. Step-by-step setup

Open a terminal (Command Prompt / PowerShell on Windows, Terminal on Mac/Linux) and run these
commands inside the project folder.

### Step 1 — Create a "virtual environment"

A virtual environment is a private, isolated space for this project's Python libraries, so they
don't clash with anything else on your computer.

```bash
# Mac / Linux
python3 -m venv venv
source venv/bin/activate

# Windows (PowerShell)
python -m venv venv
venv\Scripts\activate
```

After this, your terminal prompt should show `(venv)` at the start. That means it's active.

### Step 2 — Install the required libraries

```bash
pip install -r requirements.txt
```

### Step 3 — Install the browser Playwright needs

Playwright drives a real Chrome browser, so it needs to download one:

```bash
playwright install chromium
```

### Step 4 — Create your settings file

Copy the example settings file to a real one called `.env`:

```bash
# Mac / Linux
cp .env.example .env

# Windows (PowerShell)
copy .env.example .env
```

For your **first run you don't need to edit anything** — the defaults run in safe dry-run mode.
When you're ready for real scraping, see [section 9](#9-configuration--the-env-file-explained).

That's it — setup is done. ✅

---

## 7. Running the app

With your virtual environment active (you see `(venv)`), start the app:

```bash
python app.py
```

You'll see output saying it's running on `http://localhost:5000`. Open that address in your web
browser, and you'll see the scraper page.

To stop the app, go back to the terminal and press **Ctrl + C**.

> By default this runs in **dry-run mode** — it uses fake sample data and makes **no** calls to
> LinkedIn or any AI. This is safe and free. Read the next section to understand it.

---

## 8. Understanding DRY_RUN (read this!)

This is **the single most important thing to understand** about the project.

`DRY_RUN` is a setting that defaults to **`true`**. When it's on:

- LinkedIn is **never** contacted — the app returns made-up mock jobs.
- The AI is **never** called — relevance is scored with simple keyword matching.
- Nothing costs money, and there's no risk to your LinkedIn account.

This is perfect for learning how the app works and testing changes. The whole assembly line runs
end-to-end, just with fake data.

**If you see fake/sample jobs and wonder why** — it's because `DRY_RUN` is still `true`. This is
the #1 source of confusion for newcomers.

To do **real** scraping, you must do **two** things:

1. Set `DRY_RUN=false` in your `.env` file.
2. Fill in valid LinkedIn credentials and an LLM key (see the next section).

You can also flip it temporarily for a single run from the command line:

```bash
# Mac / Linux
DRY_RUN=false python app.py

# Windows (PowerShell)
$env:DRY_RUN="false"; python app.py
```

---

## 9. Configuration — the `.env` file explained

The `.env` file holds all your settings and secrets. It is **git-ignored**, meaning it never gets
uploaded or shared. Every setting below is read once in [`config/__init__.py`](config/__init__.py).

| Setting | Default | What it does |
|---------|---------|--------------|
| `DRY_RUN` | `true` | **Master safety switch.** `true` = fake data, no network. `false` = real scraping. |
| `LINKEDIN_EMAIL` | — | Your LinkedIn login email (only needed when `DRY_RUN=false`). |
| `LINKEDIN_PASSWORD` | — | Your LinkedIn password (only needed when `DRY_RUN=false`). |
| `LLM_PROVIDER` | `ollama` | Which AI to use: `openai`, `anthropic`, or `ollama` (self-hosted). |
| `LLM_API_KEY` | — | Your API key for the chosen AI provider. |
| `LLM_BASE_URL` | `https://ollama.siliconmango.in` | The address of the Ollama server (only for `ollama`). |
| `LLM_MODEL` | `gemma4:31b` | Which AI model to use, e.g. `gpt-4o-mini` for OpenAI. |
| `RELEVANCE_THRESHOLD` | `0.65` | Minimum score (0.0–1.0) a job needs to be kept. Higher = stricter. |
| `MAX_JOBS_DEFAULT` | `100` | Default target number of jobs if you don't specify one. |
| `PLAYWRIGHT_HEADLESS` | `true` | `true` = browser runs invisibly. Set `false` to *watch* it work (great for debugging). |
| `MAX_DETAIL_CONCURRENCY` | `3` | How many job pages to process at once. |
| `PORT` | `5000` | The web address port the app runs on. |
| `FLASK_DEBUG` | `false` | `true` = developer mode with auto-reload and detailed errors. |

There are also optional **Gmail settings** (`GMAIL_*`) intended for automatically reading
LinkedIn's email verification codes during login. This feature is **not currently wired into the
live code** (it was deferred — see [docs/TASKS.md](docs/TASKS.md) T14), so you can ignore those for now.

### Picking an AI provider

- **`openai`** — easiest if you have an OpenAI account. Set `LLM_PROVIDER=openai`,
  `LLM_API_KEY=sk-...`, and `LLM_MODEL=gpt-4o-mini`.
- **`anthropic`** — for Claude. Set `LLM_PROVIDER=anthropic`, your `LLM_API_KEY`, and a Claude
  `LLM_MODEL`.
- **`ollama`** — a self-hosted model (the project's default points at Silicon Mango's server).
  Uses an `x-api-key` header for auth.

> **Remember:** even if the AI provider is misconfigured or down, the app won't crash — it quietly
> falls back to keyword-based parsing and scoring. Every connection to the outside world is wrapped
> in a safety net.

---

## 10. Using the web page

1. Open `http://localhost:5000`.
2. In the big text box, **describe the jobs you want** in plain English. Example:
   > *Extract NGO sector accountant and junior financial analyst roles in India*
3. Set **Max jobs** — how many results you want (5 to 500).
4. Click **Start Scraping**.
5. Watch the **progress bar** fill up. It shows how many jobs have been found and what it's
   currently searching for.
6. When it's done, **Download** buttons appear for XLSX (Excel), CSV, and JSON.

Behind the scenes, the page sends your request to the server, then opens a live "stream"
connection so the server can push progress updates back to your browser in real time.

---

## 11. The API endpoints

If you want to use the app programmatically (without the web page), these are the URLs it
responds to. An "endpoint" is just a web address that does something when you visit it.

| Method & URL | What it does |
|--------------|--------------|
| `GET /` | Shows the web page. |
| `POST /scrape` | Starts a new scrape. Send JSON: `{"prompt": "...", "max_jobs": 100}`. Returns a `run_id`. |
| `GET /stream/<run_id>` | A live progress stream (Server-Sent Events) for that run. |
| `GET /download/<run_id>/xlsx` | Download that run's results as an Excel file. |
| `GET /download/<run_id>/csv` | Download as CSV. |
| `GET /download/<run_id>/json` | Download as JSON. |
| `GET /runs/<run_id>` | Get the run's details + all its jobs as JSON. |
| `GET /health` | A simple check that returns `{"status": "ok"}` (used by Docker). |

Example using `curl` to start a scrape:

```bash
curl -X POST http://localhost:5000/scrape \
  -H "Content-Type: application/json" \
  -d '{"prompt": "junior accountant NGO jobs in India", "max_jobs": 20}'
```

> **How a scrape runs internally:** `POST /scrape` immediately creates a record and starts the
> work in a **background thread**, then returns a `run_id` right away. The actual scraping happens
> in the background and reports progress through `/stream/<run_id>`. Because the browser is shared
> globally and isn't thread-safe, the app is designed to run **one scrape at a time**.

---

## 12. Where your data is stored

Everything the app produces lives in the **`data/`** folder (created automatically, and
git-ignored so it never gets committed):

- **`data/jobs.db`** — a SQLite database file. This is the app's memory. It has three tables:
  - `search_runs` — one row per scrape you start (the prompt, when it ran, status, count).
  - `jobs` — the actual jobs found. Each job is **unique by its LinkedIn job ID**, so the same job
    found across different searches is only stored once (this is called *de-duplication*).
  - `search_attempts` — a log of every individual search the app tried, useful for debugging.
- **`data/exports/`** — where exported files can be written.

You can open `data/jobs.db` with any free SQLite viewer (like [DB Browser for SQLite](https://sqlitebrowser.org/))
to inspect the data directly.

> The database **creates itself automatically** the first time the app's database code runs — you
> never have to set it up manually.

---

## 13. Deploying it for real (Docker / Raspberry Pi)

These are for when you want the app running continuously on a server, not just on your laptop.
**You can skip this section while learning.**

### Option A — Docker (easiest for most servers)

[Docker](https://www.docker.com/) packages the app and everything it needs into one container, so
it runs the same way on any machine.

```bash
docker compose build
docker compose up
```

This reads your `.env` file, exposes the app on the `PORT` you set, and keeps your `data/` folder
on the host machine so your database survives restarts.

### Option B — Raspberry Pi + Cloudflare Tunnel

The [`run.sh`](run.sh) script is an automated installer for running this on a Raspberry Pi (a tiny
home computer) and making it reachable from the internet **without opening any ports on your
router**, using a Cloudflare Tunnel.

```bash
sudo ./run.sh
```

It installs system packages, sets up Python and Playwright, creates a Cloudflare tunnel, and
registers the app as a **systemd service** (so it auto-starts on boot and restarts if it crashes).
The script is *idempotent* — safe to run again after changing settings. It will prompt you for a
hostname (e.g. `scrape.yourdomain.com`) the first time.

---

## 14. Troubleshooting

| Problem | Likely cause & fix |
|---------|--------------------|
| **"It keeps returning fake jobs like 'Mock NGO Corp'."** | `DRY_RUN` is still `true`. Set `DRY_RUN=false` in `.env` **and** add real credentials. |
| **`playwright` errors / "browser not found".** | You skipped `playwright install chromium`. Run it. |
| **`ModuleNotFoundError` when starting.** | Your virtual environment isn't active, or you skipped `pip install -r requirements.txt`. Activate `venv` and reinstall. |
| **LinkedIn login fails or asks for verification.** | LinkedIn sometimes sends an email code or shows a CAPTCHA. Set `PLAYWRIGHT_HEADLESS=false` in `.env` to *watch* the browser and solve it manually. The login is then saved to `playwright_auth.json` for next time. |
| **Jobs are found but all filtered out.** | Your `RELEVANCE_THRESHOLD` may be too high. Lower it (e.g. `0.5`) in `.env`. |
| **The AI isn't being used.** | Check `LLM_PROVIDER`, `LLM_API_KEY`, and `LLM_MODEL`. If they're wrong, the app silently falls back to keyword matching — it won't error, it just won't use AI. |
| **Progress bar stalls / "Connection lost".** | The scrape may have hit LinkedIn rate limits. The app throttles itself (waits 2–4s between actions); try a smaller `max_jobs`. |

> **A note on LinkedIn's rules:** automated scraping of LinkedIn may be against its Terms of
> Service, and LinkedIn actively limits this behaviour. Use this tool responsibly, at a low volume,
> and at your own risk. The built-in throttling exists to behave more like a human.

---

## 15. Ideas for upgrading the project

Since you want to take this "from existing to somewhere," here are sensible next steps, roughly
from easiest to hardest:

- **Easy wins**
  - Add a page to browse past runs (the data is already in `data/jobs.db` and the `/runs/<id>`
    endpoint already exists — it just needs a UI).
  - Show the relevance **score and reason** for each job in the web UI.
  - Add more locations/sectors to the keyword fallback lists in
    [`engine/prompt_parser.py`](engine/prompt_parser.py).

- **Medium**
  - Finish the deferred **Gmail verification** feature (read LinkedIn's email codes automatically —
    the settings already exist; see [docs/TASKS.md](docs/TASKS.md) T14).
  - Add a **scheduler** so scrapes run automatically (e.g. every morning).
  - Add **email notifications** when a run finishes.

- **Bigger**
  - Add a proper **test suite** — there currently is none, so the recommended way to verify changes
    is to run in `DRY_RUN=true` and watch the full pipeline.
  - Support **concurrent runs** by giving each scrape its own browser instance (today the browser is
    a shared global, so only one run at a time is safe).
  - Add **user accounts** so multiple people can use a shared deployment.

When testing any change, **always start in `DRY_RUN=true`** — it exercises the entire pipeline with
mock data, for free and with zero risk.

---

## Glossary

- **API** — A set of web addresses (endpoints) a program exposes so other programs can talk to it.
- **CSV** — A plain-text spreadsheet format (comma-separated values). Opens in Excel.
- **Dry-run** — A safe practice mode that uses fake data and makes no real network calls.
- **Endpoint** — A single URL the server responds to (e.g. `/scrape`).
- **Environment variable / `.env`** — A setting stored outside the code, often a secret. Lives in
  the `.env` file here.
- **Heuristic** — A simple rule-of-thumb method (here, keyword matching) used as a backup when the
  AI isn't available.
- **LLM (Large Language Model)** — The AI that understands text, like the model behind ChatGPT.
- **Orchestrator** — The "manager" piece of code that coordinates all the other pieces
  ([`self_refinement.py`](engine/self_refinement.py)).
- **Playwright** — A tool that automates a real web browser.
- **Prompt** — The plain-English request you type in.
- **SQLite** — A database that's just a single file on disk; no separate server needed.
- **SSE (Server-Sent Events)** — A way for the server to keep pushing live updates to your browser
  (powers the progress bar).
- **Virtual environment (venv)** — An isolated folder of Python libraries just for this project.

---

Built by [Silicon Mango](https://www.siliconmango.com).
