"""Scrape LinkedIn job URLs using Selenium."""

from __future__ import annotations

import logging
import os
import time

import pandas as pd
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from selenium.webdriver.chrome.webdriver import WebDriver as ChromeWebDriver


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------
_DEFAULT_MAX_PAGES = 10
_LOGIN_TIMEOUT_SECONDS = 30
_PAGE_LOAD_WAIT_SECONDS = 10

_RETRY_ATTEMPTS = 2

# Persistent Chrome profile used for passwordless scraping: a human signs in
# once via `python -m agents.linkedin_login`, and the saved session cookies
# do the rest. The tool never sees or stores the password.
DEFAULT_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cache", "linkedin_profile")

# URL fragments that mean "this browser is NOT logged in".
_LOGGED_OUT_MARKERS = ("/login", "/authwall", "/checkpoint", "/uas/",
                       "/session-redirect")


def profile_dir_from_env() -> str | None:
    """Directory holding the persistent Chrome profile, when profile mode is on."""
    d = os.environ.get("LINKEDIN_PROFILE_DIR")
    return os.path.abspath(d) if d else None


def is_logged_out_url(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _LOGGED_OUT_MARKERS)


def session_valid(driver: "ChromeWebDriver") -> bool:
    """True when the profile still holds a live LinkedIn session."""
    try:
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(3)
        return not is_logged_out_url(driver.current_url)
    except Exception:  # noqa: BLE001 — any navigation failure = unusable session
        return False


def _setup_driver(headless: bool | None = None,
                  profile_dir: str | None = None) -> ChromeWebDriver:
    """Create and configure a Chrome WebDriver.

    Parameters
    ----------
    headless : bool or None
        * True  — run in headless mode (browser window hidden).
        * False — show the browser window (interactive login supported).
        * None  — use ``LINKEDIN_HEADLESS`` env var (default: **False** for interactive login).
    profile_dir : str or None
        Persistent Chrome profile directory. When set, a session signed in
        once via ``python -m agents.linkedin_login`` is reused by every
        later scrape — no password involved.
    """
    if headless is None:
        # Default to non-headless so users can see and interact with LinkedIn's login page.
        headless = os.environ.get("LINKEDIN_HEADLESS", "false").lower() in ("1", "true", "yes")

    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_dir}")
    else:
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # Improve compatibility with LinkedIn's anti-bot detection.
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Container deployments bake chromium + chromedriver into the image and
    # point these env vars at them; otherwise Selenium Manager resolves (and
    # may download) the pair automatically on a desktop machine.
    chrome_binary = os.environ.get("CHROME_BINARY")
    if chrome_binary:
        options.binary_location = chrome_binary

    # Strip proxy env vars that can cause 502 tunnel errors.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(var, None)

    driver_path = os.environ.get("CHROMEDRIVER_PATH") or None
    service = Service(executable_path=driver_path) if driver_path else Service()
    return ChromeWebDriver(service=service, options=options)


def _login(driver: "WebDriver", email: str, password: str) -> None:
    """Log into LinkedIn using email and password.

    The browser window stays visible by default (LINKEDIN_HEADLESS=false) so
    a human can complete any unexpected challenge (2FA/captcha) — but the
    automated fill must run first so unattended runs work. A stray
    ``breakpoint()`` here used to hang every web-triggered run in pdb.
    """
    wait = WebDriverWait(driver, _LOGIN_TIMEOUT_SECONDS)

    # Navigate directly to the login page which shows both panels side-by-side.
    driver.get("https://www.linkedin.com/login")

    # The login page renders both panels (Sign in + Use passkey). We use the first email input.
    email_field = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
    password_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")

    email_field.send_keys(email)
    time.sleep(1)
    password_field.send_keys(password)

    sign_in_button = driver.find_element(By.XPATH, "//span[normalize-space()='Sign in']/ancestor::button")
    sign_in_button.click()
    time.sleep(5)

    # If LinkedIn shows a follow-up submit (e.g. consent/challenge), click it
    # when present; otherwise proceed.
    try:
        submit = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']"))
        )
        submit.click()
    except TimeoutException:
        logger.info("No follow-up submit button — login appears complete")

    # Wait until we're actually logged in (login page gone) so scraping does
    # not start on a half-authenticated session.
    try:
        WebDriverWait(driver, _LOGIN_TIMEOUT_SECONDS).until(
            lambda d: "/login" not in d.url and "/checkpoint" not in d.url
        )
    except TimeoutException:
        logger.warning(
            "Still on a login/checkpoint page after %ds — if LinkedIn asked "
            "for 2FA, complete it in the browser window.",
            _LOGIN_TIMEOUT_SECONDS,
        )


