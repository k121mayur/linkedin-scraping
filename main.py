import time
import os
from playwright.sync_api import sync_playwright

# --- Configuration ---
LINKEDIN_EMAIL = "shgplusplus@gmail.com"
LINKEDIN_PASSWORD = "TPO@123"
AUTH_FILE_PATH = "playwright_auth.json"

def main():
    with sync_playwright() as p:
        # Launch browser (run in headed mode to see what's happening)
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()

        # Check if we have a saved authentication state
        if os.path.exists(AUTH_FILE_PATH):
            print("Authentication file found, loading state...")
            context = browser.new_context(storage_state=AUTH_FILE_PATH)
            page = context.new_page()
            page.goto("https://www.linkedin.com/feed/")
            # Quick check to see if login was successful
            try:
                 page.wait_for_selector('//div[contains(@class, "feed-identity-module")]', timeout=15000)
                 print("Logged in successfully using stored state.")
            except:
                 print("Stored session might be invalid. Re-authenticating...")
                 # If check fails, we proceed to log in again
                 page.goto("https://www.linkedin.com/login")
                 login(page)
        else:
            print("No authentication file found. Logging in...")
            page = context.new_page()
            page.goto("https://www.linkedin.com/login")
            login(page)

        # Go to the network page to find people
        print("Navigating to 'My Network' page...")
        page.goto("https://www.linkedin.com/mynetwork/")

        # Wait for the main container of recommendations to load
        page.wait_for_selector('//ul[contains(@class, "discover-entity-list")]')
        print("Scraping connection names...")

        # Find all profile card elements
        profile_cards = page.locator('//li[contains(@class, "discover-entity-list__item")]').all()

        scraped_data = []
        for card in profile_cards[:10]: # Limit to first 10 for this example
            try:
                name = card.locator('//span[contains(@class, "discover-person-card__name")]').inner_text()
                title = card.locator('//span[contains(@class, "discover-person-card__occupation")]').inner_text()
                scraped_data.append({"name": name.strip(), "title": title.strip()})
            except Exception as e:
                print(f"Could not extract info from a card: {e}")

        # Print results
        for person in scraped_data:
            print(f"Name: {person['name']}, Title: {person['title']}")

        # Keep the browser open for a few seconds to see the result
        time.sleep(5)
        browser.close()

def login(page):
    """Handles the login process and saves the auth state."""
    print("Entering credentials...")
    page.fill('input#username', LINKEDIN_EMAIL)
    page.fill('input#password', LINKEDIN_PASSWORD)
    page.click('button[type="submit"]')

    # Wait for navigation to the feed page, which indicates a successful login.
    # Increase timeout if you have a slow connection.
    try:
        page.wait_for_url("https://www.linkedin.com/feed/", timeout=60000)
        print("Login successful!")
        # Save authentication state
        page.context.storage_state(path=AUTH_FILE_PATH)
        print(f"Authentication state saved to {AUTH_FILE_PATH}")
    except Exception as e:
        print(f"Login failed. You might need to solve a CAPTCHA manually. Error: {e}")

        time.sleep(120)


if __name__ == "__main__":
    main()