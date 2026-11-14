"""Focused offline cancellation checks using real Tk controls and held workers."""

import os
import queue
import threading
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scoopy import ui


class CancelUiTests(unittest.TestCase):
    def setUp(self):
        self.app = ui.DownloaderApp.__new__(ui.DownloaderApp)
        tk.Tk.__init__(self.app)
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.app.driver = object()
        self.app.busy = False
        self.app.operation_event = None
        self.app.closing = threading.Event()
        self.app.retry_states = {}
        self.app.ui_queue = queue.Queue()
        self.app.debug_queue = queue.Queue()
        self.app.setup_ui()
        self.app.after = Mock()
        self.app.update_buttons()

    def test_cancel_waits_for_worker_completion_and_next_event_is_fresh(self):
        app = self.app
        workers = []
        success = Mock()
        cookies = [{"value": "offline-session"}]
        browser = app.driver
        main_thread = threading.get_ident()

        def download(*args, **options):
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertIs(options["cancel_event"], event)
            ui.check_cancel(options["cancel_event"])

        with patch.object(ui.threading, "Thread") as thread, \
                patch.object(ui, "read_browser_cookies", return_value=cookies), \
                patch.object(ui, "start_download", side_effect=download), \
                patch.object(ui.messagebox, "showinfo") as info, \
                patch.object(ui.messagebox, "showerror") as error:
            thread.side_effect = lambda **kw: SimpleNamespace(
                start=lambda: workers.append(kw["target"]))
            app.start_operation(
                lambda event: app.download_videos("offline", cancel_event=event),
                success, cancellable=True)
            event = app.operation_event
            self.assertEqual(app.cancel_button.winfo_manager(), "pack")
            app.cancel_operation()
            self.assertTrue(event.is_set())
            self.assertTrue(app.busy)
            self.assertEqual(app.cancel_button.cget("text"), "Cancelling…")
            self.assertTrue(app.cancel_button.instate(["disabled"]))
            # Let the held worker reach cookie cleanup after cancellation.
            with patch.object(ui, "check_cancel", side_effect=[None, ui.OperationCancelled()]):
                workers.pop()()
            self.assertTrue(app.busy)
            self.assertEqual(cookies, [])
            app.check_debug_queue()
            self.assertFalse(app.busy)
            self.assertIsNone(app.operation_event)
            self.assertEqual(app.cancel_button.winfo_manager(), "")
            self.assertIs(app.driver, browser)
            success.assert_not_called()
            error.assert_not_called()
            info.assert_called_once_with("Operation cancelled", "Operation cancelled")
            app.start_operation(lambda event: None, success, cancellable=True)
            self.assertIsNot(app.operation_event, event)
            self.assertFalse(app.operation_event.is_set())
            workers.pop()()
            app.check_debug_queue()
            app.start_operation(lambda: 42, success)
            self.assertIsNone(app.operation_event)
            self.assertEqual(app.cancel_button.winfo_manager(), "")
            workers.pop()()
            app.check_debug_queue()
            success.assert_called_with(42)

    def test_folder_scoped_continue_retry_and_cancel_before_saving_links(self):
        app = self.app
        directory = os.path.abspath(os.path.join(ui.BASE_DIR, "videos"))
        app.retry_states[os.path.normcase(directory)] = SimpleNamespace(
            failed_urls=["failed"], remaining_urls=["pending", "interrupted"])
        app.update_buttons()
        self.assertEqual(app.retry_button.cget("text"), "Retry failed (1)")
        self.assertEqual(app.continue_button.cget("text"), "Continue remaining (2)")
        with patch.object(app, "start_operation") as start, \
                patch.object(app, "download_videos") as download:
            app.start_continue_remaining_thread()
            event = threading.Event()
            start.call_args.args[0](event)
            download.assert_called_once_with(
                directory, continue_remaining=True, cancel_event=event)
            self.assertTrue(start.call_args.kwargs["cancellable"])
            download.reset_mock()
            app.start_retry_failed_thread()
            start.call_args.args[0](event)
            download.assert_called_once_with(
                directory, retry_failed=True, cancel_event=event)
            app.busy = True
            app.update_buttons()
            self.assertTrue(app.continue_button.instate(["disabled"]))
            start.reset_mock()
            app.start_continue_remaining_thread()
            start.assert_not_called()
            app.busy = False
            app.driver = None
            app.update_buttons()
            self.assertTrue(app.continue_button.instate(["disabled"]))
            app.folder_name_var.set("other")
            self.assertEqual(app.continue_button.winfo_manager(), "")
            self.assertEqual(app.retry_button.winfo_manager(), "")

        app.driver = object()
        event = threading.Event()
        cookies = [{"value": "offline-session"}]

        def collect(driver, *, cancel_event):
            self.assertIs(cancel_event, event)
            event.set()
            return ["offline-post"]

        with patch.object(ui, "read_browser_cookies", return_value=cookies), \
                patch.object(ui, "collect_reel_links", side_effect=collect), \
                patch.object(ui, "save_links") as save:
            with self.assertRaises(ui.OperationCancelled):
                app.scrape_links(event)
            save.assert_not_called()
            self.assertEqual(cookies, [])


if __name__ == "__main__":
    unittest.main()
