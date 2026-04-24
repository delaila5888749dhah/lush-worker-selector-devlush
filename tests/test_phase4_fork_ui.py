"""Phase 4 [B2, D6] — driver.py Fork-1 CDP click + VBV cancel priority tests.

Covers:
* ``handle_ui_lock_focus_shift`` routes through ``bounding_box_click``
  (CDP ``Input.dispatchMouseEvent`` path, isTrusted=True) and never
  instantiates ``ActionChains`` — removes the Selenium-native anti-bot
  fingerprint identified in audit B2.
* Static grep: no ``ActionChains`` references remain inside the Fork-1
  (``handle_ui_lock_focus_shift``) region of ``modules/cdp/driver.py``.
* ``SEL_VBV_CANCEL_BUTTONS`` priority tuple + ``_find_vbv_cancel_button``
  helper correctly prefer Cancel over Close, fall back to close-icon
  selectors, and return ``(None, None)`` when no match is found.
"""
from __future__ import annotations

import re
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_COMPLETE_PURCHASE,
    SEL_NEUTRAL_DIV,
    SEL_VBV_CANCEL_BTN,
    SEL_VBV_CANCEL_BUTTONS,
)


# ---------------------------------------------------------------------------
# Task 1 — handle_ui_lock_focus_shift uses CDP bounding_box_click
# ---------------------------------------------------------------------------


class TestHandleUiLockFocusShiftUsesBoundingBoxClick(unittest.TestCase):
    def test_calls_bounding_box_click_twice_neutral_then_submit(self):
        driver = MagicMock(name="givex_driver")
        # Stub time.sleep to keep the test fast.
        with patch.object(drv.time, "sleep"):
            result = drv.handle_ui_lock_focus_shift(driver)

        self.assertTrue(result)
        self.assertEqual(driver.bounding_box_click.call_count, 2)
        # Order matters: neutral body first, Complete-Purchase second.
        calls = [c.args[0] for c in driver.bounding_box_click.call_args_list]
        self.assertEqual(calls, [SEL_NEUTRAL_DIV, SEL_COMPLETE_PURCHASE])

    def test_never_instantiates_actionchains(self):
        driver = MagicMock(name="givex_driver")
        # Any access to ``_ActionChains`` in the module under test would
        # appear as a call on this MagicMock — we assert zero calls.
        ac_spy = MagicMock(name="_ActionChains")
        with patch.object(drv, "_ActionChains", ac_spy), \
                patch.object(drv.time, "sleep"):
            drv.handle_ui_lock_focus_shift(driver)
        ac_spy.assert_not_called()

    def test_returns_false_when_driver_lacks_bounding_box_click(self):
        driver = MagicMock(name="raw_driver", spec=[])
        self.assertFalse(drv.handle_ui_lock_focus_shift(driver))

    def test_neutral_click_failure_returns_false_and_skips_submit(self):
        driver = MagicMock(name="givex_driver")
        driver.bounding_box_click.side_effect = RuntimeError("cdp down")
        with patch.object(drv.time, "sleep"):
            self.assertFalse(drv.handle_ui_lock_focus_shift(driver))
        self.assertEqual(driver.bounding_box_click.call_count, 1)

    def test_submit_click_failure_returns_false(self):
        driver = MagicMock(name="givex_driver")
        # First call (neutral) succeeds, second (submit) fails.
        driver.bounding_box_click.side_effect = [None, RuntimeError("fail")]
        with patch.object(drv.time, "sleep"):
            self.assertFalse(drv.handle_ui_lock_focus_shift(driver))
        self.assertEqual(driver.bounding_box_click.call_count, 2)


class TestFocusShiftGrepNoActionChains(unittest.TestCase):
    """Static assertion: Fork-1 region has zero ActionChains references."""

    def test_no_actionchains_in_fork1_region(self):
        """No executable ActionChains call inside the Fork-1 helper.

        Per issue acceptance criterion, comments/docstrings referencing
        ``ActionChains`` (e.g. documenting *why* we removed it) are
        allowed — only executable references are forbidden.
        """
        import inspect
        source = inspect.getsource(drv.handle_ui_lock_focus_shift)
        # Strip comments + docstring so the check only sees real code.
        # Triple-quoted docstring: remove first "..." block.
        code = re.sub(r'"""[\s\S]*?"""', "", source, count=1)
        # Strip trailing line comments.
        code = "\n".join(
            line.split("#", 1)[0] for line in code.splitlines()
        )
        self.assertNotIn("ActionChains(", code)
        self.assertNotIn("_ActionChains(", code)


# ---------------------------------------------------------------------------
# Task 2 — VBV cancel selector priority
# ---------------------------------------------------------------------------


class TestVbvCancelSelectorTuple(unittest.TestCase):
    def test_tuple_has_at_least_ten_selectors(self):
        self.assertGreaterEqual(len(SEL_VBV_CANCEL_BUTTONS), 10)

    def test_backward_compat_alias_is_joined_string(self):
        self.assertEqual(SEL_VBV_CANCEL_BTN, ", ".join(SEL_VBV_CANCEL_BUTTONS))

    def test_priority_order_cancel_first(self):
        # The first priority bucket must target "cancel".
        self.assertIn("cancel", SEL_VBV_CANCEL_BUTTONS[0].lower())


def _make_driver_with_matches(match_map):
    """Build a GivexDriver-like stub whose ``find_elements`` returns values
    from ``match_map`` (selector -> list) and [] otherwise."""
    driver = MagicMock(spec=drv.GivexDriver)

    def _find(selector):
        return list(match_map.get(selector, []))

    driver.find_elements.side_effect = _find
    return driver


class TestFindVbvCancelButtonPriority(unittest.TestCase):
    def test_priority_cancel_wins_over_close(self):
        cancel_el = MagicMock(name="cancel_btn")
        close_el = MagicMock(name="close_btn")
        # Both Cancel and Close selectors have matches; Cancel must win.
        match_map = {
            SEL_VBV_CANCEL_BUTTONS[0]: [cancel_el],
            "button[aria-label*='close' i]": [close_el],
        }
        driver = _make_driver_with_matches(match_map)
        element, sel = drv.GivexDriver._find_vbv_cancel_button(driver)
        self.assertIs(element, cancel_el)
        self.assertEqual(sel, SEL_VBV_CANCEL_BUTTONS[0])

    def test_falls_back_to_close_icon(self):
        close_el = MagicMock(name="close_btn")
        match_map = {"button[aria-label*='close' i]": [close_el]}
        driver = _make_driver_with_matches(match_map)
        element, sel = drv.GivexDriver._find_vbv_cancel_button(driver)
        self.assertIs(element, close_el)
        self.assertEqual(sel, "button[aria-label*='close' i]")

    def test_returns_none_when_absent(self):
        driver = _make_driver_with_matches({})
        element, sel = drv.GivexDriver._find_vbv_cancel_button(driver)
        self.assertIsNone(element)
        self.assertIsNone(sel)


if __name__ == "__main__":
    unittest.main()
