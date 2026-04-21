"""Tests for handle_ui_lock_focus_shift (C6 — Blueprint §6 Ngã rẽ 1)."""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    handle_ui_lock_focus_shift,
)


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
        neutral_el = MagicMock(name="neutral_body")
        btn = MagicMock(name="complete_purchase_btn")
        driver.find_element.side_effect = lambda by, sel: (
            neutral_el if sel == SEL_NEUTRAL_DIV else btn
        )

        result = handle_ui_lock_focus_shift(driver)

        self.assertTrue(result)
        # Two ActionChains instances: one for neutral, one for retry click.
        self.assertEqual(len(_FakeActionChains.instances), 2)
        neutral_chain = _FakeActionChains.instances[0]
        retry_chain = _FakeActionChains.instances[1]
        self.assertEqual(neutral_chain.calls[0], ("move_to_element", neutral_el))
        self.assertEqual(neutral_chain.calls[1], ("click",))
        self.assertEqual(neutral_chain.calls[-1], ("perform",))
        # 0.5s wait between the two clicks.
        self.mock_sleep.assert_called_once_with(0.5)
        # Two find_element calls: neutral div + purchase button.
        self.assertEqual(driver.find_element.call_count, 2)
        driver.find_element.assert_any_call("css selector", SEL_NEUTRAL_DIV)
        driver.find_element.assert_any_call("css selector", SEL_COMPLETE_PURCHASE)
        # Retry click targeted the re-located element.
        self.assertEqual(retry_chain.calls[0], ("move_to_element", btn))
        self.assertEqual(retry_chain.calls[1], ("click",))
        self.assertEqual(retry_chain.calls[-1], ("perform",))

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
        # Exactly two find_element calls (neutral div + purchase btn)
        # and exactly two ActionChains instances.
        self.assertEqual(driver.find_element.call_count, 2)
        self.assertEqual(len(_FakeActionChains.instances), 2)


if __name__ == "__main__":
    unittest.main()
