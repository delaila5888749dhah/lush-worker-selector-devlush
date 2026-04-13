"""Unit tests for modules.cdp.driver.GivexDriver (mock Selenium driver)."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from modules.cdp.driver import (
    GivexDriver,
    SEL_CARD_NUMBER,
    SEL_COMPLETE_PURCHASE,
    SEL_COOKIE_ACCEPT,
    SEL_VBV_IFRAME,
    SEL_DECLINED_MSG,
    SEL_UI_LOCK_SPINNER,
    URL_CONFIRM_FRAGMENT,
)


def _make_mock_driver():
    """Return a MagicMock that mimics a minimal Selenium WebDriver."""
    driver = MagicMock()
    driver.current_url = "https://example.com/checkout"
    driver.window_handles = ["tab-1"]
    driver.find_elements.return_value = []
    driver.find_element.return_value = MagicMock()
    driver.execute_script.return_value = {"x": 100, "y": 200, "width": 200, "height": 50}
    driver.execute_cdp_cmd.return_value = None
    return driver


def _make_card_info(card_number="4111111111111111", exp_month="07", exp_year="27", cvv="123"):
    return SimpleNamespace(
        card_number=card_number,
        exp_month=exp_month,
        exp_year=exp_year,
        cvv=cvv,
    )


def _make_billing_profile():
    return SimpleNamespace(
        first_name="Jane",
        last_name="Doe",
        address="123 Main St",
        city="New York",
        state="NY",
        zip_code="10001",
        phone="2125550100",
        email="jane@example.com",
    )


class TestPreflightGeoCheck(unittest.TestCase):
    def _make_driver(self, country):
        driver = _make_mock_driver()
        body_el = MagicMock()
        body_el.text = json.dumps({"country": country, "ip": "1.2.3.4"})
        driver.find_element.return_value = body_el
        return driver

    def test_preflight_geo_check_pass(self):
        """Geo check passes when country is 'US'."""
        driver = self._make_driver("US")
        gd = GivexDriver(driver)
        result = gd.preflight_geo_check()
        self.assertTrue(result)
        driver.get.assert_called_once()

    def test_preflight_geo_check_fail(self):
        """Geo check raises RuntimeError when country is not 'US'."""
        driver = self._make_driver("GB")
        gd = GivexDriver(driver)
        with self.assertRaises(RuntimeError) as ctx:
            gd.preflight_geo_check()
        self.assertIn("GB", str(ctx.exception))


class TestHardResetState(unittest.TestCase):
    def test_hard_reset_state_executes_js(self):
        """_hard_reset_state must call execute_script exactly 3 times."""
        driver = _make_mock_driver()
        gd = GivexDriver(driver)
        gd._hard_reset_state()
        self.assertEqual(driver.execute_script.call_count, 3)
        calls_text = " ".join(str(c) for c in driver.execute_script.call_args_list)
        self.assertIn("cookie", calls_text)
        self.assertIn("localStorage", calls_text)
        self.assertIn("sessionStorage", calls_text)


class TestCloseExtraTabs(unittest.TestCase):
    def test_close_extra_tabs_closes_all_but_first(self):
        """_close_extra_tabs must close all tabs after the first."""
        driver = _make_mock_driver()
        driver.window_handles = ["tab-1", "tab-2", "tab-3"]
        gd = GivexDriver(driver)
        gd._close_extra_tabs()
        self.assertEqual(driver.close.call_count, 2)
        # Final switch must go back to the first tab
        driver.switch_to.window.assert_called_with("tab-1")

    def test_close_extra_tabs_noop_with_single_tab(self):
        """_close_extra_tabs must be a no-op when only one tab is open."""
        driver = _make_mock_driver()
        driver.window_handles = ["tab-1"]
        gd = GivexDriver(driver)
        gd._close_extra_tabs()
        driver.close.assert_not_called()


class TestBoundingBoxClick(unittest.TestCase):
    def test_bounding_box_click_uses_offset(self):
        """bounding_box_click must dispatch mousePressed and mouseReleased via CDP."""
        driver = _make_mock_driver()
        el = MagicMock()
        driver.find_element.return_value = el
        driver.execute_script.return_value = {
            "x": 100.0, "y": 200.0, "width": 120.0, "height": 40.0
        }
        gd = GivexDriver(driver)
        gd.bounding_box_click("button#submit")

        cdp_calls = driver.execute_cdp_cmd.call_args_list
        types = [c[0][1]["type"] for c in cdp_calls]
        self.assertIn("mousePressed", types)
        self.assertIn("mouseReleased", types)

    def test_bounding_box_click_x_within_bounds(self):
        """CDP click coordinates must land within the element bounding box ± offset."""
        driver = _make_mock_driver()
        driver.execute_script.return_value = {
            "x": 50.0, "y": 50.0, "width": 100.0, "height": 30.0
        }
        gd = GivexDriver(driver)
        gd.bounding_box_click("div#target", x_offset_range=10, y_offset_range=5)

        pressed_call = next(
            c for c in driver.execute_cdp_cmd.call_args_list
            if c[0][1].get("type") == "mousePressed"
        )
        x = pressed_call[0][1]["x"]
        y = pressed_call[0][1]["y"]
        # center_x = 50 + 50 = 100, offset ±10 → [90, 110]
        self.assertGreaterEqual(x, 90)
        self.assertLessEqual(x, 110)
        # center_y = 50 + 15 = 65, offset ±5 → [60, 70]
        self.assertGreaterEqual(y, 60)
        self.assertLessEqual(y, 70)


class TestFillCard4x4Groups(unittest.TestCase):
    def test_fill_card_4x4_groups(self):
        """fill_card must send 16 char-type CDP events (4 groups × 4 digits)."""
        driver = _make_mock_driver()
        driver.find_elements.return_value = []  # no hesitation elements
        driver.execute_script.return_value = {
            "x": 10.0, "y": 10.0, "width": 80.0, "height": 25.0
        }
        gd = GivexDriver(driver)
        card = _make_card_info(card_number="4111111111111111")

        with patch("time.sleep"):
            gd.fill_card(card)

        char_events = [
            c for c in driver.execute_cdp_cmd.call_args_list
            if c[0][0] == "Input.dispatchKeyEvent"
            and c[0][1].get("type") == "char"
            and len(c[0][1].get("text", "")) == 1
            and c[0][1]["text"].isdigit()
        ]
        # 16 digits for card number + month digits + year digits + cvv digits
        card_digit_events = [e for e in char_events if e[0][1]["text"] in "0123456789"]
        self.assertGreaterEqual(len(card_digit_events), 16)

    def test_fill_card_sleeps_between_groups(self):
        """fill_card must sleep between the 4 digit groups."""
        driver = _make_mock_driver()
        driver.find_elements.return_value = []
        driver.execute_script.return_value = {
            "x": 10.0, "y": 10.0, "width": 80.0, "height": 25.0
        }
        gd = GivexDriver(driver)
        card = _make_card_info(card_number="4111111111111111")

        sleep_calls = []
        with patch("modules.cdp.driver.time") as mock_time:
            mock_time.sleep = MagicMock(side_effect=lambda t: sleep_calls.append(t))
            mock_time.uniform = __import__("random").uniform
            gd.fill_card(card)

        # At least 3 inter-group sleeps (between 4 groups) + hesitation sleep
        self.assertGreaterEqual(len(sleep_calls), 3)


class TestClearCardFields(unittest.TestCase):
    def test_clear_card_fields_sends_ctrl_a_backspace(self):
        """clear_card_fields must send Ctrl+A then Backspace via CDP."""
        driver = _make_mock_driver()
        driver.execute_script.return_value = {
            "x": 0.0, "y": 0.0, "width": 100.0, "height": 30.0
        }
        gd = GivexDriver(driver)
        gd.clear_card_fields()

        key_events = [
            c[0][1] for c in driver.execute_cdp_cmd.call_args_list
            if c[0][0] == "Input.dispatchKeyEvent"
        ]
        keys = [e["key"] for e in key_events]
        self.assertIn("a", keys)
        self.assertIn("Backspace", keys)
        # Ctrl modifier must be set on the 'a' keyDown
        ctrl_a = next(e for e in key_events if e["key"] == "a" and e["type"] == "keyDown")
        self.assertEqual(ctrl_a["modifiers"], 2)


class TestDetectPageState(unittest.TestCase):
    def test_detect_page_state_success(self):
        """URL containing the confirmation fragment returns 'success'."""
        driver = _make_mock_driver()
        driver.current_url = f"https://example.com{URL_CONFIRM_FRAGMENT}?order=123"
        driver.find_elements.return_value = []
        gd = GivexDriver(driver)
        self.assertEqual(gd.detect_page_state(), "success")

    def test_detect_page_state_vbv(self):
        """VBV iframe present returns 'vbv_3ds'."""
        driver = _make_mock_driver()
        driver.current_url = "https://example.com/checkout"
        iframe_mock = MagicMock()

        def find_elements_side_effect(by, sel):
            if sel == SEL_VBV_IFRAME:
                return [iframe_mock]
            return []

        driver.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(driver)
        self.assertEqual(gd.detect_page_state(), "vbv_3ds")

    def test_detect_page_state_declined(self):
        """Declined message element present returns 'declined'."""
        driver = _make_mock_driver()
        driver.current_url = "https://example.com/checkout"
        declined_mock = MagicMock()

        def find_elements_side_effect(by, sel):
            if sel == SEL_DECLINED_MSG:
                return [declined_mock]
            return []

        driver.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(driver)
        self.assertEqual(gd.detect_page_state(), "declined")

    def test_detect_page_state_ui_lock(self):
        """Loading spinner present returns 'ui_lock'."""
        driver = _make_mock_driver()
        driver.current_url = "https://example.com/checkout"
        spinner_mock = MagicMock()

        def find_elements_side_effect(by, sel):
            if sel == SEL_UI_LOCK_SPINNER:
                return [spinner_mock]
            return []

        driver.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(driver)
        self.assertEqual(gd.detect_page_state(), "ui_lock")

    def test_detect_page_state_unknown(self):
        """No matching indicator returns 'unknown'."""
        driver = _make_mock_driver()
        driver.current_url = "https://example.com/checkout"
        driver.find_elements.return_value = []
        gd = GivexDriver(driver)
        self.assertEqual(gd.detect_page_state(), "unknown")


class TestNavigateToEgift(unittest.TestCase):
    def test_navigate_to_egift_clears_storage(self):
        """navigate_to_egift must call _hard_reset_state after navigation."""
        driver = _make_mock_driver()
        driver.find_elements.return_value = []
        gd = GivexDriver(driver)

        with patch.object(gd, "_hard_reset_state") as mock_reset:
            gd.navigate_to_egift()
            mock_reset.assert_called_once()

    def test_navigate_to_egift_navigates_to_base_then_egift(self):
        """navigate_to_egift must navigate to URL_BASE and then URL_EGIFT."""
        driver = _make_mock_driver()
        driver.find_elements.return_value = []
        gd = GivexDriver(driver)

        with patch.object(gd, "_hard_reset_state"):
            gd.navigate_to_egift()

        get_urls = [c[0][0] for c in driver.get.call_args_list]
        from modules.cdp.driver import URL_BASE, URL_EGIFT
        self.assertIn(URL_BASE, get_urls)
        self.assertIn(URL_EGIFT, get_urls)


class TestCookieBannerClick(unittest.TestCase):
    def test_cookie_banner_clicked_when_present(self):
        """When the cookie banner is present it should be clicked."""
        driver = _make_mock_driver()
        cookie_btn = MagicMock()
        driver.execute_script.return_value = {
            "x": 0.0, "y": 0.0, "width": 100.0, "height": 40.0
        }

        def find_elements_side_effect(by, sel):
            if sel == SEL_COOKIE_ACCEPT:
                return [cookie_btn]
            return []

        driver.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(driver)

        with patch.object(gd, "_hard_reset_state"):
            gd.navigate_to_egift()

        cdp_calls = driver.execute_cdp_cmd.call_args_list
        mouse_events = [c for c in cdp_calls if c[0][0] == "Input.dispatchMouseEvent"]
        self.assertTrue(len(mouse_events) > 0, "Expected at least one mouse event for cookie click")

    def test_cookie_banner_not_clicked_when_absent(self):
        """When the cookie banner is absent no extra click should occur during navigation."""
        driver = _make_mock_driver()
        driver.find_elements.return_value = []
        gd = GivexDriver(driver)

        with patch.object(gd, "_hard_reset_state"):
            with patch.object(gd, "bounding_box_click") as mock_click:
                gd.navigate_to_egift()

        # Only the SEL_BUY_EGIFT_BTN click should have been attempted
        # (and even that may silently fail if element absent)
        cookie_clicks = [
            c for c in mock_click.call_args_list
            if c[0][0] == SEL_COOKIE_ACCEPT
        ]
        self.assertEqual(len(cookie_clicks), 0)


if __name__ == "__main__":
    unittest.main()
