"""Tests for hard_reset_browser_state (C4 — Blueprint §3)."""
import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import _HARD_RESET_JS, hard_reset_browser_state


class TestHardResetBrowserState(unittest.TestCase):
    def test_hard_reset_executes_js_and_delete_cookies(self):
        drv = MagicMock()
        hard_reset_browser_state(drv)
        drv.execute_script.assert_called_once_with(_HARD_RESET_JS)
        drv.delete_all_cookies.assert_called_once_with()

    def test_hard_reset_swallows_execute_script_exception(self):
        drv = MagicMock()
        drv.execute_script.side_effect = RuntimeError("storage disabled")
        # Must NOT raise.
        hard_reset_browser_state(drv)
        drv.delete_all_cookies.assert_called_once_with()

    def test_hard_reset_swallows_delete_cookies_exception(self):
        drv = MagicMock()
        drv.delete_all_cookies.side_effect = RuntimeError("cookies locked")
        # Must NOT raise.
        hard_reset_browser_state(drv)
        drv.execute_script.assert_called_once_with(_HARD_RESET_JS)

    def test_hard_reset_js_covers_storage_and_cookies(self):
        # Sanity: the inlined JS references all three storage APIs and wraps
        # each in try/catch per spec C4.
        self.assertIn("localStorage.clear", _HARD_RESET_JS)
        self.assertIn("sessionStorage.clear", _HARD_RESET_JS)
        self.assertIn("document.cookie", _HARD_RESET_JS)
        self.assertEqual(_HARD_RESET_JS.count("try {"), 3)


if __name__ == "__main__":
    unittest.main()
