import email
import imaplib
import io
import json
import re
import time
import uuid
from pathlib import Path
from queue import SimpleQueue
from threading import Lock, Thread
from email.message import Message
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse
from flask import Flask

from flask import Response, jsonify, render_template, request, send_file, stream_with_context
from playwright.sync_api import Page, TimeoutError, sync_playwright

from config import (
    AUTH_FILE_PATH,
    DEFAULT_KEYWORD,
    DEFAULT_LOCATION,
    FLASK_DEBUG,
    FLASK_PORT,
    GMAIL_APP_PASSWORD,
    GMAIL_IMAP_FOLDER,
    GMAIL_IMAP_HOST,
    GMAIL_IMAP_PORT,
    GMAIL_POLL_INTERVAL,
    GMAIL_POLL_TIMEOUT,
    GMAIL_USERNAME,
    GMAIL_VERIFICATION_SENDER,
    HEADLESS,
    LINKEDIN_EMAIL,
    LINKEDIN_PASSWORD,
)

try:
    import pandas as pd
except ImportError:
    pd = None

app = Flask(__name__, template_folder="templates")


def create_app() -> Flask:
    """Factory used by Gunicorn to obtain the Flask application."""
    return app

ProgressCallback = Optional[Callable[[str], None]]

jobs_store: Dict[str, Dict[str, object]] = {}
jobs_lock = Lock()


def ensure_credentials() -> Tuple[str, str]:
    email = LINKEDIN_EMAIL.strip()
    password = LINKEDIN_PASSWORD.strip()
    if not email or not password or email == "change-me@example.com" or password == "change-me-password":
        raise RuntimeError(
            "Update LINKEDIN_EMAIL and LINKEDIN_PASSWORD in config.py or provide them via environment variables."
        )
    return email, password


def emit_progress(callback: ProgressCallback, message: str) -> None:
    print(message)
    if callback:
        callback(message)


_SESSION_HEALTH_SELECTORS: Tuple[str, ...] = (
    "div.feed-identity-module",
    "header.global-nav",
    "div.global-nav__me",
    "button[aria-label='Start a post']",
    "a[href*='/mynetwork/']",
)


_VERIFICATION_URL_TOKENS: Tuple[str, ...] = (
    "checkpoint/challenge",
    "checkpoint/verify",
    "/mfa/",
    "verification",
    "pin",
)

_VERIFICATION_INPUT_SELECTORS: Tuple[str, ...] = (
    "input#input__email_verification_pin",
    "input[name='pin']",
    "input[autocomplete='one-time-code']",
    "input[data-id='pin-input']",
    "input[data-test-id='pin-input']",
    "input[maxlength='6']",
    "input[type='text']",
)

_VERIFICATION_SPLIT_INPUT_PATTERNS: Tuple[str, ...] = (
    "input[name^='input__email_verification_pin_']",
    "input[name^='code-']",
    "input[data-test-pin-index]",
)

_VERIFICATION_SUBMIT_SELECTORS: Tuple[str, ...] = (
    "button[type='submit']",
    "button[data-test-id='verify-button']",
    "button[aria-label*='verify']",
    "button[aria-label*='continue']",
    "button:has-text('Submit')",
    "button:has-text('Continue')",
    "button:has-text('Verify')",
)

_EMAIL_CODE_PATTERN = re.compile(r"\b(\d{6})\b")


def _is_logged_in(page: Page) -> bool:
    """Return True when the current LinkedIn page looks authenticated."""
    try:
        current_url = page.url.lower()
    except Exception:
        return False

    if any(token in current_url for token in ("login", "checkpoint", "uas/")):
        return False

    for selector in _SESSION_HEALTH_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return True
        except Exception:
            continue

    return False


def _is_verification_challenge(page: Page) -> bool:
    """Detect whether the current page represents LinkedIn's verification gate."""
    try:
        current_url = page.url.lower()
    except Exception:
        current_url = ""

    if any(token in current_url for token in _VERIFICATION_URL_TOKENS):
        return True

    candidate_selectors = _VERIFICATION_INPUT_SELECTORS + _VERIFICATION_SPLIT_INPUT_PATTERNS
    for selector in candidate_selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_enabled():
                return True
        except Exception:
            continue
    return False


