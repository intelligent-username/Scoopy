import contextlib
import io
import os
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

import yt_dlp
from selenium.common.exceptions import WebDriverException
from scoopy import browser as browser_module, session, download, remux, ui


def browser_cookie(name='sessionid', value='test-session', **overrides):
    cookie = {
        'name': name, 'value': value, 'domain': '.instagram.com',
        'path': '/', 'secure': True, 'httpOnly': True,
    }
    cookie.update(overrides)
    return cookie


def browser(cookies=None, url='https://www.instagram.com/user/saved/'):
    return SimpleNamespace(
        current_url=url,
        get_cookies=Mock(return_value=[browser_cookie()] if cookies is None else cookies),
        quit=Mock(),
    )


class BrowserTests(unittest.TestCase):
    def test_start_driver_launches_managed_chrome_with_dedicated_profile(self):
        driver = Mock()
        with patch.object(browser_module.webdriver, 'Chrome', return_value=driver) as chrome:
            result = browser_module.start_driver()
        self.assertIs(result, driver)
        options = chrome.call_args.kwargs['options']
        self.assertIn(f'--user-data-dir={browser_module.CHROME_PROFILE_DIR}', options.arguments)
        driver.maximize_window.assert_called_once()
        driver.get.assert_called_once_with('https://www.instagram.com/')

    def test_start_driver_forces_browser_download_and_restores_environment(self):
        driver = Mock()
        observed_during_call = []

        def record_environment(options):
            observed_during_call.append(os.environ.get(browser_module.SE_FORCE_BROWSER_DOWNLOAD))
            return driver

        os.environ[browser_module.SE_FORCE_BROWSER_DOWNLOAD] = 'previous'
        try:
            with patch.object(browser_module.webdriver, 'Chrome', side_effect=record_environment):
                browser_module.start_driver()
        finally:
            restored = os.environ.pop(browser_module.SE_FORCE_BROWSER_DOWNLOAD)
        self.assertEqual(observed_during_call, ['true'])
        self.assertEqual(restored, 'previous')

        with patch.object(browser_module.webdriver, 'Chrome', side_effect=record_environment):
            browser_module.start_driver()
        self.assertNotIn(browser_module.SE_FORCE_BROWSER_DOWNLOAD, os.environ)

    def test_start_driver_raises_actionable_error_when_browser_cannot_start(self):
        with patch.object(browser_module.webdriver, 'Chrome',
                          side_effect=WebDriverException('network down')):
            with self.assertRaisesRegex(session.AppError, 'managed browser'):
                browser_module.start_driver()
        self.assertNotIn(browser_module.SE_FORCE_BROWSER_DOWNLOAD, os.environ)


