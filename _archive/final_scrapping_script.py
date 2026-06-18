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
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from playwright.sync_api import Page, TimeoutError, sync_playwright

# Configuration imports - these will be loaded from environment variables
try:
    import pandas as pd
except ImportError:
    pd = None

app = Flask(__name__, template_folder="templates")

# Global variables for job tracking
jobs_store: Dict[str, dict] = {}
jobs_lock = Lock()

def create_app() -> Flask:
    """Factory used by Gunicorn to obtain the Flask application."""
    return app

ProgressCallback = Optional[Callable[[str], None]]

def ensure_credentials():
    """Ensure we have LinkedIn credentials available."""
    # This would normally read from environment variables
    # For testing purposes, we'll use defaults that can be overridden
    email = "change-me@example.com"
    password = "change-me-password"

    # In a real implementation, these should be read from environment variables
    # or config file
    return email, password

def emit_progress(progress_callback: ProgressCallback, message: str) -> None:
    """Emit progress update if callback is provided."""
    if progress_callback:
        progress_callback(message)


def _await_authenticated_session(page: Page, progress_callback: ProgressCallback = None) -> None:
    """Wait for authentication to complete."""
    # Wait for the page to load with a reasonable timeout
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except TimeoutError:
        pass

    # Check if we're logged in by looking for elements that appear on the feed page
    try:
        # Try to find elements that only appear when logged in
        page.locator("div.feed-identity-module").wait_for(timeout=15000)
        return
    except TimeoutError:
        pass

    # Try alternative selectors for logged-in state
    try:
        page.locator("nav[aria-label='Navigation']").wait_for(timeout=15000)
        return
    except TimeoutError:
        pass

    # If we're still not sure, check the URL
    try:
        current_url = page.url
        if "linkedin.com/feed" in current_url or "linkedin.com/home" in current_url:
            return
    except Exception:
        pass

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
        try:
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
        except Exception as e:
            emit_progress(progress_callback, f"Error with stored auth: {e}. Re-authenticating...")

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


def scrape_jobs(page: Page, keyword: str, location: str, progress_callback: ProgressCallback = None) -> List[Dict[str, str]]:
    """Scrape job listings from LinkedIn."""
    jobs_data = []

    # Navigate to the jobs search page
    emit_progress(progress_callback, "Navigating to jobs page...")
    try:
        search_url = f"https://www.linkedin.com/jobs/search/?keywords={quote(keyword)}&location={quote(location)}"
        page.goto(search_url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception as e:
        emit_progress(progress_callback, f"Failed to navigate to jobs page: {e}")
        return jobs_data

    # Get the number of job results
    try:
        result_count_element = page.locator("h1.jobs-search-results-list__title")
        if result_count_element.is_visible():
            count_text = result_count_element.inner_text()
            # Extract number from text like "2,456 jobs"
            import re
            match = re.search(r'(\d+(?:,\d+)*)', count_text)
            if match:
                count = int(match.group(1).replace(',', ''))
                emit_progress(progress_callback, f"Found {count} job listings")
        else:
            emit_progress(progress_callback, "Could not determine number of jobs")
    except Exception:
        pass

    # Try to find job cards
    try:
        # Wait for job results to load
        page.wait_for_selector("li.jobs-search-results__list-item", timeout=30000)

        # Get all job listings
        job_listings = page.locator("li.jobs-search-results__list-item").all()
        emit_progress(progress_callback, f"Processing {len(job_listings)} job listings")

        for i, job in enumerate(job_listings[:50]):  # Process first 50 jobs
            try:
                emit_progress(progress_callback, f"Processing job {i+1}...")

                # Extract title
                title = ""
                try:
                    title_element = job.locator("a.job-card-list__title")
                    if title_element.is_visible():
                        title = title_element.inner_text().strip()
                except Exception:
                    pass

                # Extract company name
                company = ""
                try:
                    company_element = job.locator("a.job-card-list__subtitle")
                    if company_element.is_visible():
                        company = company_element.inner_text().strip()
                except Exception:
                    pass

                # Extract job URL (this is tricky since LinkedIn's structure changes)
                job_url = ""
                try:
                    url_element = job.locator("a.job-card-list__title")
                    if url_element.is_visible():
                        job_url = url_element.get_attribute("href") or ""
                        if job_url and not job_url.startswith("http"):
                            job_url = "https://www.linkedin.com" + job_url
                except Exception:
                    pass

                # Extract description (simplified)
                desc = ""
                try:
                    desc_element = job.locator(".job-card-list__desc")
                    if desc_element.is_visible():
                        desc = desc_element.inner_text().strip()
                except Exception:
                    pass

                # If we found at least a title or company, add to results
                if title or company:
                    jobs_data.append({
                        "title": title or "N/A",
                        "company": company or "N/A",
                        "company_link": "N/A",
                        "apply_link": job_url or "N/A",
                        "description": desc or "N/A"
                    })

            except Exception as e:
                emit_progress(progress_callback, f"Error processing job {i+1}: {e}")
                continue

    except Exception as e:
        emit_progress(progress_callback, f"Error scraping jobs: {e}")

    return jobs_data