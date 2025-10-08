import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- LinkedIn credentials ---
# Update these values or set the environment variables with the same names.
LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL") or "change-me@example.com"
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD") or "change-me-password"

# --- Playwright / scraping behaviour ---
AUTH_FILE_PATH = Path(os.getenv("LINKEDIN_AUTH_FILE", "playwright_auth.json"))
DEFAULT_KEYWORD = os.getenv("DEFAULT_KEYWORD", "Software Engineer")
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "India")

PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS")
if PLAYWRIGHT_HEADLESS is None:
    HEADLESS = True
else:
    HEADLESS = PLAYWRIGHT_HEADLESS.lower() not in {"false", "0", "no"}

# --- Flask app settings ---
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
FLASK_PORT = int(os.getenv("PORT", "5000"))