def interactive_login(driver: "ChromeWebDriver", timeout: int = 300) -> bool:
    """Open LinkedIn and let a human sign in in the visible browser window.

    The tool never handles the password: the user types it into LinkedIn
    themselves, and the persistent profile keeps the resulting session
    cookies. Returns True once the browser lands on a logged-in page.
    """
    driver.get("https://www.linkedin.com/login")
    print("\n  A browser window is open on LinkedIn's sign-in page.\n"
          "  Sign in there yourself (password, passkey, or 2FA) — this tool\n"
          f"  never sees your password. Waiting up to {timeout}s…\n",
          flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not is_logged_out_url(driver.current_url):
                time.sleep(2)  # let the session cookies settle
                return True
        except Exception:  # noqa: BLE001 — window closed mid-login
            return False
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _scroll_into_view(driver: "WebDriver", element) -> None:
    """Ensure *element* is scrolled into the visible viewport."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center', behavior:'instant'});", element)  # noqa: SLF001


def _scrape_page(driver: "WebDriver", page_number: int) -> list[str]:
    """Scrape job URLs from the current LinkedIn page."""
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    listings = driver.find_elements(
        By.XPATH, "//a[contains(@href, '/jobs/view/')]"
    )
    urls: list[str] = []
    for listing in listings:
        href = listing.get_attribute("href")
        if href and not href.startswith("https://www.linkedin.com/jobs/view/"):
            continue  # skip the "view all" link itself
        urls.append(href)

    return urls


def _go_to_next_page(driver: "WebDriver", page_number: int) -> bool:
    """Click the 'Page N+1' button. Returns True on success."""
    try:
        wait = WebDriverWait(driver, 5)
        next_btn = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, f"//button[@aria-label='Page {page_number + 1}']")
            )
        )
        _scroll_into_view(driver, next_btn)
        time.sleep(1)
        next_btn.click()
        time.sleep(_PAGE_LOAD_WAIT_SECONDS)
        return True
    except TimeoutException:
        logger.info("No more pages to scrape after page %d", page_number)
        return False


def scrape_linkedin_job_urls(
    target_jobs_urls: list[str],
    max_pages: int = _DEFAULT_MAX_PAGES,
    headless: bool | None = None,
) -> pd.DataFrame:
    """Scrape recommended job URLs from LinkedIn.

    Parameters
    ----------
    target_jobs_urls : list[str]
        List of LinkedIn search/collection URLs to scrape.
    max_pages : int
        Maximum number of pagination pages per URL.

    Auth resolution (first match wins):
      1. ``LINKEDIN_PROFILE_DIR`` set and its session still valid → reuse the
         signed-in profile. No password involved.
      2. ``LINKEDIN_EMAIL`` / ``LINKEDIN_PASSWORD`` env vars → automated
         fill (legacy path).
      3. Neither → open a visible browser and wait for the user to sign in
         themselves into the persistent profile.
    """
    profile_dir = profile_dir_from_env()
    have_creds = bool(os.environ.get("LINKEDIN_EMAIL")
                      and os.environ.get("LINKEDIN_PASSWORD"))

    if profile_dir:
        if headless is None:
            headless = os.environ.get(
                "LINKEDIN_HEADLESS", "false").lower() in ("1", "true", "yes")
        driver = _setup_driver(headless=headless, profile_dir=profile_dir)
        try:
            if not session_valid(driver):
                if headless and have_creds:
                    logger.warning(
                        "Saved LinkedIn profile session is not valid; "
                        "re-filling login from LINKEDIN_EMAIL/LINKEDIN_PASSWORD "
                        "(this also refreshes the saved profile).")
                    _login(driver, os.environ["LINKEDIN_EMAIL"],
                           os.environ["LINKEDIN_PASSWORD"])
                else:
                    if headless:
                        raise RuntimeError(
                            "LinkedIn session in the saved profile is not valid. "
                            "Re-authenticate once with `python -m agents.linkedin_login` "
                            "(or clear LINKEDIN_PROFILE_DIR to use password mode)."
                        )
                    if not interactive_login(driver):
                        raise RuntimeError(
                            "No LinkedIn sign-in completed — Run aborted. "
                            "Nothing was scraped."
                        )
            seen_urls = _scrape_urls(driver, target_jobs_urls, max_pages)
            cleaned = [u.split("/?")[0] for u in seen_urls]
            return pd.DataFrame({"job_url": cleaned})
        finally:
            driver.quit()
        # (unreachable) profile branch returns or raises above

    email = os.environ.get("LINKEDIN_EMAIL") or input("Enter your LinkedIn email: ")
    password = os.environ.get("LINKEDIN_PASSWORD") or input("Enter your LinkedIn password: ")

    driver = _setup_driver(headless=headless)

    try:
        # Login once — subsequent URLs stay logged in.
        _login(driver, email, password)
        seen_urls = _scrape_urls(driver, target_jobs_urls, max_pages)
    finally:
        driver.quit()

    # Normalise URLs (strip query params). NOTE: an earlier version rewrote
    # https->http here, which produced broken click-out links in the viewer.
    cleaned = [u.split("/?")[0] for u in seen_urls]
    return pd.DataFrame({"job_url": cleaned})


def _scrape_urls(driver: "ChromeWebDriver", target_jobs_urls: list[str],
                 max_pages: int) -> set[str]:
    """Collect job URLs across all target search URLs (driver must be logged in)."""
    seen_urls: set[str] = set()
    for url_idx, target_url in enumerate(target_jobs_urls):
        logger.info("Scraping URL %d/%d …", url_idx + 1, len(target_jobs_urls))
        driver.get(target_url)
        time.sleep(_PAGE_LOAD_WAIT_SECONDS)

        page_number = 1
        while page_number <= max_pages:
            print(f"Scraping page {page_number}…")

            # Retry up to _RETRY_ATTEMPTS times on transient failures.
            for attempt in range(1, _RETRY_ATTEMPTS + 1):
                try:
                    urls_on_page = _scrape_page(driver, page_number)
                    before = len(seen_urls)
                    seen_urls.update(urls_on_page)
                    new_count = len(seen_urls) - before
                    if new_count:
                        print(f"  → found {new_count} new job URLs")
                    break  # success — exit retry loop
                except Exception as exc:
                    logger.warning(
                        "Scrape attempt %d failed (page %d): %s",
                        attempt, page_number, exc,
                    )
                    if attempt == _RETRY_ATTEMPTS:
                        raise

            if not _go_to_next_page(driver, page_number):
                break
            page_number += 1
    return seen_urls