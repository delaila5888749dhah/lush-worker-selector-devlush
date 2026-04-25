"""Tests for CDP-based dropdown selection — Phase 3C Task 2 (audit [G2]).

Covers:
- ``_cdp_select_option`` opens the dropdown via ``bounding_box_click``
  rather than the Selenium ``Select`` helper.
- Selection navigates with ``ArrowDown``/``ArrowUp`` named-key events
  and confirms with ``Enter``.
- Missing option values raise ``ValueError``.
- Missing element raises ``SelectorTimeoutError``.
"""

import unittest
from unittest.mock import MagicMock, patch

from modules.cdp import driver as drv
from modules.cdp.driver import GivexDriver
from modules.common.exceptions import SelectorTimeoutError


def _make_driver():
    d = MagicMock()
    d.current_url = "https://example.com/page"
    d.find_elements.return_value = []
    return d


class TestCdpSelectOption(unittest.TestCase):
    """_cdp_select_option uses CDP click + key navigation, not Select.select_by_value."""

    def test_dropdown_uses_cdp_keynav_not_select_by_value(self):
        """Select class must not be instantiated; dispatch_key must be called."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        # current_idx=0, target_idx=2 (delta=+2 → 2× ArrowDown + Enter)
        selenium.execute_script.return_value = [0, 2]

        gd = GivexDriver(selenium, strict=False)

        with patch("modules.cdp.driver.dispatch_key", create=True) as _unused, \
                patch("modules.cdp.keyboard.dispatch_key") as mock_dispatch, \
                patch.object(gd, "bounding_box_click") as mock_click, \
                patch("modules.cdp.driver.time.sleep"):
            gd._cdp_select_option("#month", "03")

        # Dropdown opened via bounding_box_click (CDP isTrusted=True).
        mock_click.assert_called_once_with("#month")

        # 2× ArrowDown + 1× Enter
        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["ArrowDown", "ArrowDown", "Enter"])

    def test_dropdown_uses_arrow_up_when_target_below_current(self):
        """Negative delta → ArrowUp."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = [5, 2]  # delta=-3

        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.keyboard.dispatch_key") as mock_dispatch, \
                patch.object(gd, "bounding_box_click"), \
                patch("modules.cdp.driver.time.sleep"):
            gd._cdp_select_option("#year", "2024")

        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["ArrowUp", "ArrowUp", "ArrowUp", "Enter"])

    def test_dropdown_no_prior_selection_navigates_from_top(self):
        """When selectedIndex==-1, ArrowDown lands on index 0 first."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        # No current selection → current_idx=-1, target_idx=0 → 1 ArrowDown
        selenium.execute_script.return_value = [-1, 0]

        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.keyboard.dispatch_key") as mock_dispatch, \
                patch.object(gd, "bounding_box_click"), \
                patch("modules.cdp.driver.time.sleep"):
            gd._cdp_select_option("#country", "US")

        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["ArrowDown", "Enter"])

    def test_dropdown_missing_value_raises(self):
        """Option value not present → ValueError."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = [0, -1]  # not found

        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.keyboard.dispatch_key"), \
                patch.object(gd, "bounding_box_click"), \
                patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(ValueError):
                gd._cdp_select_option("#country", "ZZ")

    def test_dropdown_missing_element_raises_selector_timeout(self):
        """Empty find_elements → SelectorTimeoutError."""
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium, strict=False)
        with self.assertRaises(SelectorTimeoutError):
            gd._cdp_select_option("#missing", "x")

    def test_dropdown_does_not_use_select_class(self):
        """Acceptance: Select(elements[0]) MUST NOT be invoked anywhere
        in the payment/billing dropdown flow.

        We assert by introspection: the driver module no longer imports
        the ``Select`` symbol from ``selenium.webdriver.support.ui``.
        """
        self.assertFalse(hasattr(drv, "Select"),
                         "Select must not be imported in modules.cdp.driver")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
