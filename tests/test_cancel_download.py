"""Focused offline cancellation state and partial-component regressions."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from scoopy import download
from scoopy.cancellation import OperationCancelled


class CancelDownloadTests(unittest.TestCase):
    def test_cancelled_carousel_continue_and_retry_use_separate_queues(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            urls = [f'https://www.instagram.com/p/{name}/' for name in ('bad', 'album', 'later')]
            links = Path(folder, 'links.txt')
            links.write_text('\n'.join(urls), encoding='utf-8')
            state = download.DownloadRetryState()
            event = Event()
            attempted = []
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl

            def extract(url, download=False):
                if url == urls[0]:
                    raise download_module_error('fixture')
                return {'entries': [{'id': 'one'}, {'id': 'two'}]} if url == urls[1] else {'id': 'last'}

            def save(entry, output, work, cookies, index, *, cancel_event=None):
                attempted.append(entry['id'])
                path = Path(work, 'saved.mp4')
                path.write_bytes(b'fixture')
                published = download._publish(str(path), output, cancel_event=cancel_event)
                if entry['id'] == 'one' and cancel_event is not None:
                    event.set()
                return published, False

            download_module_error = download.yt_dlp.utils.DownloadError
            ydl.extract_info.side_effect = extract
            with patch.object(download.yt_dlp, 'YoutubeDL', return_value=ydl), \
                    patch.object(download, '_download_entry', side_effect=save):
                with self.assertRaisesRegex(
                        OperationCancelled, 'Download cancelled — 1 saved, 1 failed, 2 remaining'):
                    download.start_download(folder, [], links, retry_state=state, cancel_event=event)
                self.assertEqual(state.failed_urls, urls[:1])
                self.assertEqual(state.remaining_urls, urls[1:])
                self.assertEqual(state.completed_entries[urls[1]], {'one'})
                event.clear()
                with patch.object(download, '_read_link_lines', side_effect=AssertionError('file read')):
                    download.start_download(folder, [], retry_state=state,
                                            continue_remaining=True, cancel_event=event)
                    self.assertEqual(attempted, ['one', 'two', 'last'])
                    self.assertEqual(state.failed_urls, urls[:1])
                    self.assertEqual(state.remaining_urls, [])
                    ydl.extract_info.side_effect = lambda *args, **kwargs: {'id': 'retried'}
                    download.start_download(folder, [], retry_state=state, retry_failed=True)
                self.assertEqual(attempted, ['one', 'two', 'last', 'retried'])
                self.assertEqual(state.failed_urls, [])
                self.assertEqual(len(list(Path(folder, 'completed_videos').glob('*.mp4'))), 4)

    def test_wrapped_stream_cancel_recovers_partial_without_failure_or_publication(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            event = Event()
            summary = {'downloaded': 0, 'silent': 0, 'failed': []}
            work = os.path.join(folder, download.WORK_DIR_NAME)
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl

            def interrupted(*args, **kwargs):
                Path(work, 'entry-0001', 'video.mp4.part').write_bytes(b'partial')
                event.set()
                raise download.yt_dlp.utils.DownloadError('unsafe signed URL fixture')

            ydl.process_ie_result.side_effect = interrupted
            entry = {'id': 'photo', '_scoopy_photo': True}
            with patch.object(download.yt_dlp, 'YoutubeDL', return_value=ydl), \
                    patch.object(download, '_publish') as publish:
                with self.assertRaises(OperationCancelled):
                    download._process_entry(entry, folder, work, [], summary, 1, cancel_event=event)
                publish.assert_not_called()
            self.assertEqual(summary, {'downloaded': 0, 'silent': 0, 'failed': []})
            self.assertEqual(Path(folder, 'recovery', 'entry-0001', 'video.mp4.part').read_bytes(),
                             b'partial')


if __name__ == '__main__':
    unittest.main()
