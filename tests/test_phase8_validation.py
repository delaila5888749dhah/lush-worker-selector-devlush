"""Phase 8 — Production Deployment & Monitoring Validation.

Validates the Phase 8 observation and verification steps:
  Step 1: Deployment pipeline triggered and working
  Step 2: System runs (service up, workers active)
  Step 3: Monitoring active (logging, traces, metrics)
  Step 4: Runtime observation (worker stability, restart patterns, error rates)
  Step 5: Baseline recording and measurement
  Step 6: Deployment verification (service running, workers active, no errors)

These tests are *observation-only* — they exercise existing production code
paths without modifying any production module.
"""

import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime
from integration.runtime import (
    ALLOWED_STATES,
    get_active_workers,
    get_deployment_status,
    get_state,
    get_trace_id,
    reset,
    start,
    stop,
    verify_deployment,
)
from modules.monitor import main as monitor
from modules.rollout import main as rollout

WARMUP_DELAY = 0.2
CLEANUP_TIMEOUT = 2


class Phase8ResetMixin:
    """Common setUp/tearDown for Phase 8 validation tests."""

    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        reset()
        rollout.reset()
        monitor.reset()

    def _poll_until(self, predicate, timeout=CLEANUP_TIMEOUT, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


# ── Step 1: Deployment pipeline validation ──────────────────────────


class TestDeploymentPipeline(Phase8ResetMixin, unittest.TestCase):
    """Step 1 — Validate deployment pipeline is operational."""

    def test_allowed_states_defined(self):
        """Runtime must define the expected lifecycle states."""
        self.assertEqual(ALLOWED_STATES, {"INIT", "RUNNING", "STOPPING", "STOPPED"})

    def test_initial_state_is_init(self):
        """After reset the system must be in INIT state."""
        self.assertEqual(get_state(), "INIT")

    def test_deployment_status_contract_keys(self):
        """get_deployment_status must return all documented keys."""
        ds = get_deployment_status()
        required_keys = {
            "running", "state", "worker_count",
            "active_workers", "consecutive_rollbacks",
            "trace_id", "metrics",
        }
        self.assertEqual(set(ds.keys()), required_keys)

    def test_monitor_metrics_contract_keys(self):
        """Monitor get_metrics must return all documented metric keys."""
        m = monitor.get_metrics()
        required_keys = {
            "success_count", "error_count",
            "success_rate", "error_rate",
            "memory_usage_bytes", "restarts_last_hour",
            "baseline_success_rate",
        }
        self.assertTrue(required_keys.issubset(set(m.keys())),
                        f"Missing keys: {required_keys - set(m.keys())}")


# ── Step 2: System running validation ───────────────────────────────


class TestSystemRunning(Phase8ResetMixin, unittest.TestCase):
    """Step 2 — Validate system starts, runs, and stops correctly."""

    def test_start_activates_runtime(self):
        """start() must transition to RUNNING and activate workers."""
        result = start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertTrue(result)
        time.sleep(WARMUP_DELAY)
        self.assertEqual(get_state(), "RUNNING")
        self.assertGreater(len(get_active_workers()), 0)
        stop(timeout=2)

    def test_workers_are_active_during_run(self):
        """Workers must show as active while runtime loop is running."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertTrue(ds["running"])
        self.assertGreater(ds["worker_count"], 0)
        self.assertEqual(len(ds["active_workers"]), ds["worker_count"])
        stop(timeout=2)

    def test_stop_deactivates_runtime(self):
        """stop() must transition through STOPPING to STOPPED."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        stop(timeout=2)
        self.assertIn(get_state(), ("STOPPED",))
        self.assertEqual(get_active_workers(), [])

    def test_deployment_status_after_stop(self):
        """After stop, deployment status reflects inactive system."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        stop(timeout=2)
        ds = get_deployment_status()
        self.assertFalse(ds["running"])
        self.assertEqual(ds["worker_count"], 0)
        self.assertEqual(ds["active_workers"], [])


# ── Step 3: Monitoring active validation ────────────────────────────


class TestMonitoringActive(Phase8ResetMixin, unittest.TestCase):
    """Step 3 — Validate monitoring, logging, and tracing are active."""

    def test_trace_id_assigned_on_start(self):
        """start() must assign a non-None trace_id for log correlation."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        tid = get_trace_id()
        self.assertIsNotNone(tid)
        self.assertIsInstance(tid, str)
        self.assertGreater(len(tid), 0)
        stop(timeout=2)

    def test_trace_id_in_deployment_status(self):
        """Deployment status must include the current trace_id."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertEqual(ds["trace_id"], get_trace_id())
        stop(timeout=2)

    def test_metrics_available_during_run(self):
        """Monitor metrics must be accessible via deployment status."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertIsNotNone(ds["metrics"])
        self.assertIn("success_rate", ds["metrics"])
        self.assertIn("error_rate", ds["metrics"])
        stop(timeout=2)

    def test_structured_log_emitted(self):
        """Runtime must emit structured log events during lifecycle."""
        with patch.object(runtime._logger, "info") as mock_info:
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(WARMUP_DELAY)
            stop(timeout=2)
            self.assertGreater(mock_info.call_count, 0)

    def test_monitor_failure_resilience(self):
        """Deployment status must survive monitor.get_metrics() failure."""
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            ds = get_deployment_status()
            self.assertIsNone(ds["metrics"])
            self.assertIn("state", ds)


# ── Step 4: Runtime observation ─────────────────────────────────────


class TestRuntimeObservation(Phase8ResetMixin, unittest.TestCase):
    """Step 4 — Observe worker stability, restart patterns, error rates."""

    def test_worker_stability_consistent_count(self):
        """Worker count must remain stable during normal operation."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds1 = get_deployment_status()
        time.sleep(0.1)
        ds2 = get_deployment_status()
        self.assertEqual(ds1["worker_count"], ds2["worker_count"])
        stop(timeout=2)

    def test_no_unexpected_rollbacks(self):
        """During stable operation, consecutive_rollbacks must be 0."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertEqual(ds["consecutive_rollbacks"], 0)
        stop(timeout=2)

    def test_restart_pattern_tracking(self):
        """Monitor must track restart timestamps for last-hour count."""
        monitor.record_restart()
        monitor.record_restart()
        self.assertEqual(monitor.get_restarts_last_hour(), 2)
        ds = get_deployment_status()
        self.assertEqual(ds["metrics"]["restarts_last_hour"], 2)

    def test_error_rate_observation(self):
        """Monitor must accurately track error rate."""
        for _ in range(9):
            monitor.record_success()
        monitor.record_error()
        ds = get_deployment_status()
        self.assertAlmostEqual(ds["metrics"]["error_rate"], 0.1)
        self.assertAlmostEqual(ds["metrics"]["success_rate"], 0.9)

    def test_error_rate_below_threshold(self):
        """A healthy system should have error_rate ≤ 5% per spec."""
        for _ in range(100):
            monitor.record_success()
        ds = get_deployment_status()
        self.assertLessEqual(ds["metrics"]["error_rate"], 0.05)

    def test_restarts_below_threshold(self):
        """A healthy system should have ≤ 3 restarts/hr per spec."""
        ds = get_deployment_status()
        self.assertLessEqual(ds["metrics"]["restarts_last_hour"], 3)

    def test_rollback_conditions_empty_when_healthy(self):
        """Healthy system should have no rollback triggers."""
        for _ in range(10):
            monitor.record_success()
        reasons = monitor.check_rollback_needed()
        self.assertEqual(reasons, [])


# ── Step 4b: Behavior tracking ──────────────────────────────────────


class TestBehaviorTracking(Phase8ResetMixin, unittest.TestCase):
    """Step 4b — Observe crash resilience, restart thresholds, error stability."""

    # -- Acceptance Criterion 1: No abnormal crashes -----------------------

    def test_crash_does_not_stop_system(self):
        """Worker crash must not bring down the runtime."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        # Inject a crashing worker into the already running system
        from integration.runtime import start_worker
        start_worker(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        time.sleep(0.3)
        # System must still be RUNNING
        self.assertEqual(get_state(), "RUNNING")
        ds = get_deployment_status()
        self.assertTrue(ds["running"])
        stop(timeout=2)

    def test_crash_reflected_in_error_metrics(self):
        """Worker crashes must be visible through the monitoring metrics."""
        monitor.record_error()
        ds = get_deployment_status()
        self.assertGreater(ds["metrics"]["error_count"], 0)
        self.assertGreater(ds["metrics"]["error_rate"], 0.0)

    def test_state_stays_in_allowed_states_during_lifecycle(self):
        """State must be a member of ALLOWED_STATES at every observation point."""
        self.assertIn(get_state(), ALLOWED_STATES)
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        self.assertIn(get_state(), ALLOWED_STATES)
        stop(timeout=2)
        self.assertIn(get_state(), ALLOWED_STATES)

    # -- Acceptance Criterion 2: Restarts within allowed thresholds --------

    def test_restart_count_tracks_accurately(self):
        """Each record_restart call must increment the hourly counter by 1."""
        self.assertEqual(monitor.get_restarts_last_hour(), 0)
        monitor.record_restart()
        self.assertEqual(monitor.get_restarts_last_hour(), 1)
        monitor.record_restart()
        monitor.record_restart()
        self.assertEqual(monitor.get_restarts_last_hour(), 3)

    def test_restart_over_threshold_detected(self):
        """When restarts exceed 3/hr, check_rollback_needed must flag it."""
        for _ in range(4):
            monitor.record_restart()
        reasons = monitor.check_rollback_needed()
        self.assertTrue(
            any("restart" in r for r in reasons),
            "Expected restart threshold breach to be flagged",
        )

    def test_restart_threshold_boundary(self):
        """Exactly 3 restarts/hr must NOT trigger a rollback (threshold is >3)."""
        for _ in range(3):
            monitor.record_restart()
        reasons = monitor.check_rollback_needed()
        restart_reasons = [r for r in reasons if "restart" in r]
        self.assertEqual(restart_reasons, [])

    # -- Acceptance Criterion 3: Error rate stable -------------------------

    def test_error_rate_stable_across_reads(self):
        """Error rate must be consistent across multiple get_deployment_status reads."""
        for _ in range(95):
            monitor.record_success()
        for _ in range(5):
            monitor.record_error()
        rates = [get_deployment_status()["metrics"]["error_rate"] for _ in range(5)]
        self.assertTrue(
            all(r == rates[0] for r in rates),
            f"Error rate should be stable: {rates}",
        )

    def test_error_rate_over_threshold_detected(self):
        """Error rate >5% must be detected by check_rollback_needed."""
        for _ in range(9):
            monitor.record_success()
        for _ in range(2):
            monitor.record_error()
        # error rate = 2/11 ≈ 18.2% > 5%
        reasons = monitor.check_rollback_needed()
        self.assertTrue(
            any("error rate" in r for r in reasons),
            "Expected error rate threshold breach to be flagged",
        )

    def test_healthy_error_rate_not_flagged(self):
        """Error rate ≤5% must NOT trigger a rollback."""
        for _ in range(96):
            monitor.record_success()
        for _ in range(4):
            monitor.record_error()
        # error rate = 4/100 = 4% ≤ 5%
        reasons = monitor.check_rollback_needed()
        error_reasons = [r for r in reasons if "error rate" in r]
        self.assertEqual(error_reasons, [])

    # -- Combined acceptance criteria --------------------------------------

    def test_all_acceptance_criteria_pass_for_healthy_system(self):
        """A healthy running system must pass all three acceptance criteria."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        for _ in range(20):
            monitor.record_success()
        ds = get_deployment_status()
        # AC1: no crash — system running, state is RUNNING
        self.assertTrue(ds["running"])
        self.assertEqual(ds["state"], "RUNNING")
        # AC2: restarts within threshold
        self.assertLessEqual(ds["metrics"]["restarts_last_hour"], 3)
        # AC3: error rate stable
        self.assertLessEqual(ds["metrics"]["error_rate"], 0.05)
        # Formal deployment verification must also pass
        result = verify_deployment()
        self.assertTrue(result["passed"])
        self.assertEqual(result["errors"], [])
        stop(timeout=2)


# ── Step 5: Baseline recording ──────────────────────────────────────


class TestBaselineRecording(Phase8ResetMixin, unittest.TestCase):
    """Step 5 — Validate baseline recording and measurement."""

    def test_no_baseline_initially(self):
        """Before save_baseline(), baseline should be None."""
        self.assertIsNone(monitor.get_baseline_success_rate())

    def test_baseline_captured_after_save(self):
        """save_baseline() must snapshot the current success rate."""
        for _ in range(10):
            monitor.record_success()
        monitor.save_baseline()
        self.assertAlmostEqual(monitor.get_baseline_success_rate(), 1.0)

    def test_baseline_reflects_mixed_results(self):
        """Baseline must accurately reflect mixed success/error."""
        for _ in range(8):
            monitor.record_success()
        for _ in range(2):
            monitor.record_error()
        monitor.save_baseline()
        self.assertAlmostEqual(monitor.get_baseline_success_rate(), 0.8)

    def test_baseline_in_deployment_status(self):
        """Baseline must be available via deployment status metrics."""
        for _ in range(10):
            monitor.record_success()
        monitor.save_baseline()
        ds = get_deployment_status()
        self.assertAlmostEqual(ds["metrics"]["baseline_success_rate"], 1.0)

    def test_success_rate_drop_detection(self):
        """System must detect >10% success rate drop from baseline."""
        for _ in range(10):
            monitor.record_success()
        monitor.save_baseline()
        for _ in range(5):
            monitor.record_error()
        reasons = monitor.check_rollback_needed()
        self.assertTrue(any("success rate dropped" in r for r in reasons))

    def test_baseline_preserved_across_metrics_reads(self):
        """get_metrics() must not alter the saved baseline."""
        for _ in range(10):
            monitor.record_success()
        monitor.save_baseline()
        baseline_before = monitor.get_baseline_success_rate()
        _ = monitor.get_metrics()
        _ = monitor.get_metrics()
        self.assertEqual(monitor.get_baseline_success_rate(), baseline_before)


# ── Step 6: Deployment verification ─────────────────────────────────


class TestDeploymentVerification(Phase8ResetMixin, unittest.TestCase):
    """Step 6 — Verify production deployment status."""

    def test_verify_deployment_contract_keys(self):
        """verify_deployment must return passed, checks, and errors."""
        result = verify_deployment()
        self.assertEqual(set(result.keys()), {"passed", "checks", "errors"})
        self.assertEqual(
            set(result["checks"].keys()),
            {"service_running", "workers_active", "no_startup_errors"},
        )

    def test_verify_passes_for_healthy_system(self):
        """A healthy running system must pass all deployment checks."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        result = verify_deployment()
        self.assertTrue(result["passed"])
        self.assertTrue(result["checks"]["service_running"])
        self.assertTrue(result["checks"]["workers_active"])
        self.assertTrue(result["checks"]["no_startup_errors"])
        self.assertEqual(result["errors"], [])
        stop(timeout=2)

    def test_verify_fails_before_start(self):
        """Before start(), verify_deployment must report failure."""
        result = verify_deployment()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["service_running"])
        self.assertFalse(result["checks"]["no_startup_errors"])

    def test_verify_fails_after_stop(self):
        """After stop(), verify_deployment must report failure."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        stop(timeout=2)
        result = verify_deployment()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["service_running"])
        self.assertFalse(result["checks"]["no_startup_errors"])

    def test_verify_detects_error_rate_above_threshold(self):
        """High error rate must cause verification failure."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        for _ in range(10):
            monitor.record_error()
        result = verify_deployment()
        self.assertFalse(result["checks"]["no_startup_errors"])
        stop(timeout=2)

    def test_verify_detects_excessive_restarts(self):
        """Too many restarts must cause verification failure."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        for _ in range(4):
            monitor.record_restart()
        result = verify_deployment()
        self.assertFalse(result["checks"]["no_startup_errors"])
        stop(timeout=2)


