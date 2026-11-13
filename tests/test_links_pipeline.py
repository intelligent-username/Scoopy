"""Links-pipeline tests: ingestion, validation, dedupe and robustness.

Covers the reading/validating side of download.start_download: what lines of
links.txt become download URLs, what is dropped and why, and how the pipeline
reacts to missing, empty, BOM/CRLF or non-UTF-8 files. yt-dlp is mocked the
same way tests/test_main.py does it; no network access happens.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from scoopy import download, session


def combined_format(format_id='both1'):
    return {'format_id': format_id, 'ext': 'mp4', 'vcodec': 'avc1.64001f',
            'acodec': 'mp4a.40.2', 'protocol': 'https',
            'url': 'https://example.invalid/both.mp4'}


class IngestionTests(unittest.TestCase):
    """How lines of links.txt turn into the URL list that gets downloaded."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.links = Path(self.temp.name, 'links.txt')
        self.output = os.path.join(self.temp.name, 'videos')
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def run_download(self):
        """Run start_download with the per-URL worker mocked out."""
        with patch.object(download, '_process_url') as process:
            summary = download.start_download(self.output, [], str(self.links))
        urls = [call.args[0] for call in process.call_args_list]
        return summary, urls, self.stdout.getvalue()

    def test_bom_crlf_and_blank_lines_are_tolerated(self):
        self.links.write_text(
            '\ufeffhttps://www.instagram.com/reel/abc/\r\n'
            '\r\n'
            '   \r\n'
            'https://www.instagram.com/p/xyz/\r\n',
            encoding='utf-8')
        _, urls, _ = self.run_download()
        self.assertEqual(urls, [
            'https://www.instagram.com/reel/abc/',
            'https://www.instagram.com/p/xyz/',
        ])

    def test_query_fragment_and_duplicates_are_stripped_and_deduped(self):
        self.links.write_text(
            'https://www.instagram.com/reel/abc/?igsh=tok123\n'
            'https://www.instagram.com/p/xyz/?utm_source=share#frag\n'
            'https://www.instagram.com/reel/abc/\n'
            'https://www.instagram.com/p/xyz/\n',
            encoding='utf-8')
        _, urls, output = self.run_download()
        self.assertEqual(urls, [
            'https://www.instagram.com/reel/abc/',
            'https://www.instagram.com/p/xyz/',
        ])
        self.assertIn('duplicate of an earlier link', output)

    def test_reels_and_trailing_slash_less_post_paths_are_accepted(self):
        self.links.write_text(
            'https://www.instagram.com/reels/Q123/\n'
            'https://www.instagram.com/p/ABC123\n',
            encoding='utf-8')
        _, urls, _ = self.run_download()
        self.assertEqual(urls, [
            'https://www.instagram.com/reels/Q123/',
            'https://www.instagram.com/p/ABC123',
        ])

    def test_invalid_lines_are_dropped_with_category_and_line_number_only(self):
        self.links.write_text(
            'https://www.instagram.com/reel/good/\n'
            'http://www.instagram.com/reel/insecure/\n'
            'https://example.com/reel/other/\n'
            'https://user:secret@www.instagram.com/p/creds/\n'
            'https://www.instagram.com/accounts/login/\n'
            'https://www.instagram.com/p/\n'
            'https://instagram.com.evil.test/p/test/\n',
            encoding='utf-8')
        _, urls, output = self.run_download()
        self.assertEqual(urls, ['https://www.instagram.com/reel/good/'])
        self.assertIn('Dropped 6 of 7', output)
        self.assertIn('not an https:// link', output)
        self.assertIn('not an Instagram link', output)
        self.assertIn('contains embedded login credentials', output)
        self.assertIn('not a post link', output)
        # Categories and line numbers only: no URL text, no credential values.
        self.assertNotIn('example.com', output)
        self.assertNotIn('evil.test', output)
        self.assertNotIn('user:secret', output)
        self.assertNotIn('accounts/login', output)
        self.assertNotIn('insecure', output)

    def test_all_links_invalid_raises_and_never_starts_the_downloader(self):
        self.links.write_text('https://example.com/reel/nope/\n', encoding='utf-8')
        with patch.object(download.yt_dlp, 'YoutubeDL') as downloader:
            with self.assertRaisesRegex(session.AppError, 'None of the 1 link'):
                download.start_download(self.output, [], str(self.links))
            downloader.assert_not_called()

    def test_missing_file_reports_file_missing_and_never_starts_the_downloader(self):
        with patch.object(download.yt_dlp, 'YoutubeDL') as downloader:
            with self.assertRaisesRegex(session.AppError, 'does not exist'):
                download.start_download(self.output, [], str(self.links))
            downloader.assert_not_called()

    def test_empty_and_whitespace_only_files_keep_the_pinned_no_links_message(self):
        for text in ('', ' \n', '\r\n\r\n'):
            with self.subTest(text=repr(text)):
                self.links.write_text(text, encoding='utf-8')
                with patch.object(download.yt_dlp, 'YoutubeDL') as downloader:
                    with self.assertRaisesRegex(session.AppError, 'No links found'):
                        download.start_download(self.output, [], str(self.links))
                    downloader.assert_not_called()

    def test_non_utf8_bytes_are_replaced_not_fatal(self):
        self.links.write_bytes(
            'https://www.instagram.com/reel/good/\n'.encode('utf-8')
            + b'\xff\xfe broken bytes \x9a\n'
            + 'https://www.instagram.com/reel/also-good/\n'.encode('utf-8'))
        _, urls, output = self.run_download()
        self.assertEqual(urls, [
            'https://www.instagram.com/reel/good/',
            'https://www.instagram.com/reel/also-good/',
        ])
        self.assertIn('Dropped 1 of 3', output)


