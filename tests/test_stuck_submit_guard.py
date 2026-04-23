"""Tests for Issue #186 — 3s stuck-submit guard in detect_page_state.

Covers:
- test_3s_timeout_maps_to_ui_lock  — driver stuck for entire 3s → returns 'ui_lock'
- test_success_detected_during_poll — URL changes to /confirmation during poll → 'success'
- test_vbv_detected_during_poll     — VBV iframe appears during poll → 'vbv_3ds'
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, URL_CONFIRM_FRAGMENTS, SEL_VBV_IFRAME


def _make_driver(current_url="https://example.com/checkout"):
    d = MagicMock()
    d.current_url = current_url
    d.find_elements.return_value = []
    body_el = MagicMock()
    body_el.text = ""
    d.find_element.return_value = body_el
    return d


class TestStuckSubmitGuard(unittest.TestCase):
    """detect_page_state: 3s polling fallback for stuck/unknown page states."""

    def test_3s_timeout_maps_to_ui_lock(self):
        """When all state signals remain absent for 3s, returns 'ui_lock'."""
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)

        # time.time returns 0.0 for deadline calc, then 100.0 to exit loop immediately.
        with patch("modules.cdp.driver.time.sleep") as mock_sleep, \
             patch("modules.cdp.driver.time.time", side_effect=[0.0, 100.0]):
            result = gd.detect_page_state()

        self.assertEqual(result, "ui_lock")
        # sleep was NOT called because the loop body never executed
        mock_sleep.assert_not_called()

    def test_success_detected_during_poll(self):
        """URL changes to a confirmation fragment during the 3s poll → 'success'."""
        confirm_url = "https://example.com" + URL_CONFIRM_FRAGMENTS[0]
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)

        # time.time: first call sets deadline, second is while-check (within 3s).
        # After one sleep, current_url changes to a confirm URL.
        call_count = {"n": 0}

        def url_side_effect():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "https://example.com/checkout"
            return confirm_url

        selenium_mock = selenium
        type(selenium_mock).current_url = property(lambda self: url_side_effect())

        with patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.time", side_effect=[0.0, 1.0, 1.0]):
            result = gd.detect_page_state()

        self.assertEqual(result, "success")

    def test_vbv_detected_during_poll(self):
        """VBV iframe appears during the 3s poll → 'vbv_3ds'."""
        selenium = _make_driver()
        iframe_el = MagicMock()
        first_vbv = SEL_VBV_IFRAME.split(",")[0].strip()

        # First call (initial check before poll): no elements found.
        # Poll loop: return iframe element for VBV selector.
        call_count = {"n": 0}

        def find_elements_side_effect(_method, selector):
            call_count["n"] += 1
            # Initial pass through the checks returns nothing.
            if call_count["n"] <= 3:
                return []
            # Inside the poll loop: return iframe for VBV selector.
            if selector.strip() == first_vbv:
                return [iframe_el]
            return []

        selenium.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(selenium)

        with patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.time", side_effect=[0.0, 1.0, 1.0]):
            result = gd.detect_page_state()

        self.assertEqual(result, "vbv_3ds")

    def test_declined_detected_during_poll(self):
        """'error=vv' appears in URL during the 3s poll → 'declined'."""
        selenium = _make_driver()
        selenium.find_elements.return_value = []
        gd = GivexDriver(selenium)

        call_count = {"n": 0}

        def url_side_effect():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "https://example.com/checkout"
            return "https://example.com/checkout?error=vv"

        type(selenium).current_url = property(lambda self: url_side_effect())

        with patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.time", side_effect=[0.0, 1.0, 1.0]):
            result = gd.detect_page_state()

        self.assertEqual(result, "declined")

    def test_ui_lock_spinner_detected_during_poll(self):
        """Spinner appears during the 3s poll → 'ui_lock'."""
        selenium = _make_driver()
        from modules.cdp.driver import SEL_UI_LOCK_SPINNER
        spinner_el = MagicMock()
        first_spinner = SEL_UI_LOCK_SPINNER.split(",")[0].strip()

        call_count = {"n": 0}

        def find_elements_side_effect(_method, selector):
            call_count["n"] += 1
            if call_count["n"] <= 3:
                return []
            if selector.strip() == first_spinner:
                return [spinner_el]
            return []

        selenium.find_elements.side_effect = find_elements_side_effect
        gd = GivexDriver(selenium)

        with patch("modules.cdp.driver.time.sleep"), \
             patch("modules.cdp.driver.time.time", side_effect=[0.0, 1.0, 1.0]):
            result = gd.detect_page_state()

        self.assertEqual(result, "ui_lock")


if __name__ == "__main__":
    unittest.main()
