"""Phase 8 — Production Monitoring Observation Tests.

Observation-only tests that validate the three monitoring acceptance criteria
by exercising *existing* production code paths.  No new modules, no system
modifications — pure observation.

Acceptance criteria verified:
  1. Logging is active (runtime logger emits structured events).
  2. trace_id is assigned and trackable (12-char hex, unique per lifecycle).
  3. Metrics (monitor.get_metrics) return data (not None in normal state).
"""

import logging
import time
import unittest
from unittest.mock import patch

from integration import runtime
from integration.runtime import (
    get_deployment_status,
    get_trace_id,
    start,
    stop,
)
from modules.monitor import main as monitor
from modules.rollout import main as rollout

WARMUP_DELAY = 0.2


class ObservationResetMixin:
    """Common setUp/tearDown for monitoring observation tests."""

    def setUp(self):
        runtime.reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        runtime.reset()
        rollout.reset()
        monitor.reset()


# ── Logging observation ─────────────────────────────────────────────


class TestLoggingObservation(ObservationResetMixin, unittest.TestCase):
    """Observe that the runtime logger emits structured log events."""

    def test_runtime_logger_exists(self):
        """integration.runtime must use a named logger."""
        logger = logging.getLogger("integration.runtime")
        self.assertIsNotNone(logger)
        self.assertEqual(logger.name, "integration.runtime")

    def test_structured_log_emitted_on_start_stop(self):
        """Runtime must emit structured info-level log events during lifecycle."""
        with patch.object(runtime._logger, "info") as mock_info:
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(WARMUP_DELAY)
            stop(timeout=2)
            self.assertGreater(mock_info.call_count, 0)

    def test_log_contains_trace_id(self):
        """Log events must include the trace_id for correlation."""
        logged_args = []
        with patch.object(runtime._logger, "info", side_effect=lambda fmt, *a: logged_args.append(a)):
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(WARMUP_DELAY)
            tid = get_trace_id()
            stop(timeout=2)
        # At least one log call must contain the trace_id value
        found = any(tid in str(args) for args in logged_args)
        self.assertTrue(found, f"trace_id {tid!r} not found in any log args")


# ── trace_id observation ────────────────────────────────────────────


class TestTraceIdObservation(ObservationResetMixin, unittest.TestCase):
    """Observe that trace_id is assigned and follows the expected format."""

    def test_trace_id_none_before_start(self):
        """Before start(), trace_id must be None."""
        self.assertIsNone(get_trace_id())

    def test_trace_id_assigned_on_start(self):
        """start() must assign a non-None trace_id."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid = get_trace_id()
        self.assertIsNotNone(tid)
        self.assertIsInstance(tid, str)
        stop(timeout=2)

    def test_trace_id_length(self):
        """trace_id must be exactly 12 characters."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid = get_trace_id()
        self.assertEqual(len(tid), 12)
        stop(timeout=2)

    def test_trace_id_hex_format(self):
        """trace_id must contain only lowercase hexadecimal characters."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid = get_trace_id()
        self.assertRegex(tid, r"^[0-9a-f]{12}$")
        stop(timeout=2)

    def test_trace_id_in_deployment_status(self):
        """Deployment status must include the current trace_id."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertEqual(ds["trace_id"], get_trace_id())
        stop(timeout=2)

    def test_trace_id_unique_across_restarts(self):
        """Each start() must generate a new trace_id."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid1 = get_trace_id()
        stop(timeout=2)

        runtime.reset()
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid2 = get_trace_id()
        stop(timeout=2)

        self.assertNotEqual(tid1, tid2)


# ── Metrics observation ─────────────────────────────────────────────


class TestMetricsObservation(ObservationResetMixin, unittest.TestCase):
    """Observe that monitor.get_metrics() returns complete, non-None data."""

    def test_get_metrics_returns_dict(self):
        """monitor.get_metrics() must return a dict."""
        m = monitor.get_metrics()
        self.assertIsInstance(m, dict)

    def test_metrics_required_keys(self):
        """Metrics must contain all documented keys."""
        m = monitor.get_metrics()
        required_keys = {
            "success_count", "error_count", "success_rate",
            "error_rate", "memory_usage_bytes", "restarts_last_hour",
            "baseline_success_rate",
        }
        self.assertTrue(required_keys.issubset(set(m.keys())),
                        f"Missing keys: {required_keys - set(m.keys())}")

    def test_initial_metrics_not_none(self):
        """All metrics except baseline_success_rate must be non-None initially."""
        m = monitor.get_metrics()
        for key, value in m.items():
            if key == "baseline_success_rate":
                continue  # allowed to be None before save_baseline()
            self.assertIsNotNone(value, f"Metric {key!r} is None")

    def test_metrics_during_run_not_none(self):
        """During a running system, deployment status metrics must be non-None."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertIsNotNone(ds["metrics"])
        for key, value in ds["metrics"].items():
            if key == "baseline_success_rate":
                continue
            self.assertIsNotNone(value, f"Metric {key!r} is None during run")
        stop(timeout=2)

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

    def test_metrics_resilience_in_deployment_status(self):
        """Deployment status must survive monitor.get_metrics() failure."""
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            ds = get_deployment_status()
            self.assertIsNone(ds["metrics"])
            self.assertIn("state", ds)


if __name__ == "__main__":
    unittest.main()
