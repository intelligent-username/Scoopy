"""Offline shutdown tests: real threads, mocked process termination, no Tk root."""

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import browser_cleanup as cleanup, ui


def app_with(driver=None, browser_was_open=False):
    return SimpleNamespace(
        closing=threading.Event(), driver_lock=threading.Lock(),
        driver=driver, browser_was_open=browser_was_open,
        destroy=Mock(), monitor_login=Mock(),
    )


class BoundedCleanupTests(unittest.TestCase):
    def test_graceful_quit_and_absent_driver(self):
        driver = SimpleNamespace(quit=Mock())
        self.assertTrue(ui.shutdown_driver(driver))
        driver.quit.assert_called_once()
        self.assertFalse(ui.shutdown_driver(None))

    def test_quit_failure_returns_false(self):
        driver = SimpleNamespace(quit=Mock(side_effect=RuntimeError("secret")))
        with patch("builtins.print") as output:
            self.assertFalse(ui.shutdown_driver(driver))
        self.assertNotIn("secret", str(output.call_args_list))

    def test_hung_quit_has_real_thread_deadline(self):
        release = threading.Event()
        finished = threading.Event()

        def quit_driver():
            release.wait()
            finished.set()

        try:
            with patch.object(cleanup, "QUIT_TIMEOUT", 0.05):
                started = time.monotonic()
                self.assertFalse(ui.shutdown_driver(SimpleNamespace(quit=quit_driver)))
                self.assertLess(time.monotonic() - started, 0.8)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_supervisor_is_non_daemon_but_bounded_even_for_stuck_cleanup(self):
        release = threading.Event()
        with patch.object(cleanup, "CLEANUP_TIMEOUT", 0.05):
            supervisor = cleanup.start_cleanup(release.wait)
            try:
                self.assertFalse(supervisor.daemon)
                supervisor.join(1)
                self.assertFalse(supervisor.is_alive())
            finally:
                release.set()

    def test_force_timeout_is_bounded_even_if_subprocess_does_not_return(self):
        release = threading.Event()
        finished = threading.Event()

        def stuck_run(*args, **kwargs):
            release.wait()
            finished.set()

        with patch.object(cleanup.platform, "system", return_value="Windows"), \
                patch.object(cleanup, "FORCE_TIMEOUT", 0.05), \
                patch.object(cleanup.subprocess, "run", side_effect=stuck_run):
            try:
                started = time.monotonic()
                self.assertFalse(cleanup.close_profile_browser(r"C:\Owned Profile"))
                self.assertLess(time.monotonic() - started, 0.8)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))

    def test_profile_force_command_is_literal_and_pid_tree_scoped(self):
        profile = r"C:\Owned [Profile]\it's"
        with patch.object(cleanup.platform, "system", return_value="Windows"), \
                patch.object(cleanup.subprocess, "run") as run:
            self.assertTrue(cleanup.close_profile_browser(profile))
        args, kwargs = run.call_args
        script = args[0][-1]
        self.assertIn("it''s", script)
        self.assertIn("[regex]::Escape($profile)", script)
        self.assertIn("(?=\\s|$)", script)
        self.assertIn("taskkill.exe /PID $_.ProcessId /T /F", script)
        self.assertNotIn("-like", script)
        self.assertNotIn("/IM", script)
        self.assertEqual(kwargs["timeout"], cleanup.FORCE_TIMEOUT)
        self.assertTrue(kwargs["check"])

    def test_non_windows_never_invokes_process_termination(self):
        with patch.object(cleanup.platform, "system", return_value="Linux"), \
                patch.object(cleanup.subprocess, "run") as run:
            self.assertFalse(cleanup.close_profile_browser("/owned/profile"))
        run.assert_not_called()

    def test_supervisor_survives_child_main_thread_exit(self):
        # This is our own Python child only. It opens no browser/profile/files.
        source = (
            "import time\n"
            "from scoopy.browser_cleanup import start_cleanup\n"
            "def finish():\n"
            "    time.sleep(0.2)\n"
            "    print('cleanup-finished', flush=True)\n"
            "start_cleanup(finish)\n"
            "print('main-finished', flush=True)\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-B", "-c", source], env=environment,
            capture_output=True, text=True, timeout=5, check=True,
        )
        self.assertIn("main-finished", result.stdout)
        self.assertIn("cleanup-finished", result.stdout)

    def test_hung_cleanup_does_not_prevent_child_interpreter_exit(self):
        source = (
            "import threading\n"
            "from scoopy import browser_cleanup as cleanup\n"
            "cleanup.CLEANUP_TIMEOUT = 0.1\n"
            "cleanup.start_cleanup(threading.Event().wait)\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-B", "-c", source], env=environment,
            capture_output=True, text=True, timeout=5, check=True,
        )
        self.assertEqual(result.returncode, 0)


class WindowCloseTests(unittest.TestCase):
    def test_close_is_prompt_idempotent_and_worker_never_destroys_tk(self):
        release = threading.Event()
        driver = SimpleNamespace(quit=Mock(side_effect=lambda: release.wait()))
        app = app_with(driver, True)
        main_thread = threading.get_ident()
        destroy_threads = []
        app.destroy.side_effect = lambda: destroy_threads.append(threading.get_ident())
        with patch.object(cleanup, "QUIT_TIMEOUT", 0.05), \
                patch.object(ui, "close_orphan_profile_chrome") as force:
            try:
                started = time.monotonic()
                ui.DownloaderApp.on_closing(app)
                ui.DownloaderApp.on_closing(app)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertTrue(app.closing.is_set())
                self.assertIsNone(app.driver)
                deadline = time.monotonic() + 1
                while not force.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                force.assert_called_once()
                driver.quit.assert_called_once()
                app.destroy.assert_called_once()
                self.assertEqual(destroy_threads, [main_thread])
            finally:
                release.set()

    def test_close_without_driver_scans_only_when_launch_was_attempted(self):
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                app = app_with(browser_was_open=attempted)
                done = threading.Event()

                def run_cleanup(action):
                    def work():
                        try:
                            action()
                        finally:
                            done.set()
                    worker = threading.Thread(target=work, daemon=False)
                    worker.start()
                    return worker

                with patch.object(cleanup, "start_cleanup", side_effect=run_cleanup), \
                        patch.object(ui, "close_orphan_profile_chrome") as force:
                    ui.DownloaderApp.on_closing(app)
                    self.assertTrue(done.wait(1))
                    self.assertEqual(force.call_count, int(attempted))

    def test_open_after_close_never_launches(self):
        app = app_with()
        app.closing.set()
        with patch.object(ui, "start_driver") as launch:
            ui.DownloaderApp.open_browser(app)
        launch.assert_not_called()

    def test_driver_returned_after_close_is_not_published_and_is_cleaned(self):
        app = app_with()
        driver = SimpleNamespace(quit=Mock())
        returned = threading.Event()
        finished = threading.Event()

        def launch():
            self.assertTrue(app.browser_was_open)
            returned.set()
            finished.wait(1)
            return driver

        with patch.object(ui, "start_driver", side_effect=launch), \
                patch.object(ui, "close_orphan_profile_chrome"):
            worker = threading.Thread(target=ui.DownloaderApp.open_browser, args=(app,))
            worker.start()
            self.assertTrue(returned.wait(1))
            ui.DownloaderApp.on_closing(app)
            finished.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            deadline = time.monotonic() + 1
            while not driver.quit.called and time.monotonic() < deadline:
                time.sleep(0.01)
            driver.quit.assert_called_once()
        self.assertIsNone(app.driver)
        app.monitor_login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
