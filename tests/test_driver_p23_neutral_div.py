"""P2-3 tests: handle_ui_lock_focus_shift uses SEL_NEUTRAL_DIV (#122).

Verifies that handle_ui_lock_focus_shift clicks the element found via
SEL_NEUTRAL_DIV (css selector "body") rather than using move_by_offset,
so focus always lands on a known neutral element.
"""
import time
import unittest
from unittest.mock import MagicMock, call, patch

from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    handle_ui_lock_focus_shift,
)


def _make_driver():
    d = MagicMock()
    neutral_el = MagicMock(name="neutral_body")
    purchase_btn = MagicMock(name="purchase_btn")
    d.find_element.side_effect = lambda by, sel: (
        neutral_el if sel == SEL_NEUTRAL_DIV else purchase_btn
    )
    return d, neutral_el, purchase_btn


class NeutralDivClickTest(unittest.TestCase):
    """handle_ui_lock_focus_shift must click SEL_NEUTRAL_DIV element."""

    def setUp(self):
        self.mock_chains_cls = MagicMock()
        self.patcher = patch("modules.cdp.driver._ActionChains", self.mock_chains_cls)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    @patch("time.sleep")
    def test_neutral_element_found_by_css_selector(self, _sleep):
        """find_element called with 'css selector' and SEL_NEUTRAL_DIV."""
        d, neutral_el, _ = _make_driver()
        handle_ui_lock_focus_shift(d)
        self.assertIn(
            call("css selector", SEL_NEUTRAL_DIV),
            d.find_element.call_args_list,
        )

    @patch("time.sleep")
    def test_move_to_element_used_not_move_by_offset(self, _sleep):
        """ActionChains.move_to_element is used; move_by_offset is NOT called."""
        d, neutral_el, _ = _make_driver()
        handle_ui_lock_focus_shift(d)
        chain_instance = self.mock_chains_cls.return_value
        # First call to move_to_element must be with the neutral element
        first_call_args = chain_instance.move_to_element.call_args_list[0]
        self.assertEqual(first_call_args, call(neutral_el))
        chain_instance.move_by_offset.assert_not_called()

    @patch("time.sleep")
    def test_returns_true_on_success(self, _sleep):
        d, _, _ = _make_driver()
        result = handle_ui_lock_focus_shift(d)
        self.assertTrue(result)

    @patch("time.sleep")
    def test_returns_false_on_find_element_failure(self, _sleep):
        d = MagicMock()
        d.find_element.side_effect = Exception("element not found")
        result = handle_ui_lock_focus_shift(d)
        self.assertFalse(result)

    @patch("time.sleep")
    def test_purchase_btn_also_clicked(self, _sleep):
        """After neutral click, the purchase button is also clicked via ActionChains."""
        d, _, purchase_btn = _make_driver()
        handle_ui_lock_focus_shift(d)
        # ActionChains instantiated twice: once for neutral click, once for button click
        self.assertEqual(self.mock_chains_cls.call_count, 2)


if __name__ == "__main__":
    unittest.main()
