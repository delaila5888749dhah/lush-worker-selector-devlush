"""Tests for handle_ui_lock_focus_shift (Blueprint §6 Ngã rẽ 1).

Phase 4 [B2]: the helper now routes clicks through
``GivexDriver.bounding_box_click`` (CDP ``Input.dispatchMouseEvent``,
``isTrusted=True``) rather than Selenium ``ActionChains`` — the latter
leaves a detectable anti-bot fingerprint.
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    handle_ui_lock_focus_shift,
)


class TestFocusShiftRetry(unittest.TestCase):
    def setUp(self):
        self._sleep_patcher = patch.object(drv.time, "sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_neutral_click_then_recompute_bbox_then_click(self):
        driver = MagicMock(name="givex_driver")
        result = handle_ui_lock_focus_shift(driver)

        self.assertTrue(result)
        # Two bounding_box_click calls: neutral body first, then Complete Purchase.
        self.assertEqual(driver.bounding_box_click.call_count, 2)
        ordered = [c.args[0] for c in driver.bounding_box_click.call_args_list]
        self.assertEqual(ordered, [SEL_NEUTRAL_DIV, SEL_COMPLETE_PURCHASE])
        # 0.5s wait between the two clicks.
        self.mock_sleep.assert_called_once_with(0.5)

    def test_returns_false_on_exception(self):
        driver = MagicMock(name="givex_driver")
        driver.bounding_box_click.side_effect = RuntimeError("element gone")
        result = handle_ui_lock_focus_shift(driver)
        self.assertFalse(result)

    def test_returns_false_when_driver_lacks_bounding_box_click(self):
        """Raw Selenium driver (no GivexDriver wrapper) cannot be used."""
        driver = MagicMock(name="raw_driver", spec=[])
        self.assertFalse(handle_ui_lock_focus_shift(driver))

    def test_only_one_retry_per_invocation(self):
        """handle_ui_lock_focus_shift must never loop / retry internally."""
        driver = MagicMock(name="givex_driver")
        handle_ui_lock_focus_shift(driver)
        # Exactly two bounding_box_click calls — no internal loop.
        self.assertEqual(driver.bounding_box_click.call_count, 2)


if __name__ == "__main__":
    unittest.main()
