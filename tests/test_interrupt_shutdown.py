import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import interrupts, ui


class InterruptHandlerTests(unittest.TestCase):
    def setUp(self):
        self.exit_process = Mock()
        self.fake_os = SimpleNamespace(
            name="nt",
            environ={"SystemRoot": r"C:\Windows"},
            getpid=Mock(return_value=4321),
            _exit=self.exit_process,
        )
        self.os_patch = patch.object(interrupts, "os", self.fake_os)
        self.os_patch.start()
        self.addCleanup(self.os_patch.stop)
        self.timer_patch = patch.object(interrupts.threading, "Timer")
        self.timer = self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.run_patch = patch.object(interrupts.subprocess, "run")
        self.run = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.flags_patch = patch.object(
            interrupts.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
        )
        self.flags_patch.start()
        self.addCleanup(self.flags_patch.stop)

    def test_windows_interrupt_targets_only_own_pid_tree_with_bounded_wait(self):
        interrupts.make_interrupt_handler()(signal.SIGINT, None)

        command = self.run.call_args.args[0]
        expected_executable = Path(r"C:\Windows") / "System32" / "taskkill.exe"
        self.assertEqual(
            command, [str(expected_executable), "/PID", "4321", "/T", "/F"]
        )
        options = self.run.call_args.kwargs
        self.assertEqual(options["timeout"], interrupts.TREE_TIMEOUT_SECONDS)
        self.assertIs(options["check"], False)
        self.assertNotIn("shell", options)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[stream], subprocess.DEVNULL)
        self.exit_process.assert_called_once_with(130)

    def test_watchdog_is_daemon_and_exits_independently(self):
        interrupts.make_interrupt_handler()(signal.SIGINT, None)

        self.timer.assert_called_once_with(
            interrupts.INTERRUPT_TIMEOUT_SECONDS, self.exit_process, args=(130,)
        )
        self.assertTrue(self.timer.return_value.daemon)
        self.timer.return_value.start.assert_called_once_with()
        callback = self.timer.call_args.args[1]
        callback(*self.timer.call_args.kwargs["args"])
        self.assertEqual(self.exit_process.call_count, 2)

    def test_repeated_interrupt_skips_duplicate_cleanup(self):
        handler = interrupts.make_interrupt_handler()
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        self.run.assert_called_once()
        self.timer.assert_called_once()
        self.assertEqual(self.exit_process.call_count, 2)

    def test_cleanup_errors_always_exit_without_logging(self):
        for error in (OSError("private details"), subprocess.TimeoutExpired("taskkill", 1)):
            with self.subTest(error_type=type(error).__name__):
                self.exit_process.reset_mock()
                self.run.side_effect = error
                with patch("builtins.print") as output:
                    interrupts.make_interrupt_handler()(signal.SIGINT, None)
                self.exit_process.assert_called_once_with(130)
                output.assert_not_called()

    def test_unexpected_cleanup_error_still_exits(self):
        self.run.side_effect = RuntimeError("unexpected")
        with self.assertRaises(RuntimeError):
            interrupts.make_interrupt_handler()(signal.SIGINT, None)
        self.exit_process.assert_called_once_with(130)

    def test_watchdog_start_failure_still_exits(self):
        self.timer.return_value.start.side_effect = RuntimeError("cannot start thread")
        with self.assertRaises(RuntimeError):
            interrupts.make_interrupt_handler()(signal.SIGINT, None)
        self.exit_process.assert_called_once_with(130)
        self.run.assert_not_called()

    def test_non_windows_exits_without_guessing_process_ownership(self):
        self.fake_os.name = "posix"
        interrupts.make_interrupt_handler()(signal.SIGINT, None)
        self.exit_process.assert_called_once_with(130)
        self.run.assert_not_called()
        self.timer.assert_not_called()

    def test_failed_taskkill_status_does_not_prevent_exit(self):
        self.run.return_value = SimpleNamespace(returncode=1)
        interrupts.make_interrupt_handler()(signal.SIGINT, None)
        self.exit_process.assert_called_once_with(130)


class MainInterruptIntegrationTests(unittest.TestCase):
    def test_main_registers_independent_handler_before_constructing_tk(self):
        app = Mock()
        registered = []
        fake_os = SimpleNamespace(name="posix", _exit=Mock())

        def register(signum, handler):
            self.assertEqual(signum, signal.SIGINT)
            registered.append(handler)

        def construct_app():
            self.assertEqual(len(registered), 1)
            return app

        app.mainloop.side_effect = lambda: registered[0](signal.SIGINT, None)
        with (
            patch.object(interrupts, "os", fake_os),
            patch.object(ui.signal, "signal", side_effect=register),
            patch.object(ui, "_set_windows_taskbar_identity"),
            patch.object(ui, "DownloaderApp", side_effect=construct_app),
        ):
            ui.main()
        fake_os._exit.assert_called_once_with(130)
        app.after.assert_not_called()
        app.on_closing.assert_not_called()
        app.protocol.assert_called_once_with("WM_DELETE_WINDOW", app.on_closing)
        app.mainloop.assert_called_once_with()

    def test_unavailable_signal_registration_does_not_prevent_startup(self):
        for error in (OSError("unavailable"), ValueError("not main thread")):
            with self.subTest(error_type=type(error).__name__):
                app = Mock()
                with (
                    patch.object(ui.signal, "signal", side_effect=error),
                    patch.object(ui, "_set_windows_taskbar_identity"),
                    patch.object(ui, "DownloaderApp", return_value=app),
                ):
                    ui.main()
                app.mainloop.assert_called_once_with()


class IsolatedInterruptProbeTests(unittest.TestCase):
    def test_watchdog_exits_own_child_when_cleanup_never_returns(self):
        # The only real exit in this suite occurs inside this owned Python child.
        # Simulate the Windows cleanup stall without running taskkill anywhere.
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        code = (
            "import os, signal, sys, time\n"
            "from types import SimpleNamespace\n"
            f"sys.path.insert(0, {source_root!r})\n"
            "from scoopy import interrupts\n"
            "interrupts.os = SimpleNamespace(name='nt', _exit=os._exit)\n"
            "interrupts.INTERRUPT_TIMEOUT_SECONDS = 0.15\n"
            "interrupts._terminate_windows_tree = lambda: time.sleep(30)\n"
            "signal.signal(signal.SIGINT, interrupts.make_interrupt_handler())\n"
            "signal.raise_signal(signal.SIGINT)\n"
        )
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # Only this test's own child; never any browser process.
            process.communicate(timeout=5)
            self.fail("Owned interrupt probe did not exit within five seconds.")
        self.assertEqual(process.returncode, 130, stderr.decode(errors="replace"))
        self.assertEqual(stdout, b"")
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
