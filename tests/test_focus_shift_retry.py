"""Tests for handle_ui_lock_focus_shift (Phase 4 audit [B2] — CDP path).

The focus-shift retry now dispatches both clicks (neutral div + re-click on
Complete Purchase) through ``GivexDriver.bounding_box_click`` so that the
events carry ``isTrusted=True`` and do not reveal a Selenium fingerprint.
The legacy ActionChains path has been removed from the Fork-1 region.
"""
import pathlib
import re
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    handle_ui_lock_focus_shift,
)


class TestFocusShiftRetryCdp(unittest.TestCase):
    def setUp(self):
        self._sleep_patcher = patch.object(drv.time, "sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_handle_ui_lock_focus_shift_uses_bounding_box_click(self):
        """Both clicks go through bounding_box_click (CDP); no ActionChains."""
        givex = MagicMock(name="givex_driver")
        result = handle_ui_lock_focus_shift(givex)

        self.assertTrue(result)
        # Exactly two CDP clicks: neutral div, then Complete Purchase.
        self.assertEqual(givex.bounding_box_click.call_count, 2)
        givex.bounding_box_click.assert_any_call(SEL_NEUTRAL_DIV)
        givex.bounding_box_click.assert_any_call(SEL_COMPLETE_PURCHASE)
        # 0.5s settle wait between the two clicks.
        self.mock_sleep.assert_called_once_with(0.5)

    def test_returns_false_when_neutral_click_raises(self):
        givex = MagicMock(name="givex_driver")
        givex.bounding_box_click.side_effect = [RuntimeError("rect missing"), None]
        self.assertFalse(handle_ui_lock_focus_shift(givex))
        # Only the neutral click was attempted — helper never retries itself.
        self.assertEqual(givex.bounding_box_click.call_count, 1)

    def test_returns_false_when_purchase_click_raises(self):
        givex = MagicMock(name="givex_driver")
        givex.bounding_box_click.side_effect = [None, RuntimeError("cdp fail")]
        self.assertFalse(handle_ui_lock_focus_shift(givex))
        self.assertEqual(givex.bounding_box_click.call_count, 2)

    def test_returns_false_when_driver_missing_bounding_box_click(self):
        plain_driver = MagicMock(spec=[])  # no bounding_box_click attribute
        self.assertFalse(handle_ui_lock_focus_shift(plain_driver))


class TestHandleUiLockGrepNoActionChains(unittest.TestCase):
    """Static assertion: no ActionChains references in the Fork-1 region."""

    def test_handle_ui_lock_grep_no_actionchains_in_driver(self):
        """grep_no_actionchains_in_driver for the Fork-1 handler region.

        Acceptance: zero ``ActionChains`` references in executable code within
        the Fork-1 region; mentions inside docstrings / comments are allowed
        (see #259 acceptance: "0 hits hoặc chỉ trong comments").
        """
        path = pathlib.Path(drv.__file__)
        source = path.read_text(encoding="utf-8")
        match = re.search(
            r"^def handle_ui_lock_focus_shift\b.*?(?=^def |\Z)",
            source,
            flags=re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, "handle_ui_lock_focus_shift not found")
        region = match.group(0)
        # Strip triple-quoted docstring then strip ``# …`` line comments.
        region_no_doc = re.sub(r'"""[\s\S]*?"""', "", region)
        code_lines = []
        for line in region_no_doc.splitlines():
            # Drop line comments while preserving any leading code.
            code = line.split("#", 1)[0]
            code_lines.append(code)
        executable = "\n".join(code_lines)
        self.assertNotIn("ActionChains", executable)
        self.assertNotIn("_ActionChains", executable)


if __name__ == "__main__":
    unittest.main()