class CookieTests(unittest.TestCase):
    def test_missing_browser_requires_login(self):
        with self.assertRaisesRegex(session.AppError, 'Open the browser'):
            session.read_browser_cookies(None)

    def test_only_https_instagram_pages_are_allowed(self):
        for url in ('https://instagram.com.evil.test/', 'https://evilinstagram.com/',
                    'http://www.instagram.com/', 'about:blank'):
            with self.subTest(url=url):
                driver = browser(url=url)
                with self.assertRaises(session.AppError):
                    session.read_browser_cookies(driver)
                driver.get_cookies.assert_not_called()

    def test_missing_empty_and_expired_session_require_login(self):
        cases = ([], [browser_cookie('csrftoken')], [browser_cookie(value='')],
                 [browser_cookie(expiry=int(time.time()) - 10)])
        for cookies in cases:
            with self.subTest(cookies=cookies):
                with self.assertRaisesRegex(session.AppError, 'No active Instagram login'):
                    session.read_browser_cookies(browser(cookies))

    def test_filters_unrelated_and_expired_cookies(self):
        cookies = session.read_browser_cookies(browser([
            browser_cookie(), browser_cookie('csrftoken'),
            browser_cookie('unrelated', domain='.example.com'),
            browser_cookie('lookalike', domain='.instagram.com.evil.test'),
            browser_cookie('missing-domain', domain=''),
            browser_cookie('expired', expiry=int(time.time()) - 10),
        ]))
        self.assertEqual([cookie.name for cookie in cookies], ['sessionid', 'csrftoken'])

    def test_preserves_security_and_expiry_attributes(self):
        expiry = int(time.time()) + 3600
        session_cookie, csrf = session.read_browser_cookies(browser([
            browser_cookie(),
            browser_cookie('csrftoken', domain='www.instagram.com',
                           path='/api/', expiry=expiry, httpOnly=False),
        ]))
        self.assertTrue(session_cookie.secure)
        self.assertTrue(session_cookie.discard)
        self.assertTrue(session_cookie.domain_specified)
        self.assertTrue(session_cookie.domain_initial_dot)
        self.assertTrue(session_cookie.has_nonstandard_attr('HttpOnly'))
        self.assertEqual(csrf.expires, expiry)
        self.assertEqual(csrf.path, '/api/')
        self.assertFalse(csrf.discard)
        self.assertFalse(csrf.domain_specified)
        self.assertFalse(csrf.has_nonstandard_attr('HttpOnly'))

    def test_closed_browser_exception_propagates_without_cookie_values(self):
        driver = browser()
        driver.get_cookies.side_effect = WebDriverException('browser disconnected')
        with self.assertRaises(WebDriverException):
            session.read_browser_cookies(driver)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.links = Path(self.temp.name, 'links.txt')
        self.links.write_text('https://www.instagram.com/reel/test/\n', encoding='utf-8')
        self.output = os.path.join(self.temp.name, 'videos')
        self.cookies = session.read_browser_cookies(browser())
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.fixtures = Path(__file__).resolve().parent / 'fixtures' / 'media'

    def video_format(self, format_id='vid1'):
        return {'format_id': format_id, 'ext': 'mp4', 'vcodec': 'avc1.64001f',
                'acodec': 'none', 'protocol': 'https', 'url': 'https://example.invalid/video.mp4'}

    def audio_format(self, format_id='aud1'):
        return {'format_id': format_id, 'ext': 'm4a', 'vcodec': 'none',
                'acodec': 'mp4a.40.2', 'protocol': 'https', 'url': 'https://example.invalid/audio.m4a'}

    def combined_format(self, format_id='both1'):
        return {'format_id': format_id, 'ext': 'mp4', 'vcodec': 'avc1.64001f',
                'acodec': 'mp4a.40.2', 'protocol': 'https', 'url': 'https://example.invalid/both.mp4'}

    def entry(self, *formats):
        return {'id': 'test', 'title': 'Test Video', 'formats': list(formats)}

    def fake_process(self, files_by_format):
        """A process_ie_result mock that simulates finished downloads."""
        def process(ydl, entry, download):
            self.assertTrue(download)
            format_id = ydl.params['format']
            for hook in ydl.params['progress_hooks']:
                hook({'status': 'finished', 'filename': files_by_format[format_id]})
            return entry
        return process

    def finish_file(self, name, source):
        target = Path(self.output, download.WORK_DIR_NAME, 'entry-0001', name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return str(target)

    def assert_cookiejar_isolation(self, ydl):
        self.assertNotIn('cookies', ydl.params)
        self.assertNotIn('cookiefile', ydl.params)
        self.assertNotIn('cookiesfrombrowser', ydl.params)
        self.assertIsNone(ydl.cookiejar.filename)
        self.assertFalse(ydl.params['cachedir'])
        for url, expected in (
            ('https://www.instagram.com/', 'sessionid=test-session'),
            ('https://example.com/', None),
            ('https://instagram.com.evil.test/', None),
            ('http://www.instagram.com/', None),
        ):
            request = Request(url)
            ydl.cookiejar.add_cookie_header(request)
            self.assertEqual(request.get_header('Cookie'), expected)

    def test_real_ytdlp_cookiejar_handoff_without_network_or_cookie_file(self):
        video_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'video-only.mp4')
        observed = {}

        def mock_extract(ydl, url, download):
            self.assertFalse(download)
            observed['extract'] = ydl
            return self.entry(self.video_format())

        def mock_process(ydl, entry, download):
            observed['process'] = ydl
            self.fake_process({'vid1': video_file})(ydl, entry, download)

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result', mock_process):
            summary = download.start_download(self.output, self.cookies, str(self.links))
        self.assert_cookiejar_isolation(observed['extract'])
        self.assert_cookiejar_isolation(observed['process'])
        # The video-only fixture has no audio track, so it is published as a
        # silent video with a clear status.
        self.assertEqual(summary, {'downloaded': 1, 'silent': 1, 'failed': []})
        self.assertEqual(sorted(path.name for path in Path(self.output).iterdir()),
                         ['completed_videos'])
        self.assertEqual(sorted(path.name for path in
                                Path(self.output, 'completed_videos').iterdir()),
                          ['NA_Test Video.mp4'])

    def test_combined_download_publishes_verified_file(self):
        combined_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'combined.mp4')

        def mock_extract(ydl, url, download):
            return self.entry(self.combined_format())

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'both1': combined_file})):
            summary = download.start_download(self.output, self.cookies, str(self.links))
        published = Path(self.output, 'completed_videos', 'NA_Test Video.mp4')
        self.assertTrue(published.is_file())
        self.assertEqual(summary, {'downloaded': 1, 'silent': 0, 'failed': []})
        self.assertNotIn('recovery', [path.name for path in Path(self.output).iterdir()])

    def test_separate_streams_are_downloaded_and_combined_without_ffmpeg(self):
        video_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'video-only.mp4')
        audio_file = self.finish_file('NA_Test Video.m4a', self.fixtures / 'audio.m4a')

        def mock_extract(ydl, url, download):
            return self.entry(self.video_format(), self.audio_format())

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'vid1': video_file, 'aud1': audio_file})):
            summary = download.start_download(self.output, self.cookies, str(self.links))
        self.assertEqual(summary, {'downloaded': 1, 'silent': 0, 'failed': []})
        published = Path(self.output, 'completed_videos', 'NA_Test Video.mp4')
        self.assertTrue(published.is_file())
        info = remux.inspect_media(published)
        audio = info['audio']
        self.assertTrue(audio and audio['samples'])
        self.assertEqual(info['video']['entry'], 'avc1')

    def test_missing_audio_is_published_as_silent(self):
        video_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'video-only.mp4')

        def mock_extract(ydl, url, download):
            return self.entry(self.video_format())

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'vid1': video_file})):
            summary = download.start_download(self.output, self.cookies, str(self.links))
        self.assertEqual(summary, {'downloaded': 1, 'silent': 1, 'failed': []})
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Test Video.mp4').is_file())

    def test_publish_never_overwrites_existing_outputs(self):
        def mock_extract(ydl, url, download):
            return self.entry(self.combined_format())

        def run_once():
            source = self.finish_file('NA_Test Video.mp4', self.fixtures / 'combined.mp4')
            with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                    patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                                 self.fake_process({'both1': source})):
                download.start_download(self.output, self.cookies, str(self.links))

        run_once()
        run_once()
        self.assertEqual(Path(self.output, 'completed_videos', 'NA_Test Video.mp4').read_bytes(),
                          (self.fixtures / 'combined.mp4').read_bytes())
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Test Video (2).mp4').is_file())

    def test_failed_merge_keeps_components_in_recovery(self):
        video_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'video-only.mp4')

        def mock_extract(ydl, url, download):
            return self.entry(self.video_format(), self.audio_format())

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'vid1': video_file, 'aud1': 'missing.m4a'})):
            with self.assertRaisesRegex(session.AppError, 'recovery'):
                download.start_download(self.output, self.cookies, str(self.links))
        recovered = list(Path(self.output, 'recovery').rglob('*.mp4'))
        self.assertEqual([path.name for path in recovered], ['NA_Test Video.mp4'])

    def test_batch_continues_after_failure_and_reports_partial_success(self):
        good_file = self.finish_file('NA_Test Video.mp4', self.fixtures / 'combined.mp4')

        def mock_extract(ydl, url, download):
            if 'broken' in url:
                raise yt_dlp.utils.DownloadError('unable to download')
            return self.entry(self.combined_format())

        self.links.write_text(
            'https://www.instagram.com/reel/broken/\nhttps://www.instagram.com/reel/works/\n',
            encoding='utf-8')
        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'both1': good_file})), \
                self.assertRaises(session.AppError) as raised:
            download.start_download(self.output, self.cookies, str(self.links))
        self.assertIn('1 download(s) failed', str(raised.exception))

    def test_empty_links_fail_before_creating_downloader(self):
        self.links.write_text(' \n', encoding='utf-8')
        with patch.object(download.yt_dlp, 'YoutubeDL') as downloader:
            with self.assertRaisesRegex(session.AppError, 'No links found'):
                download.start_download(self.output, self.cookies, str(self.links))
            downloader.assert_not_called()

    def test_non_instagram_or_non_post_links_are_rejected(self):
        for url in ('https://example.com/reel/test/', 'file:///etc/passwd',
                    'https://instagram.com.evil.test/p/test/',
                    'https://user:password@www.instagram.com/p/test/',
                    'http://www.instagram.com/p/test/', 'https://www.instagram.com/accounts/login/'):
            with self.subTest(url=url):
                self.links.write_text(url, encoding='utf-8')
                with patch.object(download.yt_dlp, 'YoutubeDL') as downloader:
                    with self.assertRaises(session.AppError):
                        download.start_download(self.output, self.cookies, str(self.links))
                    downloader.assert_not_called()

    def test_partial_failure_is_not_reported_as_success(self):
        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info',
                          side_effect=download.yt_dlp.utils.DownloadError('login required')):
            with self.assertRaisesRegex(session.AppError, '1 download\\(s\\) failed'):
                download.start_download(self.output, self.cookies, str(self.links))
        self.assertNotIn('Download process finished.', self.stdout.getvalue())

    def test_unexpected_exception_propagates_to_worker(self):
        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info',
                          side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                download.start_download(self.output, self.cookies, str(self.links))


