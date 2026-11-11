import time
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

