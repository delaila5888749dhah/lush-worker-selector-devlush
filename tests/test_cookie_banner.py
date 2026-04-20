"""PR-4 L2 — Cookie banner handler tests."""
import unittest
from unittest.mock import MagicMock

from modules.cdp import driver as cdp_driver


def _make_gd_instance(find_elements_result, click_side_effect=None):
    gd = cdp_driver.GivexDriver.__new__(cdp_driver.GivexDriver)
    gd._driver = MagicMock()
    gd._persona = None
    gd._cursor = None
    gd._sm = None
    gd.find_elements = MagicMock(return_value=find_elements_result)
    gd.bounding_box_click = MagicMock(side_effect=click_side_effect)
    return gd


class CookieBannerTests(unittest.TestCase):
    def test_accept_cookies_when_present(self):
        gd = _make_gd_instance(find_elements_result=[MagicMock()])
        ok = gd.accept_cookies_if_present()
        self.assertTrue(ok)
        gd.bounding_box_click.assert_called_once_with(
            cdp_driver.SEL_COOKIE_ACCEPT,
        )

    def test_no_cookies_returns_false_no_raise(self):
        gd = _make_gd_instance(find_elements_result=[])
        ok = gd.accept_cookies_if_present()
        self.assertFalse(ok)
        gd.bounding_box_click.assert_not_called()

    def test_click_failure_returns_false_no_raise(self):
        gd = _make_gd_instance(
            find_elements_result=[MagicMock()],
            click_side_effect=RuntimeError("click failed"),
        )
        ok = gd.accept_cookies_if_present()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
