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
