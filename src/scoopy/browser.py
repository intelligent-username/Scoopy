import os
import time
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from .paths import CHROME_PROFILE_DIR, LINKS_FILE
from .session import AppError

# --- Scraping Logic ---
SCROLL_PAUSE = 1.5
MAX_NO_NEW_ROUNDS = 12 # How many scrolls with no new links before stopping
FIRST_LINK_TIMEOUT = 20.0 # Seconds to wait for the grid to hydrate before scrolling
FIRST_LINK_POLL = 0.5

# Instagram markup varies by surface and A/B rollout: reel hrefs appear as
# "/reel/" and "/reels/", and items are sometimes wrapped in role="link"
# containers rather than plain anchors. Cast a wide net here; every candidate
# href is strictly filtered to Instagram post/reel URLs by _post_url_from_href,
# so profile, hashtag and other links are never collected.
ANCHOR_SELECTOR = 'a[href]'
FALLBACK_SELECTOR = '[role="link"][href]'

INSTAGRAM_ORIGIN = 'https://www.instagram.com'

SE_FORCE_BROWSER_DOWNLOAD = 'SE_FORCE_BROWSER_DOWNLOAD'


def start_driver():
    """
    Launches a Selenium-managed Chrome for Testing with a dedicated app
    profile. Selenium Manager resolves the driver and downloads the browser
    if it is not already cached.
    """
    print("Launching the managed browser for Scoopy (first run needs internet)...")
    options = webdriver.ChromeOptions()
    options.add_argument(f'--user-data-dir={CHROME_PROFILE_DIR}')
    previous = os.environ.get(SE_FORCE_BROWSER_DOWNLOAD)
    os.environ[SE_FORCE_BROWSER_DOWNLOAD] = 'true'
    try:
        driver = webdriver.Chrome(options=options)
    except (WebDriverException, OSError) as error:
        raise AppError(
            "Could not start the managed browser. Check your internet "
            "connection and disk space, then try again.") from error
    finally:
        if previous is None:
            os.environ.pop(SE_FORCE_BROWSER_DOWNLOAD, None)
        else:
            os.environ[SE_FORCE_BROWSER_DOWNLOAD] = previous

    try:
        driver.maximize_window()
        driver.get("https://www.instagram.com/")
    except Exception:
        driver.quit()
        raise
    print("Browser opened to Instagram. Please log in.")
    return driver

def _post_url_from_href(href):
    """
    Normalizes an element href into a canonical Instagram post/reel URL, or
    returns None for anything that is not a post or reel.

    Handles the variants seen across Instagram surfaces: relative or absolute
    URLs, the plural "/reels/" form (canonicalized to "/reel/", which both the
    downloader and yt-dlp accept), tracking query parameters, and trailing
    slashes. Non-post links (profiles, hashtags, login, other domains) are
    rejected.
    """
    if not href:
        return None
    parsed = urlsplit(href.strip())
    if parsed.hostname is not None and not (
            parsed.hostname == 'instagram.com'
            or parsed.hostname.endswith('.instagram.com')):
        return None
    path = parsed.path
    if path.startswith('/reels/'):
        path = '/reel/' + path[len('/reels/'):]
    segments = [segment for segment in path.split('/') if segment]
    if len(segments) == 2 and segments[0] in ('p', 'reel'):
        return f"{INSTAGRAM_ORIGIN}/{segments[0]}/{segments[1]}"
    return None

def _candidate_hrefs(driver):
    """
    Gathers href values from every candidate element on the page: real anchors
    plus Instagram's role="link" wrappers. Stale elements are ignored.
    """
    hrefs = []
    for selector in (ANCHOR_SELECTOR, FALLBACK_SELECTOR):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                href = element.get_attribute("href")
            except Exception:
                continue # Ignore stale elements
            if href:
                hrefs.append(href)
    return hrefs

def _wait_for_first_link(driver):
    """
    Waits until at least one post/reel link is present (grids can take a while
    to hydrate after the page opens), or until FIRST_LINK_TIMEOUT elapses.
    """
    deadline = time.monotonic() + FIRST_LINK_TIMEOUT
    while time.monotonic() < deadline:
        if any(_post_url_from_href(href) for href in _candidate_hrefs(driver)):
            return True
        time.sleep(FIRST_LINK_POLL)
    return False

def collect_reel_links(driver):
    """
    Collects all unique post/reel links from the currently open page.
    Assumes the user is already logged in and on a collection/profile page.

    Args:
        driver: The Selenium WebDriver instance.

    Returns:
        A list of unique, sorted links.
    """
    print("Starting link collection. Waiting for the grid to load...")
    if not _wait_for_first_link(driver):
        print("No post links appeared on this page. Nothing to collect.")
        return []

    print("Grid loaded. Scrolling down.")
    seen = set()
    no_new_rounds = 0
    last_count = 0

    while no_new_rounds < MAX_NO_NEW_ROUNDS:
        for href in _candidate_hrefs(driver):
            post_url = _post_url_from_href(href)
            if post_url:
                seen.add(post_url)

        current_count = len(seen)
        if current_count == last_count:
            no_new_rounds += 1
            print(f"No new links found on this scroll. (Round {no_new_rounds}/{MAX_NO_NEW_ROUNDS})")
        else:
            no_new_rounds = 0
            last_count = current_count
            print(f"Collected {current_count} unique links so far...")

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(SCROLL_PAUSE)

    print(f"\n✅ Finished scrolling. Total unique links: {len(seen)}")
    return sorted(seen)

def save_links(links):
    """
    Saves the collected links to the global LINKS_FILE.
    """
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for link in links:
            f.write(link + "\n")
    print(f"Saved {len(links)} links to {LINKS_FILE}")
