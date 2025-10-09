import io
import json
import time
import uuid
from pathlib import Path
from queue import SimpleQueue
from threading import Lock, Thread
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse
from flask import Flask

from flask import Response, jsonify, render_template, request, send_file, stream_with_context
from playwright.sync_api import TimeoutError, sync_playwright

from config import (
    AUTH_FILE_PATH,
    DEFAULT_KEYWORD,
    DEFAULT_LOCATION,
    FLASK_DEBUG,
    FLASK_PORT,
    HEADLESS,
    LINKEDIN_EMAIL,
    LINKEDIN_PASSWORD,
)

try:
    import pandas as pd
except ImportError:
    pd = None

app = Flask(__name__, template_folder="templates")

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
            page.wait_for_selector("div.feed-identity-module", timeout=15000)
            emit_progress(progress_callback, "LinkedIn session restored.")
            return context, page
        except TimeoutError:
            emit_progress(progress_callback, "Stored authentication looks invalid. Re-authenticating...")
            page.close()
            context.close()

    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login", timeout=60000)
    page.fill("#username", email)
    page.fill("#password", password)
    page.click('button[type="submit"]')

    try:
        page.wait_for_url("https://www.linkedin.com/feed/", timeout=60000)
    except TimeoutError as exc:
        page.close()
        context.close()
        raise RuntimeError(
            "LinkedIn login did not complete. Check credentials or solve any verification challenge."
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
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)
