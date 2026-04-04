"""Tests for integration.monitoring — production monitoring setup & validation.

Validates:
  - setup_monitoring configures logging handlers and format.
  - validate_monitoring reports correct status for the three acceptance criteria.
  - trace_id format follows the 12-char hex pattern.
  - Metrics completeness check detects missing keys.
"""

import logging
import time
import unittest
from unittest.mock import patch

from integration import monitoring, runtime
from integration.monitoring import setup_monitoring, validate_monitoring
from modules.monitor import main as monitor


class MonitoringResetMixin:
    """Common setUp/tearDown for monitoring tests."""

    def setUp(self):
        runtime.reset()
        monitor.reset()
        monitoring.reset()
        # Remove any handlers added by setup_monitoring so each test is isolated.
        root = logging.getLogger()
        self._original_handlers = list(root.handlers)
        self._original_level = root.level

    def tearDown(self):
        runtime.reset()
        monitor.reset()
        monitoring.reset()
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self._original_handlers:
                root.removeHandler(h)
        root.setLevel(self._original_level)


# ── setup_monitoring tests ──────────────────────────────────────────


class TestSetupMonitoring(MonitoringResetMixin, unittest.TestCase):
    """Validate setup_monitoring configures logging properly."""

    def test_adds_handler_to_root_logger(self):
        """setup_monitoring must add at least one handler to the root logger."""
        root = logging.getLogger()
        before = len(root.handlers)
        setup_monitoring()
        self.assertGreater(len(root.handlers), before)

    def test_sets_log_level(self):
        """setup_monitoring must set the root logger level."""
        setup_monitoring(level=logging.DEBUG)
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_idempotent(self):
        """Repeated calls must not add duplicate handlers."""
        root = logging.getLogger()
        setup_monitoring()
        count_after_first = len(root.handlers)
        setup_monitoring()
        self.assertEqual(len(root.handlers), count_after_first)

    def test_handler_has_formatter(self):
        """The added handler must have a Formatter."""
        root = logging.getLogger()
        before = set(id(h) for h in root.handlers)
        setup_monitoring()
        new_handlers = [h for h in root.handlers if id(h) not in before]
        self.assertTrue(len(new_handlers) > 0)
        for h in new_handlers:
            self.assertIsNotNone(h.formatter)


# ── validate_monitoring tests ───────────────────────────────────────


class TestValidateMonitoring(MonitoringResetMixin, unittest.TestCase):
    """Validate validate_monitoring reports correct status."""

    def test_contract_keys(self):
        """validate_monitoring must return passed, checks, and errors."""
        result = validate_monitoring()
        self.assertEqual(set(result.keys()), {"passed", "checks", "errors"})
        self.assertEqual(
            set(result["checks"].keys()),
            {"logging_active", "trace_id_valid", "metrics_available"},
        )

    def test_fails_before_start(self):
        """Before start(), trace_id_valid must be False."""
        setup_monitoring()
        result = validate_monitoring()
        self.assertFalse(result["checks"]["trace_id_valid"])
        self.assertFalse(result["passed"])

    def test_passes_when_running(self):
        """All checks must pass when runtime is RUNNING."""
        setup_monitoring()
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        result = validate_monitoring()
        self.assertTrue(result["checks"]["logging_active"])
        self.assertTrue(result["checks"]["trace_id_valid"])
        self.assertTrue(result["checks"]["metrics_available"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["errors"], [])
        runtime.stop(timeout=2)

    def test_logging_active_with_handler(self):
        """logging_active must be True when a handler is configured."""
        setup_monitoring()
        result = validate_monitoring()
        self.assertTrue(result["checks"]["logging_active"])

    def test_metrics_available_always(self):
        """metrics_available must be True even when runtime is not started."""
        result = validate_monitoring()
        self.assertTrue(result["checks"]["metrics_available"])

    def test_metrics_failure_handled(self):
        """metrics_available must be False when get_metrics raises."""
        with patch("integration.monitoring.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            result = validate_monitoring()
            self.assertFalse(result["checks"]["metrics_available"])
            self.assertTrue(any("raised" in e for e in result["errors"]))


# ── trace_id format tests ──────────────────────────────────────────


class TestTraceIdFormat(MonitoringResetMixin, unittest.TestCase):
    """Validate trace_id format — 12 hex characters."""

    def test_trace_id_length(self):
        """trace_id must be exactly 12 characters."""
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        tid = runtime.get_trace_id()
        self.assertEqual(len(tid), 12)
        runtime.stop(timeout=2)

    def test_trace_id_hex_chars(self):
        """trace_id must contain only hexadecimal characters."""
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        tid = runtime.get_trace_id()
        self.assertRegex(tid, r"^[0-9a-f]{12}$")
        runtime.stop(timeout=2)

    def test_trace_id_unique_across_restarts(self):
        """Each start() must generate a new trace_id."""
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        tid1 = runtime.get_trace_id()
        runtime.stop(timeout=2)

        runtime.reset()
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        tid2 = runtime.get_trace_id()
        runtime.stop(timeout=2)

        self.assertNotEqual(tid1, tid2)


# ── Metrics completeness tests ─────────────────────────────────────


class TestMetricsCompleteness(MonitoringResetMixin, unittest.TestCase):
    """Validate all metric values are populated in normal state."""

    def test_initial_metrics_not_none(self):
        """All metrics except baseline_success_rate must be non-None initially."""
        m = monitor.get_metrics()
        for key, value in m.items():
            if key == "baseline_success_rate":
                continue  # allowed to be None before save_baseline()
            self.assertIsNotNone(value, f"Metric {key!r} is None")

    def test_metrics_during_run_not_none(self):
        """During a running system, all non-baseline metrics must be non-None."""
        runtime.start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.2)
        ds = runtime.get_deployment_status()
        self.assertIsNotNone(ds["metrics"])
        for key, value in ds["metrics"].items():
            if key == "baseline_success_rate":
                continue
            self.assertIsNotNone(value, f"Metric {key!r} is None during run")
        runtime.stop(timeout=2)

    def test_metrics_with_baseline_all_present(self):
        """After save_baseline, all metrics including baseline must be non-None."""
        for _ in range(5):
            monitor.record_success()
        monitor.save_baseline()
        m = monitor.get_metrics()
        for key, value in m.items():
            self.assertIsNotNone(value, f"Metric {key!r} is None after baseline")

    def test_metrics_types(self):
        """Metric values must have the expected types."""
        for _ in range(3):
            monitor.record_success()
        monitor.record_error()
        monitor.record_restart()
        m = monitor.get_metrics()
        self.assertIsInstance(m["success_count"], int)
        self.assertIsInstance(m["error_count"], int)
        self.assertIsInstance(m["success_rate"], float)
        self.assertIsInstance(m["error_rate"], float)
        self.assertIsInstance(m["memory_usage_bytes"], int)
        self.assertIsInstance(m["restarts_last_hour"], int)


if __name__ == "__main__":
    unittest.main()
