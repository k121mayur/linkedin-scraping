"""LinkedIn search client using Playwright with auth, throttle, and public fallback."""

from __future__ import annotations

import random
import time
import re
from config import (
    LINKEDIN_EMAIL, LINKEDIN_PASSWORD, PLAYWRIGHT_HEADLESS,
    AUTH_FILE_PATH, DRY_RUN,
)

LINKEDIN_URL = "https://www.linkedin.com"
LOGIN_URL = f"{LINKEDIN_URL}/login"
JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"

CARD_SELECTORS = [
    ".job-search-card",
    ".jobs-search-results__list-item",
    ".job-card-container",
]

TITLE_SELECTORS = [
    ".job-search-card__title",
    ".job-card-list__title",
    ".job-card-container__link",
]

COMPANY_SELECTORS = [
    ".job-search-card__subtitle",
    ".job-card-container__company-name",
]

LOCATION_SELECTORS = [
    ".job-search-card__location",
    ".job-card-container__metadata-item",
]

_browser = None
_page = None


def _init_playwright():
    """Start Playwright sync API."""
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


def _throttle():
    time.sleep(random.uniform(2, 4))


def _try_selectors(page, selectors: list[str]):
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            return el.inner_text().strip()
    return None


def _try_selectors_all(page, selectors: list[str]):
    for sel in selectors:
        els = page.query_selector_all(sel)
        if els:
            return els
    return []

def _login():
    """Log into LinkedIn using the already-initialized browser and persist auth state."""
    global _page
    # Browser must already be initialized by _ensure_auth()
    assert _browser is not None, "_login called before browser init"
    _page = _browser.new_page()

    _page.goto(LOGIN_URL, wait_until="domcontentloaded")
    _throttle()

    _page.fill("#username", LINKEDIN_EMAIL)
    _page.fill("#password", LINKEDIN_PASSWORD)
    _page.click("button[type='submit']")
    _throttle()

    try:
        _page.wait_for_url(f"{LINKEDIN_URL}/feed*", timeout=15000)
    except Exception:
        pass

    _page.context.storage_state(path=str(AUTH_FILE_PATH))


def _ensure_auth():
    global _browser, _page
    if _browser is not None:
        return

    pw = _init_playwright()
    _browser = pw.chromium.launch(headless=PLAYWRIGHT_HEADLESS)

    auth_exists = AUTH_FILE_PATH.exists() and AUTH_FILE_PATH.is_file()
    if auth_exists:
        ctx = _browser.new_context(storage_state=str(AUTH_FILE_PATH))
        _page = ctx.new_page()
    else:
        _login()
    assert _page is not None, "Failed to initialize page"


def _page_safe():
    assert _page is not None, "Browser not initialized"
    return _page


def search(query: str, location: str = "") -> list[dict]:
    if DRY_RUN:
        return _mock_search(query, location)

    try:
        _ensure_auth()
    except Exception as e:
        import sys
        print(f"[linkedin_client] Auth failed: {e}", file=sys.stderr, flush=True)

    if _page is None:
        return []

    params = []
    if query:
        params.append(f"keywords={_url_quote(query)}")
    if location:
        params.append(f"location={_url_quote(location)}")
    url = JOBS_SEARCH_URL + "?" + "&".join(params)

    try:
        _page_safe().goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        try:
            _login()
            _page_safe().goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return []

    _throttle()

    for _ in range(3):
        _page_safe().keyboard.press("End")
        time.sleep(1)

    cards = _try_selectors_all(_page_safe(), CARD_SELECTORS)
    if not cards:
        return []

    results = []
    for card in cards[:50]:
        try:
            link_el = card.query_selector("a")
            link = (link_el.get_attribute("href") or "") if link_el else ""
            job_id = _extract_job_id(link)

            title = (_clean_text(card, TITLE_SELECTORS) or
                     link_el.inner_text().strip() if link_el else "")

            results.append({
                "job_id": job_id,
                "title": title,
                "company": _clean_text(card, COMPANY_SELECTORS) or "",
                "location": _clean_text(card, LOCATION_SELECTORS) or "",
                "posted": "",
                "link": link.split("?")[0] if link else "",
                "snippet": "",
            })
        except Exception:
            continue

    return results


def get_job_detail(job_url: str) -> dict | None:
    if DRY_RUN:
        return {
            "description": f"Mock description for {job_url}",
            "apply_url": job_url,
            "posted_date": "2 days ago",
            "company_url": "",
        }

    try:
        page = _page_safe()
        page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
        _throttle()

        try:
            page.click("button:has-text('Show more')", timeout=3000)
            _throttle()
        except Exception:
            pass

        desc_el = page.query_selector(".jobs-description__content, .description__text, #job-details")
        description = desc_el.inner_text() if desc_el else ""

        apply_el = page.query_selector("a[href*='linkedin.com/comm/jobs']")
        apply_url = apply_el.get_attribute("href") if apply_el else job_url

        posted_el = page.query_selector(".jobs-unified-top-card__posted-date, .posted-time-ago__text")
        posted_date = posted_el.inner_text().strip() if posted_el else ""

        company_link = page.query_selector(".jobs-unified-top-card__company-name a")
        company_url = company_link.get_attribute("href") if company_link else ""

        return {
            "description": description,
            "apply_url": apply_url,
            "posted_date": posted_date,
            "company_url": company_url,
        }
    except Exception:
        return None


def close():
    global _browser, _page
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
        _page = None


def _clean_text(card, selectors: list[str]) -> str:
    for sel in selectors:
        el = card.query_selector(sel)
        if el:
            return el.inner_text().strip()
    return ""


def _extract_job_id(url: str) -> str:
    m = re.search(r"/view/(\d+)", url)
    return m.group(1) if m else url


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


def _mock_search(query: str, location: str) -> list[dict]:
    return [
        {
            "job_id": f"mock_{i}",
            "title": f"{query.split()[0].title()} Position {i}",
            "company": "Mock NGO Corp",
            "location": location or "India",
            "posted": "1 week ago",
            "link": f"https://linkedin.com/jobs/view/mock_{i}",
            "snippet": f"This is a mock job for {query}",
        }
        for i in range(1, 6)
    ]
