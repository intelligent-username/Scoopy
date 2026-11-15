"""Offline checks that settings is a page in the main window, not a dialog."""

import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from scoopy import ui


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.app = ui.DownloaderApp.__new__(ui.DownloaderApp)
        tk.Tk.__init__(self.app)
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.app.driver = None
        self.app.busy = False
        self.app.settings_visible = False
        self.app.closing = threading.Event()
        self.callback_errors = []
        self.app.report_callback_exception = (
            lambda *exc: self.callback_errors.append(exc))
        self.enterContext(
            patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=False))
        self.enterContext(
            patch.object(ui.profile_crypto, 'is_unlocked', return_value=False))
        self.app.setup_ui()

    @staticmethod
    def _is_descendant(widget, ancestor):
        while widget is not None:
            if widget is ancestor:
                return True
            widget = widget.master
        return False

    def test_profile_controls_live_only_on_the_settings_page(self):
        app = self.app
        self.assertEqual(app.main_page.winfo_manager(), 'pack')
        self.assertEqual(app.settings_page.winfo_manager(), '')
        for widget in (app.profile_button, app.profile_lock_button,
                       app.profile_status_label):
            self.assertTrue(self._is_descendant(widget, app.settings_page))
            self.assertFalse(self._is_descendant(widget, app.main_page))
        self.assertEqual(app.back_button.winfo_manager(), 'pack')
        self.assertTrue(app.settings_button.winfo_manager())

    def test_gear_swaps_pages_without_opening_a_window(self):
        app = self.app
        app.show_settings_page()
        self.assertEqual(app.main_page.winfo_manager(), '')
        self.assertEqual(app.settings_page.winfo_manager(), 'pack')
        self.assertEqual(app.profile_button.winfo_manager(), 'grid')
        self.assertEqual(app.profile_lock_button.winfo_manager(), 'grid')
        self.assertIs(app.settings_page.winfo_toplevel(), app)
        self.assertEqual(
            [child for child in app.winfo_children() if isinstance(child, tk.Toplevel)],
            [])
        app.show_main_page()
        self.assertEqual(app.main_page.winfo_manager(), 'pack')
        self.assertEqual(app.settings_page.winfo_manager(), '')
        self.assertFalse(app.settings_visible)

    def test_opening_settings_refreshes_the_encryption_state(self):
        with patch.object(ui.profile_crypto, 'is_encrypted_profile', return_value=True), \
                patch.object(ui.profile_crypto, 'is_unlocked', return_value=False):
            self.app.show_settings_page()
        self.assertEqual(self.app.profile_button.cget('text'), 'Disable encryption')
        self.assertIn('disabled', self.app.profile_lock_button.state())
        self.assertIn('Encrypted at rest', self.app.profile_status_label.cget('text'))

    def test_settings_and_back_are_idempotent(self):
        app = self.app
        app.show_settings_page()
        app.show_settings_page()
        self.assertEqual(app.settings_page.winfo_manager(), 'pack')
        app.show_main_page()
        app.show_main_page()
        self.assertEqual(app.main_page.winfo_manager(), 'pack')

    def test_gear_and_back_buttons_are_wired_without_crashing(self):
        app = self.app
        app.settings_button.invoke()
        self.assertTrue(app.settings_visible)
        self.assertEqual(app.settings_page.winfo_manager(), 'pack')
        app.back_button.invoke()
        self.assertFalse(app.settings_visible)
        self.assertEqual(app.main_page.winfo_manager(), 'pack')
        # The encryption actions are reachable from the settings page and must
        # stay inert until a passphrase dialog returns a value.
        app.settings_button.invoke()
        with patch.object(app, '_ask_passphrase', return_value=None):
            app.profile_button.invoke()
            app.profile_lock_button.invoke()
        self.assertEqual(app.settings_page.winfo_manager(), 'pack')
        self.assertEqual(self.callback_errors, [])


if __name__ == '__main__':
    unittest.main()
