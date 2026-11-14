"""Offline regressions for saved navigation, the real log widget and photos."""

import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scoopy import session


class SavedNavigationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.driver = SimpleNamespace(
            current_url='https://www.instagram.com/',
            execute_script=Mock(return_value={
                'saved': [], 'profile': ['https://www.instagram.com/my.account/']}),
        )

        def navigate(url):
            self.driver.current_url = url

        self.driver.get = Mock(side_effect=navigate)

    def test_own_profile_opens_saved(self):
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_called_once_with(
            'https://www.instagram.com/my.account/saved/')

    def test_saved_link_is_preferred(self):
        self.driver.execute_script.return_value['saved'] = [
            'https://www.instagram.com/actual_user/saved/']
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_called_once_with(
            'https://www.instagram.com/actual_user/saved/')

    def test_existing_collection_is_not_redirected(self):
        self.driver.current_url = 'https://www.instagram.com/me/saved/collection123/'
        self.assertTrue(session.navigate_to_saved(self.driver))
        self.driver.get.assert_not_called()
        self.driver.execute_script.assert_not_called()

    def test_missing_or_unsafe_profile_falls_back_home(self):
        for value in (None, [], 'bad', {'profile': [
                'https://evil.test/me/', 'https://www.instagram.com/accounts/',
                'https://user:secret@www.instagram.com/me/',
                'https://www.instagram.com/p/123/',
                'https://www.instagram.com:443/me/']}):
            with self.subTest(value=value):
                self.driver.current_url = 'https://www.instagram.com/'
                self.driver.get.reset_mock()
                self.driver.execute_script.return_value = value
                self.assertFalse(session.navigate_to_saved(self.driver))
                self.driver.get.assert_called_once_with('https://www.instagram.com/')

    def test_failed_or_redirected_navigation_falls_back(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                self.driver.current_url = 'https://www.instagram.com/'
                calls = []

                def navigate(url):
                    calls.append(url)
                    if '/saved/' in url:
                        if fail:
                            raise RuntimeError('PRIVATE_SESSION')
                        self.driver.current_url = 'https://www.instagram.com/accounts/login/'
                    else:
                        self.driver.current_url = url

                self.driver.get.side_effect = navigate
                self.assertFalse(session.navigate_to_saved(self.driver))
                self.assertEqual(calls[-1], 'https://www.instagram.com/')

    def test_broken_browser_does_not_leak_exception(self):
        self.driver.execute_script.side_effect = RuntimeError('PRIVATE_SESSION')
        self.driver.get.side_effect = RuntimeError('PRIVATE_SESSION')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(session.navigate_to_saved(self.driver))
        self.assertNotIn('PRIVATE_SESSION', output.getvalue())


if __name__ == '__main__':
    unittest.main()
