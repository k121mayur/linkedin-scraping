import argparse
import os
import time
from pathlib import Path
from typing import List, Dict
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError, Error

try:
    import pandas as pd
except ImportError:
    pd = None

AUTH_FILE_PATH = "playwright_auth.json"
OUTPUT_EXCEL = "linkedin_jobs.xlsx"
DEFAULT_KEYWORD = "Software Engineer"
DEFAULT_LOCATION = "India"

LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD", "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple LinkedIn job scraper")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--auth-file", default=AUTH_FILE_PATH)
    parser.add_argument("--output", default=OUTPUT_EXCEL)
    return parser.parse_args()


def ensure_credentials() -> tuple:
    email = LINKEDIN_EMAIL or input("LinkedIn email: ").strip()
    password = LINKEDIN_PASSWORD or input("LinkedIn password: ").strip()
    if not email or not password:
        raise ValueError("LinkedIn credentials required.")
    return email, password


def create_authenticated_context(browser, auth_path: str, email: str, password: str):
    if os.path.exists(auth_path):
        print("Using saved authentication...")
        context = browser.new_context(storage_state=auth_path)
        page = context.new_page()
        page.goto("https://www.linkedin.com/feed/")
        try:
            page.wait_for_selector('div.feed-identity-module', timeout=15000)
            return context, page
        except TimeoutError:
            context.close()

    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login")
    page.fill('#username', email)
    page.fill('#password', password)
    page.click('button[type="submit"]')
    page.wait_for_url("https://www.linkedin.com/feed/", timeout=60000)
    context.storage_state(path=auth_path)
    return context, page


def scrape_jobs(page, keyword: str, location: str) -> List[Dict[str, str]]:
    search_url = f"https://www.linkedin.com/jobs/search/?keywords={quote(keyword)}&location={quote(location)}"
    print(f"Navigating to {search_url}")
    page.goto(search_url, timeout=90000)
    page.wait_for_timeout(5000)

    print("Scrolling through the job list...")
    job_list_container = page.locator('ul.aMxwlMUecxnEBAbDlRfcnVLOFRBGAKflastJYY')
    if not job_list_container:
        print("Job list not found.")
        return []

    # Scroll fully to load all job listings
    for _ in range(10):
        page.mouse.wheel(0, 2000)
        time.sleep(2)

    jobs_data = []

    job_items = job_list_container.locator('li')
    count = job_items.count()
    print(f"Found {count} job listings.")

    for i in range(count):
        try:
            # Re-locate each iteration to avoid stale handles if list reflows
            job = job_list_container.locator('li').nth(i)
            job.scroll_into_view_if_needed()

            # Prefer the specific, non-disabled job link
            link = job.locator('a.job-card-container__link:not(.disabled)')
            if link.count() == 0:
                link = job.locator('a').first

            # Faster fail: use a smaller click timeout; retry once with force
            try:
                link.wait_for(state='visible', timeout=3000)
                link.click(timeout=3000)
            except Exception:
                # Nudge viewport & try a force click quickly
                page.mouse.wheel(0, 800)
                try:
                    link.click(force=True, timeout=500)
                except Exception as e:
                    print(f"Click retry failed for job {i+1}: {e}")
                    continue

            # Wait for the right pane to appear and stabilize
            page.wait_for_selector('div.jobs-search__job-details--wrapper', timeout=10000)
            detail_div = page.locator('div.jobs-search__job-details--wrapper')

            title = detail_div.locator('h1').inner_text() if detail_div.locator('h1').count() > 0 else ''
            company = detail_div.locator('div.job-details-jobs-unified-top-card__company-name').inner_text() if detail_div.locator('div.job-details-jobs-unified-top-card__company-name').count() > 0 else ''
            desc = detail_div.locator('#job-details').inner_text() if detail_div.locator('#job-details').count() > 0 else ''

            jobs_data.append({
                'title': title,
                'company': company,
                'description': desc
            })

            print(f"[{i+1}/{count}] Collected: {title} at {company}")

        except Exception as e:
            print(f"Error processing job {i+1}: {e}")
            continue

    return jobs_data


def save_to_excel(data: List[Dict[str, str]], output: str):
    if not data:
        print("No data to save.")
        return
    if pd is not None:
        df = pd.DataFrame(data)
        df.to_excel(output, index=False)
    else:
        import csv
        with open(output.replace('.xlsx', '.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['title', 'company', 'description'])
            writer.writeheader()
            writer.writerows(data)
    print(f"Saved {len(data)} jobs to {output}")


def main():
    args = parse_args()
    email, password = ensure_credentials()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context, page = create_authenticated_context(browser, args.auth_file, email, password)
        try:
            data = scrape_jobs(page, args.keyword, args.location)
            save_to_excel(data, args.output)
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
