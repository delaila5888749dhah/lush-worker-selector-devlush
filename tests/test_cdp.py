"""Unit tests for modules/cdp/main.py.

Covers:
- Driver registry (register_driver, unregister_driver, _get_driver)
- _sanitize_error() PII redaction
- _register_pid() and force_kill() PID tracking
- Business-logic delegation (detect_page_state, fill_card, fill_billing,
  clear_card_fields)
- Thread-safety smoke test for the driver registry
"""

import signal
import threading
import unittest
from unittest.mock import MagicMock, patch

import modules.cdp.main as cdp
from modules.cdp.main import (
    _sanitize_error,
    _register_pid,
    force_kill,
    register_driver,
    unregister_driver,
    detect_page_state,
    fill_card,
    fill_billing,
    clear_card_fields,
)
from modules.common.exceptions import PageStateError, SelectorTimeoutError


def _reset_cdp():
    """Clear both internal registries to give each test a clean slate."""
    with cdp._registry_lock:
        cdp._driver_registry.clear()
        cdp._pid_registry.clear()


class DriverRegistryTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()

    def tearDown(self):
        _reset_cdp()

    def test_register_and_get_driver(self):
        driver = MagicMock()
        register_driver("w1", driver)
        # _get_driver should return the same object
        self.assertIs(cdp._get_driver("w1"), driver)

    def test_unregister_driver_removes_entry(self):
        driver = MagicMock()
        register_driver("w1", driver)
        unregister_driver("w1")
        with self.assertRaises(RuntimeError):
            cdp._get_driver("w1")

    def test_unregister_unknown_worker_is_noop(self):
        # Should not raise
        unregister_driver("nonexistent")

    def test_get_driver_raises_when_not_registered(self):
        with self.assertRaises(RuntimeError):
            cdp._get_driver("ghost")

    def test_register_overwrites_previous_driver(self):
        driver_a = MagicMock()
        driver_b = MagicMock()
        register_driver("w1", driver_a)
        register_driver("w1", driver_b)
        self.assertIs(cdp._get_driver("w1"), driver_b)


# ---------------------------------------------------------------------------
# _sanitize_error() tests
# ---------------------------------------------------------------------------

