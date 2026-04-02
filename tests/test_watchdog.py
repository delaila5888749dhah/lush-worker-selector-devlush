import threading
import unittest

from modules.watchdog.main import (
    _notify_total,
    _reset_monitor,
    enable_network_monitor,
    wait_for_total,
)
from modules.common.exceptions import SessionFlaggedError


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        _reset_monitor()

    def tearDown(self):
        _reset_monitor()

    def test_enable_network_monitor_allows_wait(self):
        enable_network_monitor()
        _notify_total(42.0)
        result = wait_for_total(timeout=1)
        self.assertEqual(result, 42.0)

    def test_wait_for_total_without_enable_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            wait_for_total(timeout=1)

    def test_wait_for_total_timeout_raises_session_flagged_error(self):
        enable_network_monitor()
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(timeout=0.05)

    def test_notify_total_before_wait(self):
        enable_network_monitor()
        _notify_total(99.99)
        result = wait_for_total(timeout=1)
        self.assertEqual(result, 99.99)

    def test_notify_total_from_another_thread(self):
        enable_network_monitor()

        def signal():
            _notify_total(55.0)

        t = threading.Thread(target=signal)
        t.start()
        result = wait_for_total(timeout=2)
        t.join()
        self.assertEqual(result, 55.0)

    def test_wait_disables_monitor_on_success(self):
        enable_network_monitor()
        _notify_total(10.0)
        wait_for_total(timeout=1)
        with self.assertRaises(RuntimeError):
            wait_for_total(timeout=0.05)

    def test_wait_disables_monitor_on_timeout(self):
        enable_network_monitor()
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(timeout=0.05)
        with self.assertRaises(RuntimeError):
            wait_for_total(timeout=0.05)

    def test_enable_resets_previous_state(self):
        enable_network_monitor()
        _notify_total(100.0)
        enable_network_monitor()
        _notify_total(200.0)
        result = wait_for_total(timeout=1)
        self.assertEqual(result, 200.0)

    def test_reset_monitor_clears_state(self):
        enable_network_monitor()
        _notify_total(50.0)
        _reset_monitor()
        with self.assertRaises(RuntimeError):
            wait_for_total(timeout=0.05)
