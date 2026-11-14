from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from scoopy import browser, cancellation, remux


class CancellationCoreTests(unittest.TestCase):
    def test_browser_hydration_wait_cancels_without_touching_saved_links(self):
        event = Event()
        cancellation.check_cancel(None)
        with patch.object(cancellation.time, 'sleep') as sleep:
            cancellation.wait_cancel(None, 0.25)
            sleep.assert_called_once_with(0.25)
        driver = Mock()
        driver.find_elements.return_value = []
        with TemporaryDirectory() as directory:
            links = Path(directory, 'links.txt')
            links.write_text('previous links\n', encoding='utf-8')

            def cancel_wait(seconds):
                event.set()
                return True

            with patch.object(browser, 'LINKS_FILE', str(links)), \
                    patch.object(event, 'wait', side_effect=cancel_wait) as wait:
                with self.assertRaisesRegex(
                        cancellation.OperationCancelled, '^Operation cancelled$'):
                    browser.collect_reel_links(driver, cancel_event=event)
            wait.assert_called_once()
            driver.execute_script.assert_not_called()
            self.assertEqual(links.read_text(encoding='utf-8'), 'previous links\n')
            driver.reset_mock()
            with self.assertRaises(cancellation.OperationCancelled):
                browser.collect_reel_links(driver, cancel_event=event)
            driver.find_elements.assert_not_called()

    def test_remux_sample_cancellation_cleans_only_temporary_output(self):
        event = Event()
        with TemporaryDirectory() as directory, ExitStack() as stack:
            video = Path(directory, 'video.mp4')
            audio = Path(directory, 'audio.m4a')
            output = Path(directory, 'temporary.mp4')
            video.write_bytes(b'video component')
            audio.write_bytes(b'audio component')
            stack.enter_context(patch.object(remux, 'validate_video_stream'))
            stack.enter_context(patch.object(remux, 'validate_audio_stream'))
            validate = stack.enter_context(patch.object(remux, '_validate_output'))
            for name in (
                    'MP4Read', 'MP4Modify', 'MP4FindTrackId',
                    'MP4GetTrackFixedSampleDuration', 'MP4GetTrackTimeScale',
                    'MP4AddAudioTrack', 'MP4GetTrackESConfiguration',
                    'MP4SetTrackESConfiguration', 'MP4GetTrackLanguage',
                    'MP4SetTrackLanguage'):
                stack.enter_context(patch.object(remux.raw, name))
            stack.enter_context(patch.object(
                remux.raw, 'MP4GetTrackNumberOfSamples', return_value=3))
            copy = stack.enter_context(patch.object(
                remux.raw, 'MP4CopySample', side_effect=lambda *args: event.set()))
            with self.assertRaises(cancellation.OperationCancelled):
                remux.remux_to_mp4(video, audio, output, cancel_event=event)
            copy.assert_called_once()
            validate.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(video.read_bytes(), b'video component')
            self.assertEqual(audio.read_bytes(), b'audio component')


if __name__ == '__main__':
    unittest.main()
