"""Integration tests: real yt-dlp over a local HTTP server, no FFmpeg.

These tests exercise the full pipeline with a real YoutubeDL instance and a
local media server, using a fake Instagram extractor so no network access to
Instagram happens. FFmpeg/external processes are forbidden during the tests.
"""
import http.server
import contextlib
import functools
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from scoopy import download, remux
from scoopy.session import AppError

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'media'
SERVER_PORT = 0
RECORDED_REQUESTS = []
_RECORD_LOCK = threading.Lock()


class MediaRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # Keep test output clean.

    def do_GET(self):
        with _RECORD_LOCK:
            RECORDED_REQUESTS.append({
                'path': self.path,
                'cookie': self.headers.get('Cookie'),
                'user_agent': self.headers.get('User-Agent'),
            })
        super().do_GET()


class FakeInstagramIE(InfoExtractor):
    """Serves local fixture URLs for instagram.com/p/<id>/ post URLs."""

    _VALID_URL = r'https?://(?:www\.)?instagram\.com/p/(?P<id>[A-Za-z0-9_-]+)/?$'

    def _real_extract(self, url):
        post_id = self._match_id(url)
        base = f'http://127.0.0.1:{SERVER_PORT}'
        headers = {'User-Agent': 'ScoopyPipelineTest/1.0'}
        video = {'format_id': 'video', 'ext': 'mp4', 'vcodec': 'avc1.64001f',
                 'acodec': 'none', 'protocol': 'http',
                 'url': f'{base}/video-only.mp4', 'http_headers': headers}
        audio = {'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none',
                 'acodec': 'mp4a.40.2', 'protocol': 'http',
                 'url': f'{base}/audio.m4a', 'http_headers': headers}
        combined = {'format_id': 'combined', 'ext': 'mp4', 'vcodec': 'avc1.64001f',
                    'acodec': 'mp4a.40.2', 'protocol': 'http',
                    'url': f'{base}/combined.mp4', 'http_headers': headers}

        def entry(suffix, *formats):
            return {'id': f'{post_id}{suffix}', 'title': 'Pipeline Test', 'formats': list(formats)}

        if post_id == 'pipeline-combined':
            return entry('', combined)
        if post_id == 'pipeline-separate':
            return entry('', video, audio)
        if post_id == 'pipeline-silent':
            return entry('', video)
        if post_id == 'pipeline-audio-fails':
            broken = dict(audio, url=f'{base}/missing.m4a')
            return entry('', video, broken)
        if post_id == 'pipeline-carousel':
            return {'_type': 'playlist', 'id': post_id, 'title': 'Carousel',
                    'webpage_url': url,
                    'entries': [entry('-one', video), entry('-two', video, audio)]}
        raise yt_dlp.utils.DownloadError(f'No media for {post_id}')


class PipelineYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, params=None, **kwargs):
        super().__init__(params, **kwargs)
        for ie_key in [key for key in self._ies if 'instagram' in key.lower()]:
            del self._ies[ie_key]  # Keep the extraction fully offline.
        self.add_info_extractor(FakeInstagramIE())
        generic = self._ies.pop('Generic', None)
        if generic is not None:
            self._ies['Generic'] = generic  # Try the fake extractor first.


class ForbidExternalProcesses:
    """Fail the test if yt-dlp (or anything) tries to spawn FFmpeg."""

    def __init__(self):
        self._patchers = [
            patch.object(subprocess, 'Popen', side_effect=AssertionError('external process attempted')),
            patch.object(subprocess, 'run', side_effect=AssertionError('external process attempted')),
            patch.object(os, 'system', side_effect=AssertionError('external process attempted')),
        ]

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()

    def __exit__(self, *args):
        for patcher in self._patchers:
            patcher.stop()


class DownloadPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global SERVER_PORT
        cls.server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            functools.partial(MediaRequestHandler, directory=str(FIXTURES)))
        SERVER_PORT = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name, 'videos')
        self.links = Path(self.temp.name, 'links.txt')
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(ForbidExternalProcesses())
        with _RECORD_LOCK:
            RECORDED_REQUESTS.clear()

    def run_download(self, *post_ids, cookies=()):
        self.links.write_text(
            ''.join(f'https://www.instagram.com/p/{post_id}/\n' for post_id in post_ids),
            encoding='utf-8')
        with patch.object(download.yt_dlp, 'YoutubeDL', PipelineYoutubeDL):
            return download.start_download(str(self.output), list(cookies), str(self.links))

    def recorded_paths(self):
        with _RECORD_LOCK:
            return [request['path'] for request in RECORDED_REQUESTS]

    def test_separate_streams_download_over_http_and_combine(self):
        summary = self.run_download('pipeline-separate')
        self.assertEqual(summary, {'downloaded': 1, 'silent': 0, 'failed': []})
        published = Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4')
        self.assertTrue(published.is_file())
        info = remux.inspect_media(published)
        audio = info['audio']
        self.assertTrue(audio and audio['samples'])
        self.assertEqual(info['video']['entry'], 'avc1')
        self.assertEqual(
            sorted(set(path for path in self.recorded_paths() if not path.startswith('/missing'))),
            ['/audio.m4a', '/video-only.mp4'])

    def test_combined_format_downloads_one_file(self):
        summary = self.run_download('pipeline-combined')
        self.assertEqual(summary, {'downloaded': 1, 'silent': 0, 'failed': []})
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4').is_file())
        self.assertEqual([path for path in self.recorded_paths() if path != '/missing.m4a'],
                         ['/combined.mp4'])

    def test_silent_video_when_no_audio_format_exists(self):
        summary = self.run_download('pipeline-silent')
        self.assertEqual(summary, {'downloaded': 1, 'silent': 1, 'failed': []})
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4').is_file())
        self.assertFalse(remux.combined_mp4_has_audio(
            Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4')))

    def test_carousel_processes_entries_independently(self):
        summary = self.run_download('pipeline-carousel')
        self.assertEqual(summary['downloaded'], 2)
        self.assertEqual(summary['silent'], 1)
        self.assertEqual(summary['failed'], [])
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4').is_file())
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Pipeline Test (2).mp4').is_file())

    def test_failed_audio_keeps_video_in_recovery_and_reports(self):
        with self.assertRaisesRegex(Exception, '1 download\\(s\\) failed'):
            self.run_download('pipeline-audio-fails')
        recovered = list(Path(self.output, 'recovery').rglob('*.mp4'))
        self.assertEqual([path.name for path in recovered], ['NA_Pipeline Test.mp4'])
        self.assertFalse(Path(self.output, 'NA_Pipeline Test.mp4').is_file())
        self.assertFalse(
            Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4').is_file())
        self.assertIn('/missing.m4a', self.recorded_paths())

    def test_extraction_failure_is_recorded_and_batch_continues(self):
        with self.assertRaises(AppError) as raised:
            self.run_download('pipeline-broken', 'pipeline-silent')
        message = str(raised.exception)
        self.assertIn('1 download(s) failed', message)
        self.assertIn('https://www.instagram.com/p/pipeline-broken/', message)
        self.assertIn('check Instagram login and rate limits', message)
        self.assertTrue(Path(self.output, 'completed_videos', 'NA_Pipeline Test.mp4').is_file())

    def test_cookies_are_never_sent_outside_instagram(self):
        from http.cookiejar import Cookie
        cookie = Cookie(
            version=0, name='sessionid', value='test-session', port=None,
            port_specified=False, domain='.instagram.com', domain_specified=True,
            domain_initial_dot=True, path='/', path_specified=True, secure=True,
            expires=None, discard=True, comment=None, comment_url=None,
            rest={'HttpOnly': None}, rfc2109=False)
        self.run_download('pipeline-combined', cookies=[cookie])
        with _RECORD_LOCK:
            cookies = [request['cookie'] for request in RECORDED_REQUESTS]
        self.assertTrue(cookies)
        self.assertTrue(all(value is None for value in cookies),
                        'Instagram cookies must not leak to non-Instagram hosts')

    def test_download_failure_does_not_leave_work_directory(self):
        with self.assertRaises(Exception):
            self.run_download('pipeline-broken')
        self.assertNotIn('.scoopy-work', os.listdir(self.output))


if __name__ == '__main__':
    unittest.main()
