"""Tests for the fixed scrape selector and give-up logic (Track 2).

Covers collect_reel_links with SimpleNamespace-style mocked drivers, mirroring
the mocking approach in test_main.py. Owns nothing outside the scrape fix.
"""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import browser as browser_module
from scoopy import session


def element(href=None, error=None):
    """A mocked DOM element exposing href (or raising, for stale elements)."""
    if error is not None:
        getter = Mock(side_effect=error)
    else:
        getter = Mock(return_value=href)
    return SimpleNamespace(get_attribute=getter)


def driver_with(*selector_batches, scripts=None):
    """
    A mocked WebDriver whose find_elements returns the given batches in order
    (last batch repeats), keyed by CSS selector.

    Each batch is a {selector: [elements]} dict; unlisted selectors return [].
    """
    calls = []

    def find_elements(by, selector):
        calls.append(selector)
        index = min(len(calls) - 1, len(selector_batches) - 1)
        return selector_batches[index].get(selector, [])

    return SimpleNamespace(find_elements=find_elements,
                           execute_script=scripts if scripts is not None else Mock())


def fast_collect(driver):
    """Run collect_reel_links with sleeps and the give-up cap reduced for speed."""
    with patch.object(browser_module, 'SCROLL_PAUSE', 0), \
            patch.object(browser_module, 'FIRST_LINK_POLL', 0), \
            patch.object(browser_module, 'MAX_NO_NEW_ROUNDS', 2), \
            contextlib.redirect_stdout(io.StringIO()), \
            patch('builtins.print'):
        return browser_module.collect_reel_links(driver)


class PostUrlNormalizationTests(unittest.TestCase):
    def test_plural_reels_hrefs_are_canonicalized_to_reel(self):
        self.assertEqual(
            browser_module._post_url_from_href('/reels/abc123/?igsh=MTHz'),
            'https://www.instagram.com/reel/abc123')
        self.assertEqual(
            browser_module._post_url_from_href('https://www.instagram.com/reels/abc123/'),
            'https://www.instagram.com/reel/abc123')

    def test_post_and_reel_hrefs_are_normalized(self):
        for href, expected in (
            ('/p/xyz/', 'https://www.instagram.com/p/xyz'),
            ('/reel/xyz', 'https://www.instagram.com/reel/xyz'),
            ('https://www.instagram.com/p/xyz/?utm=1', 'https://www.instagram.com/p/xyz'),
        ):
            with self.subTest(href=href):
                self.assertEqual(browser_module._post_url_from_href(href), expected)

    def test_non_post_hrefs_are_rejected(self):
        for href in ('/username/', '/explore/tags/fun/', '/accounts/login/',
                     '/reel/', '/p/', '/reels/', None, '',
                     'https://example.com/p/xyz/',
                     'https://instagram.com.evil.test/p/xyz/',
                     'https://www.instagram.com/stories/highlights/1/'):
            with self.subTest(href=href):
                self.assertIsNone(browser_module._post_url_from_href(href))


class CollectReelLinksTests(unittest.TestCase):
    def test_plural_reels_grid_links_are_collected(self):
        driver = driver_with({'a[href]': [
            element('/reels/one/?igsh=MTHz'), element('/p/two/'),
        ]}, {'a[href]': [element('/reels/one/?igsh=MTHz'), element('/p/two/')]})
        self.assertEqual(fast_collect(driver), [
            'https://www.instagram.com/p/two',
            'https://www.instagram.com/reel/one',
        ])

    def test_absolute_urls_dedupe_with_relative_variants(self):
        anchor = element('https://www.instagram.com/reel/one/')
        driver = driver_with({'a[href]': [anchor, element('/reel/one')]})
        self.assertEqual(fast_collect(driver), ['https://www.instagram.com/reel/one'])

    def test_non_post_links_never_enter_the_results(self):
        driver = driver_with({'a[href]': [
            element('/username/'), element('/explore/tags/fun/'),
            element('https://example.com/p/xyz/'),
        ]})
        with self.assertRaisesRegex(session.AppError, 'No supported post links were found'):
            fast_collect(driver)

    def test_role_link_fallback_elements_are_checked(self):
        driver = driver_with(
            {'a[href]': [], '[role="link"][href]': [element('/reel/fallback/')]},
            {'a[href]': [], '[role="link"][href]': [element('/reel/fallback/')]})
        self.assertEqual(fast_collect(driver), ['https://www.instagram.com/reel/fallback'])

    def test_stale_elements_are_ignored_without_stopping_collection(self):
        driver = driver_with({'a[href]': [
            element(error=ValueError('stale')), element('/p/good/'),
        ]})
        self.assertEqual(fast_collect(driver), ['https://www.instagram.com/p/good'])

    def test_collection_continues_until_no_new_rounds_cap(self):
        driver = driver_with(
            {'a[href]': [element('/p/one/')]},
            {'a[href]': [element('/p/one/'), element('/p/two/')]},
            {'a[href]': [element('/p/one/'), element('/p/two/')]} # Repeats forever
        )
        driver.execute_script.return_value = True
        self.assertEqual(fast_collect(driver), [
            'https://www.instagram.com/p/one', 'https://www.instagram.com/p/two'])
        # Scroll occurs after rounds with new links (1, 2) and after first no-new round (3).
        # On round 4, no_new_rounds reaches the cap and loop breaks without scrolling.
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_no_links_after_timeout_raises_without_saving(self):
        driver = driver_with({'a[href]': []})
        with patch.object(browser_module, 'FIRST_LINK_TIMEOUT', 0.01), \
                patch.object(browser_module, 'FIRST_LINK_POLL', 0), \
                patch.object(browser_module, 'MAX_NO_NEW_ROUNDS', 2), \
                contextlib.redirect_stdout(io.StringIO()), \
                patch('builtins.print'):
            with self.assertRaisesRegex(session.AppError, 'No supported post links were found'):
                browser_module.collect_reel_links(driver)
        # Empty grids are scrolled in case hydration arrives late, but the
        # give-up cap bounds that work instead of scrolling forever.
        self.assertGreater(driver.execute_script.call_count, 0)
        self.assertLessEqual(driver.execute_script.call_count,
                             browser_module.MAX_NO_NEW_ROUNDS)

    def test_no_secrets_or_cookies_appear_in_output(self):
        driver = driver_with({'a[href]': [element('/reel/one/')]})
        output = io.StringIO()
        with patch.object(browser_module, 'SCROLL_PAUSE', 0), \
                patch.object(browser_module, 'FIRST_LINK_POLL', 0), \
                patch.object(browser_module, 'MAX_NO_NEW_ROUNDS', 1), \
                contextlib.redirect_stdout(output):
            browser_module.collect_reel_links(driver)
        self.assertNotIn('sessionid', output.getvalue())
        self.assertNotIn('csrftoken', output.getvalue())


if __name__ == '__main__':
    unittest.main()
