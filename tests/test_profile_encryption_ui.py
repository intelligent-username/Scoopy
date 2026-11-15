"""Offline UI tests for profile encryption flows: no Tk root, no browser, no network."""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, PropertyMock

import tkinter as tk

from scoopy import ui
from scoopy import profile_crypto
from scoopy import browser_cleanup


class InlineThread:
    """Run workers deterministically; UI events must still wait for queue processing."""

    def __init__(self, target, daemon, args=None, kwargs=None):
        self.target = target
        self.args = args or ()
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class ProfileEncryptionUITests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            driver=None, busy=False, ui_queue=queue.Queue(), debug_queue=queue.Queue(),
            browser_button=Mock(), scrape_button=Mock(), download_button=Mock(),
            download_hint=Mock(), folder_name_var=Mock(), after=Mock(),
            browser_was_open=False, destroy=Mock(),
            closing=threading.Event(), driver_lock=threading.Lock(),
            retry_states={}, view_button=Mock(), remove_failed_button=Mock(),
            retry_button=Mock(), continue_button=Mock(), cancel_button=Mock(),
            debug_area=Mock(), debug_count_label=Mock(), _debug_follow=True,
            _line_open=False, _debug_replace=False, _debug_count=0,
            profile_button=Mock(), profile_lock_button=Mock(),
            profile_status_label=Mock(),
        )
        self.app.folder_name_var.get.return_value = 'videos'
        self.enterContext(patch.object(ui.os.path, 'getsize', return_value=128))
        self.enterContext(patch.object(profile_crypto, 'is_encrypted_profile', return_value=False))
        # Bind all DownloaderApp methods to the app instance
        for name in ('update_buttons', 'check_debug_queue',
                     'start_download_thread', 'download_videos', 'start_scraping_thread',
                     'scrape_links', '_update_folder_path',
                     '_refresh_recovery_button', '_refresh_retry_button',
                     '_enable_profile_encryption', '_disable_profile_encryption',
                     '_ask_passphrase', 'lock_profile_now', '_profile_changed',
                     '_on_browser_opened', '_refresh_profile_controls',
                     'toggle_profile_encryption', '_lock_after_disconnect'):
            setattr(self.app, name, getattr(ui.DownloaderApp, name).__get__(self.app, ui.DownloaderApp))
        # Mock the ones that need special handling
        self.app.start_operation = Mock()
        self.app.start_browser_thread = ui.DownloaderApp.start_browser_thread.__get__(self.app, ui.DownloaderApp)
        self.app.on_closing = ui.DownloaderApp.on_closing.__get__(self.app, ui.DownloaderApp)
        self.messages = self.enterContext(patch.object(ui, 'messagebox'))
        # Don't mock simpledialog here - individual tests will patch askstring
        self.enterContext(patch.object(ui.threading, 'Thread', InlineThread))
        self.enterContext(patch.object(ui, 'start_driver'))
        self.enterContext(patch.object(ui, 'shutdown_driver', return_value=True))
        self.close_orphan = self.enterContext(patch.object(ui, 'close_orphan_profile_chrome', return_value=True))
        def inline_start_cleanup(action, timeout=None):
            action()
        self.enterContext(patch.object(browser_cleanup, 'start_cleanup', inline_start_cleanup))
        self.enterContext(patch('builtins.print'))

    def _call(self, method_name, *args, **kwargs):
        return getattr(self.app, method_name)(*args, **kwargs)

    def test_start_browser_thread_prompts_when_encrypted_and_no_key(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.profile_crypto, 'current_session_key', return_value=None), \
             patch.object(ui.simpledialog, 'askstring', return_value='correct123'):

            self._call('start_browser_thread')

            # is_encrypted_profile is called in start_browser_thread and in open_browser (via start_operation target)
            self.assertGreaterEqual(ui.profile_crypto.is_encrypted_profile.call_count, 1)
            ui.simpledialog.askstring.assert_called_once()
            self.app.start_operation.assert_called_once()

    def test_start_browser_thread_no_prompt_when_not_encrypted(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False), \
             patch.object(ui.profile_crypto, 'current_session_key', return_value=None), \
             patch.object(ui.simpledialog, 'askstring') as mock_ask:

            self._call('start_browser_thread')

            mock_ask.assert_not_called()
            self.app.start_operation.assert_called_once()

    def test_start_browser_thread_cancel_aborts_without_worker(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.profile_crypto, 'current_session_key', return_value=None), \
             patch.object(ui.simpledialog, 'askstring', return_value=None):

            self._call('start_browser_thread')

            self.app.start_operation.assert_not_called()

    def test_toggle_profile_encryption_calls_enable_when_not_encrypted(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False), \
             patch.object(ui.simpledialog, 'askstring', side_effect=['valid123', 'valid123']), \
             patch.object(ui.messagebox, 'askyesno', return_value=True), \
             patch.object(ui.profile_crypto, 'enable_encryption') as mock_enable:

            self.app._enable_profile_encryption = ui.DownloaderApp._enable_profile_encryption.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.app._profile_changed = Mock()

            self._call('toggle_profile_encryption')

            self.app.start_operation.assert_called_once()
            target = self.app.start_operation.call_args.args[0]
            target()
            mock_enable.assert_called_once_with('valid123', ui.CHROME_PROFILE_DIR, ui.ENCRYPTED_PROFILE_DIR)

    def test_toggle_profile_encryption_calls_disable_when_encrypted(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.simpledialog, 'askstring', return_value='pass123'), \
             patch.object(ui.messagebox, 'askyesno', return_value=True), \
             patch.object(ui.profile_crypto, 'disable_encryption') as mock_disable:

            self.app._disable_profile_encryption = ui.DownloaderApp._disable_profile_encryption.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.app._profile_changed = Mock()

            self._call('toggle_profile_encryption')

            self.app.start_operation.assert_called_once()
            target = self.app.start_operation.call_args.args[0]
            target()
            mock_disable.assert_called_once()
            args, _ = mock_disable.call_args
            self.assertEqual(args[0], 'pass123')

    def test_enable_flow_rejects_mismatched_passphrases(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False), \
             patch.object(ui.simpledialog, 'askstring', side_effect=['first123', 'second123']):

            self.app._enable_profile_encryption = ui.DownloaderApp._enable_profile_encryption.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.messages.askyesno.return_value = True

            self._call('toggle_profile_encryption')

            self.app.start_operation.assert_not_called()
            self.messages.showerror.assert_called_once()
            self.assertIn('do not match', str(self.messages.showerror.call_args))

    def test_enable_flow_rejects_short_passphrase(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False), \
             patch.object(ui.simpledialog, 'askstring', side_effect=['short', 'short']):

            self.app._enable_profile_encryption = ui.DownloaderApp._enable_profile_encryption.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.messages.askyesno.return_value = True

            self._call('toggle_profile_encryption')

            self.app.start_operation.assert_not_called()
            self.messages.showerror.assert_called_once()
            self.assertIn('too short', str(self.messages.showerror.call_args).lower())

    def test_lock_profile_now_verifies_passphrase_when_no_key(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.profile_crypto, 'current_session_key', return_value=None), \
             patch.object(ui.profile_crypto, 'verify_passphrase', return_value=Mock()) as mock_verify, \
             patch.object(ui.simpledialog, 'askstring', return_value='pass123'):

            self.app.lock_profile_now = ui.DownloaderApp.lock_profile_now.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.app._profile_changed = Mock()

            self._call('lock_profile_now')

            mock_verify.assert_called_once_with('pass123', ui.ENCRYPTED_PROFILE_DIR)
            self.app.start_operation.assert_called_once()

    def test_lock_profile_now_reports_incorrect_passphrase(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.profile_crypto, 'current_session_key', return_value=None), \
             patch.object(ui.profile_crypto, 'verify_passphrase', side_effect=profile_crypto.ProfileEncryptionError('Incorrect passphrase.')), \
             patch.object(ui.simpledialog, 'askstring', return_value='wrong'):

            self.app.lock_profile_now = ui.DownloaderApp.lock_profile_now.__get__(self.app, ui.DownloaderApp)
            self.app.start_operation = Mock()
            self.app._profile_changed = Mock()

            self._call('lock_profile_now')

            self.messages.showerror.assert_called_once()
            self.assertIn('Incorrect passphrase', str(self.messages.showerror.call_args))
            self.app.start_operation.assert_not_called()

    def test_on_closing_locks_when_encrypted(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
             patch.object(ui.profile_crypto, 'lock_session_profile', return_value=True) as mock_lock:

            self.app.on_closing = ui.DownloaderApp.on_closing.__get__(self.app, ui.DownloaderApp)
            self.app.driver = None
            self.app.browser_was_open = True

            self._call('on_closing')

            mock_lock.assert_called_once()
            self.close_orphan.assert_called_once()

    def test_on_closing_skips_lock_when_not_encrypted(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False), \
             patch.object(ui.profile_crypto, 'lock_session_profile') as mock_lock:

            self.app.on_closing = ui.DownloaderApp.on_closing.__get__(self.app, ui.DownloaderApp)
            self.app.driver = None
            self.app.browser_was_open = True

            self._call('on_closing')

            mock_lock.assert_not_called()
            self.close_orphan.assert_called_once()

    def test_refresh_profile_controls_states(self):
        test_cases = [
            (False, False, True, "Enable encryption", "Lock profile", tk.DISABLED),
            (True, False, True, "Disable encryption", "Lock profile", tk.DISABLED),
            (True, True, True, "Disable encryption", "Lock profile", tk.NORMAL),
            (True, True, False, "Disable encryption", "Lock profile", tk.DISABLED),
        ]
        for encrypted, unlocked, can_manage, btn_text, lock_text, lock_state in test_cases:
            with self.subTest(encrypted=encrypted, unlocked=unlocked):
                with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=encrypted), \
                     patch.object(ui.profile_crypto, 'is_unlocked', return_value=unlocked):
                    self.app.busy = not can_manage
                    self.app.driver = None if can_manage else Mock()

                    self._call('_refresh_profile_controls')

                    self.app.profile_button.config.assert_called_with(
                        text=btn_text,
                        state=tk.NORMAL if can_manage else tk.DISABLED
                    )
                    self.app.profile_lock_button.config.assert_called_with(
                        text=lock_text,
                        state=lock_state
                    )


if __name__ == '__main__':
    unittest.main()