"""Offline, headless regressions for download completion and Tk queue dispatch."""

import contextlib
import io
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from scoopy import ui


class AppHarness:
    """Use production operation methods without creating a Tk interpreter."""

    download_videos = ui.DownloaderApp.download_videos
    start_download_thread = ui.DownloaderApp.start_download_thread
    start_operation = ui.DownloaderApp.start_operation
    check_debug_queue = ui.DownloaderApp.check_debug_queue

    def __init__(self):
        self.driver = object()
        self.folder_name_var = Mock()
        self.folder_name_var.get.return_value = "videos"
        self.busy = False
        self.closing = threading.Event()
        self.ui_queue = queue.Queue()
        self.debug_queue = queue.Queue()
        self.exists = True
        self.scheduled = []
        self.button_states = []
        self.rendered = []
        self.tk_threads = []

    def winfo_exists(self):
        self.tk_threads.append(threading.get_ident())
        return self.exists

    def update_buttons(self):
        self.tk_threads.append(threading.get_ident())
        self.button_states.append(self.busy)

    def write_to_debug(self, message):
        self.tk_threads.append(threading.get_ident())
        self.rendered.append(message)

    def after(self, delay, callback):
        self.tk_threads.append(threading.get_ident())
        self.scheduled.append((delay, callback))


class ContinuousQueue:
    """Deterministically model a producer that always replaces consumed work."""

    def __init__(self, item):
        self.item = item
        self.consumed = 0

    def get_nowait(self):
        self.consumed += 1
        return self.item


