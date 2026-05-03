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
from modules.cdp.keyboard import dispatch_key
from modules.common.exceptions import CDPCommandError, SelectorTimeoutError


def _make_driver():
    d = MagicMock()
    d.current_url = "https://example.com/page"
    d.find_elements.return_value = []
    return d


def _option_result(current_idx, values):
    return [current_idx, values[current_idx] if current_idx >= 0 else "", False, [{"value": value, "text": value} for value in values]]


class TestCdpSelectOption(unittest.TestCase):
    """_cdp_select_option uses CDP click + key navigation, not Select.select_by_value."""

    def test_dropdown_uses_cdp_keynav_not_select_by_value(self):
        """Select class must not be instantiated; dispatch_key must be called."""
        selenium = _make_driver()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        # current_idx=0, target_idx=2 (delta=+2 → 2× ArrowDown + Enter)
        selenium.execute_script.return_value = _option_result(0, ["01", "02", "03"])

        gd = GivexDriver(selenium, strict=False)

        with patch("modules.cdp.keyboard.dispatch_key") as mock_dispatch, \
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
        selenium.execute_script.return_value = _option_result(
            5, ["2022", "2023", "2024", "2025", "2026", "2027"]
        )  # delta=-3

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
        selenium.execute_script.return_value = _option_result(-1, ["US", "CA"])

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
        selenium.execute_script.return_value = _option_result(0, ["US", "CA"])

        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.keyboard.dispatch_key"), \
                patch.object(gd, "bounding_box_click"), \
                patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(ValueError):
                gd._cdp_select_option("#country", "ZZ")

    def test_dropdown_unexpected_metadata_message_includes_safe_shape_summary(self):
        """Malformed JS result diagnostics should explain the result shape."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = [-1, -1]

        gd = GivexDriver(selenium, strict=False)
        with patch("modules.cdp.keyboard.dispatch_key") as mock_dispatch, \
                patch.object(gd, "bounding_box_click"), \
                patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(ValueError) as ctx:
                gd._cdp_select_option("#country", "US")

        message = str(ctx.exception)
        self.assertIn("type=list", message)
        self.assertIn("len=2", message)
        self.assertIn("item_types=['int', 'int']", message)
        self.assertIn("selector=UNREGISTERED_SELECTOR", message)
        mock_dispatch.assert_not_called()

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

    def test_dropdown_aborts_when_arrow_dispatch_fails(self):
        """Audit [F1]: a failed ``dispatch_key`` MUST stop the navigation
        loop and surface the error — silently continuing then sending
        Enter would leave the dropdown unchanged while pretending success.
        """
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = _option_result(0, ["01", "02", "03"])

        gd = GivexDriver(selenium, strict=False)
        with patch(
            "modules.cdp.keyboard.dispatch_key", return_value=False,
        ) as mock_dispatch, patch.object(
            gd, "bounding_box_click",
        ), patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(CDPCommandError):
                gd._cdp_select_option("#month", "03")
        # Only the first ArrowDown should be attempted before the abort —
        # crucially Enter must NOT be dispatched after a failed arrow.
        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["ArrowDown"])

    def test_dropdown_aborts_when_enter_dispatch_fails(self):
        """Audit [F1]: a failed confirming Enter must also surface."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = _option_result(2, ["01", "02", "03"])

        gd = GivexDriver(selenium, strict=False)
        with patch(
            "modules.cdp.keyboard.dispatch_key", return_value=False,
        ) as mock_dispatch, patch.object(
            gd, "bounding_box_click",
        ), patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(CDPCommandError):
                gd._cdp_select_option("#month", "03")
        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["Enter"])

    def test_dropdown_missing_value_does_not_dispatch_any_key(self):
        """Audit [F2]: when the option is missing, no key event should fire."""
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = _option_result(0, ["US", "CA"])

        gd = GivexDriver(selenium, strict=False)
        with patch(
            "modules.cdp.keyboard.dispatch_key",
        ) as mock_dispatch, patch.object(
            gd, "bounding_box_click",
        ), patch("modules.cdp.driver.time.sleep"):
            with self.assertRaises(ValueError):
                gd._cdp_select_option("#country", "ZZ")
        mock_dispatch.assert_not_called()

    def test_dropdown_no_prior_selection_navigates_to_inner_index(self):
        """Audit [F2/N2]: generalize the no-prior-selection formula — for
        ``[-1, 3]`` we expect ``target_idx + 1`` ArrowDown presses then Enter.
        """
        selenium = _make_driver()
        selenium.find_elements.return_value = [MagicMock()]
        selenium.execute_script.return_value = _option_result(-1, ["US", "CA", "MX", "FR"])

        gd = GivexDriver(selenium, strict=False)
        with patch(
            "modules.cdp.keyboard.dispatch_key",
        ) as mock_dispatch, patch.object(
            gd, "bounding_box_click",
        ), patch("modules.cdp.driver.time.sleep"):
            gd._cdp_select_option("#country", "FR")

        keys = [c.args[1] for c in mock_dispatch.call_args_list]
        self.assertEqual(keys, ["ArrowDown"] * 4 + ["Enter"])


