import time
import re
from http.cookiejar import Cookie
from urllib.parse import urlsplit

class AppError(Exception):
    """An actionable message that is safe to show without session details."""


def is_instagram_domain(domain):
    domain = domain.lower().lstrip('.')
    return domain == 'instagram.com' or domain.endswith('.instagram.com')


def read_browser_cookies(driver):
    """Read only this controlled browser's live Instagram session into memory."""
    if driver is None:
        raise AppError("Open the browser and log in to Instagram first.")
    page = urlsplit(driver.current_url)
    if page.scheme != 'https' or not is_instagram_domain(page.hostname or ''):
        raise AppError("Navigate the app's browser to https://www.instagram.com/ and log in first.")

    cookies = []
    for item in driver.get_cookies():
        domain = item.get('domain', '')
        if not domain or not is_instagram_domain(domain):
            continue
        expires = item.get('expiry')
        if expires is not None and expires <= time.time():
            continue
        cookies.append(Cookie(
            version=0, name=item['name'], value=item['value'],
            port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'),
            domain_initial_dot=domain.startswith('.'),
            path=item.get('path', '/'), path_specified=True,
            secure=item.get('secure', False), expires=expires,
            discard=expires is None, comment=None, comment_url=None,
            rest={'HttpOnly': None} if item.get('httpOnly') else {},
            rfc2109=False,
        ))

    if not any(cookie.name == 'sessionid' and cookie.value for cookie in cookies):
        raise AppError(
            "No active Instagram login was found. Log in in the app's browser, "
            "complete any verification, then try again.")
    return cookies


def navigate_to_saved(driver) -> bool:
    """Open the account's saved page, or fall back to Instagram home.

    Account links come from the authenticated page's navigation, never from
    post authors, cookie values, or a guessed username.
    """
    home = 'https://www.instagram.com/'

    def saved_url(value: object, *, profile: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != 'https'
                    or parsed.hostname not in ('www.instagram.com', 'instagram.com')
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is not None):
                return None
        except ValueError:
            return None
        match = re.fullmatch(
            r'/([A-Za-z0-9_][A-Za-z0-9_.]{0,29})/'
            + (r'?' if profile else r'saved(?:/[^?#]*)?/?'),
            parsed.path)
        if match is None or match[1].lower() in {
                'accounts', 'explore', 'direct', 'reels', 'reel', 'p',
                'stories', 'about', 'legal', 'api'}:
            return None
        return f'{home}{match[1]}/saved/'

    try:
        # Do not pull the user out of an already selected saved collection.
        if saved_url(driver.current_url):
            return True
        links = driver.execute_script("""
            const links = [...document.querySelectorAll('a[href]')];
            return {
                saved: links.filter(a => /\\/saved\\//.test(a.pathname)).map(a => a.href),
                profile: links.filter(a =>
                    a.textContent.trim() === 'Profile' ||
                    a.querySelector('[aria-label="Profile"]') ||
                    (a.closest('nav, [role="navigation"]') && a.querySelector('img'))
                ).map(a => a.href)
            };
        """)
        target = None
        if isinstance(links, dict):
            for key in ('saved', 'profile'):
                candidates = links.get(key)
                if not isinstance(candidates, list):
                    continue
                target = next((url for item in candidates
                               if (url := saved_url(item, profile=key == 'profile'))), None)
                if target:
                    break
        if target is None:
            raise AppError("The account's saved-page link was not available.")
        driver.get(target)
        if saved_url(driver.current_url) != target:
            raise AppError("Instagram did not open the saved page.")
        print("Saved collections opened. Choose a collection, then click Scrape Links.")
        return True
    except Exception:
        # Browser errors can include session data; never echo them.
        print("Could not open saved collections automatically. Opening Instagram home.")
        try:
            driver.get(home)
        except Exception:
            print("Could not open Instagram home. Navigate to your saved collection manually.")
        return False

