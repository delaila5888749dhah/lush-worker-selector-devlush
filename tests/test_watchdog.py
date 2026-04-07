import threading
import unittest

from modules.watchdog.main import (
    notify_total,
    reset,
    enable_network_monitor,
    wait_for_total,
)
from modules.common.exceptions import SessionFlaggedError

_WID = "worker-test"


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        reset()

    def tearDown(self):
        reset()

    def test_enable_network_monitor_allows_wait(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 42.0)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 42.0)

    def test_wait_for_total_without_enable_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=1)

    def test_wait_for_total_timeout_raises_session_flagged_error(self):
        enable_network_monitor(_WID)
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(_WID, timeout=0.05)

    def test_notify_total_before_wait(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 99.99)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 99.99)

    def test_notify_total_from_another_thread(self):
        enable_network_monitor(_WID)

        def signal():
            notify_total(_WID, 55.0)

        t = threading.Thread(target=signal)
        t.start()
        result = wait_for_total(_WID, timeout=2)
        t.join()
        self.assertEqual(result, 55.0)

    def test_wait_disables_monitor_on_success(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 10.0)
        wait_for_total(_WID, timeout=1)
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_wait_disables_monitor_on_timeout(self):
        enable_network_monitor(_WID)
        with self.assertRaises(SessionFlaggedError):
            wait_for_total(_WID, timeout=0.05)
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_enable_resets_previous_state(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 100.0)
        enable_network_monitor(_WID)
        notify_total(_WID, 200.0)
        result = wait_for_total(_WID, timeout=1)
        self.assertEqual(result, 200.0)

    def test_reset_clears_state(self):
        enable_network_monitor(_WID)
        notify_total(_WID, 50.0)
        reset()
        with self.assertRaises(RuntimeError):
            wait_for_total(_WID, timeout=0.05)

    def test_different_workers_are_isolated(self):
        wid_a = "worker-a"
        wid_b = "worker-b"
        enable_network_monitor(wid_a)
        enable_network_monitor(wid_b)
        notify_total(wid_a, 111.0)
        notify_total(wid_b, 222.0)
        result_a = wait_for_total(wid_a, timeout=1)
        result_b = wait_for_total(wid_b, timeout=1)
        self.assertEqual(result_a, 111.0)
        self.assertEqual(result_b, 222.0)

    def test_notify_total_noop_for_unknown_worker(self):
        # Should not raise
        notify_total("nonexistent-worker", 42.0)
