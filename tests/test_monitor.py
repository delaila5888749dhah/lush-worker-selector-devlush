import threading
import time
import unittest
from unittest.mock import mock_open, patch

from modules.monitor.main import (
    check_rollback_needed,
    get_baseline_success_rate,
    get_error_rate,
    get_memory_usage_bytes,
    get_metrics,
    get_restarts_last_hour,
    get_success_rate,
    record_error,
    record_restart,
    record_success,
    reset,
    save_baseline,
)


class MonitorResetMixin:
    def setUp(self):
        reset()

    def tearDown(self):
        reset()


class TestRecordSuccess(MonitorResetMixin, unittest.TestCase):
    def test_single_success(self):
        record_success()
        self.assertEqual(get_success_rate(), 1.0)

    def test_multiple_successes(self):
        for _ in range(5):
            record_success()
        self.assertEqual(get_success_rate(), 1.0)
        self.assertEqual(get_error_rate(), 0.0)


class TestRecordError(MonitorResetMixin, unittest.TestCase):
    def test_single_error(self):
        record_error()
        self.assertEqual(get_error_rate(), 1.0)

    def test_mixed_success_and_error(self):
        for _ in range(3):
            record_success()
        record_error()
        self.assertAlmostEqual(get_success_rate(), 0.75)
        self.assertAlmostEqual(get_error_rate(), 0.25)


class TestSuccessRate(MonitorResetMixin, unittest.TestCase):
    def test_no_tasks_returns_one(self):
        self.assertEqual(get_success_rate(), 1.0)

    def test_all_errors_returns_zero(self):
        record_error()
        record_error()
        self.assertEqual(get_success_rate(), 0.0)


class TestErrorRate(MonitorResetMixin, unittest.TestCase):
    def test_no_tasks_returns_zero(self):
        self.assertEqual(get_error_rate(), 0.0)

    def test_all_success_returns_zero(self):
        record_success()
        self.assertEqual(get_error_rate(), 0.0)


class TestRecordRestart(MonitorResetMixin, unittest.TestCase):
    def test_restart_counted_in_last_hour(self):
        record_restart()
        self.assertEqual(get_restarts_last_hour(), 1)

    def test_old_restart_not_counted(self):
        from modules.monitor import main as monitor_module

        with monitor_module._lock:
            monitor_module._restart_timestamps.append(time.time() - 7200)
        self.assertEqual(get_restarts_last_hour(), 0)


class TestMemoryUsage(MonitorResetMixin, unittest.TestCase):
    def test_returns_integer(self):
        usage = get_memory_usage_bytes()
        self.assertIsInstance(usage, int)
        self.assertGreaterEqual(usage, 0)

    def test_fallback_on_error(self):
        with patch("builtins.open", side_effect=OSError("no proc")):
            self.assertEqual(get_memory_usage_bytes(), 0)


class TestBaseline(MonitorResetMixin, unittest.TestCase):
    def test_no_baseline_initially(self):
        self.assertIsNone(get_baseline_success_rate())

    def test_save_baseline_captures_rate(self):
        for _ in range(8):
            record_success()
        for _ in range(2):
            record_error()
        save_baseline()
        self.assertAlmostEqual(get_baseline_success_rate(), 0.8)

    def test_save_baseline_empty_returns_one(self):
        save_baseline()
        self.assertEqual(get_baseline_success_rate(), 1.0)


class TestGetMetrics(MonitorResetMixin, unittest.TestCase):
    def test_initial_metrics(self):
        m = get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertEqual(m["error_count"], 0)
        self.assertEqual(m["success_rate"], 1.0)
        self.assertEqual(m["error_rate"], 0.0)
        self.assertEqual(m["restarts_last_hour"], 0)
        self.assertIsNone(m["baseline_success_rate"])

    def test_metrics_after_activity(self):
        record_success()
        record_error()
        record_restart()
        m = get_metrics()
        self.assertEqual(m["success_count"], 1)
        self.assertEqual(m["error_count"], 1)
        self.assertAlmostEqual(m["success_rate"], 0.5)
        self.assertAlmostEqual(m["error_rate"], 0.5)
        self.assertEqual(m["restarts_last_hour"], 1)


class TestCheckRollbackNeeded(MonitorResetMixin, unittest.TestCase):
    def test_healthy_no_rollback(self):
        for _ in range(10):
            record_success()
        self.assertEqual(check_rollback_needed(), [])

    def test_high_error_rate_triggers_rollback(self):
        record_success()
        record_error()
        reasons = check_rollback_needed()
        self.assertTrue(any("error rate" in r for r in reasons))

    def test_success_rate_drop_triggers_rollback(self):
        for _ in range(10):
            record_success()
        save_baseline()
        # Simulate degradation: add many errors
        for _ in range(10):
            record_error()
        reasons = check_rollback_needed()
        self.assertTrue(any("success rate dropped" in r for r in reasons))

    def test_excessive_restarts_triggers_rollback(self):
        for _ in range(10):
            record_success()
        for _ in range(4):
            record_restart()
        reasons = check_rollback_needed()
        self.assertTrue(any("worker restarts" in r for r in reasons))

    def test_memory_over_limit_triggers_rollback(self):
        for _ in range(10):
            record_success()
        mem_over = 3 * 1024 * 1024 * 1024  # 3 GB
        proc_content = f"VmRSS:\t{mem_over // 1024} kB\n"
        with patch("builtins.open", mock_open(read_data=proc_content)):
            reasons = check_rollback_needed()
        self.assertTrue(any("memory usage" in r for r in reasons))

    def test_no_baseline_skips_success_drop_check(self):
        record_error()
        reasons = check_rollback_needed()
        # Should flag error rate but NOT success rate drop (no baseline)
        self.assertTrue(any("error rate" in r for r in reasons))
        self.assertFalse(any("success rate dropped" in r for r in reasons))


class TestThreadSafety(MonitorResetMixin, unittest.TestCase):
    def test_concurrent_record(self):
        errors = []

        def worker():
            try:
                for _ in range(100):
                    record_success()
                    record_error()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        m = get_metrics()
        self.assertEqual(m["success_count"], 500)
        self.assertEqual(m["error_count"], 500)


class TestReset(MonitorResetMixin, unittest.TestCase):
    def test_reset_clears_all(self):
        record_success()
        record_error()
        record_restart()
        save_baseline()
        reset()
        m = get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertEqual(m["error_count"], 0)
        self.assertEqual(m["restarts_last_hour"], 0)
        self.assertIsNone(m["baseline_success_rate"])


if __name__ == "__main__":
    unittest.main()
