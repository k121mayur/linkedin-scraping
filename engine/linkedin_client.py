"""LinkedIn search client using Playwright with an authenticated session.

Scraping strategy (kept deliberately): log into the LinkedIn account (reusing a
saved session when possible) and scrape the authenticated jobs search. The job
link for every card is built from its numeric job id (canonical
``/jobs/view/<id>``) so exported links are always real and clickable.
"""

from __future__ import annotations

import random
import time
import re
from config import (
    LINKEDIN_EMAIL, LINKEDIN_PASSWORD, PLAYWRIGHT_HEADLESS,
    AUTH_FILE_PATH, DRY_RUN, LINKEDIN_JOB_VIEW_URL,
    JOBS_PER_PAGE, MAX_SEARCH_PAGES, SCROLL_PASSES, PAGE_NAV_TIMEOUT,
)
from engine.email_verifier import fetch_verification_code, gmail_configured

LINKEDIN_URL = "https://www.linkedin.com"
LOGIN_URL = f"{LINKEDIN_URL}/login"
FEED_URL = f"{LINKEDIN_URL}/feed/"
JOBS_SEARCH_URL = f"{LINKEDIN_URL}/jobs/search/"

_pw = None
_browser = None
_context = None
_page = None


# ── Playwright lifecycle ─────────────────────────────────────

def _init_playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright().start()


def _throttle(lo: float = 2.0, hi: float = 4.0):
    time.sleep(random.uniform(lo, hi))


def _first_visible(page, selectors: list[str]):
    """Return the first visible element matching any selector (login forms can
    be duplicated in the DOM with a hidden copy)."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                item = loc.nth(i)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _fill_first(page, selectors: list[str], value: str):
    """Fill the first visible matching input. Returns the locator filled, or None."""
    el = _first_visible(page, selectors)
    if el is None:
        return None
    try:
        el.fill(value, timeout=8000)
        return el
    except Exception:
        return None


_EMAIL_SELECTORS = [
    'input[autocomplete="username"]', '#username',
    'input[name="session_key"]', 'input[type="email"]',
]
_PASSWORD_SELECTORS = [
    'input[autocomplete="current-password"]', '#password',
    'input[name="session_password"]', 'input[type="password"]',
]


_PIN_SELECTORS = [
    'input[name="pin"]',
    'input#input__email_verification_pin',
    'input[autocomplete="one-time-code"]',
    'input[placeholder*="code" i]',
    'input[aria-label*="code" i]',
    'input[id*="verification"]',
    'input[id*="pin"]',
]


def _find_pin_input(page):
    """Locate LinkedIn's email-verification PIN input, if the challenge is shown."""
    for sel in _PIN_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    # On a checkpoint/challenge page, fall back to the first visible text-like input.
    try:
        if "/checkpoint" in (page.url or "") or "/challenge" in (page.url or ""):
            els = page.query_selector_all(
                'input:not([type="hidden"]):not([type="checkbox"]):not([type="submit"])'
                ':not([type="button"]):not([type="password"]):not([type="radio"])'
            )
            for el in els:
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _submit_pin(page, pin_el, code: str) -> None:
    try:
        pin_el.fill(code)
    except Exception:
        try:
            pin_el.type(code, delay=40)
        except Exception:
            return
    for sel in ("button#email-pin-submit-button", "button[type='submit']"):
        try:
            page.locator(sel).first.click(timeout=4000)
            return
        except Exception:
            continue
    for name in ("Submit", "Verify", "Continue", "Agree", "Done", "Next"):
        try:
            page.get_by_role("button", name=name).first.click(timeout=2500)
            return
        except Exception:
            continue
    try:
        pin_el.press("Enter")
    except Exception:
        pass


def _handle_email_challenge(page, after_epoch: float, used_codes: set[str]) -> bool:
    """If an email-PIN challenge is shown, fetch the code from Gmail and submit it.

    Returns True if a code was submitted this call, else False.
    """
    pin_el = _find_pin_input(page)
    if pin_el is None:
        return False
    if not gmail_configured():
        return False
    code = fetch_verification_code(after_epoch=after_epoch, already_used=used_codes)
    if not code:
        return False
    import sys
    print(f"[linkedin_client] Auto-entering email verification code {code}",
          file=sys.stderr, flush=True)
    used_codes.add(code)
    _submit_pin(page, pin_el, code)
    return True


