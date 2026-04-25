import random
import unittest
from unittest.mock import MagicMock

from modules.cdp.driver import cdp_click_iframe_element


class _FixedRng:
    def __init__(self, values):
        self._values = list(values)

    def uniform(self, _low, _high):
        return self._values.pop(0)


def _make_driver(elem_rect, iframe_rect):
    driver = MagicMock()
    driver.switch_to = MagicMock()
    driver._driver = driver
    iframe = MagicMock(name="iframe")
    elem = MagicMock(name="element")

    def find_element(_by, selector):
        return iframe if selector == "iframe" else elem

    def execute_script(_script, element):
        return elem_rect if element is elem else iframe_rect

    driver.find_element.side_effect = find_element
    driver.execute_script.side_effect = execute_script
    return driver, iframe, elem


class TestVbvCdpAbsClick(unittest.TestCase):
    def test_abs_coords_computed_correctly(self):
        elem_rect = {"left": 50, "top": 30, "width": 80, "height": 20}
        iframe_rect = {"left": 100, "top": 200}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)
        rng = _FixedRng([0.0] * 8)

        abs_x, abs_y = cdp_click_iframe_element(driver, "iframe", "button", rng=rng)

        self.assertAlmostEqual(abs_x, 190.0)
        self.assertAlmostEqual(abs_y, 240.0)

    def test_random_offset_within_bounds(self):
        elem_rect = {"left": 50, "top": 30, "width": 80, "height": 20}
        iframe_rect = {"left": 100, "top": 200}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)
        rng = random.Random(9)
        base_x = 100 + 50 + 40
        base_y = 200 + 30 + 10

        for _ in range(100):
            abs_x, abs_y = cdp_click_iframe_element(driver, "iframe", "button", rng=rng)
            offset_x = abs_x - base_x
            offset_y = abs_y - base_y
            self.assertGreaterEqual(offset_x, -15)
            self.assertLessEqual(offset_x, 15)
            self.assertGreaterEqual(offset_y, -5)
            self.assertLessEqual(offset_y, 5)

    def test_dispatches_moved_pressed_then_released(self):
        """The 3-event CDP click sequence is emitted in canonical order."""
        elem_rect = {"left": 0, "top": 0, "width": 10, "height": 10}
        iframe_rect = {"left": 0, "top": 0}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)

        cdp_click_iframe_element(
            driver, "iframe", "button", rng=_FixedRng([0.0] * 8),
        )

        calls = [call.args[1]["type"] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(calls, ["mouseMoved", "mousePressed", "mouseReleased"])

    def test_switches_back_to_default_content_before_dispatch(self):
        elem_rect = {"left": 0, "top": 0, "width": 10, "height": 10}
        iframe_rect = {"left": 0, "top": 0}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)
        order = []

        driver.switch_to.default_content.side_effect = lambda: order.append("default")
        driver.execute_cdp_cmd.side_effect = lambda *_args, **_kwargs: order.append("dispatch")

        cdp_click_iframe_element(driver, "iframe", "button", rng=_FixedRng([0.0] * 8))
        self.assertIn("default", order)
        self.assertIn("dispatch", order)
        self.assertLess(order.index("default"), order.index("dispatch"))


    def test_default_content_restored_on_find_element_raise(self):
        """If element lookup inside iframe raises, default_content() must still run."""
        driver = MagicMock()
        driver.switch_to = MagicMock()
        driver._driver = driver
        iframe = MagicMock(name="iframe")

        def find_element(_by, selector):
            if selector == "iframe":
                return iframe
            raise RuntimeError("element not found")

        driver.find_element.side_effect = find_element

        with self.assertRaises(RuntimeError):
            cdp_click_iframe_element(driver, "iframe", "button", rng=_FixedRng([0.0, 0.0]))

        driver.switch_to.frame.assert_called_once_with(iframe)
        driver.switch_to.default_content.assert_called_once_with()

    def test_default_content_restored_on_execute_script_raise(self):
        """If getBoundingClientRect script raises inside iframe,
        default_content() must still run."""
        elem_rect = {"left": 0, "top": 0, "width": 10, "height": 10}
        iframe_rect = {"left": 0, "top": 0}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)
        driver.execute_script.side_effect = RuntimeError("script failed")

        with self.assertRaises(RuntimeError):
            cdp_click_iframe_element(driver, "iframe", "button", rng=_FixedRng([0.0, 0.0]))

        driver.switch_to.default_content.assert_called_once_with()