class SanitizeErrorTests(unittest.TestCase):
    def test_redacts_16_digit_card_number(self):
        msg = "Card 4111111111111111 was declined"
        result = _sanitize_error(msg)
        self.assertNotIn("4111111111111111", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_redacts_email_address(self):
        msg = "User user@example.com submitted payment"
        result = _sanitize_error(msg)
        self.assertNotIn("user@example.com", result)
        self.assertIn("[REDACTED-EMAIL]", result)

    def test_redacts_cvv_pattern(self):
        msg = "cvv=123 was rejected"
        result = _sanitize_error(msg)
        self.assertNotIn("cvv=123", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_redacts_cvv_with_spaces(self):
        msg = "CVV = 9876 mismatch"
        result = _sanitize_error(msg)
        self.assertNotIn("9876", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_clean_string_passes_through_unchanged(self):
        msg = "Connection refused to checkout endpoint"
        result = _sanitize_error(msg)
        self.assertEqual(msg, result)

    def test_multiple_pii_types_all_redacted(self):
        msg = "Card 5500005555555559 from admin@corp.com cvv=456"
        result = _sanitize_error(msg)
        self.assertNotIn("5500005555555559", result)
        self.assertNotIn("admin@corp.com", result)
        self.assertNotIn("456", result)
        self.assertIn("[REDACTED-CARD]", result)
        self.assertIn("[REDACTED-EMAIL]", result)


# ---------------------------------------------------------------------------
# _register_pid() and force_kill() tests
# ---------------------------------------------------------------------------

class PidRegistryTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()

    def tearDown(self):
        _reset_cdp()

    def test_register_pid_stores_pid(self):
        _register_pid("w1", 12345)
        with cdp._registry_lock:
            self.assertEqual(cdp._pid_registry.get("w1"), 12345)

    def test_force_kill_calls_os_kill_with_sigkill(self):
        _register_pid("w1", 99999)
        with patch("modules.cdp.main.os.kill") as mock_kill:
            force_kill("w1")
        mock_kill.assert_called_once_with(99999, signal.SIGKILL)

    def test_force_kill_removes_pid_from_registry(self):
        _register_pid("w1", 99999)
        with patch("modules.cdp.main.os.kill"):
            force_kill("w1")
        with cdp._registry_lock:
            self.assertNotIn("w1", cdp._pid_registry)

    def test_force_kill_noop_when_no_pid_registered(self):
        # Should not raise
        with patch("modules.cdp.main.os.kill") as mock_kill:
            force_kill("ghost")
        mock_kill.assert_not_called()

    def test_force_kill_tolerates_process_lookup_error(self):
        _register_pid("w1", 1)
        with patch("modules.cdp.main.os.kill", side_effect=ProcessLookupError):
            # Should not propagate
            force_kill("w1")

    def test_force_kill_pid_removed_even_on_process_lookup_error(self):
        _register_pid("w1", 1)
        with patch("modules.cdp.main.os.kill", side_effect=ProcessLookupError):
            force_kill("w1")
        with cdp._registry_lock:
            self.assertNotIn("w1", cdp._pid_registry)


# ---------------------------------------------------------------------------
# detect_page_state() delegation tests
# ---------------------------------------------------------------------------

class DetectPageStateTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()
        self.driver = MagicMock()
        register_driver("w1", self.driver)

    def tearDown(self):
        _reset_cdp()

    def test_delegates_to_driver(self):
        self.driver.detect_page_state.return_value = "ui_lock"
        result = detect_page_state("w1")
        self.assertEqual(result, "ui_lock")
        self.driver.detect_page_state.assert_called_once_with()

    def test_returns_success_state(self):
        self.driver.detect_page_state.return_value = "success"
        self.assertEqual(detect_page_state("w1"), "success")

    def test_returns_vbv_3ds_state(self):
        self.driver.detect_page_state.return_value = "vbv_3ds"
        self.assertEqual(detect_page_state("w1"), "vbv_3ds")

    def test_returns_declined_state(self):
        self.driver.detect_page_state.return_value = "declined"
        self.assertEqual(detect_page_state("w1"), "declined")

    def test_propagates_selector_timeout_error(self):
        self.driver.detect_page_state.side_effect = SelectorTimeoutError(
            "#checkout-total", 5.0
        )
        with self.assertRaises(SelectorTimeoutError):
            detect_page_state("w1")

    def test_propagates_page_state_error(self):
        self.driver.detect_page_state.side_effect = PageStateError("unknown_state")
        with self.assertRaises(PageStateError):
            detect_page_state("w1")

    def test_raises_runtime_error_without_driver(self):
        with self.assertRaises(RuntimeError):
            detect_page_state("unregistered")


# ---------------------------------------------------------------------------
# fill_card() delegation tests
# ---------------------------------------------------------------------------

class FillCardTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()
        self.driver = MagicMock()
        register_driver("w1", self.driver)

    def tearDown(self):
        _reset_cdp()

    def test_delegates_to_driver(self):
        card_info = MagicMock()
        fill_card(card_info, "w1")
        self.driver.fill_card.assert_called_once_with(card_info)

    def test_raises_runtime_error_without_driver(self):
        with self.assertRaises(RuntimeError):
            fill_card(MagicMock(), "unregistered")


# ---------------------------------------------------------------------------
# fill_billing() delegation tests
# ---------------------------------------------------------------------------

class FillBillingTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()
        self.driver = MagicMock()
        register_driver("w1", self.driver)

    def tearDown(self):
        _reset_cdp()

    def test_delegates_to_driver(self):
        profile = MagicMock()
        fill_billing(profile, "w1")
        self.driver.fill_billing.assert_called_once_with(profile)

    def test_raises_runtime_error_without_driver(self):
        with self.assertRaises(RuntimeError):
            fill_billing(MagicMock(), "unregistered")


# ---------------------------------------------------------------------------
# clear_card_fields() delegation tests
# ---------------------------------------------------------------------------

class ClearCardFieldsTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()
        self.driver = MagicMock()
        register_driver("w1", self.driver)

    def tearDown(self):
        _reset_cdp()

    def test_delegates_to_driver(self):
        clear_card_fields("w1")
        self.driver.clear_card_fields.assert_called_once_with()

    def test_raises_runtime_error_without_driver(self):
        with self.assertRaises(RuntimeError):
            clear_card_fields("unregistered")


# ---------------------------------------------------------------------------
# Thread-safety smoke test
# ---------------------------------------------------------------------------

class ThreadSafetyTests(unittest.TestCase):
    def setUp(self):
        _reset_cdp()

    def tearDown(self):
        _reset_cdp()

    def test_concurrent_register_unregister_no_exception(self):
        """Multiple threads hammering register/unregister must not raise."""
        errors = []
        num_threads = 20
        iterations = 50

        def worker(wid):
            for _ in range(iterations):
                try:
                    register_driver(wid, MagicMock())
                    unregister_driver(wid)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"w{i}",))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Thread errors: {errors}")


if __name__ == "__main__":
    unittest.main()