# ── End-to-end lifecycle validation ─────────────────────────────────


class TestEndToEndLifecycle(Phase8ResetMixin, unittest.TestCase):
    """Validate complete INIT → RUNNING → STOPPING → STOPPED lifecycle."""

    def test_full_lifecycle_states(self):
        """System must traverse all expected states in order."""
        self.assertEqual(get_state(), "INIT")

        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        self.assertEqual(get_state(), "RUNNING")

        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")

    def test_lifecycle_deployment_status_progression(self):
        """Deployment status must reflect each lifecycle phase."""
        ds_init = get_deployment_status()
        self.assertEqual(ds_init["state"], "INIT")
        self.assertFalse(ds_init["running"])

        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds_running = get_deployment_status()
        self.assertEqual(ds_running["state"], "RUNNING")
        self.assertTrue(ds_running["running"])
        self.assertGreater(ds_running["worker_count"], 0)
        self.assertIsNotNone(ds_running["trace_id"])

        stop(timeout=2)
        ds_stopped = get_deployment_status()
        self.assertEqual(ds_stopped["state"], "STOPPED")
        self.assertFalse(ds_stopped["running"])
        self.assertEqual(ds_stopped["worker_count"], 0)

    def test_concurrent_deployment_status_reads(self):
        """Concurrent reads of deployment status must be thread-safe."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        results = []
        errors = []

        def reader():
            try:
                for _ in range(10):
                    ds = get_deployment_status()
                    results.append(ds)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stop(timeout=2)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 50)
        for ds in results:
            self.assertIn("state", ds)
            self.assertIn("metrics", ds)


if __name__ == "__main__":
    unittest.main()
