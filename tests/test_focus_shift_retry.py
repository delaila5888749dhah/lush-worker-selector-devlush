"""Tests for handle_ui_lock_focus_shift (C6 — Blueprint §6 Ngã rẽ 1)."""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import SEL_COMPLETE_PURCHASE, handle_ui_lock_focus_shift


class _FakeActionChains:
    """Recording stand-in for selenium's ActionChains."""

    instances = []

    def __init__(self, driver):
        self.driver = driver
        self.calls = []
        _FakeActionChains.instances.append(self)

    def move_by_offset(self, x, y):
        self.calls.append(("move_by_offset", x, y))
        return self

    def move_to_element(self, el):
        self.calls.append(("move_to_element", el))
        return self

    def click(self):
        self.calls.append(("click",))
        return self

    def perform(self):
        self.calls.append(("perform",))
        return self


class TestFocusShiftRetry(unittest.TestCase):
    def setUp(self):
        _FakeActionChains.instances = []
        self._patcher = patch.object(drv, "_ActionChains", _FakeActionChains)
        self._patcher.start()
        self._sleep_patcher = patch.object(drv.time, "sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._sleep_patcher.stop()

    def test_neutral_click_then_recompute_bbox_then_click(self):
        driver = MagicMock()
        btn = MagicMock(name="complete_purchase_btn")
        driver.find_element.return_value = btn

        result = handle_ui_lock_focus_shift(driver, neutral_xy=(20, 20))

        self.assertTrue(result)
        # Two ActionChains instances: one for neutral, one for retry click.
        self.assertEqual(len(_FakeActionChains.instances), 2)
        neutral, retry = _FakeActionChains.instances
        self.assertEqual(neutral.calls[0], ("move_by_offset", 20, 20))
        self.assertEqual(neutral.calls[1], ("click",))
        self.assertEqual(neutral.calls[-1], ("perform",))
        # 0.5s wait between the two clicks.
        self.mock_sleep.assert_called_once_with(0.5)
        # Re-located the complete-purchase button by CSS selector.
        driver.find_element.assert_called_once_with(
            "css selector", SEL_COMPLETE_PURCHASE
        )
        # Retry click targeted the re-located element.
        self.assertEqual(retry.calls[0], ("move_to_element", btn))
        self.assertEqual(retry.calls[1], ("click",))
        self.assertEqual(retry.calls[-1], ("perform",))

    def test_returns_false_on_exception(self):
        driver = MagicMock()
        driver.find_element.side_effect = RuntimeError("element gone")
        result = handle_ui_lock_focus_shift(driver)
        self.assertFalse(result)

    def test_returns_false_when_action_chains_raises(self):
        driver = MagicMock()
        # Patch ActionChains to raise on first instantiation.
        with patch.object(drv, "_ActionChains", side_effect=RuntimeError("no chains")):
            self.assertFalse(handle_ui_lock_focus_shift(driver))

    def test_only_one_retry_per_invocation(self):
        """handle_ui_lock_focus_shift must never loop / retry internally."""
        driver = MagicMock()
        driver.find_element.return_value = MagicMock()
        handle_ui_lock_focus_shift(driver)
        # Exactly one find_element call and exactly two ActionChains instances.
        self.assertEqual(driver.find_element.call_count, 1)
        self.assertEqual(len(_FakeActionChains.instances), 2)


if __name__ == "__main__":
    unittest.main()
