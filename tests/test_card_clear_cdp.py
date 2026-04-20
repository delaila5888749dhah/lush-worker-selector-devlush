import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_CARD_CVV, SEL_CARD_NUMBER


def _make_driver():
    driver = MagicMock()
    element = MagicMock()
    driver.find_elements.return_value = [element]
    return driver


class TestCardClearCdp(unittest.TestCase):
    def test_dispatches_ctrl_a_with_modifier_2(self):
        driver = _make_driver()
        gd = GivexDriver(driver)
        with patch.object(gd, "bounding_box_click"):
            gd.clear_card_fields_cdp()
        events = [call.args[1] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertTrue(any(evt.get("modifiers") == 2 and evt.get("key") == "a" for evt in events))

    def test_dispatches_backspace_after_ctrl_a(self):
        driver = _make_driver()
        gd = GivexDriver(driver)
        with patch.object(gd, "bounding_box_click"):
            gd.clear_card_fields_cdp()
        events = [call.args[1]["key"] for call in driver.execute_cdp_cmd.call_args_list]
        self.assertEqual(events[:2], ["a", "a"])
        self.assertEqual(events[2:4], ["Backspace", "Backspace"])

    def test_clears_both_card_number_and_cvv(self):
        driver = _make_driver()
        gd = GivexDriver(driver)
        with patch.object(gd, "bounding_box_click"):
            gd.clear_card_fields_cdp()
        self.assertEqual(driver.execute_cdp_cmd.call_count, 8)
        selectors = [call.args[1] for call in driver.find_elements.call_args_list]
        self.assertIn(SEL_CARD_NUMBER, selectors)
        self.assertIn(SEL_CARD_CVV, selectors)


if __name__ == "__main__":
    unittest.main()
