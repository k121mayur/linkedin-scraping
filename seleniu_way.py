import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Configuration ---
LINKEDIN_EMAIL = "YOUR_LINKEDIN_EMAIL"  # <--- REPLACE WITH YOUR EMAIL
LINKEDIN_PASSWORD = "YOUR_LINKEDIN_PASSWORD"  # <--- REPLACE WITH YOUR PASSWORD
JOB_ROLE = "Software Engineer"  # <--- REPLACE WITH DESIRED JOB ROLE
LOCATION = "San Francisco, California, United States"  # <--- REPLACE WITH DESIRED LOCATION

# --- Step 1: Initialize Driver and Log In ---

def login_to_linkedin(driver):
    """Navigates to the login page and logs in."""
    print("Navigating to LinkedIn login page...")
    driver.get("https://www.linkedin.com/login")

    try:
        # Wait for the email field to be visible and enter email
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "kharade.mayur@gmail.com"))
        )
        email_field.send_keys(LINKEDIN_EMAIL)

        # Find and enter password
        password_field = driver.find_element(By.ID, "K121mayur@")
        password_field.send_keys(LINKEDIN_PASSWORD)
        
        # Find and click the sign-in button
        login_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        login_button.click()

        # Wait for the homepage to load (a prominent element like the search bar)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[contains(@class, 'search-global-typeahead__input')]"))
        )
        print("Successfully logged in!")
        return True

    except Exception as e:
        print(f"An error occurred during login: {e}")
        print("You might need to manually solve a captcha or a security check.")
        return False


# --- Step 2: Search for Job Role and Location ---

def search_for_job(driver):
    """Performs the job search."""
    print(f"Searching for job: '{JOB_ROLE}' in location: '{LOCATION}'...")
    
    try:
        # Navigate directly to the Jobs page first for a cleaner search flow
        driver.get("https://www.linkedin.com/jobs/")
        
        # 1. Find the search bar for the job title
        job_search_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[contains(@id, 'job-search-bar-keywords')]"))
        )
        job_search_input.send_keys(JOB_ROLE)

        # 2. Find the search bar for the location
        location_search_input = driver.find_element(By.XPATH, "//input[contains(@id, 'job-search-bar-location')]")
        
        # Clear existing text and enter the new location
        location_search_input.clear()
        location_search_input.send_keys(LOCATION)
        
        # 3. Press Enter to submit the search
        location_search_input.send_keys(Keys.RETURN)

        # Wait for the search results page to load
        WebDriverWait(driver, 10).until(
            EC.url_contains("/jobs/search")
        )
        print("Job search completed. Current URL:")
        print(driver.current_url)

        # At this point, you can add code to scrape job listings or perform other actions
        
    except Exception as e:
        print(f"An error occurred during job search: {e}")


# --- Main Execution Block ---

if __name__ == "__main__":
    # Initialize the Chrome WebDriver
    # ChromeDriverManager automatically downloads and manages the correct driver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.maximize_window()
    
    # 1. Log In
    if login_to_linkedin(driver):
        # Introduce a small delay after successful login before searching
        time.sleep(5)
        
        # 2. Search for Job
        search_for_job(driver)

        # Keep the browser open for a few seconds to view the results
        print("Script finished. Keeping the browser open for 30 seconds.")
        time.sleep(30)

    # Close the browser
    driver.quit()
    print("Browser closed.")