def _normalize_html_to_text(raw: str) -> str:
    """Collapse basic HTML content into plain text for code extraction."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip()


def _decode_message_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        # Non-bytes payload; fall back to direct string conversion
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw
        return ""

    charset = part.get_content_charset() or "utf-8"
    try:
        decoded = payload.decode(charset, errors="ignore")
    except Exception:
        decoded = payload.decode("utf-8", errors="ignore")
    return decoded


def _extract_code_from_message(msg: Message) -> Optional[str]:
    """Search LinkedIn emails for the verification code."""
    parts: Iterable[Message]
    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = (msg,)

    for part in parts:
        if part.get_content_maintype() not in {"text", "multipart"}:
            continue
        if part.get_content_maintype() == "multipart":
            continue

        text = _decode_message_part(part)
        if part.get_content_subtype() == "html":
            text = _normalize_html_to_text(text)
        match = _EMAIL_CODE_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def _fetch_latest_code_once(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    sender: Optional[str],
) -> Optional[str]:
    with imaplib.IMAP4_SSL(host, port) as client:
        client.login(username, password)
        status, _ = client.select(folder)
        if status != "OK":
            raise RuntimeError(f"Unable to open IMAP folder '{folder}'.")

        search_terms = ["UNSEEN"]
        if sender:
            search_terms.extend(["FROM", f'"{sender}"'])
        status, data = client.search(None, *search_terms)
        message_ids: List[bytes] = []
        if status == "OK":
            message_ids = [msg_id for msg_id in data[0].split() if msg_id]

        if not message_ids:
            fallback_terms = []
            if sender:
                fallback_terms = ["FROM", f'"{sender}"']
            status, data = client.search(None, *(fallback_terms or ["ALL"]))
            if status == "OK" and data:
                fallback_ids = [msg_id for msg_id in data[0].split() if msg_id]
                # Take the most recent handful of messages
                message_ids = fallback_ids[-10:]

        for msg_id in reversed(message_ids):
            status, payload = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload:
                continue
            for _part in payload:
                if not isinstance(_part, tuple) or len(_part) < 2:
                    continue
                raw = _part[1]
                msg = email.message_from_bytes(raw)
                code = _extract_code_from_message(msg)
                if code:
                    client.store(msg_id, "+FLAGS", "(\\Seen)")
                    return code
    return None


def _fetch_linkedin_verification_code(
    progress_callback: ProgressCallback,
) -> str:
    username = (GMAIL_USERNAME or "").strip()
    password = (GMAIL_APP_PASSWORD or "").strip()
    if not username or not password:
        raise RuntimeError(
            "Cannot solve LinkedIn verification automatically: configure GMAIL_USERNAME and GMAIL_APP_PASSWORD."
        )

    host = (GMAIL_IMAP_HOST or "imap.gmail.com").strip()
    port = int(GMAIL_IMAP_PORT or 993)
    folder = (GMAIL_IMAP_FOLDER or "INBOX").strip()
    sender = (GMAIL_VERIFICATION_SENDER or "").strip() or None
    poll_interval = max(2.0, float(GMAIL_POLL_INTERVAL or 8.0))
    timeout = max(poll_interval + 5.0, float(GMAIL_POLL_TIMEOUT or 180.0))

    emit_progress(progress_callback, "Waiting for LinkedIn verification email...")
    deadline = time.time() + timeout
    last_error: Optional[Exception] = None
    while time.time() < deadline:
        try:
            code = _fetch_latest_code_once(host, port, username, password, folder, sender)
        except Exception as exc:  # pylint: disable=broad-except
            last_error = exc
            emit_progress(progress_callback, f"IMAP polling issue: {exc}")
            code = None
        if code:
            emit_progress(progress_callback, "Received LinkedIn verification code from Gmail.")
            return code
        time.sleep(poll_interval)

    if last_error:
        raise RuntimeError(
            f"LinkedIn verification email not found within {int(timeout)} seconds (last error: {last_error})."
        ) from last_error
    raise RuntimeError(f"LinkedIn verification email not found within {int(timeout)} seconds.")


def _fill_verification_code(page: Page, code: str) -> bool:
    """Type the verification code into whichever input layout LinkedIn uses."""
    for selector in _VERIFICATION_INPUT_SELECTORS:
        try:
            locator = page.locator(selector)
        except Exception:
            continue
        if locator.count() == 0:
            continue
        try:
            locator.first.fill("")
            locator.first.fill(code)
            return True
        except Exception:
            continue

    for selector in _VERIFICATION_SPLIT_INPUT_PATTERNS:
        try:
            locator = page.locator(selector)
        except Exception:
            continue
        count = locator.count()
        if count < len(code):
            continue
        success = True
        for idx, digit in enumerate(code):
            try:
                locator.nth(idx).fill("")
                locator.nth(idx).fill(digit)
            except Exception:
                success = False
                break
        if success:
            return True
    return False


def _submit_verification(page: Page) -> bool:
    for selector in _VERIFICATION_SUBMIT_SELECTORS:
        try:
            locator = page.locator(selector)
        except Exception:
            continue
        if locator.count() == 0:
            continue
        try:
            locator.first.click()
            return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _solve_verification_challenge(page: Page, progress_callback: ProgressCallback) -> None:
    code = _fetch_linkedin_verification_code(progress_callback)
    if not _fill_verification_code(page, code):
        raise RuntimeError("Unable to locate verification input on LinkedIn checkpoint page.")
    _submit_verification(page)
    emit_progress(progress_callback, "Submitted verification code to LinkedIn.")


def _await_authenticated_session(page: Page, progress_callback: ProgressCallback) -> None:
    """Wait until LinkedIn marks the session as authenticated, solving verification if needed."""
    # Give additional breathing room beyond the IMAP timeout.
    safety_window = max(float(GMAIL_POLL_TIMEOUT or 180.0) + 60.0, 120.0)
    deadline = time.time() + safety_window
    while time.time() < deadline:
        if _is_logged_in(page):
            return
        if _is_verification_challenge(page):
            emit_progress(progress_callback, "LinkedIn requested verification; attempting automatic solve.")
            _solve_verification_challenge(page, progress_callback)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except TimeoutError:
                pass
            continue
        try:
            page.wait_for_url("**/feed/**", timeout=10000)
        except TimeoutError:
            pass
        if _is_logged_in(page):
            return
        page.wait_for_timeout(1500)

    raise RuntimeError("LinkedIn login did not finish before the verification timeout.")


def create_authenticated_context(
    browser,
    auth_path: Path,
    email: str,
    password: str,
    progress_callback: ProgressCallback = None,
):
    auth_path = Path(auth_path)
    if auth_path.exists():
        emit_progress(progress_callback, "Using saved authentication...")
        context = browser.new_context(storage_state=str(auth_path))
        page = context.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            pass
        try:
            _await_authenticated_session(page, progress_callback)
        except RuntimeError:
            emit_progress(progress_callback, "Stored authentication looks invalid. Re-authenticating...")
            page.close()
            context.close()
        else:
            emit_progress(progress_callback, "LinkedIn session restored.")
            return context, page

    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login", timeout=60000)
    page.fill("#username", email)
    page.fill("#password", password)
    page.click('button[type="submit"]')

    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except TimeoutError:
        pass

    try:
        _await_authenticated_session(page, progress_callback)
    except RuntimeError as exc:
        page.close()
        context.close()
        raise RuntimeError(
            "LinkedIn login did not complete successfully, even after attempting verification."
        ) from exc

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(auth_path))
    emit_progress(progress_callback, "LinkedIn login successful.")
    return context, page


def scrape_jobs(
    page,
    keyword: str,
    location: str,
    progress_callback: ProgressCallback = None,
) -> List[Dict[str, str]]:
    search_url = (
        f"https://www.linkedin.com/jobs/search/?keywords={quote(keyword)}&location={quote(location)}"
    )
    emit_progress(progress_callback, f"Navigating to {search_url}")
    try:
        page.goto(search_url, timeout=90000, wait_until="domcontentloaded")
    except TimeoutError as exc:
        emit_progress(progress_callback, f"Navigation warning: {exc}. Continuing with current page state.")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except TimeoutError:
        pass

    # Close common popups that can block interaction
    for dismiss_selector in (
        "button[aria-label='Dismiss']",
        "button[aria-label='Close']",
        "button.artdeco-button--primary[aria-label='Accept cookies']",
        "button[aria-label='Accept all']",
        "button[aria-label='Allow essential and optional cookies']",
        "button[aria-label='Dismiss this message']",
    ):
        try:
            dismiss_btn = page.locator(dismiss_selector)
            if dismiss_btn.count() > 0 and dismiss_btn.first.is_visible():
                dismiss_btn.first.click()
                page.wait_for_timeout(300)
        except Exception:
            continue

    job_item_selector = ",".join(
        [
            "li.jobs-search-results__list-item",
            "li[data-occludable-job-id]",
            "ul.jobs-search__results-list li",
            "ul.jobs-search-results__list li",
            "ul.scaffold-layout__list-container li",
            "div.jobs-search-two-pane__results-list li",
        ]
    )

    try:
        page.wait_for_selector(job_item_selector, timeout=30000)
    except TimeoutError:
        emit_progress(progress_callback, "Job list not found.")
        return []

    # Scroll to load more job cards
    for _ in range(10):
        page.mouse.wheel(0, 2000)
        time.sleep(2)

    jobs_data: List[Dict[str, str]] = []
    job_items = page.locator(job_item_selector)
    count = job_items.count()
    emit_progress(progress_callback, f"Found {count} job listings.")

    def extract_first_text(container, selectors) -> str:
        for selector in selectors:
            target = container.locator(selector)
            if target.count() == 0:
                continue
            try:
                text = target.first.inner_text().strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    detail_selectors = [
        "div.jobs-search__job-details--wrapper",
        "div.jobs-search-two-pane__job-details",
        "section.two-pane-job-details",
        "div.jobs-details__main-content",
        "div.jobs-unified-description__container",
    ]

    for i in range(count):
        try:
            job = job_items.nth(i)
            job.scroll_into_view_if_needed()

            link = job.locator("a[href*='/jobs/view/']:not(.disabled)")
            if link.count() == 0:
                link = job.locator("a.job-card-container__link:not(.disabled)")
            if link.count() == 0:
                link = job.locator("a").first

            try:
                link.wait_for(state="visible", timeout=4000)
                link.click(timeout=4000)
            except Exception:
                page.mouse.wheel(0, 800)
                try:
                    link.click(force=True, timeout=800)
                except Exception as e:
                    emit_progress(progress_callback, f"Click retry failed for job {i + 1}: {e}")
                    continue

            detail_div = None
            for selector in detail_selectors:
                locator = page.locator(selector)
                try:
                    locator.first.wait_for(state="visible", timeout=8000)
                    detail_div = locator.first
                    break
                except TimeoutError:
                    continue

            if detail_div is None:
                emit_progress(progress_callback, f"Job {i + 1}: job detail pane did not appear.")
                continue

            def first_attr_from(selectors: List[str], attrs: List[str]) -> str:
                for selector in selectors:
                    for source in (detail_div, page):
                        target = source.locator(selector)
                        if target.count() == 0:
                            continue
                        for attr in attrs:
                            try:
                                value = target.first.get_attribute(attr)
                            except Exception:
                                value = None
                            if value:
                                normalized = normalize_link(value)
                                if normalized:
                                    return normalized
                                return value.strip()
                        try:
                            dataset_value = target.first.evaluate(
                                """(el) => {
                                    if (!el || !el.dataset) return "";
                                    const data = el.dataset;
                                    return (
                                        data.url ||
                                        data.applyUrl ||
                                        data.applyLink ||
                                        data.jobUrl ||
                                        data.jobApplyUrl ||
                                        data.href ||
                                        data.link ||
                                        data.companyUrl ||
                                        data.companyLink ||
                                        data.orgUrl ||
                                        data.orgLink ||
                                        data.topcardUrl ||
                                        data.buttonUrl ||
                                        data.jobId ||
                                        ""
                                    );
                                }"""
                            )
                        except Exception:
                            dataset_value = None
                        if dataset_value:
                            normalized = normalize_link(dataset_value)
                            if normalized:
                                return normalized
                            dataset_value = str(dataset_value).strip()
                            if dataset_value:
                                return dataset_value
                return ""

            job_url = ""
            job_id_attr = ""
            try:
                job_id_attr = job.get_attribute("data-occludable-job-id") or ""
            except Exception:
                job_id_attr = ""

            try:
                link_href = link.get_attribute("href")
            except Exception:
                link_href = None
            if link_href:
                job_url = normalize_link(link_href)
            if not job_url and job_id_attr:
                job_url = normalize_link(f"/jobs/view/{job_id_attr}")
            if not job_url:
                try:
                    current_url = page.url
                except Exception:
                    current_url = ""
                if current_url:
                    try:
                        parsed = urlparse(current_url)
                        query_params = parse_qs(parsed.query)
                        current_job_ids = query_params.get("currentJobId")
                        if current_job_ids:
                            candidate = current_job_ids[0]
                            if candidate:
                                job_url = normalize_link(f"/jobs/view/{candidate}")
                    except Exception:
                        pass

            title = extract_first_text(
                detail_div,
                [
                    "h1",
                    "h1 span",
                    "span.jobs-unified-top-card__job-title",
                    "span.top-card-layout__title",
                ],
            )
            company = extract_first_text(
                detail_div,
                [
                    "a.jobs-unified-top-card__company-name",
                    "span.jobs-unified-top-card__company-name",
                    "div.job-details-jobs-unified-top-card__company-name",
                    "a.topcard__org-name-link",
                    "span.top-card-layout__second-subline-item",
                ],
            )
            desc = extract_first_text(
                detail_div,
                [
                    "#job-details",
                    "div.jobs-unified-description__container",
                    "div.jobs-description__content",
                    "div.jobs-description-content__text",
                    "div.show-more-less-html__markup",
                ],
            )

            company_link = first_attr_from(
                [
                    "div.job-details-jobs-unified-top-card__company-name a[href]",
                    "a[data-test-app-aware-link][href*='/company/']",
                    "a.jobs-unified-top-card__company-name[href]",
                    "a.topcard__org-name-link[href]",
                    "a.top-card-layout__second-subline-item[href]",
                    "a[href*='linkedin.com/company/']",
                ],
                ["href", "data-url", "data-company-url", "data-link"],
            )
            if not company_link:
                try:
                    company_locator = job.locator("a[href*='/company/']")
                    if company_locator.count() > 0:
                        href = company_locator.first.get_attribute("href")
                        if href:
                            company_link = normalize_link(href)
                except Exception:
                    pass

            apply_link = first_attr_from(
                [
                    "a[data-control-name='jobdetails_topcard_inapply']",
                    "a[data-control-name='jobdetails_topcard_inapply_enhanced']",
                    "a[data-tracking-control-name='public_jobs_apply-link-offsite']",
                    "a[data-tracking-control-name*='apply']",
                    "a.jobs-apply-button--top-card",
                    "a.jobs-apply-button",
                    "a.top-card-layout__cta",
                    "div.jobs-apply-button--top-card a",
                    "button.jobs-apply-button",
                    "button[data-job-id]",
                ],
                [
                    "href",
                    "data-url",
                    "data-job-url",
                    "data-apply-url",
                    "data-redirect-url",
                    "data-external-url",
                    "data-job-id",
                ],
            )

            if apply_link and apply_link.isdigit():
                apply_link = normalize_link(f"/jobs/view/{apply_link}")

            if not apply_link and job_id_attr:
                apply_link = normalize_link(f"/jobs/view/{job_id_attr}")

            if not apply_link and job_url:
                apply_link = job_url

            jobs_data.append(
                {
                    "title": title,
                    "company": company,
                    "company_link": company_link or "N/A",
                    "apply_link": apply_link or "N/A",
                    "description": desc,
                }
            )
            emit_progress(progress_callback, f"[{i + 1}/{count}] Collected: {title} at {company}")

        except Exception as exc:
            emit_progress(progress_callback, f"Error processing job {i + 1}: {exc}")
            continue

    return jobs_data


def jobs_to_excel(data: List[Dict[str, str]]) -> io.BytesIO:
    if not data:
        raise RuntimeError("No job postings were scraped for the provided criteria.")
    if pd is None:
        raise RuntimeError("pandas is required to build the Excel file. Install pandas and retry.")

    df = pd.DataFrame(data)
    stream = io.BytesIO()
    try:
        df.to_excel(stream, index=False)
    except ValueError as exc:
        raise RuntimeError(
            "Writing Excel output requires the 'openpyxl' dependency. Install it and retry."
        ) from exc
    stream.seek(0)
    return stream


def slugify(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value.lower())
    parts = [segment for segment in cleaned.split("_") if segment]
    return "_".join(parts) or "results"


def normalize_link(value: Optional[str]) -> str:
    if not value:
        return ""
    link = value.strip()
    if not link:
        return ""
    if link.startswith("//"):
        link = f"https:{link}"
    elif link.startswith("/"):
        link = f"https://www.linkedin.com{link}"
    elif link.startswith("www."):
        link = f"https://{link}"
    elif link.startswith("linkedin.com"):
        link = f"https://{link}"
    return link


def build_download_name(keyword: str, location: str) -> str:
    timestamp = time.strftime("%Y%m%d%H%M%S")
    return f"linkedin_jobs_{slugify(keyword)}_{slugify(location)}_{timestamp}.xlsx"


def run_scraper(keyword: str, location: str, progress_callback: ProgressCallback = None):
    email, password = ensure_credentials()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context, page = create_authenticated_context(
            browser, AUTH_FILE_PATH, email, password, progress_callback
        )
        try:
            data = scrape_jobs(page, keyword, location, progress_callback)
        finally:
            context.close()
            browser.close()

    excel_stream = jobs_to_excel(data)
    return excel_stream, build_download_name(keyword, location)


@app.route("/", methods=["GET", "POST"])
def index():
    keyword = request.args.get("keyword", DEFAULT_KEYWORD)
    location = request.args.get("location", DEFAULT_LOCATION)
    error = None

    if request.method == "POST":
        keyword = request.form.get("keyword", "").strip()
        location = request.form.get("location", "").strip()

        if not keyword or not location:
            error = "Both job title and location are required."
        else:
            try:
                excel_stream, filename = run_scraper(keyword, location)
                return send_file(
                    excel_stream,
                    as_attachment=True,
                    download_name=filename,
                    mimetype=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                )
            except RuntimeError as exc:
                error = str(exc)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"Unexpected error: {exc}")
                error = "An unexpected error occurred while scraping. Check the logs for details."

    return render_template("index.html", keyword=keyword, location=location, error=error)


def _scrape_job_worker(job_id: str, keyword: str, location: str) -> None:
    with jobs_lock:
        job_entry = jobs_store.get(job_id)
    if not job_entry:
        return

    queue: SimpleQueue = job_entry["queue"]  # type: ignore[assignment]

    def progress(message: str) -> None:
        queue.put({"type": "log", "message": message})

    try:
        progress("Starting LinkedIn login...")
        excel_stream, filename = run_scraper(keyword, location, progress_callback=progress)
        file_bytes = excel_stream.getvalue()
        with jobs_lock:
            job_entry["status"] = "completed"
            job_entry["result"] = file_bytes
            job_entry["filename"] = filename
        queue.put({"type": "done"})
    except Exception as exc:  # pylint: disable=broad-except
        with jobs_lock:
            job_entry["status"] = "error"
            job_entry["error"] = str(exc)
        queue.put({"type": "error", "message": str(exc)})
    finally:
        queue.put(None)
        if job_entry.get("status") == "error":
            with jobs_lock:
                jobs_store.pop(job_id, None)


@app.post("/scrape")
def start_scrape():
    payload = request.get_json(silent=True) or {}
    keyword = str(payload.get("keyword", "")).strip()
    location = str(payload.get("location", "")).strip()

    if not keyword or not location:
        return jsonify({"error": "Both job title and location are required."}), 400

    job_id = uuid.uuid4().hex
    queue: SimpleQueue = SimpleQueue()
    job_entry: Dict[str, object] = {
        "queue": queue,
        "status": "pending",
        "result": None,
        "filename": None,
        "error": None,
    }

    with jobs_lock:
        jobs_store[job_id] = job_entry

    Thread(target=_scrape_job_worker, args=(job_id, keyword, location), daemon=True).start()

    return jsonify({"job_id": job_id})


@app.get("/stream/<job_id>")
def stream_progress(job_id: str):
    with jobs_lock:
        job_entry = jobs_store.get(job_id)
    if not job_entry:
        return jsonify({"error": "Unknown job id."}), 404

    queue: SimpleQueue = job_entry["queue"]  # type: ignore[assignment]

    def event_stream():
        while True:
            message = queue.get()
            if message is None:
                break
            yield f"data: {json.dumps(message)}\n\n"
        yield "event: end\ndata: {}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(stream_with_context(event_stream()), headers=headers, mimetype="text/event-stream")


@app.get("/download/<job_id>")
def download_result(job_id: str):
    with jobs_lock:
        job_entry = jobs_store.get(job_id)
    if not job_entry:
        return jsonify({"error": "Unknown job id."}), 404

    if job_entry.get("status") != "completed":
        return jsonify({"error": "Job not completed."}), 409

    file_bytes: Optional[bytes] = job_entry.get("result")  # type: ignore[assignment]
    filename: Optional[str] = job_entry.get("filename")  # type: ignore[assignment]
    if file_bytes is None or filename is None:
        return jsonify({"error": "Result missing."}), 500

    buffer = io.BytesIO(file_bytes)
    buffer.seek(0)

    response = send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    with jobs_lock:
        jobs_store.pop(job_id, None)

    return response


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)
