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
import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import modules.cdp.main as cdp
import modules.cdp.proxy as proxy_mod
from modules.cdp.main import (
    _sanitize_error,
    _register_pid,
    force_kill,
    register_driver,
    unregister_driver,
    register_browser_profile,
    unregister_browser_profile,
    detect_page_state,
    fill_card,
    fill_billing,
    fill_payment_and_billing,
    clear_card_fields,
)
from modules.cdp.proxy import ProxyPool, get_default_pool
from modules.common.exceptions import PageStateError, SelectorTimeoutError


def _reset_cdp():
    """Clear both internal registries to give each test a clean slate."""
    with cdp._registry_lock:  # pylint: disable=protected-access
        cdp._driver_registry.clear()  # pylint: disable=protected-access
        cdp._pid_registry.clear()  # pylint: disable=protected-access
        cdp._bitbrowser_registry.clear()  # pylint: disable=protected-access


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

    def test_unregister_browser_profile_removes_entry(self):
        register_browser_profile("w1", "profile-1")
        self.assertEqual(cdp.get_browser_profile("w1"), "profile-1")
        unregister_browser_profile("w1")
        self.assertIsNone(cdp.get_browser_profile("w1"))

    def test_unregister_browser_profile_is_idempotent(self):
        unregister_browser_profile("missing")
        register_browser_profile("w1", "profile-1")
        unregister_browser_profile("w1")
        unregister_browser_profile("w1")
        self.assertIsNone(cdp.get_browser_profile("w1"))


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

    def test_redacts_cvv_with_colon_separator(self):
        """CVV followed by a colon separator must be redacted."""
        msg = "Field cvv: 321 was rejected"
        result = _sanitize_error(msg)
        self.assertNotIn("321", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_redacts_cvv_with_space_only_separator(self):
        """CVV followed by whitespace only must be redacted."""
        msg = "card cvv 654 invalid"
        result = _sanitize_error(msg)
        self.assertNotIn("654", result)
        self.assertIn("[REDACTED-CVV]", result)

    def test_redacts_cvv_with_hyphen_separator(self):
        """CVV followed by a hyphen separator must be redacted."""
        msg = "header cvv-2345 declined"
        result = _sanitize_error(msg)
        self.assertNotIn("2345", result)
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

    def test_redacts_amex_pan(self):
        """INV-PII-UNIFIED-01: canonical sanitiser redacts 15-digit Amex PANs."""
        msg = "Amex 378282246310005 declined"
        result = _sanitize_error(msg)
        self.assertNotIn("378282246310005", result)
        self.assertIn("[REDACTED-CARD]", result)

    def test_redacts_amex_pan_grouped(self):
        """Amex grouped 4-6-5 form must be redacted."""
        msg = "Amex 3782 822463 10005 declined"
        result = _sanitize_error(msg)
        self.assertNotIn("3782 822463 10005", result)
        self.assertIn("[REDACTED-CARD]", result)


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

    def test_delegates_to_real_givex_driver(self):
        """Regression: cdp.fill_card must work against a real GivexDriver.

        Previously regressed when ``GivexDriver.fill_card`` was removed —
        the public wrapper then raised ``AttributeError`` at runtime even
        though the ``spec/interface.md`` contract for ``fill_card`` was
        still published.
        """
        from modules.cdp.driver import GivexDriver  # noqa: PLC0415
        real_driver = GivexDriver(MagicMock())
        register_driver("w-real", real_driver)
        card_info = MagicMock()
        with patch.object(real_driver, "fill_card_fields") as mock_fill:
            fill_card(card_info, "w-real")
        mock_fill.assert_called_once_with(card_info)


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
# fill_payment_and_billing() delegation tests
# ---------------------------------------------------------------------------

class FillPaymentAndBillingTests(unittest.TestCase):
    """Tests for fill_payment_and_billing() delegation to the registered driver."""

    def setUp(self):
        """Register a mock driver for worker 'w1'."""
        _reset_cdp()
        self.driver = MagicMock()
        register_driver("w1", self.driver)

    def tearDown(self):
        """Clear internal registries after each test."""
        _reset_cdp()

    def test_delegates_to_driver(self):
        """fill_payment_and_billing() forwards card_info and billing_profile to the driver."""
        card_info = MagicMock()
        billing_profile = MagicMock()
        fill_payment_and_billing(card_info, billing_profile, "w1")
        self.driver.fill_payment_and_billing.assert_called_once_with(
            card_info, billing_profile
        )

    def test_raises_runtime_error_without_driver(self):
        """RuntimeError raised when no driver is registered for the worker."""
        with self.assertRaises(RuntimeError):
            fill_payment_and_billing(MagicMock(), MagicMock(), "unregistered")


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


class ProxyPoolTests(unittest.TestCase):
    """Unit tests for the ProxyPool thread-safe proxy assignment pool."""

    def tearDown(self):
        proxy_mod._default_pool = None  # pylint: disable=protected-access

    def test_acquire_unique_proxies_for_three_workers(self):
        """Acquiring 3 proxies for 3 workers returns 3 distinct URLs."""
        pool = ProxyPool(["http://p1:8080", "http://p2:8080", "http://p3:8080"])
        proxy_w1 = pool.acquire("w1")
        proxy_w2 = pool.acquire("w2")
        proxy_w3 = pool.acquire("w3")
        self.assertEqual(
            {proxy_w1, proxy_w2, proxy_w3},
            {"http://p1:8080", "http://p2:8080", "http://p3:8080"},
        )

    def test_acquire_returns_none_when_pool_empty(self):
        """4th acquire on a 3-proxy pool returns None without raising."""
        pool = ProxyPool(["http://p1:8080", "http://p2:8080", "http://p3:8080"])
        self.assertIsNotNone(pool.acquire("w1"))
        self.assertIsNotNone(pool.acquire("w2"))
        self.assertIsNotNone(pool.acquire("w3"))
        self.assertIsNone(pool.acquire("w4"))

    def test_release_returns_proxy_to_pool(self):
        """Released proxy becomes available for reacquisition."""
        pool = ProxyPool(["http://p1:8080"])
        assigned = pool.acquire("w1")
        self.assertEqual(pool.available_count(), 0)
        pool.release("w1")
        self.assertEqual(pool.available_count(), 1)
        self.assertEqual(pool.acquire("w2"), assigned)

    def test_default_pool_without_proxy_list_file_starts_empty(self):
        """Default singleton pool is empty when PROXY_LIST_FILE is unset."""
        with patch.dict(os.environ, {}, clear=True):
            proxy_mod._default_pool = None  # pylint: disable=protected-access
            pool = get_default_pool()
            self.assertEqual(pool.available_count(), 0)
            self.assertIsNone(pool.acquire("worker-1"))

    def test_load_from_file(self):
        """load_from_file reads non-empty lines and adds them to the pool."""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("http://p1:8080\n")
            handle.write("\n")
            handle.write("socks5://user:pass@p2:1080\n")
            path = handle.name
        try:
            pool = ProxyPool()
            loaded = pool.load_from_file(path)
            self.assertEqual(loaded, 2)
            self.assertEqual(pool.available_count(), 2)
        finally:
            os.unlink(path)

    def test_load_from_file_nonexistent_path_raises_file_not_found(self):
        """load_from_file on a missing path raises FileNotFoundError."""
        pool = ProxyPool()
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = os.path.join(tmp_dir, "nope.txt")
            with self.assertRaises(FileNotFoundError):
                pool.load_from_file(missing)

    def test_load_from_file_empty_file_returns_zero(self):
        """An empty file returns 0 and leaves the pool empty."""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            path = handle.name
        try:
            pool = ProxyPool()
            self.assertEqual(pool.load_from_file(path), 0)
            self.assertEqual(pool.available_count(), 0)
        finally:
            os.unlink(path)

    def test_load_from_file_called_twice_appends(self):
        """Calling load_from_file twice appends proxies from both files."""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("http://p1:8080\n")
            path1 = handle.name
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("http://p2:8080\nhttp://p3:8080\n")
            path2 = handle.name
        try:
            pool = ProxyPool()
            first = pool.load_from_file(path1)
            second = pool.load_from_file(path2)
            self.assertEqual(first, 1)
            self.assertEqual(second, 2)
            self.assertEqual(pool.available_count(), 3)
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_init_loads_from_proxy_list_file_env_var(self):
        """Setting PROXY_LIST_FILE causes ProxyPool() to load that file."""
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write("http://env-proxy-1:8080\nhttp://env-proxy-2:8080\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"PROXY_LIST_FILE": path}, clear=False):
                pool = ProxyPool()
            self.assertEqual(pool.available_count(), 2)
            self.assertEqual(pool.acquire("w1"), "http://env-proxy-1:8080")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
