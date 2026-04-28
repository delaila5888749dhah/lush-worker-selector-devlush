"""[D7] Verify the popup XPath fallback uses CDP dispatch only.

The FSM handler path must not invoke native Selenium ``element.click()``
because it emits ``isTrusted=False`` events that are easily detected by
anti-bot fingerprinting. The XPath fallback in
``modules.cdp.driver._popup_xpath_click_close`` must instead resolve the
element's bounding rect via JS and dispatch a CDP
``Input.dispatchMouseEvent`` sequence at a randomized point inside the
rect, identical to :meth:`GivexDriver.bounding_box_click`.
"""
from __future__ import annotations

import random
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from selenium.common.exceptions import WebDriverException

from modules.cdp import driver as drv
from modules.cdp.driver import _popup_xpath_click_close


def _rect(left=100.0, top=200.0, width=50.0, height=30.0):
    return {"left": left, "top": top, "width": width, "height": height}


class TestPopupXPathClickCloseUsesCDP(unittest.TestCase):
    """``_popup_xpath_click_close`` must dispatch CDP events, not .click()."""

    def test_cdp_dispatch_sequence_emitted_inside_rect(self):
        rect = _rect()
        element = MagicMock()
        base = MagicMock()
        base.find_elements.return_value = [element]
        base.execute_script.return_value = rect
        wrapper = SimpleNamespace(_driver=base, _rnd=random.Random(7))

        result = _popup_xpath_click_close(wrapper)

        self.assertTrue(result)
        # Native click is forbidden in the FSM handler path.
        element.click.assert_not_called()
        cdp_calls = [
            c for c in base.execute_cdp_cmd.call_args_list
            if c[0][0] == "Input.dispatchMouseEvent"
        ]
        self.assertEqual(len(cdp_calls), 3)
        types = [c[0][1]["type"] for c in cdp_calls]
        self.assertEqual(types, ["mouseMoved", "mousePressed", "mouseReleased"])
        for c in cdp_calls:
            params = c[0][1]
            self.assertEqual(params["button"], "left")
            self.assertEqual(params["clickCount"], 1)
            # Each event coord should land inside the bounding rect (with a
            # tiny ±0.5px sub-pixel jitter tolerance applied per event).
            self.assertGreaterEqual(params["x"], rect["left"] - 0.5)
            self.assertLessEqual(
                params["x"], rect["left"] + rect["width"] + 0.5
            )
            self.assertGreaterEqual(params["y"], rect["top"] - 0.5)
            self.assertLessEqual(
                params["y"], rect["top"] + rect["height"] + 0.5
            )

    def test_returns_false_when_no_xpath_match(self):
        base = MagicMock()
        base.find_elements.return_value = []
        wrapper = SimpleNamespace(_driver=base, _rnd=random.Random(0))

        self.assertFalse(_popup_xpath_click_close(wrapper))
        base.execute_cdp_cmd.assert_not_called()

    def test_returns_false_when_find_elements_raises(self):
        base = MagicMock()
        base.find_elements.side_effect = WebDriverException("driver gone")
        wrapper = SimpleNamespace(_driver=base, _rnd=random.Random(0))

        self.assertFalse(_popup_xpath_click_close(wrapper))
        base.execute_cdp_cmd.assert_not_called()

    def test_skips_zero_size_rect_and_tries_next(self):
        bad_el = MagicMock()
        good_el = MagicMock()
        base = MagicMock()
        base.find_elements.return_value = [bad_el, good_el]
        # First element has zero-size rect; second has a valid rect.
        base.execute_script.side_effect = [
            {"left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0},
            _rect(),
        ]
        wrapper = SimpleNamespace(_driver=base, _rnd=random.Random(1))

        self.assertTrue(_popup_xpath_click_close(wrapper))
        bad_el.click.assert_not_called()
        good_el.click.assert_not_called()
        cdp_calls = [
            c for c in base.execute_cdp_cmd.call_args_list
            if c[0][0] == "Input.dispatchMouseEvent"
        ]
        self.assertEqual(len(cdp_calls), 3)

    def test_falls_through_to_next_when_cdp_dispatch_raises(self):
        bad_el = MagicMock()
        good_el = MagicMock()
        base = MagicMock()
        base.find_elements.return_value = [bad_el, good_el]
        base.execute_script.return_value = _rect()
        # First dispatchMouseEvent call raises; subsequent calls succeed.
        seq = {"n": 0}

        def cdp_side_effect(cmd, params):
            if cmd == "Input.dispatchMouseEvent":
                seq["n"] += 1
                if seq["n"] == 1:
                    raise WebDriverException("transient")

        base.execute_cdp_cmd.side_effect = cdp_side_effect
        wrapper = SimpleNamespace(_driver=base, _rnd=random.Random(1))

        self.assertTrue(_popup_xpath_click_close(wrapper))
        bad_el.click.assert_not_called()
        good_el.click.assert_not_called()
        # 1 (failed) + 3 (success on second element) = 4 dispatch calls.
        cdp_calls = [
            c for c in base.execute_cdp_cmd.call_args_list
            if c[0][0] == "Input.dispatchMouseEvent"
        ]
        self.assertEqual(len(cdp_calls), 4)

    def test_no_native_click_in_handler_source(self):
        """Static guard: the FSM handler function source must not call .click()."""
        import ast
        import inspect
        src = inspect.getsource(_popup_xpath_click_close)
        tree = ast.parse(src)
        func = tree.body[0]
        # Drop the docstring node so prose mentions of ``.click()`` don't trip
        # the static check.
        if (
            func.body
            and isinstance(func.body[0], ast.Expr)
            and isinstance(func.body[0].value, ast.Constant)
            and isinstance(func.body[0].value.value, str)
        ):
            func.body = func.body[1:]
        code_only = ast.unparse(func)
        self.assertNotIn(".click()", code_only)


if __name__ == "__main__":
    unittest.main()