class TestVbvCancelSelectorPriority(unittest.TestCase):
    """Phase 4 audit [D6] — SEL_VBV_CANCEL_BUTTONS priority + helper."""

    def _make_givex(self, selector_to_elements):
        """Build a driver-stub whose find_elements returns per-selector lists."""
        from modules.cdp import driver as drv  # noqa: PLC0415

        givex = MagicMock(spec=[
            "find_elements", "_find_vbv_cancel_button",
        ])
        givex.find_elements.side_effect = lambda sel: selector_to_elements.get(sel, [])
        # Bind the real helper against the stub so we exercise the real logic.
        givex._find_vbv_cancel_button = (
            lambda: drv.GivexDriver._find_vbv_cancel_button(givex)
        )
        return givex

    def test_sel_vbv_cancel_buttons_is_priority_tuple(self):
        from modules.cdp.driver import SEL_VBV_CANCEL_BUTTONS, SEL_VBV_CANCEL_BTN

        self.assertIsInstance(SEL_VBV_CANCEL_BUTTONS, tuple)
        # Blueprint acceptance: ≥10 entries ordered by priority.
        self.assertGreaterEqual(len(SEL_VBV_CANCEL_BUTTONS), 10)
        # Cancel selectors must precede any generic close/X selectors.
        first_close = next(
            (i for i, s in enumerate(SEL_VBV_CANCEL_BUTTONS) if "close" in s.lower()),
            len(SEL_VBV_CANCEL_BUTTONS),
        )
        last_cancel = max(
            (i for i, s in enumerate(SEL_VBV_CANCEL_BUTTONS) if "cancel" in s.lower()),
            default=-1,
        )
        self.assertLess(last_cancel, first_close,
                        "Cancel selectors must have higher priority than Close")
        # Backward-compat alias — comma-joined string preserves legacy shape.
        self.assertIsInstance(SEL_VBV_CANCEL_BTN, str)
        for sel in SEL_VBV_CANCEL_BUTTONS:
            self.assertIn(sel, SEL_VBV_CANCEL_BTN)

    def test_vbv_cancel_selector_priority_cancel_first(self):
        """Page has both Cancel and Close — Cancel wins (highest priority)."""
        cancel_el = MagicMock(name="cancel_btn")
        close_el = MagicMock(name="close_btn")
        mapping = {
            "button[id*='cancel' i]": [cancel_el],
            "button[aria-label*='close' i]": [close_el],
        }
        givex = self._make_givex(mapping)
        el, sel = givex._find_vbv_cancel_button()
        self.assertIs(el, cancel_el)
        self.assertEqual(sel, "button[id*='cancel' i]")

    def test_vbv_cancel_selector_falls_back_to_close_icon(self):
        """No Cancel/Return; only a close aria-label match — it is used."""
        close_el = MagicMock(name="close_btn")
        mapping = {"button[aria-label*='close' i]": [close_el]}
        givex = self._make_givex(mapping)
        el, sel = givex._find_vbv_cancel_button()
        self.assertIs(el, close_el)
        self.assertEqual(sel, "button[aria-label*='close' i]")

    def test_vbv_cancel_selector_returns_none_when_absent(self):
        givex = self._make_givex({})
        el, sel = givex._find_vbv_cancel_button()
        self.assertIsNone(el)
        self.assertIsNone(sel)


if __name__ == "__main__":
    unittest.main()