class TestFlexibleOptionMatching(unittest.TestCase):
    """Expiry dropdown helper accepts site-specific value/text variants."""

    def test_month_numeric_matches_unpadded_value_with_name_text(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_MONTH,
            "06",
            [{"value": "6", "text": "June"}],
        )
        self.assertEqual(idx, 0)

    def test_month_exact_value_wins_before_normalized_match(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_MONTH,
            "06",
            [
                {"value": "6", "text": "June"},
                {"value": "06", "text": "June"},
            ],
        )
        self.assertEqual(idx, 1)

    def test_month_numeric_matches_abbrev_value(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_MONTH,
            "06",
            [{"value": "jun", "text": "June"}],
        )
        self.assertEqual(idx, 0)

    def test_month_name_matches_numeric_value(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_MONTH,
            "June",
            [{"value": "6", "text": "6"}],
        )
        self.assertEqual(idx, 0)

    def test_year_full_matches_two_digit_value(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_YEAR,
            "2028",
            [{"value": "28", "text": "28"}],
            current_year=2026,
        )
        self.assertEqual(idx, 0)

    def test_year_two_digit_matches_full_year_in_window(self):
        idx = drv._find_matching_option_index(
            drv.SEL_CARD_EXPIRY_YEAR,
            "28",
            [{"value": "2028", "text": "2028"}],
            current_year=2026,
        )
        self.assertEqual(idx, 0)

    def test_year_two_digit_does_not_match_pre_2000_year(self):
        with self.assertRaises(ValueError) as ctx:
            drv._find_matching_option_index(
                drv.SEL_CARD_EXPIRY_YEAR,
                "28",
                [{"value": "1928", "text": "1928"}],
                current_year=2026,
            )
        message = str(ctx.exception)
        self.assertIn("Available values=['1928']", message)
        self.assertIn("texts=['1928']", message)

    def test_month_no_match_lists_expiry_values_and_texts(self):
        with self.assertRaises(ValueError) as ctx:
            drv._find_matching_option_index(
                drv.SEL_CARD_EXPIRY_MONTH,
                "06",
                [
                    {"value": "01", "text": "January"},
                    {"value": "02", "text": "February"},
                ],
            )
        message = str(ctx.exception)
        self.assertIn("Available values=['01', '02']", message)
        self.assertIn("texts=['January', 'February']", message)


class TestDispatchKey(unittest.TestCase):
    """Audit [F2]: direct coverage for ``modules.cdp.keyboard.dispatch_key``."""

    def test_dispatch_key_emits_keydown_keyup_payload(self):
        """ArrowDown must emit keyDown+keyUp with proper code + windowsVirtualKeyCode."""
        selenium = MagicMock()
        result = dispatch_key(selenium, "ArrowDown")
        self.assertTrue(result)
        calls = selenium.execute_cdp_cmd.call_args_list
        self.assertEqual(len(calls), 2)
        for call, expected_type in zip(calls, ("keyDown", "keyUp")):
            method, payload = call.args
            self.assertEqual(method, "Input.dispatchKeyEvent")
            self.assertEqual(payload["type"], expected_type)
            self.assertEqual(payload["key"], "ArrowDown")
            self.assertEqual(payload["code"], "ArrowDown")
            self.assertEqual(payload["windowsVirtualKeyCode"], 40)
            self.assertEqual(payload["modifiers"], 0)
            self.assertFalse(payload["isKeypad"])

    def test_dispatch_key_enter_payload(self):
        """Enter must map to vk=13."""
        selenium = MagicMock()
        self.assertTrue(dispatch_key(selenium, "Enter"))
        for call in selenium.execute_cdp_cmd.call_args_list:
            self.assertEqual(call.args[1]["windowsVirtualKeyCode"], 13)
            self.assertEqual(call.args[1]["code"], "Enter")

    def test_dispatch_key_unsupported_key_raises_value_error(self):
        """Unknown key names must raise ``ValueError`` (no CDP call attempted)."""
        selenium = MagicMock()
        with self.assertRaises(ValueError):
            dispatch_key(selenium, "Nope")
        selenium.execute_cdp_cmd.assert_not_called()

    def test_dispatch_key_returns_false_on_cdp_failure(self):
        """CDP exceptions are swallowed and reported via False return value."""
        selenium = MagicMock()
        selenium.execute_cdp_cmd.side_effect = RuntimeError("boom")
        self.assertFalse(dispatch_key(selenium, "Enter"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
