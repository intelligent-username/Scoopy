import os
import re
import tempfile
import time
from collections.abc import Iterable
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
MAX_COLLECTION_SECONDS = 300.0
MAX_SCROLL_ROUNDS = 200

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
    print("🚀 Launching the managed browser for Scoopy (first run needs internet)...")
    options = webdriver.ChromeOptions()
    options.add_argument(f'--user-data-dir={CHROME_PROFILE_DIR}')
    # Instagram can keep background requests open indefinitely; the user can
    # log in as soon as the document is interactive.
    options.page_load_strategy = 'eager'
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
        driver.set_page_load_timeout(30)
        driver.get("https://www.instagram.com/")
    except Exception:
        driver.quit()
        raise
    print("🌐 Browser opened to Instagram. Please log in.")
    return driver

def _post_url_from_href(href: str | None) -> str | None:
    """
    Normalizes an element href into a canonical Instagram post/reel URL, or
    returns None for anything that is not a post or reel.

    Handles the variants seen across Instagram surfaces: relative or absolute
    URLs, the plural "/reels/" form (canonicalized to "/reel/", which both the
    downloader and yt-dlp accept), tracking query parameters, and trailing
    slashes. Non-post links (profiles, hashtags, login, other domains) are
    rejected.
    """
    if not isinstance(href, str) or not href:
        return None
    href = href.strip()
    if re.search(r'[\s\\\x00-\x1f\x7f]', href):
        return None
    try:
        parsed = urlsplit(href)
        if parsed.netloc:
            if parsed.scheme not in ('', 'https'):
                return None
            if (parsed.username is not None or parsed.password is not None
                    or ':' in parsed.netloc or parsed.port is not None):
                return None
            if parsed.hostname is None or re.fullmatch(
                    r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*instagram\.com',
                    parsed.hostname) is None:
                return None
        elif parsed.scheme or not href.startswith('/') or href.startswith('//'):
            return None
    except ValueError:
        return None
    match = re.fullmatch(
        r'/(?:(?P<username>[A-Za-z0-9_.]{1,30})/)?'
        r'(?P<kind>p|reel|reels)/(?P<code>[A-Za-z0-9_-]+)/?',
        parsed.path)
    if match is None:
        return None
    username = match['username']
    if username and (username in {
            'accounts', 'explore', 'direct', 'stories', 'saved', 'collections',
            'collection', 'reel', 'reels', 'p', 'about', 'legal', 'api', 'web'}
            or username.startswith('.') or username.endswith('.') or '..' in username):
        return None
    kind = 'p' if match['kind'] == 'p' else 'reel'
    return f"{INSTAGRAM_ORIGIN}/{kind}/{match['code']}"

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

def _wait_for_first_link(driver, *, collection_deadline: float | None = None):
    """
    Waits until at least one post/reel link is present (grids can take a while
    to hydrate after the page opens), or until FIRST_LINK_TIMEOUT elapses.
    """
    deadline = time.monotonic() + FIRST_LINK_TIMEOUT
    if collection_deadline is not None:
        deadline = min(deadline, collection_deadline)
    while time.monotonic() < deadline:
        if any(_post_url_from_href(href) for href in _candidate_hrefs(driver)):
            return True
        time.sleep(min(FIRST_LINK_POLL, max(0, deadline - time.monotonic())))
    return False