class PipelineIntegrationTests(unittest.TestCase):
    """End to end through start_download with yt-dlp mocked, like test_main."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.links = Path(self.temp.name, 'links.txt')
        self.output = os.path.join(self.temp.name, 'videos')
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.fixtures = Path(__file__).resolve().parent / 'fixtures' / 'media'

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

    def test_dirty_links_file_downloads_each_valid_post_exactly_once(self):
        self.links.write_text(
            '\ufeffhttps://www.instagram.com/reel/one/?igsh=abc\r\n'
            'https://www.instagram.com/reel/one/\r\n'
            'https://www.instagram.com/accounts/login/\r\n',
            encoding='utf-8')
        combined_file = self.finish_file(
            'NA_Test Video.mp4', self.fixtures / 'combined.mp4')

        def mock_extract(ydl, url, download):
            self.assertEqual(url, 'https://www.instagram.com/reel/one/')
            return {'id': 'test', 'title': 'Test Video',
                    'formats': [combined_format()]}

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                patch.object(download.yt_dlp.YoutubeDL, 'process_ie_result',
                             self.fake_process({'both1': combined_file})):
            summary = download.start_download(self.output, [], str(self.links))
        self.assertEqual(summary, {'downloaded': 1, 'silent': 0, 'failed': []})
        self.assertTrue(Path(self.output, 'NA_Test Video.mp4').is_file())
        output = self.stdout.getvalue()
        self.assertIn('duplicate of an earlier link', output)
        self.assertIn('Dropped 2 of 3', output)

    def test_download_error_summary_keeps_url_and_actionable_reason(self):
        self.links.write_text(
            'https://www.instagram.com/reel/broken/?igsh=tok\n', encoding='utf-8')

        def mock_extract(ydl, url, download):
            raise yt_dlp.utils.DownloadError('unable to download')

        with patch.object(download.yt_dlp.YoutubeDL, 'extract_info', mock_extract), \
                self.assertRaises(session.AppError) as raised:
            download.start_download(self.output, [], str(self.links))
        message = str(raised.exception)
        self.assertIn('1 download(s) failed', message)
        self.assertIn('https://www.instagram.com/reel/broken/', message)
        self.assertNotIn('igsh=tok', message)


if __name__ == '__main__':
    unittest.main()