class InlineThread:
    """Run workers deterministically; UI events must still wait for queue processing."""

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            driver=browser(), busy=False, ui_queue=queue.Queue(), debug_queue=queue.Queue(),
            browser_button=Mock(), scrape_button=Mock(), download_button=Mock(),
            download_hint=Mock(), folder_name_var=Mock(), after=Mock(), write_to_debug=Mock(),
            browser_was_open=False, destroy=Mock(),
            closing=threading.Event(), driver_lock=threading.Lock(),
        )
        self.app.folder_name_var.get.return_value = 'videos'
        # The links-file gate in update_buttons must not depend on machine state.
        self.enterContext(patch.object(ui.os.path, 'getsize', return_value=128))
        for name in ('update_buttons', 'start_operation', 'check_debug_queue',
                     'start_download_thread', 'download_videos', 'start_scraping_thread',
                     'scrape_links', 'on_closing'):
            setattr(self.app, name, MethodType(getattr(ui.DownloaderApp, name), self.app))
        self.messages = self.enterContext(patch.object(ui, 'messagebox'))
        self.enterContext(patch.object(ui.threading, 'Thread', InlineThread))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_success_is_dispatched_on_ui_queue_and_restores_buttons(self):
        callback = Mock()
        self.app.start_operation(lambda: 42, callback)
        self.assertTrue(self.app.busy)
        callback.assert_not_called()
        self.messages.showinfo.assert_not_called()
        self.app.check_debug_queue()
        callback.assert_called_once_with(42)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.download_button.config.call_args.kwargs['state'], ui.tk.NORMAL)

    def test_worker_error_restores_buttons_without_leaking_secrets(self):
        def fail():
            raise RuntimeError('sessionid=secret')

        callback = Mock()
        self.app.start_operation(fail, callback)
        self.messages.showerror.assert_not_called()
        self.app.check_debug_queue()
        self.assertFalse(self.app.busy)
        callback.assert_not_called()
        self.assertNotIn('secret', str(self.messages.showerror.call_args))
        self.assertEqual(self.app.download_button.config.call_args.kwargs['state'], ui.tk.NORMAL)

    def test_browser_disconnect_allows_browser_to_be_reopened(self):
        driver = self.app.driver
        self.app.start_operation(Mock(side_effect=WebDriverException('secret')), Mock())
        self.app.check_debug_queue()
        driver.quit.assert_called_once()
        self.assertIsNone(self.app.driver)
        self.assertEqual(self.app.browser_button.config.call_args.kwargs['state'], ui.tk.NORMAL)
        self.assertIn('closed or disconnected', self.messages.showerror.call_args.args[1])

    def test_login_error_preserves_browser_for_login_retry(self):
        driver = self.app.driver
        driver.get_cookies.return_value = []
        self.app.start_operation(lambda: session.read_browser_cookies(driver), Mock())
        self.app.check_debug_queue()
        self.assertIs(self.app.driver, driver)
        self.assertFalse(self.app.busy)
        self.assertIn('No active Instagram login', self.messages.showerror.call_args.args[1])

    def test_busy_app_prevents_overlapping_operations_but_allows_close(self):
        self.app.busy = True
        driver = self.app.driver
        target = Mock()
        self.app.start_operation(target, Mock())
        self.app.start_download_thread()
        self.app.start_scraping_thread()
        self.app.on_closing()
        target.assert_not_called()
        driver.quit.assert_called_once()
        self.app.destroy.assert_called_once()
        self.messages.showwarning.assert_not_called()

    def test_close_quits_browser_gracefully_without_forcing(self):
        driver = self.app.driver
        self.app.browser_was_open = True
        with patch.object(ui, 'close_orphan_profile_chrome') as force_close:
            self.app.on_closing()
        driver.quit.assert_called_once()
        self.assertIsNone(self.app.driver)
        self.assertFalse(self.app.browser_was_open)
        force_close.assert_not_called()

    def test_close_forces_profile_cleanup_when_quit_fails(self):
        driver = self.app.driver
        driver.quit.side_effect = WebDriverException('session dead')
        with patch.object(ui, 'close_orphan_profile_chrome') as force_close:
            self.app.on_closing()
        driver.quit.assert_called_once()
        force_close.assert_called_once()
        self.assertIsNone(self.app.driver)

    def test_close_cleans_orphan_when_driver_reference_was_lost(self):
        self.app.driver = None
        self.app.browser_was_open = True
        with patch.object(ui, 'close_orphan_profile_chrome') as force_close:
            self.app.on_closing()
        force_close.assert_called_once()
        self.assertFalse(self.app.browser_was_open)

    def test_download_requires_browser_not_cookie_file(self):
        self.app.driver = None
        self.app.start_download_thread()
        self.assertFalse(self.app.busy)
        self.messages.showwarning.assert_called_once()

    def test_download_refreshes_cookies_each_time_and_clears_list(self):
        with patch.object(ui, 'start_download') as download:
            self.app.download_videos('videos')
            self.app.driver.get_cookies.assert_called_once()
            self.assertEqual(download.call_args.args[1], [])
            self.app.download_videos('videos')
            self.assertEqual(self.app.driver.get_cookies.call_count, 2)

    def test_download_clears_cookie_list_even_on_failure(self):
        cookies = session.read_browser_cookies(browser())
        with patch.object(ui, 'read_browser_cookies', return_value=cookies), \
                patch.object(ui, 'start_download', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.app.download_videos('videos')
        self.assertEqual(cookies, [])

    def test_missing_links_does_not_start_worker(self):
        with patch.object(ui.os.path, 'exists', return_value=False):
            self.app.start_download_thread()
        self.assertFalse(self.app.busy)
        self.messages.showerror.assert_called_once()

    def test_scrape_validates_login_before_collecting_links(self):
        self.app.driver.get_cookies.return_value = []
        with patch.object(ui, 'collect_reel_links') as collect:
            with self.assertRaises(session.AppError):
                self.app.scrape_links()
            collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
