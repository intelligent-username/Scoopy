"""Issue 2 offline regressions; no browser, session, or project data access."""

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import WebDriverException

from scoopy import browser, ui
from scoopy.session import AppError


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class GridDriver:
    def __init__(self, batches, *, role_links=()):
        self.batches = batches
        self.round = -1
        self.role_links = role_links
        self.execute_script = Mock()

    def find_elements(self, by, selector):
        if selector == browser.ANCHOR_SELECTOR:
            self.round += 1
            hrefs = self.batches[min(self.round, len(self.batches) - 1)]
        else:
            hrefs = self.role_links
        return [SimpleNamespace(get_attribute=Mock(return_value=href)) for href in hrefs]


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.clock = Clock()
        self.enterContext(patch.object(browser.time, 'monotonic', self.clock.monotonic))
        self.enterContext(patch.object(browser.time, 'sleep', self.clock.sleep))
        self.enterContext(patch.object(browser, 'FIRST_LINK_TIMEOUT', 0.2))
        self.enterContext(patch.object(browser, 'FIRST_LINK_POLL', 0.1))
        self.enterContext(patch.object(browser, 'SCROLL_PAUSE', 0.1))
        self.enterContext(patch.object(browser, 'MAX_NO_NEW_ROUNDS', 3))

    def test_delayed_hydration_within_initial_wait(self):
        driver = GridDriver([[], ['/alice/reels/Code-1/?tracking=secret']])
        self.assertEqual(browser.collect_reel_links(driver), [
            'https://www.instagram.com/reel/Code-1'])
        self.assertGreaterEqual(self.clock.now, 0.1)

    def test_empty_initial_wait_still_scrolls_and_discovers_links(self):
        driver = GridDriver([[], [], [], ['/p/late/']])
        self.assertEqual(browser.collect_reel_links(driver), [
            'https://www.instagram.com/p/late'])
        self.assertGreater(driver.execute_script.call_count, 0)
        self.assertEqual(driver.execute_script.call_args_list[0].args[1], [])

    def test_role_link_candidates_deduplicate_and_sort(self):
        driver = GridDriver([['/p/z/', '/user/reels/a/?secret=token']],
                            role_links=['https://www.instagram.com/reel/a/'])
        self.assertEqual(browser.collect_reel_links(driver), [
            'https://www.instagram.com/p/z', 'https://www.instagram.com/reel/a'])
        self.assertNotIn('secret', self.output.getvalue())
        self.assertNotIn('token', self.output.getvalue())

    def test_no_link_page_terminates_with_actionable_safe_failure(self):
        driver = GridDriver([['https://example.invalid/p/secret/?sessionid=value']])
        with self.assertRaisesRegex(AppError, 'Tiles without post/reel hrefs') as raised:
            browser.collect_reel_links(driver)
        self.assertEqual(driver.execute_script.call_count, 2)
        self.assertNotIn('secret', str(raised.exception) + self.output.getvalue())
        self.assertLess(self.clock.now, 1)

    def test_ever_growing_grid_stops_at_round_cap(self):
        driver = GridDriver([[f'/p/code{index}/'] for index in range(50)])
        with patch.object(browser, 'MAX_SCROLL_ROUNDS', 4):
            result = browser.collect_reel_links(driver)
        self.assertEqual(len(result), 4)
        self.assertEqual(driver.execute_script.call_count, 4)
        self.assertIn('may be incomplete', self.output.getvalue())

    def test_elapsed_budget_includes_initial_wait(self):
        driver = GridDriver([[]])
        with patch.object(browser, 'MAX_COLLECTION_SECONDS', 0.15):
            with self.assertRaises(AppError):
                browser.collect_reel_links(driver)
        self.assertLessEqual(self.clock.now, 0.16)
        driver.execute_script.assert_not_called()

    def test_disconnection_is_not_misreported_as_empty_success(self):
        driver = GridDriver([['/p/good/']])
        driver.execute_script.side_effect = WebDriverException('secret session text')
        with self.assertRaises(WebDriverException):
            browser.collect_reel_links(driver)
        self.assertNotIn('secret session text', self.output.getvalue())


class UrlTests(unittest.TestCase):
    def test_supported_shapes_normalize(self):
        for prefix in ('', 'alice_1/', 'some.user/'):
            for kind in ('p', 'reel', 'reels'):
                for origin in ('', 'https://instagram.com', 'https://www.instagram.com',
                               '//www.instagram.com'):
                    href = f'{origin}/{prefix}{kind}/AbC_123-/?igsh=secret#fragment'
                    canonical_kind = 'p' if kind == 'p' else 'reel'
                    with self.subTest(href=href):
                        self.assertEqual(browser._post_url_from_href(href),
                                         f'https://www.instagram.com/{canonical_kind}/AbC_123-')

    def test_unsafe_or_non_post_shapes_are_rejected(self):
        invalid = [
            None, '', '/p/', '/alice/', '/accounts/login/', '/alice/saved/123/',
            '/collections/p/code/', '/accounts/reel/code/', '/explore/p/code/',
            '/p/code/extra/', '/p//code/', '//p/code/', 'p/code/',
            '/p/../', '/p/%2f/', '/p/code%20/', '/p/code\\bad/',
            '/p/co\nde/', '/p/co de/', '/.user/p/code/', '/user../p/code/',
            'http://www.instagram.com/p/code/', 'javascript:/p/code/',
            'https:///p/code/', 'https://example.com/p/code/',
            'https://instagram.com.evil.invalid/p/code/',
            'https://user:secret@www.instagram.com/p/code/',
            'https://www.instagram.com:443/p/code/',
            'https://www.instagram.com:/p/code/', 'https://[broken/p/code/',
            'https://.instagram.com/p/code/', 'https://a..instagram.com/p/code/',
        ]
        for href in invalid:
            with self.subTest(href=href):
                self.assertIsNone(browser._post_url_from_href(href))


class ScrollScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node unavailable for offline DOM script probe')
    def test_real_script_selects_nested_grid_empty_grid_and_document(self):
        # Execute production JS against minimal standard DOM doubles, not React.
        harness = r"""
const script = SCRIPT;
function element(height, total, parent=null) {
    return {clientHeight:height, scrollHeight:total, scrollTop:0,
        parentElement:parent,
        getBoundingClientRect:()=>({width:500,height:height,top:0,bottom:height})};
}
global.window={innerHeight:800};
global.getComputedStyle=()=>({overflowY:'auto'});
const results=[];
for (const mode of ['nested','empty','document']) {
    const root=element(800,3000);
    const grid=element(400,2000,root);
    const link=element(100,100,grid);
    link.href='https://www.instagram.com/p/code/';
    global.document={scrollingElement:root,documentElement:root,
        querySelectorAll: selector => selector.startsWith('a[')
            ? (mode==='nested' ? [link] : [])
            : (mode==='document' ? [] : [grid])};
    const moved=new Function(script)(mode==='nested' ? [link.href] : []);
    results.push({mode,moved,grid:grid.scrollTop,root:root.scrollTop});
}
console.log(JSON.stringify(results));
""".replace('SCRIPT', json.dumps(browser.SCROLL_SCRIPT))
        completed = subprocess.run(
            [shutil.which('node'), '-e', harness], capture_output=True,
            text=True, timeout=10, check=True)
        self.assertEqual(json.loads(completed.stdout), [
            {'mode': 'nested', 'moved': True, 'grid': 320, 'root': 0},
            {'mode': 'empty', 'moved': True, 'grid': 320, 'root': 0},
            {'mode': 'document', 'moved': True, 'grid': 0, 'root': 640},
        ])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.links = Path(self.temp, 'links.txt')
        self.links.write_bytes(b'existing links\r\n')
        self.enterContext(patch.object(browser, 'LINKS_FILE', str(self.links)))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_empty_and_invalid_saves_leave_original_unchanged(self):
        for links in ([], ['/p/good/', 'https://evil.invalid/p/bad/']):
            with self.subTest(links=links), self.assertRaises(AppError):
                browser.save_links(links)
            self.assertEqual(self.links.read_bytes(), b'existing links\r\n')

    def test_successful_save_is_canonical_sorted_unique_atomic_replace(self):
        real_replace = browser.os.replace
        with patch.object(browser.os, 'replace', wraps=real_replace) as replace:
            browser.save_links(['/bob/reels/z/?secret=token', '/p/a/', '/reel/z/'])
        self.assertEqual(self.links.read_text(encoding='utf-8'),
                         'https://www.instagram.com/p/a\nhttps://www.instagram.com/reel/z\n')
        self.assertEqual(Path(replace.call_args.args[0]).parent, self.links.parent)
        self.assertEqual(list(Path(self.temp).iterdir()), [self.links])

    def test_failed_replace_preserves_original_and_removes_temporary(self):
        with patch.object(browser.os, 'replace', side_effect=PermissionError('secret')):
            with self.assertRaisesRegex(AppError, 'permissions') as raised:
                browser.save_links(['/p/good/'])
        self.assertNotIn('secret', str(raised.exception))
        self.assertEqual(self.links.read_bytes(), b'existing links\r\n')
        self.assertEqual(list(Path(self.temp).iterdir()), [self.links])

    def test_failed_flush_preserves_original(self):
        with patch.object(browser.os, 'fsync', side_effect=OSError('secret')):
            with self.assertRaises(AppError):
                browser.save_links(['/p/good/'])
        self.assertEqual(self.links.read_bytes(), b'existing links\r\n')
        self.assertEqual(list(Path(self.temp).iterdir()), [self.links])

    def test_ui_empty_and_failed_collection_never_save_and_clear_cookies(self):
        app = SimpleNamespace(driver=object())
        for failure in (None, AppError('No supported post links were found.')):
            cookies = [{'value': 'never logged'}]
            with patch.object(ui, 'read_browser_cookies', return_value=cookies), \
                    patch.object(ui, 'collect_reel_links', return_value=[],
                                 side_effect=failure), \
                    patch.object(ui, 'save_links') as save:
                with self.assertRaises(AppError):
                    ui.DownloaderApp.scrape_links(app)
                save.assert_not_called()
            self.assertEqual(cookies, [])
            self.assertEqual(self.links.read_bytes(), b'existing links\r\n')

    def test_ui_success_returns_saved_count(self):
        cookies = [{'value': 'never logged'}]
        links = ['https://www.instagram.com/p/good']
        with patch.object(ui, 'read_browser_cookies', return_value=cookies), \
                patch.object(ui, 'collect_reel_links', return_value=links), \
                patch.object(ui, 'save_links') as save:
            count = ui.DownloaderApp.scrape_links(SimpleNamespace(driver=object()))
        self.assertEqual(count, 1)
        self.assertEqual(cookies, [])
        save.assert_called_once_with(links)


if __name__ == '__main__':
    unittest.main()
