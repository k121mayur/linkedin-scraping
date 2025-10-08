import io
import time
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote

from flask import Flask, render_template, request, send_file
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


def ensure_credentials() -> Tuple[str, str]:
    email = LINKEDIN_EMAIL.strip()
    password = LINKEDIN_PASSWORD.strip()
    if not email or not password or email == "change-me@example.com" or password == "change-me-password":
        raise RuntimeError(
            "Update LINKEDIN_EMAIL and LINKEDIN_PASSWORD in config.py or provide them via environment variables."
        )
    return email, password


def create_authenticated_context(browser, auth_path: Path, email: str, password: str):
    auth_path = Path(auth_path)
    if auth_path.exists():
        print("Using saved authentication...")
        context = browser.new_context(storage_state=str(auth_path))
        page = context.new_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("div.feed-identity-module", timeout=15000)
            return context, page
        except TimeoutError:
            print("Stored authentication looks invalid. Re-authenticating...")
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
    return context, page


def scrape_jobs(page, keyword: str, location: str) -> List[Dict[str, str]]:
    search_url = (
        f"https://www.linkedin.com/jobs/search/?keywords={quote(keyword)}&location={quote(location)}"
    )
    print(f"Navigating to {search_url}")
    try:
        page.goto(search_url, timeout=90000, wait_until="domcontentloaded")
    except TimeoutError as exc:
        print(f"Navigation warning: {exc}. Continuing with current page state.")
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
        print("Job list not found.")
        return []

    # Scroll to load more job cards
    for _ in range(10):
        page.mouse.wheel(0, 2000)
        time.sleep(2)

    jobs_data: List[Dict[str, str]] = []
    job_items = page.locator(job_item_selector)
    count = job_items.count()
    print(f"Found {count} job listings.")

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
                    print(f"Click retry failed for job {i + 1}: {e}")
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
                print(f"Job {i + 1}: job detail pane did not appear.")
                continue

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

            jobs_data.append({"title": title, "company": company, "description": desc})
            print(f"[{i + 1}/{count}] Collected: {title} at {company}")

        except Exception as exc:
            print(f"Error processing job {i + 1}: {exc}")
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


def build_download_name(keyword: str, location: str) -> str:
    timestamp = time.strftime("%Y%m%d%H%M%S")
    return f"linkedin_jobs_{slugify(keyword)}_{slugify(location)}_{timestamp}.xlsx"


def run_scraper(keyword: str, location: str):
    email, password = ensure_credentials()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context, page = create_authenticated_context(browser, AUTH_FILE_PATH, email, password)
        try:
            data = scrape_jobs(page, keyword, location)
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
            except Exception as exc:
                print(f"Unexpected error: {exc}")
                error = "An unexpected error occurred while scraping. Check the logs for details."

    return render_template("index.html", keyword=keyword, location=location, error=error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)
