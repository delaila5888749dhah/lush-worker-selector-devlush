"""P2-3 tests: handle_ui_lock_focus_shift clicks SEL_NEUTRAL_DIV first (#122).

Phase 4 [B2] update: the helper now uses ``GivexDriver.bounding_box_click``
(CDP-based, ``isTrusted=True``) rather than Selenium ``ActionChains``
(``isTrusted=False`` — anti-bot fingerprint).  These tests assert the
new contract while preserving the P2-3 invariant that the neutral
region is clicked BEFORE the Complete-Purchase button.
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    handle_ui_lock_focus_shift,
)


def _make_driver():
    return MagicMock(name="givex_driver")


class NeutralDivClickTest(unittest.TestCase):
    """handle_ui_lock_focus_shift must CDP-click SEL_NEUTRAL_DIV first."""

    @patch("time.sleep")
    def test_neutral_element_clicked_via_bounding_box_click(self, _sleep):
        """The first bounding_box_click targets SEL_NEUTRAL_DIV."""
        d = _make_driver()
        handle_ui_lock_focus_shift(d)
        calls = [c.args[0] for c in d.bounding_box_click.call_args_list]
        self.assertEqual(calls[0], SEL_NEUTRAL_DIV)

    @patch("time.sleep")
    def test_no_action_chains_used(self, _sleep):
        """ActionChains must NOT be instantiated — CDP-only contract (B2)."""
        from modules.cdp import driver as drv
        ac_spy = MagicMock(name="_ActionChains")
        with patch.object(drv, "_ActionChains", ac_spy):
            handle_ui_lock_focus_shift(_make_driver())
        ac_spy.assert_not_called()

    @patch("time.sleep")
    def test_returns_true_on_success(self, _sleep):
        d = _make_driver()
        self.assertTrue(handle_ui_lock_focus_shift(d))

    @patch("time.sleep")
    def test_returns_false_on_click_failure(self, _sleep):
        d = _make_driver()
        d.bounding_box_click.side_effect = Exception("element not found")
        self.assertFalse(handle_ui_lock_focus_shift(d))

    @patch("time.sleep")
    def test_purchase_btn_also_clicked(self, _sleep):
        """After neutral click, the Complete-Purchase button is CDP-clicked."""
        d = _make_driver()
        handle_ui_lock_focus_shift(d)
        # Exactly two bounding_box_click invocations: neutral + purchase btn.
        self.assertEqual(d.bounding_box_click.call_count, 2)
        ordered = [c.args[0] for c in d.bounding_box_click.call_args_list]
        self.assertEqual(ordered, [SEL_NEUTRAL_DIV, SEL_COMPLETE_PURCHASE])


if __name__ == "__main__":
    unittest.main()