class UiCompletionTests(unittest.TestCase):
    def setUp(self):
        self.app = AppHarness()
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.info = patch.object(ui.messagebox, "showinfo").start()
        self.error = patch.object(ui.messagebox, "showerror").start()

    def test_actual_download_action_worker_wrapper_and_success_popup(self):
        """Only the session/downloader/filesystem boundary is mocked."""
        summary = {"downloaded": 3, "silent": 1, "failed": []}
        cookies = [{"name": "sessionid", "value": "PRIVATE_SESSION"}]
        main_thread = threading.get_ident()
        worker_threads = []

        def download(path, passed_cookies):
            worker_threads.append(threading.get_ident())
            self.assertIs(passed_cookies, cookies)
            self.assertEqual(passed_cookies[0]["value"], "PRIVATE_SESSION")
            return summary

        with patch.object(ui, "read_browser_cookies", return_value=cookies), \
                patch.object(ui, "start_download", side_effect=download), \
                patch.object(ui.os.path, "exists", return_value=True), \
                patch.object(ui.os.path, "getsize", return_value=12):
            self.app.start_download_thread()
            self.assertTrue(self.app.busy)
            # Block only this test thread until the real worker posts its result.
            completion = self.app.ui_queue.get(timeout=2)
        self.assertIs(completion[0], summary)
        self.assertIsNone(completion[1])
        self.assertEqual(cookies, [])
        self.app.ui_queue.put(completion)
        self.app.check_debug_queue()
        self.info.assert_called_once_with(
            "Download Complete", "Downloaded 3 videos (1 without audio).")
        self.error.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.button_states, [True, False])
        self.assertEqual(len(self.app.scheduled), 1)
        self.assertTrue(all(tid == main_thread for tid in self.app.tk_threads))
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], main_thread)
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_wrapper_clears_cookies_when_downloader_raises(self):
        cookies = [{"value": "PRIVATE_SESSION"}]
        failure = RuntimeError("sessionid=PRIVATE_SESSION")
        with patch.object(ui, "read_browser_cookies", return_value=cookies), \
                patch.object(ui, "start_download", side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                self.app.download_videos("unused-offline")
        self.assertIs(caught.exception, failure)
        self.assertEqual(cookies, [])
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_worker_error_is_sanitized_and_no_success_popup(self):
        cookies = [{"value": "PRIVATE_SESSION"}]
        with patch.object(ui, "read_browser_cookies", return_value=cookies), \
                patch.object(ui, "start_download",
                             side_effect=RuntimeError("sessionid=PRIVATE_SESSION")):
            self.app.start_operation(
                lambda: self.app.download_videos("unused-offline"), self.info)
            completion = self.app.ui_queue.get(timeout=2)
        self.app.ui_queue.put(completion)
        self.app.check_debug_queue()
        self.assertEqual(cookies, [])
        self.assertFalse(self.app.busy)
        self.info.assert_not_called()
        self.error.assert_called_once()
        self.assertNotIn("PRIVATE_SESSION", str(self.error.call_args))
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())
        self.assertEqual(len(self.app.scheduled), 1)

    def test_bad_callback_then_subsequent_real_operation_succeeds(self):
        bad_callback = Mock(side_effect=RuntimeError("sessionid=PRIVATE_SESSION"))
        self.app.busy = True
        self.app.ui_queue.put((None, None, bad_callback))
        self.app.check_debug_queue()
        self.assertFalse(self.app.busy)
        self.error.assert_called_once()
        callback = Mock()
        self.app.start_operation(lambda: 42, callback)
        completion = self.app.ui_queue.get(timeout=2)
        self.app.ui_queue.put(completion)
        self.app.scheduled.pop()[1]()
        callback.assert_called_once_with(42)
        self.assertFalse(self.app.busy)
        self.assertEqual(len(self.app.scheduled), 1)
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())
        self.assertNotIn("PRIVATE_SESSION", str(self.error.call_args))

    def test_failed_error_dialog_does_not_stop_later_callback(self):
        self.error.side_effect = RuntimeError("PRIVATE_SESSION")
        callback = Mock()
        self.app.ui_queue.put((None, "A safe operation failure.", callback))
        self.app.ui_queue.put((7, None, callback))
        self.app.busy = True
        self.app.check_debug_queue()
        callback.assert_called_once_with(7)
        self.assertFalse(self.app.busy)
        self.assertEqual(len(self.app.scheduled), 1)
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_button_refresh_failure_does_not_stop_pump(self):
        self.app.update_buttons = Mock(side_effect=[
            RuntimeError("PRIVATE_SESSION"), None])
        callback = Mock()
        self.app.ui_queue.put((1, None, callback))
        self.app.ui_queue.put((2, None, callback))
        self.app.busy = True
        self.app.check_debug_queue()
        callback.assert_called_once_with(2)
        self.assertFalse(self.app.busy)
        self.assertEqual(len(self.app.scheduled), 1)
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_continuous_pressure_on_both_queues_is_bounded_per_tick(self):
        callback = Mock()
        self.app.ui_queue = ContinuousQueue((5, None, callback))
        self.app.debug_queue = ContinuousQueue("progress\n")
        self.app.check_debug_queue()
        self.assertEqual(self.app.ui_queue.consumed, 20)
        self.assertEqual(self.app.debug_queue.consumed, 100)
        self.assertEqual(callback.call_count, 20)
        self.assertEqual(len(self.app.rendered), 100)
        self.app.scheduled.pop()[1]()
        self.assertEqual(self.app.ui_queue.consumed, 40)
        self.assertEqual(self.app.debug_queue.consumed, 200)
        self.assertEqual(len(self.app.scheduled), 1)

    def test_finite_queue_backlogs_remain_for_next_tick(self):
        callback = Mock()
        for number in range(25):
            self.app.ui_queue.put((number, None, callback))
        for number in range(105):
            self.app.debug_queue.put(str(number))
        self.app.check_debug_queue()
        self.assertEqual(self.app.ui_queue.qsize(), 5)
        self.assertEqual(self.app.debug_queue.qsize(), 5)
        self.app.scheduled.pop()[1]()
        self.assertTrue(self.app.ui_queue.empty())
        self.assertTrue(self.app.debug_queue.empty())
        self.assertEqual(callback.call_count, 25)
        self.assertEqual(len(self.app.rendered), 105)

    def test_log_render_failure_still_reschedules(self):
        self.app.write_to_debug = Mock(side_effect=RuntimeError("PRIVATE_SESSION"))
        self.app.debug_queue.put("entry")
        self.app.debug_queue.put("next entry")
        self.app.check_debug_queue()
        self.assertEqual(self.app.debug_queue.qsize(), 1)
        self.assertEqual(len(self.app.scheduled), 1)
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_no_dispatch_or_rescheduling_when_already_closed_or_destroyed(self):
        for state in ("closing", "destroyed", "interpreter gone"):
            with self.subTest(state=state):
                app = AppHarness()
                callback = Mock()
                app.ui_queue.put((1, None, callback))
                app.debug_queue.put("entry")
                if state == "closing":
                    app.closing.set()
                elif state == "destroyed":
                    app.exists = False
                else:
                    app.winfo_exists = Mock(side_effect=ui.tk.TclError("destroyed"))
                app.check_debug_queue()
                callback.assert_not_called()
                self.assertEqual(app.ui_queue.qsize(), 1)
                self.assertEqual(app.debug_queue.qsize(), 1)
                self.assertEqual(app.scheduled, [])

    def test_close_inside_callback_stops_dispatch_and_rescheduling(self):
        callback = Mock()
        self.app.ui_queue.put((None, None, lambda _: self.app.closing.set()))
        self.app.ui_queue.put((1, None, callback))
        self.app.debug_queue.put("entry")
        self.app.check_debug_queue()
        callback.assert_not_called()
        self.assertEqual(self.app.ui_queue.qsize(), 1)
        self.assertEqual(self.app.debug_queue.qsize(), 1)
        self.assertEqual(self.app.scheduled, [])

    def test_destroy_inside_callback_error_skips_error_dialog_and_reschedule(self):
        def destroy_then_fail(_):
            self.app.exists = False
            raise RuntimeError("PRIVATE_SESSION")

        self.app.ui_queue.put((None, None, destroy_then_fail))
        self.app.check_debug_queue()
        self.error.assert_not_called()
        self.assertEqual(self.app.scheduled, [])
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())

    def test_shutdown_during_log_render_stops_remaining_work(self):
        self.app.write_to_debug = Mock(side_effect=lambda _: self.app.closing.set())
        self.app.debug_queue.put("entry")
        self.app.debug_queue.put("next entry")
        self.app.check_debug_queue()
        self.assertEqual(self.app.debug_queue.qsize(), 1)
        self.assertEqual(self.app.scheduled, [])

    def test_destroy_racing_with_after_is_handled(self):
        self.app.after = Mock(side_effect=ui.tk.TclError("application destroyed"))
        self.app.check_debug_queue()
        self.app.after.assert_called_once()
        self.error.assert_not_called()

    def test_summary_pluralization_and_zero_counts(self):
        for downloaded, silent, expected in (
            (0, 0, "Downloaded 0 videos (0 without audio)."),
            (1, 1, "Downloaded 1 video (1 without audio)."),
            (2, 2, "Downloaded 2 videos (2 without audio)."),
        ):
            with self.subTest(downloaded=downloaded, silent=silent):
                self.assertEqual(ui.download_summary({
                    "downloaded": downloaded, "silent": silent, "failed": [],
                }), expected)

    def test_invalid_or_failed_summaries_cannot_be_reported_as_success(self):
        for summary in (
            None, {}, {"downloaded": "PRIVATE_SESSION", "silent": 0},
            {"downloaded": True, "silent": 0},
            {"downloaded": -1, "silent": 0},
            {"downloaded": 1, "silent": 2},
            {"downloaded": 1, "silent": 0, "failed": ["PRIVATE_SESSION"]},
        ):
            with self.subTest(summary=summary):
                with self.assertRaisesRegex(ValueError, "^Invalid download summary\\.$"):
                    ui.download_summary(summary)


if __name__ == "__main__":
    unittest.main()
