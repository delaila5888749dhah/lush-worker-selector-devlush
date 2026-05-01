import random
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver


def _make_driver(rect):
    driver = MagicMock()
    driver.find_elements.return_value = [MagicMock()]
    driver.execute_script.return_value = rect
    return driver


class TestBoundingBoxOffset(unittest.TestCase):
    def test_offset_x_within_minus15_plus15(self):
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        driver = _make_driver(rect)
        gd = GivexDriver(driver)
        gd._rnd = random.Random(5)

        offsets = []
        with patch.object(gd, "_ghost_move_to"), \
             patch("modules.cdp.driver.time.sleep"):
            for _ in range(100):
                driver.execute_cdp_cmd.reset_mock()
                gd.bounding_box_click("#btn")
                payload = [
                    c.args[1] for c in driver.execute_cdp_cmd.call_args_list
                    if c.args[1]["type"] == "mousePressed"
                ][0]
                center_x = rect["left"] + rect["width"] / 2
                offsets.append(payload["x"] - center_x)

        for offset in offsets:
            self.assertGreaterEqual(offset, -15)
            self.assertLessEqual(offset, 15)

    def test_offset_y_within_minus5_plus5(self):
        rect = {"left": 10, "top": 20, "width": 200, "height": 80}
        driver = _make_driver(rect)
        gd = GivexDriver(driver)
        gd._rnd = random.Random(8)

        offsets = []
        with patch.object(gd, "_ghost_move_to"), \
             patch("modules.cdp.driver.time.sleep"):
            for _ in range(100):
                driver.execute_cdp_cmd.reset_mock()
                gd.bounding_box_click("#btn")
                payload = [
                    c.args[1] for c in driver.execute_cdp_cmd.call_args_list
                    if c.args[1]["type"] == "mousePressed"
                ][0]
                center_y = rect["top"] + rect["height"] / 2
                offsets.append(payload["y"] - center_y)

        for offset in offsets:
            self.assertGreaterEqual(offset, -5)
            self.assertLessEqual(offset, 5)


if __name__ == "__main__":
    unittest.main()
