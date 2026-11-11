import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from .paths import CHROME_PROFILE_DIR, EDGE_PROFILE_DIR, FIREFOX_PROFILE_DIR, LINKS_FILE
from .session import AppError

# --- Scraping Logic ---
SCROLL_PAUSE = 1.5
MAX_NO_NEW_ROUNDS = 5 # How many scrolls with no new links before stopping

SE_FORCE_BROWSER_DOWNLOAD = 'SE_FORCE_BROWSER_DOWNLOAD'
_fallback_lock = threading.Lock()

BROWSER_ORDER = ('chrome', 'edge', 'firefox')
BROWSER_NAMES = {
    'chrome': 'Google Chrome',
    'edge': 'Microsoft Edge',
    'firefox': 'Mozilla Firefox',
}
BROWSER_PROFILES = {
    'chrome': CHROME_PROFILE_DIR,
    'edge': EDGE_PROFILE_DIR,
    'firefox': FIREFOX_PROFILE_DIR,
}

# Registry keys whose default value points at the browser executable (Windows).
_WINDOWS_REGISTRY_KEYS = {
    'chrome': (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
        r'SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
    ),
    'edge': (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
        r'SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe',
    ),
    'firefox': (
        r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe',
        r'SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe',
    ),
}

_WINDOWS_RELATIVE_PATHS = {
    'chrome': (
        r'Google\Chrome\Application\chrome.exe',
    ),
    'edge': (
        r'Microsoft\Edge\Application\msedge.exe',
    ),
    'firefox': (
        r'Mozilla Firefox\firefox.exe',
    ),
}

_WINDOWS_COMMAND_NAMES = {
    'chrome': ('chrome',),
    'edge': ('msedge',),
    'firefox': ('firefox',),
}

_MAC_BUNDLE_EXECUTABLES = {
    'chrome': ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
               os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')),
    'edge': ('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
             os.path.expanduser('~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')),
    'firefox': ('/Applications/Firefox.app/Contents/MacOS/firefox',
                os.path.expanduser('~/Applications/Firefox.app/Contents/MacOS/firefox')),
}

_LINUX_COMMAND_NAMES = {
    'chrome': ('google-chrome', 'google-chrome-stable', 'google-chrome-beta',
               'chromium', 'chromium-browser'),
    'edge': ('microsoft-edge', 'microsoft-edge-stable'),
    'firefox': ('firefox', 'firefox-esr'),
}


@dataclass(frozen=True)
class BrowserCandidate:
    """An installed browser binary with its own dedicated app profile."""
    kind: str      # 'chrome' | 'edge' | 'firefox'
    name: str      # display name
    executable: str
    profile_dir: str


def _registry_executables(kind):
    """Windows App Paths registry lookups. Returns paths without checking existence."""
    if platform.system() != 'Windows':
        return []
    import winreg
    paths = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in _WINDOWS_REGISTRY_KEYS[kind]:
            try:
                with winreg.OpenKey(root, key_path) as key:
                    value = winreg.QueryValueEx(key, '')[0]
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                paths.append(value.strip().strip('"').split('"')[0])
    return paths


def _windows_executables(kind):
    roots = [os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
             os.environ.get('LocalAppData')]
    paths = []
    for root in roots:
        if not root:
            continue
        for relative in _WINDOWS_RELATIVE_PATHS[kind]:
            paths.append(os.path.join(root, relative))
    paths.extend(_registry_executables(kind))
    for command in _WINDOWS_COMMAND_NAMES[kind]:
        found = shutil.which(command)
        if found:
            paths.append(found)
    return paths


def _mac_executables(kind):
    return list(_MAC_BUNDLE_EXECUTABLES[kind])


def _linux_executables(kind):
    paths = []
    for command in _LINUX_COMMAND_NAMES[kind]:
        found = shutil.which(command)
        if found:
            paths.append(found)
    return paths


def _discover_executables(system, kind):
    if system == 'Windows':
        return _windows_executables(kind)
    if system == 'Darwin':
        return _mac_executables(kind)
    if system == 'Linux':
        return _linux_executables(kind)
    return []


def discover_browsers():
    """Find installed Chrome, Edge, and Firefox binaries in preference order.

    Pure filesystem/registry inspection: never downloads a browser or
    launches one. Returns installed executables only, deduplicated.
    """
    system = platform.system()
    candidates = []
    seen = set()
    for kind in BROWSER_ORDER:
        for executable in _discover_executables(system, kind):
            if not executable:
                continue
            executable = os.path.normpath(os.path.abspath(executable))
            key = executable.lower()
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(executable):
                candidates.append(BrowserCandidate(
                    kind, BROWSER_NAMES[kind], executable, BROWSER_PROFILES[kind]))
    return candidates