SCROLL_SCRIPT = """
const hrefs = new Set(arguments[0]);
const visible = el => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
        rect.bottom > 0 && rect.top < window.innerHeight;
};
const scrollable = el => visible(el) && el.clientHeight > 0 &&
    el.scrollHeight > el.clientHeight + 1 &&
    /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY);
let target = null;
for (const link of document.querySelectorAll('a[href], [role="link"][href]')) {
    if (!hrefs.has(link.href || link.getAttribute('href')) || !visible(link)) continue;
    for (let parent = link.parentElement; parent; parent = parent.parentElement) {
        if (scrollable(parent)) { target = parent; break; }
    }
    if (target) break;
}
if (!target) {
    // Empty grids may need a scroll before their first href is hydrated.
    const candidates = document.querySelectorAll(
        'main, main *, [role="main"], [role="main"] *, [role="dialog"], [role="dialog"] *');
    let area = 0;
    for (const element of candidates) {
        if (!scrollable(element)) continue;
        const rect = element.getBoundingClientRect();
        const candidateArea = rect.width * rect.height;
        if (candidateArea > area) { target = element; area = candidateArea; }
    }
}
target = target || document.scrollingElement || document.documentElement;
const before = target.scrollTop;
target.scrollTop = before + Math.max(1, target.clientHeight * 0.8);
return target.scrollTop > before;
"""


def collect_reel_links(driver: webdriver.Remote) -> list[str]:
    """
    Collects all unique post/reel links from the currently open page.
    Assumes the user is already logged in and on a collection/profile page.

    Args:
        driver: The Selenium WebDriver instance.

    Returns:
        A list of unique, sorted links.
    """
    print("🔎 Starting link collection. Waiting for the grid to load...")
    deadline = time.monotonic() + MAX_COLLECTION_SECONDS
    if not _wait_for_first_link(driver, collection_deadline=deadline):
        print("⚠️ No initial post links. Checking the grid while scrolling.")

    print("🔎 Checking post links and scrolling the grid.")
    seen = set()
    no_new_rounds = 0
    last_count = 0
    bottom_checks = 0

    for _ in range(MAX_SCROLL_ROUNDS):
        if time.monotonic() >= deadline:
            print("⚠️ Collection time limit reached; results may be incomplete.")
            break
        supported_hrefs = []
        for href in _candidate_hrefs(driver):
            post_url = _post_url_from_href(href)
            if post_url:
                seen.add(post_url)
                supported_hrefs.append(href)

        current_count = len(seen)
        if current_count == last_count:
            no_new_rounds += 1
            print(f"🔗 No new links found on this scroll. (Round {no_new_rounds}/{MAX_NO_NEW_ROUNDS})")
        else:
            no_new_rounds = 0
            last_count = current_count
            print(f"🔗 Collected {current_count} unique links so far...")

        if no_new_rounds >= MAX_NO_NEW_ROUNDS:
            break
        moved = driver.execute_script(SCROLL_SCRIPT, supported_hrefs)
        if moved is False and no_new_rounds:
            bottom_checks += 1
            if bottom_checks >= 2:
                print("✅ Reached the bottom; no additional post links appeared.")
                break
        else:
            bottom_checks = 0
        time.sleep(min(SCROLL_PAUSE, max(0, deadline - time.monotonic())))
    else:
        print("⚠️ Collection scroll limit reached; results may be incomplete.")

    if not seen:
        raise AppError(
            "No supported post links were found. Open the saved collection's "
            "post grid in the app browser, wait for its thumbnails to load, "
            "and try Scrape Links again. Tiles without post/reel hrefs are not "
            "supported. Your existing saved links were kept.")
    print(f"\n✅ Finished scrolling. Total unique links: {len(seen)}")
    return sorted(seen)

def save_links(links: Iterable[str]) -> None:
    """Atomically replace saved links only with a nonempty, validated collection."""
    normalized = [_post_url_from_href(link) for link in links]
    if not normalized or any(link is None for link in normalized):
        raise AppError("No valid collection to save. Your existing saved links were kept.")
    unique_links = sorted(set(normalized))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n",
                dir=os.path.dirname(os.path.abspath(LINKS_FILE)),
                prefix=".scoopy-links-", suffix=".tmp", delete=False) as output:
            temporary_path = output.name
            for link in unique_links:
                output.write(link + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, LINKS_FILE)
    except OSError as error:
        raise AppError(
            "Could not save collected links. Check file permissions and free disk "
            "space, then retry. Your existing saved links were kept.") from error
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                print("⚠️ Could not remove the temporary links file.")
    print(f"💾 Saved {len(unique_links)} links to {LINKS_FILE}")