def _is_logged_in(page) -> bool:
    """Heuristic: are we on an authenticated LinkedIn page (not the auth wall)?"""
    try:
        url = page.url or ""
    except Exception:
        return False
    if "/login" in url or "/authwall" in url or "/checkpoint" in url or "/uas/login" in url:
        return False
    for sel in (
        "div.feed-identity-module",
        "nav.global-nav",
        "header#global-nav",
        "input.search-global-typeahead__input",
        "img.global-nav__me-photo",
    ):
        try:
            if page.query_selector(sel):
                return True
        except Exception:
            continue
    # On a /feed or /jobs URL without the auth wall, assume authenticated.
    return "/feed" in url or "/jobs" in url


def _login():
    """Log in with credentials, allowing time to solve any 2FA/captcha challenge.

    When running headed (PLAYWRIGHT_HEADLESS=false), a challenge can be solved
    manually within the wait window; the session is then persisted to
    AUTH_FILE_PATH and reused on later runs.
    """
    global _page
    assert _context is not None, "_login called before context init"
    _page = _context.new_page()

    _page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT)

    # Wait for the (React-rendered) login form to mount before filling.
    try:
        _page.wait_for_selector(
            'input[autocomplete="username"], #username, input[type="email"], input[name="session_key"]',
            timeout=20000,
        )
    except Exception:
        pass
    _throttle(1.0, 2.0)

    # LinkedIn's login markup varies (dedicated /login page, homepage variant,
    # React build with dynamic ids). Target by stable attributes, trying each.
    import sys
    email_el = _fill_first(_page, _EMAIL_SELECTORS, LINKEDIN_EMAIL)
    pwd_el = _fill_first(_page, _PASSWORD_SELECTORS, LINKEDIN_PASSWORD)

    if email_el and pwd_el:
        print("[linkedin_client] Credentials filled; submitting login.", file=sys.stderr, flush=True)
        submitted = False
        # Submitting via Enter on the filled password field avoids ambiguity with
        # the SSO "Sign in with…" buttons.
        try:
            pwd_el.press("Enter")
            submitted = True
        except Exception:
            pass
        if not submitted:
            btn = _first_visible(_page, ["button[type='submit']"])
            if btn is None:
                try:
                    btn = _page.get_by_role("button", name="Sign in", exact=True).first
                except Exception:
                    btn = None
            if btn is not None:
                try:
                    btn.click(timeout=4000)
                except Exception as e:
                    print(f"[linkedin_client] Could not click Sign in: {e}", file=sys.stderr, flush=True)
    else:
        print(
            f"[linkedin_client] Login fields not found (email={bool(email_el)}, pwd={bool(pwd_el)}).",
            file=sys.stderr, flush=True,
        )

    # Wait for an authenticated state, auto-clearing the email-PIN challenge via
    # Gmail IMAP. Records when we submitted so we only accept a freshly-sent code.
    submit_epoch = time.time()
    used_codes: set[str] = set()
    deadline = time.time() + 300
    while time.time() < deadline:
        if _is_logged_in(_page):
            break
        try:
            if _handle_email_challenge(_page, submit_epoch, used_codes):
                time.sleep(6)  # allow the checkpoint to process the code
                continue
        except Exception as e:
            import sys
            print(f"[linkedin_client] Email challenge handling error: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(3)

    if _is_logged_in(_page):
        try:
            _context.storage_state(path=str(AUTH_FILE_PATH))
        except Exception:
            pass
    else:
        import sys
        try:
            from config import DATA_DIR
            shot = str(DATA_DIR / "login_failed.png")
            _page.screenshot(path=shot)
            print(f"[linkedin_client] Login not complete. Screenshot: {shot} | url={_page.url}",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        print(
            "[linkedin_client] Login did not complete (challenge unsolved or bad "
            "credentials). Run with PLAYWRIGHT_HEADLESS=false to solve it once.",
            file=sys.stderr, flush=True,
        )


def _ensure_auth():
    """Start the browser and ensure we have an authenticated page."""
    global _pw, _browser, _context, _page
    if _page is not None and _is_logged_in(_page):
        return

    if _pw is None:
        _pw = _init_playwright()
    if _browser is None:
        # Server-safe Chromium flags: --no-sandbox is required when running as
        # root (Docker/CI), --disable-dev-shm-usage avoids /dev/shm exhaustion on
        # small servers, --disable-gpu keeps headless Chromium stable without a
        # GPU. These are no-ops on a desktop run, so they're always applied.
        _browser = _pw.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

    auth_exists = AUTH_FILE_PATH.exists() and AUTH_FILE_PATH.is_file()
    if auth_exists and _context is None:
        # Try to reuse the saved session.
        _context = _browser.new_context(storage_state=str(AUTH_FILE_PATH))
        _page = _context.new_page()
        try:
            _page.goto(FEED_URL, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT)
            _throttle(1.5, 2.5)
        except Exception:
            pass
        if _is_logged_in(_page):
            return
        # Saved session is stale — fall through to a fresh login.
        try:
            _context.close()
        except Exception:
            pass
        _context = None
        _page = None

    if _context is None:
        _context = _browser.new_context()
    _login()


def _page_safe():
    assert _page is not None, "Browser not initialized"
    return _page


# ── Search ───────────────────────────────────────────────────

def search(query: str, location: str = "", limit: int | None = None) -> list[dict]:
    """Search LinkedIn jobs, paginating to gather unique cards.

    Returns a list of card dicts; every card has a real canonical ``link``
    (https://www.linkedin.com/jobs/view/<id>) derived from its numeric job id.
    ``limit`` bounds how many unique cards to gather (stops paginating early).
    """
    if DRY_RUN:
        return _mock_search(query, location)

    try:
        _ensure_auth()
    except Exception as e:
        import sys
        print(f"[linkedin_client] Auth failed: {e}", file=sys.stderr, flush=True)

    if _page is None or not _is_logged_in(_page):
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    for page_idx in range(MAX_SEARCH_PAGES):
        start = page_idx * JOBS_PER_PAGE
        url = _build_search_url(query, location, start)
        try:
            _page_safe().goto(url, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT)
        except Exception:
            break

        _throttle(1.2, 2.2)
        if not _wait_for_cards(_page_safe()):
            break  # no results on this page → query exhausted

        _scroll_to_load(_page_safe())

        page_cards = _extract_cards(_page_safe())
        new_on_page = 0
        for card in page_cards:
            jid = card["job_id"]
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            results.append(card)
            new_on_page += 1

        # No fresh jobs surfaced on this page → we've reached the end.
        if new_on_page == 0:
            break

        if limit and len(results) >= limit:
            break

    return results


def _build_search_url(query: str, location: str, start: int) -> str:
    params = []
    if query:
        params.append(f"keywords={_url_quote(query)}")
    if location:
        params.append(f"location={_url_quote(location)}")
    if start:
        params.append(f"start={start}")
    return JOBS_SEARCH_URL + "?" + "&".join(params)


def _wait_for_cards(page) -> bool:
    """Wait for at least one job-view anchor to appear. False if none/none found."""
    try:
        page.wait_for_selector('a[href*="/jobs/view/"]', timeout=8000)
        return True
    except Exception:
        # Could be a genuinely empty result set — check the empty-state text.
        try:
            body = (page.inner_text("body") or "").lower()
            if "no matching jobs" in body or "no results found" in body:
                return False
        except Exception:
            pass
        return bool(page.query_selector('a[href*="/jobs/view/"]'))


def _scroll_to_load(page):
    """Scroll the (virtualized) results list so all ~25 cards on the page render."""
    for _ in range(SCROLL_PASSES):
        try:
            page.evaluate(
                """() => {
                    const anchors = document.querySelectorAll('a[href*="/jobs/view/"]');
                    if (anchors.length) {
                        anchors[anchors.length - 1].scrollIntoView({block: 'center'});
                    }
                    const list = document.querySelector(
                        '.jobs-search-results-list, .scaffold-layout__list, ul.jobs-search__results-list'
                    );
                    if (list) { list.scrollTop = list.scrollHeight; }
                }"""
            )
        except Exception:
            pass
        time.sleep(0.6)


def _extract_cards(page) -> list[dict]:
    """Extract job cards from the DOM, keyed off stable /jobs/view/<id> anchors."""
    try:
        raw = page.evaluate(
            """() => {
                const seen = {};
                const out = [];
                const anchors = document.querySelectorAll('a[href*="/jobs/view/"]');
                anchors.forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const m = href.match(/\\/jobs\\/view\\/(\\d+)/);
                    if (!m) return;
                    const id = m[1];
                    if (seen[id]) return;
                    seen[id] = true;
                    const card = a.closest('li, div.job-card-container, div.base-card') || a.parentElement;
                    let title = (a.getAttribute('aria-label') || a.innerText || '').trim();
                    const firstLine = title.split('\\n').map(s => s.trim()).filter(Boolean)[0];
                    if (firstLine) title = firstLine;
                    let company = '', location = '';
                    if (card) {
                        const c = card.querySelector(
                            '.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, ' +
                            '.job-card-container__company-name, .base-search-card__subtitle'
                        );
                        if (c) company = c.innerText.trim();
                        const l = card.querySelector(
                            '.job-card-container__metadata-item, .artdeco-entity-lockup__caption, ' +
                            '.job-search-card__location'
                        );
                        if (l) location = l.innerText.trim();
                    }
                    out.push({job_id: id, title: title, company: company, location: location});
                });
                return out;
            }"""
        ) or []
    except Exception:
        raw = []

    cards = []
    for item in raw:
        jid = str(item.get("job_id", "")).strip()
        if not jid.isdigit():
            continue
        cards.append({
            "job_id": jid,
            "title": (item.get("title") or "").strip(),
            "company": (item.get("company") or "").strip(),
            "location": (item.get("location") or "").strip(),
            "posted": "",
            "link": canonical_view_url(jid),
            "snippet": "",
        })
    return cards


# ── Detail ───────────────────────────────────────────────────

def get_job_detail(job_url_or_id: str) -> dict:
    """Fetch a job's full detail. Always returns a dict with a valid apply_url.

    Never returns None: on any failure it still returns the canonical link so the
    job is never dropped or left without a usable URL.
    """
    job_id = _extract_job_id(job_url_or_id)
    canonical = canonical_view_url(job_id) if job_id else (job_url_or_id or "")

    if DRY_RUN:
        return {
            "description": f"Mock description for {canonical}",
            "apply_url": canonical,
            "posted_date": "2 days ago",
            "company_url": "https://www.linkedin.com/company/mock-ngo-corp",
        }

    fallback = {"description": "", "apply_url": canonical, "posted_date": "", "company_url": ""}
    if _page is None:
        return fallback

    try:
        page = _page_safe()
        page.goto(canonical, wait_until="domcontentloaded", timeout=PAGE_NAV_TIMEOUT)
        _throttle(0.5, 1.0)

        # Expand the description if a "Show more" toggle exists (short timeout —
        # the current layout shows the full text without it).
        try:
            page.click("button:has-text('Show more')", timeout=800)
        except Exception:
            pass

        description = _extract_description(page)
        if not description:
            time.sleep(1.0)
            description = _extract_description(page)

        posted_date = ""
        for sel in (
            ".jobs-unified-top-card__posted-date",
            ".posted-time-ago__text",
            ".jobs-unified-top-card__subtitle-secondary-grouping span",
        ):
            el = page.query_selector(sel)
            if el:
                posted_date = (el.inner_text() or "").strip()
                if posted_date:
                    break

        company_url = _extract_company_url(page)

        return {
            "description": description,
            "apply_url": canonical,
            "posted_date": posted_date,
            "company_url": company_url,
        }
    except Exception:
        return fallback


def _extract_company_url(page) -> str:
    """Resolve the hiring company's LinkedIn page URL from the job detail page.

    LinkedIn's job page uses hashed/versioned class names, so anchoring on a
    fixed ``.jobs-unified-top-card__company-name a`` selector silently breaks
    (this was leaving company_url empty in exports). Instead we scan the top-card
    region for the first stable ``/company/<slug>`` anchor, then fall back to any
    such anchor on the page. Tracking query params are stripped.
    """
    try:
        href = page.evaluate(
            """() => {
                const clean = (h) => {
                    if (!h) return '';
                    try {
                        const u = new URL(h, 'https://www.linkedin.com');
                        // canonical company URL: keep only the /company/<slug> path
                        const m = u.pathname.match(/\\/company\\/[^\\/?#]+/);
                        return m ? 'https://www.linkedin.com' + m[0] : '';
                    } catch (e) { return ''; }
                };
                // Prefer an anchor inside the top card (the hiring company link).
                const scopes = [
                    '.job-details-jobs-unified-top-card__company-name',
                    '.jobs-unified-top-card__company-name',
                    '.job-details-jobs-unified-top-card__primary-description-container',
                    '.jobs-unified-top-card',
                    '.job-details-jobs-unified-top-card__container--two-pane',
                ];
                for (const s of scopes) {
                    const scope = document.querySelector(s);
                    if (!scope) continue;
                    const a = scope.querySelector('a[href*="/company/"]');
                    if (a) { const c = clean(a.getAttribute('href')); if (c) return c; }
                }
                // Fall back to the first /company/ anchor anywhere on the page.
                const any = document.querySelector('a[href*="/company/"]');
                if (any) { const c = clean(any.getAttribute('href')); if (c) return c; }
                return '';
            }"""
        )
        return (href or "").strip()
    except Exception:
        return ""


def close():
    global _pw, _browser, _context, _page
    for closer in (
        lambda: _context.close() if _context else None,
        lambda: _browser.close() if _browser else None,
        lambda: _pw.stop() if _pw else None,
    ):
        try:
            closer()
        except Exception:
            pass
    _pw = _browser = _context = _page = None


# ── Helpers ──────────────────────────────────────────────────

def canonical_view_url(job_id: str) -> str:
    """The single source of truth for a job's clickable link."""
    return f"{LINKEDIN_JOB_VIEW_URL.rstrip('/')}/{job_id}"


_DESC_END_MARKERS = (
    "similar jobs", "people also viewed", "set alert", "jobs you may be interested",
    "more jobs at", "see more jobs", "show more jobs", "report this job",
    "people you can reach", "salary insights",
)


def _trim_desc(text: str) -> str:
    """Trim trailing page boilerplate that follows the job description."""
    low = text.lower()
    cut = len(text)
    for m in _DESC_END_MARKERS:
        j = low.find(m)
        if 50 < j < cut:
            cut = j
    return text[:cut].strip()[:8000]


def _extract_description(page) -> str:
    """Pull the job description text.

    The current LinkedIn job page uses hashed class names, so we anchor on the
    stable "About the job" section heading in the page text. Older layouts with
    stable selectors are tried first.
    """
    for sel in (
        "#job-details",
        ".jobs-description__content",
        ".jobs-box__html-content",
        ".show-more-less-html__markup",
        ".description__text",
    ):
        try:
            el = page.query_selector(sel)
            if el:
                t = (el.inner_text() or "").strip()
                if len(t) > 40:
                    return _trim_desc(t)
        except Exception:
            continue
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return ""
    idx = body.lower().find("about the job")
    if idx >= 0:
        return _trim_desc(body[idx:])
    return ""


def _extract_job_id(url_or_id: str) -> str:
    s = str(url_or_id or "")
    if s.isdigit():
        return s
    m = re.search(r"/jobs/view/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", s)
    return m.group(1) if m else ""


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


def _mock_search(query: str, location: str) -> list[dict]:
    base = query.split()[0].title() if query.split() else "Job"
    return [
        {
            "job_id": f"mock_{i}",
            "title": f"{base} Position {i}",
            "company": "Mock NGO Corp",
            "location": location or "India",
            "posted": "1 week ago",
            "link": f"{LINKEDIN_JOB_VIEW_URL.rstrip('/')}/mock_{i}",
            "snippet": f"This is a mock job for {query}",
        }
        for i in range(1, 6)
    ]
