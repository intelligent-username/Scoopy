"""Small offline checks for failed-only batches and partial carousels."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import download, ui
from scoopy.session import AppError


class FailedRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.output = str(Path(self.temp, 'downloads'))
        self.links = Path(self.temp, 'links.txt')
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.state = download.DownloadRetryState()

    def test_retry_ignores_links_file_and_shrinks_to_only_remaining_failures(self):
        urls = [f'https://www.instagram.com/p/{name}/' for name in ('good', 'bad1', 'bad2')]
        self.links.write_text('\n'.join(urls), encoding='utf-8')
        failures = set(urls[1:])
        attempts = []

        def process(url, folder, work, cookies, summary, counter, *, completed_entries):
            attempts.append(url)
            if url in failures:
                raise download.yt_dlp.utils.DownloadError('fixture failure')
            summary['downloaded'] += 1
            completed_entries.add('saved')

        with patch.object(download, '_process_url', side_effect=process):
            with self.assertRaises(AppError):
                download.start_download(
                    self.output, [], str(self.links), retry_state=self.state)
            self.assertEqual(self.state.failed_urls, urls[1:])
            failures.remove(urls[1])
            attempts.clear()
            with patch.object(download, '_read_link_lines',
                              side_effect=AssertionError('Retry read the full collection')):
                with self.assertRaises(AppError):
                    download.start_download(
                        self.output, [], retry_state=self.state, retry_failed=True)
                self.assertEqual(attempts, urls[1:])
                self.assertEqual(self.state.failed_urls, urls[2:])
                failures.clear()
                attempts.clear()
                result = download.start_download(
                    self.output, [], retry_state=self.state, retry_failed=True)
                self.assertEqual(attempts, urls[2:])
                self.assertEqual(result['downloaded'], 1)
                self.assertEqual(self.state.failed_urls, [])

    def test_partial_carousel_does_not_redownload_its_successful_item(self):
        self.links.write_text('https://www.instagram.com/p/carousel/', encoding='utf-8')
        attempts = []
        fail = True
        extractor = Mock()
        extractor.__enter__ = Mock(return_value=extractor)
        extractor.__exit__ = Mock(return_value=False)
        extractor.extract_info.return_value = {'entries': [{'id': 'good'}, {'id': 'bad'}]}

        def process(entry, folder, work, cookies, summary, index):
            attempts.append(entry['id'])
            if fail and entry['id'] == 'bad':
                summary['failed'].append('fixture failure')
            else:
                summary['downloaded'] += 1

        with patch.object(download.yt_dlp, 'YoutubeDL', return_value=extractor), \
                patch.object(download, '_process_entry', side_effect=process):
            with self.assertRaises(AppError):
                download.start_download(
                    self.output, [], str(self.links), retry_state=self.state)
            self.assertEqual(attempts, ['good', 'bad'])
            fail = False
            attempts.clear()
            result = download.start_download(
                self.output, [], retry_state=self.state, retry_failed=True)
        self.assertEqual(attempts, ['bad'])
        self.assertEqual(result['downloaded'], 1)
        self.assertEqual(self.state.failed_urls, [])

    def test_retry_button_follows_selected_folder_and_uses_retry_mode(self):
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.addCleanup(root.destroy)
        frame = ttk.Frame(root)
        hint = ttk.Label(frame)
        hint.pack()
        button = ttk.Button(frame)
        key = os.path.normcase(os.path.abspath(self.output))
        self.state.directory = key
        self.state.failed_urls = ['https://www.instagram.com/p/bad/']
        app = SimpleNamespace(
            retry_states={key: self.state}, folder_name_var=tk.StringVar(root, self.output),
            retry_button=button, download_hint=hint, driver=object(), busy=False,
            closing=threading.Event(), start_operation=Mock(), download_videos=Mock())
        for name in ('_refresh_retry_button', 'start_retry_failed_thread'):
            setattr(app, name, MethodType(getattr(ui.DownloaderApp, name), app))
        app._refresh_retry_button()
        self.assertEqual(button.winfo_manager(), 'pack')
        self.assertEqual(button.cget('text'), 'Retry failed (1)')
        app.start_retry_failed_thread()
        app.start_operation.call_args.args[0]()
        app.download_videos.assert_called_once_with(self.output, retry_failed=True)
        app.busy = True
        app._refresh_retry_button()
        self.assertIn('disabled', button.state())
        app.folder_name_var.set(str(Path(self.temp, 'other')))
        app._refresh_retry_button()
        self.assertEqual(button.winfo_manager(), '')
        app.folder_name_var.set(self.output)
        self.state.failed_urls.clear()
        app._refresh_retry_button()
        self.assertEqual(button.winfo_manager(), '')


if __name__ == '__main__':
    unittest.main()
