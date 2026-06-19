"""Central configuration — env vars, paths, and runtime knobs."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Project Paths ---
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
DATA_DIR = PROJECT_ROOT / "data"
EXPORTS_DIR = DATA_DIR / "exports"
AUTH_FILE_PATH = PROJECT_ROOT / os.getenv("AUTH_FILE", "playwright_auth.json")

DATA_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# --- LinkedIn credentials ---
LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL", "change-me@example.com")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "change-me-password")

# --- Playwright / scraping behaviour ---
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() not in {"false", "0", "no"}
MAX_DETAIL_CONCURRENCY = int(os.getenv("MAX_DETAIL_CONCURRENCY", "3"))

# Canonical LinkedIn job URL base — the single source of truth for clickable links.
LINKEDIN_JOB_VIEW_URL = os.getenv("LINKEDIN_JOB_VIEW_URL", "https://www.linkedin.com/jobs/view")

# Pagination / scroll knobs for the authenticated jobs search.
JOBS_PER_PAGE = int(os.getenv("JOBS_PER_PAGE", "25"))          # LinkedIn shows 25 cards/page
MAX_SEARCH_PAGES = int(os.getenv("MAX_SEARCH_PAGES", "8"))      # cap pages per query (8*25=200 cards)
SCROLL_PASSES = int(os.getenv("SCROLL_PASSES", "5"))          # scrolls to materialize a virtualized page
PAGE_NAV_TIMEOUT = int(os.getenv("PAGE_NAV_TIMEOUT", "45000"))  # ms for page navigations
FETCH_DETAILS = os.getenv("FETCH_DETAILS", "true").lower() not in {"false", "0", "no"}

# --- AI / LLM Settings ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://ollama.siliconmango.in")
LLM_MODEL = os.getenv("LLM_MODEL", "gemma4:31b")

# --- Relevance & Search Knobs ---
RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.6"))
MAX_JOBS_DEFAULT = int(os.getenv("MAX_JOBS_DEFAULT", "100"))
# Real scraping is the default; set DRY_RUN=true explicitly to use mock data.
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# --- Gmail / verification handling ---
GMAIL_USERNAME = os.getenv("GMAIL_USERNAME")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
GMAIL_IMAP_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_IMAP_PORT = int(os.getenv("GMAIL_IMAP_PORT", "993"))
GMAIL_IMAP_FOLDER = os.getenv("GMAIL_IMAP_FOLDER", "INBOX")
GMAIL_VERIFICATION_SENDER = os.getenv("GMAIL_VERIFICATION_SENDER", "security-noreply@linkedin.com")
GMAIL_POLL_INTERVAL = float(os.getenv("GMAIL_POLL_INTERVAL", "8"))
GMAIL_POLL_TIMEOUT = float(os.getenv("GMAIL_POLL_TIMEOUT", "180"))

# --- Flask app settings ---
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
FLASK_PORT = int(os.getenv("PORT", "5000"))
