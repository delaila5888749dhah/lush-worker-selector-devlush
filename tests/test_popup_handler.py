import inspect
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import (
    SEL_POPUP_CLOSE,
    handle_something_wrong_popup,
)


class TestPopupHandler(unittest.TestCase):
    def test_clicks_close_when_popup_present(self):
        base_driver = MagicMock()
        base_driver.find_elements.return_value = [MagicMock()]
        base_driver.find_element.return_value = MagicMock()
        wrapper = MagicMock()
        wrapper._driver = base_driver
        wrapper.bounding_box_click = MagicMock()

        with patch.object(drv, "WebDriverWait", None), patch("modules.cdp.driver.time.sleep"):
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertTrue(result)
        wrapper.bounding_box_click.assert_called_once_with(SEL_POPUP_CLOSE)

    def test_returns_false_when_no_popup(self):
        base_driver = MagicMock()
        base_driver.find_elements.return_value = []
        wrapper = MagicMock()
        wrapper._driver = base_driver

        times = iter([0.0, 0.0, 1.0])
        with patch.object(drv, "WebDriverWait", None), \
             patch("modules.cdp.driver.time.monotonic", side_effect=lambda: next(times)), \
             patch("modules.cdp.driver.time.sleep"):
            result = handle_something_wrong_popup(wrapper, timeout=0.1)

        self.assertFalse(result)

    def test_NEVER_calls_removeNode(self):
        source = inspect.getsource(handle_something_wrong_popup)
        self.assertNotIn("removeNode", source)
        self.assertNotIn("removeChild", source)
        self.assertNotIn(".remove(", source)


if __name__ == "__main__":
    unittest.main()
