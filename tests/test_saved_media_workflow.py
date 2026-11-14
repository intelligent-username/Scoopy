"""Offline regressions for saved navigation, the real log widget and photos."""

import contextlib
import base64
import functools
import http.server
import io
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import download, session, ui


class SavedNavigationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.driver = SimpleNamespace(
            current_url='https://www.instagram.com/',
            execute_script=Mock(return_value={
                'saved': [], 'profile': ['https://www.instagram.com/my.account/']}),
        )

        def navigate(url):
            self.driver.current_url = url

        self.driver.get = Mock(side_effect=navigate)

    def test_own_profile_opens_saved(self):
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_called_once_with(
            'https://www.instagram.com/my.account/saved/')

    def test_saved_link_is_preferred(self):
        self.driver.execute_script.return_value['saved'] = [
            'https://www.instagram.com/actual_user/saved/']
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_called_once_with(
            'https://www.instagram.com/actual_user/saved/')

    def test_existing_collection_is_not_redirected(self):
        self.driver.current_url = 'https://www.instagram.com/me/saved/collection123/'
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_not_called()
        self.driver.execute_script.assert_not_called()

    def test_missing_or_unsafe_profile_falls_back_home(self):
        for value in (None, [], 'bad', {'profile': [
                'https://evil.test/me/', 'https://www.instagram.com/accounts/',
                'https://user:secret@www.instagram.com/me/',
                'https://www.instagram.com/p/123/',
                'https://www.instagram.com:443/me/']}):
            with self.subTest(value=value):
                self.driver.current_url = 'https://www.instagram.com/'
                self.driver.get.reset_mock()
                self.driver.execute_script.return_value = value
                self.assertFalse(session.navigate_to_saved(self.driver))
                self.driver.get.assert_called_once_with('https://www.instagram.com/')

    def test_failed_or_redirected_navigation_falls_back(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                self.driver.current_url = 'https://www.instagram.com/'
                calls = []

                def navigate(url):
                    calls.append(url)
                    if '/saved/' in url:
                        if fail:
                            raise RuntimeError('PRIVATE_SESSION')
                        self.driver.current_url = 'https://www.instagram.com/accounts/login/'
                    else:
                        self.driver.current_url = url

                self.driver.get.side_effect = navigate
                self.assertFalse(session.navigate_to_saved(self.driver))
                self.assertEqual(calls[-1], 'https://www.instagram.com/')

    def test_broken_browser_does_not_leak_exception(self):
        self.driver.execute_script.side_effect = RuntimeError('PRIVATE_SESSION')
        self.driver.get.side_effect = RuntimeError('PRIVATE_SESSION')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(session.navigate_to_saved(self.driver))
        self.assertNotIn('PRIVATE_SESSION', output.getvalue())


class LoginMonitorTests(unittest.TestCase):
    def test_saved_navigation_runs_once_despite_repeated_session_checks(self):
        for first_logged_in in (True, False):
            with self.subTest(first_logged_in=first_logged_in):
                driver = object()
                closing = Mock()
                closing.wait.side_effect = [False] * 4 + [True]
                closing.is_set.return_value = False
                app = SimpleNamespace(driver=driver, busy=False, closing=closing)
                results = ([[]] * 4 if first_logged_in else
                           [session.AppError('Login first'), [], [], []])
                with patch.object(ui, 'read_browser_cookies', side_effect=results), \
                        patch.object(ui, 'navigate_to_saved') as navigate, \
                        contextlib.redirect_stdout(io.StringIO()):
                    ui.DownloaderApp.monitor_login(app, driver)
                navigate.assert_called_once_with(driver)


class LogRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(f'Tk unavailable: {error}') from error

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.text = tk.Text(self.root)
        self.addCleanup(self.text.destroy)
        self.app = SimpleNamespace(
            debug_area=self.text, _line_open=False, _debug_replace=False,
            _debug_count=0, _debug_follow=True, debug_count_label=Mock(),
            _debug_at_bottom=lambda: True,
            _level_tag=ui.DownloaderApp._level_tag,
            _is_request_noise=ui.DownloaderApp._is_request_noise,
        )
        self.app._update_debug_count = lambda: ui.DownloaderApp._update_debug_count(self.app)

    def render(self, message):
        ui.DownloaderApp.write_to_debug(self.app, message)

    def content(self):
        return self.text.get('1.0', 'end-1c')

    def test_real_widget_renders_login_with_tags_and_split_newlines(self):
        self.render('Existing Instagram session detected.')
        self.render('\n')
        self.render('Saved collections opened.\n')
        self.assertIn('Existing Instagram session detected.', self.content())
        self.assertIn('Saved collections opened.', self.content())
        self.assertEqual(self.app._debug_count, 2)
        self.assertTrue(self.text.tag_ranges('success'))
        self.assertEqual(self.text.cget('state'), tk.DISABLED)

    def test_progress_replaces_only_current_line_across_ticks(self):
        self.render('Previous entry\n')
        self.render('Progress 10%')
        self.render('\r')
        self.render('Progress 20%')
        self.render('\r')
        self.render('\nFinished\n')
        content = self.content()
        self.assertIn('Previous entry', content)
        self.assertNotIn('Progress 10%', content)
        self.assertEqual(content.count('Progress 20%'), 1)
        self.assertEqual(self.app._debug_count, 3)

    def test_clear_resets_progress_and_entry_count(self):
        self.render('Old entry\nProgress\r')
        ui.DownloaderApp.clear_debug(self.app)
        self.render('New entry\n')
        self.assertNotIn('Old entry', self.content())
        self.assertIn('New entry', self.content())
        self.assertEqual(self.app._debug_count, 1)

    def test_render_failure_restores_readonly_state(self):
        self.app._level_tag = Mock(side_effect=RuntimeError('render failed'))
        with self.assertRaises(RuntimeError):
            self.render('An entry\n')
        self.assertEqual(self.text.cget('state'), tk.DISABLED)

    def test_failed_render_does_not_enqueue_its_own_error(self):
        app = SimpleNamespace(
            closing=threading.Event(), winfo_exists=lambda: True,
            ui_queue=queue.Queue(), debug_queue=queue.Queue(),
            write_to_debug=Mock(side_effect=RuntimeError('render failed')),
            after=Mock(), check_debug_queue=Mock(),
        )
        app.debug_queue.put('broken entry')
        with contextlib.redirect_stdout(ui.DownloaderApp.QueueStream(app.debug_queue)), \
                contextlib.redirect_stderr(ui.DownloaderApp.QueueStream(app.debug_queue)), \
                patch.object(ui.sys, '__stderr__', io.StringIO()):
            ui.DownloaderApp.check_debug_queue(app)
        self.assertTrue(app.debug_queue.empty())
        app.after.assert_called_once()


class PhotoDownloadTests(unittest.TestCase):
    # A real 1x1 PNG fixture, not a video thumbnail.
    IMAGE = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.output = self.root / 'downloads'
        self.links = self.root / 'links.txt'
        self.links.write_text('https://www.instagram.com/p/B/\n', encoding='utf-8')
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def product(self, **overrides):
        value = {
            'pk': '123456789', 'code': 'B', 'media_type': 1,
            'user': {'username': 'fixture', 'pk': '123'},
            'image_versions2': {'candidates': [
                {'url': 'https://scontent.cdninstagram.com/small.png', 'width': 1, 'height': 1},
                {'url': 'https://scontent.cdninstagram.com/photo.png', 'width': 2, 'height': 2},
            ]},
        }
        value.update(overrides)
        return value

    def test_explicit_photo_metadata_selects_largest_candidate(self):
        info = download.InstagramIE()._extract_product_media(self.product())
        self.assertTrue(info['_scoopy_photo'])
        self.assertTrue(info['formats'][0]['url'].endswith('/photo.png'))
        self.assertEqual(info['formats'][0]['ext'], 'png')

    def test_video_thumbnails_are_not_treated_as_photos(self):
        with patch.object(download._InstagramIE, '_extract_product_media',
                          return_value={'formats': []}) as original:
            info = download.InstagramIE()._extract_product_media(self.product(media_type=2))
        original.assert_called_once()
        self.assertNotIn('_scoopy_photo', info)

    def test_missing_or_unsafe_image_urls_fail_explicitly(self):
        for url in ('https://evil.test/photo.png',
                    'https://cdninstagram.com.evil.test/photo.png',
                    'http://scontent.cdninstagram.com/photo.png',
                    'https://user:secret@scontent.cdninstagram.com/photo.png'):
            with self.subTest(url=url), self.assertRaises(download.yt_dlp.utils.ExtractorError):
                download.InstagramIE()._extract_product_media(self.product(
                    image_versions2={'candidates': [{'url': url}]}))

    def test_mixed_carousel_keeps_photo_and_video_entries(self):
        video = self.product(
            pk='123456790', media_type=2,
            video_versions=[{'id': 'video', 'url': 'https://scontent.cdninstagram.com/video.mp4'}])
        info = download.InstagramIE()._extract_product(self.product(
            media_type=8, carousel_media=[self.product(), video]))
        self.assertEqual(len(info['entries']), 2)
        self.assertTrue(info['entries'][0]['_scoopy_photo'])
        self.assertNotIn('_scoopy_photo', info['entries'][1])
        self.assertTrue(info['entries'][1]['formats'])

    def run_local_download(self, payload):
        (self.root / 'photo.png').write_bytes(payload)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), functools.partial(Handler, directory=self.temp))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        product = self.product()

        def extract(extractor, url):
            # Keep real product parsing and real yt-dlp download/processing; only
            # replace Instagram's response and CDN location with local fixtures.
            info = extractor._extract_product(product)
            info['formats'][0]['url'] = f'http://127.0.0.1:{server.server_port}/photo.png'
            return info

        try:
            with patch.object(download.InstagramIE, '_real_initialize', return_value=None), \
                    patch.object(download.InstagramIE, '_real_extract', extract), \
                    patch.object(subprocess, 'Popen',
                                 side_effect=AssertionError('External process attempted')), \
                    patch.object(os, 'system',
                                 side_effect=AssertionError('External process attempted')):
                return download.start_download(str(self.output), [], str(self.links))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_real_ytdlp_downloads_photo_without_video_processing_or_overwrite(self):
        self.output.mkdir()
        (self.output / 'NA_Video by fixture.png').write_bytes(b'existing')
        with patch.object(download.remux, 'validate_video_stream',
                          side_effect=AssertionError('Photo treated as video')):
            result = self.run_local_download(self.IMAGE)
        self.assertEqual(result, {'downloaded': 0, 'silent': 0, 'failed': [], 'photos': 1})
        files = list(self.output.glob('*.png'))
        self.assertEqual(len(files), 2)
        self.assertEqual((self.output / 'NA_Video by fixture.png').read_bytes(), b'existing')
        self.assertIn(self.IMAGE, [path.read_bytes() for path in files])
        self.assertEqual(ui.download_summary(result), 'Downloaded 1 photo.')
        self.assertFalse((self.output / download.WORK_DIR_NAME).exists())

    def test_html_instead_of_image_is_not_published(self):
        with self.assertRaises(session.AppError):
            self.run_local_download(b'<html>verification required</html>')
        self.assertEqual(list(self.output.glob('*.png')), [])
        self.assertTrue(list((self.output / 'recovery').rglob('*photo*')) or
                        list((self.output / 'recovery').rglob('*.png')))

    def test_mixed_media_summary_and_invalid_photo_counts(self):
        self.assertEqual(
            ui.download_summary({'downloaded': 2, 'silent': 1, 'failed': [], 'photos': 3}),
            'Downloaded 3 photos and 2 videos (1 without audio).')
        for value in (-1, '2', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.download_summary({'downloaded': 0, 'silent': 0, 'failed': [], 'photos': value})


if __name__ == '__main__':
    unittest.main()
