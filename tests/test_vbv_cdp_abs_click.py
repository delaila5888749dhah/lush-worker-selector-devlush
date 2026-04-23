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
        rng = _FixedRng([0.0, 0.0])

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

    def test_dispatches_mousePressed_then_mouseReleased(self):
        elem_rect = {"left": 0, "top": 0, "width": 10, "height": 10}
        iframe_rect = {"left": 0, "top": 0}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)

        cdp_click_iframe_element(driver, "iframe", "button", rng=_FixedRng([0.0, 0.0]))

        calls = [call.args[1]["type"] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(calls, ["mousePressed", "mouseReleased"])

    def test_switches_back_to_default_content_before_dispatch(self):
        elem_rect = {"left": 0, "top": 0, "width": 10, "height": 10}
        iframe_rect = {"left": 0, "top": 0}
        driver, _iframe, _elem = _make_driver(elem_rect, iframe_rect)
        order = []

        driver.switch_to.default_content.side_effect = lambda: order.append("default")
        driver.execute_cdp_cmd.side_effect = lambda *_args, **_kwargs: order.append("dispatch")

        cdp_click_iframe_element(driver, "iframe", "button", rng=_FixedRng([0.0, 0.0]))

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


if __name__ == "__main__":
    unittest.main()
