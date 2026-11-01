"""Browser session regression tests using mocks only."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import browser_session


def locator(visible):
    result = MagicMock()
    element = MagicMock()
    element.is_visible.return_value = visible
    result.all.return_value = [element]
    return result


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.session = browser_session.BrowserSession(Path(self.temporary.name) / "profile")
        self.page = MagicMock()
        self.page.is_closed.return_value = False
        self.page.url = "https://gemini.google.com/app"
        self.session.page = self.page

    def tearDown(self):
        self.temporary.cleanup()

    def test_authenticated_requires_both_account_and_editor(self):
        self.page.get_by_role.return_value = locator(False)
        self.page.locator.side_effect = [locator(True), locator(True)]
        self.assertEqual(self.session.auth_state(), "authenticated")
        self.page.locator.side_effect = [locator(True), locator(False)]
        self.assertEqual(self.session.auth_state(), "loading")

    def test_visible_signin_prevents_authentication(self):
        self.page.get_by_role.return_value = locator(True)
        self.assertEqual(self.session.auth_state(), "login_required")
        self.page.locator.assert_not_called()

    def test_google_login_and_lookalike_hosts_are_not_authenticated(self):
        self.page.url = "https://accounts.google.com/v3/signin/challenge/pwd"
        self.assertEqual(self.session.auth_state(), "login_required")
        self.page.url = "https://gemini.google.com.attacker.example/app"
        self.assertEqual(self.session.auth_state(), "loading")
        self.page.locator.assert_not_called()

    def test_snapshot_does_not_read_google_login_controls(self):
        self.page.url = "https://accounts.google.com/v3/signin/challenge/pwd"
        result = self.session.snapshot()
        self.assertEqual(result["auth_state"], "login_required")
        self.assertEqual(result["controls"], [])
        self.page.locator.assert_not_called()

    def test_diagnostics_does_not_capture_google_login_page(self):
        self.page.url = "https://accounts.google.com/v3/signin/challenge/pwd"
        directory = Path(self.temporary.name) / "diagnostics"
        self.session.diagnostics(directory)
        self.page.locator.assert_not_called()
        self.page.screenshot.assert_not_called()
        self.assertEqual(list(directory.iterdir()), [])

    def test_partial_enter_failure_closes_context_before_releasing_lock(self):
        context = MagicMock()
        context.set_default_timeout.side_effect = RuntimeError("mock startup failure")
        lock = MagicMock()
        calls = MagicMock()
        calls.attach_mock(context, "context")
        calls.attach_mock(lock, "lock")
        self.session.lock = lock
        with patch.object(browser_session, "launch_persistent_context", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "mock startup failure"):
                self.session.__enter__()
        context.close.assert_called_once()
        lock.__exit__.assert_called_once()
        recorded = [call[0] for call in calls.mock_calls]
        self.assertLess(recorded.index("context.close"), recorded.index("lock.__exit__"))

    def test_context_close_failure_still_releases_profile_lock(self):
        self.session.context = MagicMock()
        self.session.context.close.side_effect = RuntimeError("mock close failure")
        self.session.lock = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "mock close failure"):
            self.session.__exit__(None, None, None)
        self.session.lock.__exit__.assert_called_once_with(None, None, None)

    def other_page(self, url="https://gemini.google.com/app"):
        page = MagicMock()
        page.url = url
        page.is_closed.return_value = False
        return page

    def test_ensure_login_keeps_current_authenticated_chat_despite_later_old_tab(self):
        self.page.url = "https://gemini.google.com/app/target-chat"
        old = self.other_page()
        self.session.context = MagicMock()
        self.session.context.pages = [self.page, old]
        with patch.object(self.session, "auth_state", return_value="authenticated"), \
             patch.object(browser_session, "emit"):
            self.assertTrue(self.session.ensure_login(timeout=1))
        self.assertIs(self.session.page, self.page)
        old.is_closed.assert_not_called()

    def test_ensure_login_keeps_loading_target_chat_until_authenticated(self):
        self.page.url = "https://gemini.google.com/app/target-chat"
        old = self.other_page()
        self.session.context = MagicMock()
        self.session.context.pages = [self.page, old]
        visited = []

        def state():
            visited.append(self.session.page)
            return "loading" if len(visited) == 1 else "authenticated"

        with patch.object(self.session, "auth_state", side_effect=state), \
             patch.object(browser_session, "emit"):
            self.assertTrue(self.session.ensure_login(timeout=1))
        self.assertEqual(visited, [self.page, self.page])
        self.assertIs(self.session.page, self.page)
        old.is_closed.assert_not_called()

    def test_ensure_login_can_follow_new_authenticated_gemini_tab(self):
        self.page.url = "https://accounts.google.com/v3/signin"
        signed_in = self.other_page()
        self.session.context = MagicMock()
        self.session.context.pages = [self.page, signed_in]

        def state():
            return "authenticated" if self.session.page is signed_in else "login_required"

        with patch.object(self.session, "auth_state", side_effect=state), \
             patch.object(browser_session, "emit"):
            self.assertTrue(self.session.ensure_login(timeout=1))
        self.assertIs(self.session.page, signed_in)

    def test_ensure_login_can_replace_closed_current_tab(self):
        self.page.is_closed.return_value = True
        signed_in = self.other_page()
        self.session.context = MagicMock()
        self.session.context.pages = [self.page, signed_in]
        with patch.object(self.session, "auth_state", return_value="authenticated"), \
             patch.object(browser_session, "emit"):
            self.assertTrue(self.session.ensure_login(timeout=1))
        self.assertIs(self.session.page, signed_in)

    def test_ensure_login_does_not_bounce_between_signed_out_gemini_tabs(self):
        other = self.other_page()
        self.session.context = MagicMock()
        self.session.context.pages = [self.page, other]
        current_checks = 0

        def state():
            nonlocal current_checks
            if self.session.page is self.page:
                current_checks += 1
                return "authenticated" if current_checks == 3 else "login_required"
            return "login_required"

        with patch.object(self.session, "auth_state", side_effect=state), \
             patch.object(browser_session, "emit"):
            self.assertTrue(self.session.ensure_login(timeout=1))
        self.assertIs(self.session.page, self.page)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)
        other.wait_for_timeout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