def _build_options(candidate):
    if candidate.kind == 'firefox':
        options = webdriver.FirefoxOptions()
        options.binary_location = candidate.executable
        # The --profile argument makes geckodriver use this directory in
        # place, so the login persists. Selenium's options.profile instead
        # unpacks a temporary copy that is discarded on quit.
        options.add_argument('--profile')
        options.add_argument(candidate.profile_dir)
        return options
    if candidate.kind == 'edge':
        options = webdriver.EdgeOptions()
        options.binary_location = candidate.executable
        options.add_argument(f'--user-data-dir={candidate.profile_dir}')
        return options
    options = webdriver.ChromeOptions()
    options.binary_location = candidate.executable
    options.add_argument(f'--user-data-dir={candidate.profile_dir}')
    return options


def launch_browser(candidate):
    """Launch an installed browser with Selenium Manager resolving the driver."""
    options = _build_options(candidate)
    if candidate.kind == 'firefox':
        return webdriver.Firefox(options=options)
    if candidate.kind == 'edge':
        return webdriver.Edge(options=options)
    return webdriver.Chrome(options=options)


def launch_managed_chrome(profile_dir=CHROME_PROFILE_DIR):
    """Last resort: let Selenium Manager download Chrome for Testing.

    SE_FORCE_BROWSER_DOWNLOAD is set only for this call and restored
    afterwards; the user's environment is never permanently changed.
    """
    options = webdriver.ChromeOptions()
    options.add_argument(f'--user-data-dir={profile_dir}')
    with _fallback_lock:
        previous = os.environ.get(SE_FORCE_BROWSER_DOWNLOAD)
        os.environ[SE_FORCE_BROWSER_DOWNLOAD] = 'true'
        try:
            return webdriver.Chrome(options=options)
        finally:
            if previous is None:
                os.environ.pop(SE_FORCE_BROWSER_DOWNLOAD, None)
            else:
                os.environ[SE_FORCE_BROWSER_DOWNLOAD] = previous


def start_driver():
    """
    Starts and returns a WebDriver instance for the best available browser.
    Tries installed Chrome, Edge, and Firefox with a dedicated app profile
    for each, then a managed Chrome for Testing download as a last resort.
    """
    print("Looking for an installed browser...")
    driver = None
    for candidate in discover_browsers():
        print(f"Trying {candidate.name}...")
        try:
            driver = launch_browser(candidate)
        except (WebDriverException, OSError):
            # Exception strings may contain session data; log a category only.
            print(f"{candidate.name} could not be started. Trying the next browser.")
            driver = None
            continue
        break

    if driver is None:
        print("No installed browser worked. Downloading a managed browser for Scoopy (needs internet)...")
        try:
            driver = launch_managed_chrome()
        except (WebDriverException, OSError):
            raise AppError(
                "Could not start any browser. Install Google Chrome, Microsoft Edge, "
                "or Mozilla Firefox (or check your internet connection for the managed "
                "browser download), then try again.")

    try:
        driver.maximize_window()
        driver.get("https://www.instagram.com/")
    except Exception:
        driver.quit()
        raise
    print("Browser opened to Instagram. Please log in.")
    return driver

def collect_reel_links(driver):
    """
    Collects all unique post/reel links from the currently open page.
    Assumes the user is already logged in and on a collection/profile page.

    Args:
        driver: The Selenium WebDriver instance.

    Returns:
        A list of unique, sorted links.
    """
    print("Starting link collection... Scrolling down.")
    seen = set()
    no_new_rounds = 0
    last_count = 0

    while no_new_rounds < MAX_NO_NEW_ROUNDS:
        anchors = driver.find_elements(By.XPATH, '//a[starts-with(@href, "/p/") or starts-with(@href, "/reel/")]')

        if not anchors:
            print("No links found on this scroll. Retrying...")

        for a in anchors:
            try:
                href = a.get_attribute("href")
                if href:
                    clean_link = href.split("?")[0].rstrip("/")
                    if clean_link not in seen:
                        seen.add(clean_link)
            except Exception:
                pass # Ignore stale elements

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
    return sorted(list(seen))

def save_links(links):
    """
    Saves the collected links to the global LINKS_FILE.
    """
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        for link in links:
            f.write(link + "\n")
    print(f"Saved {len(links)} links to {LINKS_FILE}")
