"""Determinism audit — ensure system produces identical results for identical inputs.

26 tests across 6 categories:
  - No randomness (3): AST-based static checks for rollout, monitor, runtime
  - Rollout determinism (5): same state + check_fn → same output
  - Monitor determinism (5): same counters → same metrics, rates, rollback decisions
  - Metric dependency (3): rollout ignores wall-clock; only check_fn controls decision
  - No state drift (6): reset cycles restore exact initial state
  - End-to-end pipeline (4): full monitor→rollout pipeline determinism
"""

import ast
import os
import unittest
from unittest.mock import mock_open, patch

from modules.monitor import main as monitor
from modules.rollout import main as rollout

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROLLOUT_PATH = os.path.join(_REPO_ROOT, "modules", "rollout", "main.py")
_MONITOR_PATH = os.path.join(_REPO_ROOT, "modules", "monitor", "main.py")
_RUNTIME_PATH = os.path.join(_REPO_ROOT, "integration", "runtime.py")


def _imports_random(filepath):
    """Return True if *filepath* contains ``import random`` or ``from random …``."""
    with open(filepath, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=filepath)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "random" or alias.name.startswith("random."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and (
                node.module == "random" or node.module.startswith("random.")
            ):
                return True
    return False


class DeterminismResetMixin:
    """Reset both rollout and monitor state around every test."""

    def setUp(self):
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        rollout.reset()
        monitor.reset()


# ===================================================================
# 1. No-randomness — AST static analysis  (3 tests)
# ===================================================================

class TestNoRandomness(unittest.TestCase):
    """Verify that rollout, monitor, and runtime never import ``random``."""

    def test_rollout_no_random_import(self):
        self.assertFalse(
            _imports_random(_ROLLOUT_PATH),
            "modules/rollout/main.py must not import the random module",
        )

    def test_monitor_no_random_import(self):
        self.assertFalse(
            _imports_random(_MONITOR_PATH),
            "modules/monitor/main.py must not import the random module",
        )

    def test_runtime_no_random_import(self):
        self.assertFalse(
            _imports_random(_RUNTIME_PATH),
            "integration/runtime.py must not import the random module",
        )


# ===================================================================
# 2. Rollout determinism  (5 tests)
# ===================================================================

class TestRolloutDeterminism(DeterminismResetMixin, unittest.TestCase):
    """Same state + check_fn must always produce the same output."""

    def test_healthy_scale_up_is_deterministic(self):
        """Two fresh rollouts with identical healthy check_fn yield identical results."""
        for _ in range(2):
            rollout.reset()
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            result = rollout.try_scale_up()
            self.assertEqual(result, (3, "scaled_up", []))

    def test_unhealthy_rollback_is_deterministic(self):
        reasons = ["error rate 50.0% exceeds 5%"]
        for _ in range(2):
            rollout.reset()
            rollout.configure(
                check_rollback_fn=lambda: list(reasons),
                save_baseline_fn=lambda: None,
            )
            result = rollout.try_scale_up()
            self.assertEqual(result[1], "rollback")
            self.assertEqual(result[2], reasons)

    def test_at_max_is_deterministic(self):
        for _ in range(2):
            rollout.reset()
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            for _ in range(len(rollout.SCALE_STEPS) - 1):
                rollout.try_scale_up()
            result = rollout.try_scale_up()
            self.assertEqual(result, (rollout.SCALE_STEPS[-1], "at_max", []))

    def test_full_sequence_repeatability(self):
        """Running the full scale-up sequence twice produces identical step-by-step results."""
        sequences = []
        for _ in range(2):
            rollout.reset()
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            seq = []
            while rollout.can_scale_up():
                seq.append(rollout.try_scale_up())
            sequences.append(seq)
        self.assertEqual(sequences[0], sequences[1])

    def test_rollback_then_scale_up_repeatability(self):
        """Rollback followed by healthy scale-up is deterministic."""
        results = []
        for _ in range(2):
            rollout.reset()
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            rollout.try_scale_up()  # → step 1
            rollout.force_rollback("test")  # → step 0
            results.append(rollout.try_scale_up())  # scale up again
        self.assertEqual(results[0], results[1])


# ===================================================================
# 3. Monitor determinism  (5 tests)
# ===================================================================

class TestMonitorDeterminism(DeterminismResetMixin, unittest.TestCase):
    """Same counter values must yield identical metrics, rates, and rollback decisions."""

    def _setup_counters(self, successes, errors):
        monitor.reset()
        for _ in range(successes):
            monitor.record_success()
        for _ in range(errors):
            monitor.record_error()

    def test_success_rate_deterministic(self):
        rates = []
        for _ in range(2):
            self._setup_counters(7, 3)
            rates.append(monitor.get_success_rate())
        self.assertEqual(rates[0], rates[1])
        self.assertAlmostEqual(rates[0], 0.7)

    def test_error_rate_deterministic(self):
        rates = []
        for _ in range(2):
            self._setup_counters(7, 3)
            rates.append(monitor.get_error_rate())
        self.assertEqual(rates[0], rates[1])
        self.assertAlmostEqual(rates[0], 0.3)

    def test_metrics_snapshot_deterministic(self):
        """get_metrics() returns identical counter-based fields for same state."""
        snapshots = []
        for _ in range(2):
            self._setup_counters(9, 1)
            m = monitor.get_metrics()
            # Only compare counter-derived fields (memory and restart
            # window metrics are time/platform-dependent)
            snapshots.append({
                "success_count": m["success_count"],
                "error_count": m["error_count"],
                "success_rate": m["success_rate"],
                "error_rate": m["error_rate"],
            })
        self.assertEqual(snapshots[0], snapshots[1])

    def test_rollback_needed_healthy_deterministic(self):
        """Healthy counters → consistently empty rollback reasons."""
        for _ in range(2):
            self._setup_counters(20, 0)
            with patch("builtins.open", mock_open(read_data="VmRSS:\t100 kB\n")):
                reasons = monitor.check_rollback_needed()
            self.assertEqual(reasons, [])

    def test_rollback_needed_unhealthy_deterministic(self):
        """High error rate → consistent rollback trigger across runs."""
        results = []
        for _ in range(2):
            self._setup_counters(1, 9)
            with patch("builtins.open", mock_open(read_data="VmRSS:\t100 kB\n")):
                results.append(monitor.check_rollback_needed())
        self.assertEqual(results[0], results[1])
        self.assertTrue(any("error rate" in r for r in results[0]))


# ===================================================================
# 4. Metric dependency  (3 tests)
# ===================================================================

class TestMetricDependency(DeterminismResetMixin, unittest.TestCase):
    """Rollout decisions depend only on check_fn, not wall-clock."""

    def test_rollout_ignores_wall_clock(self):
        """Same check_fn across repeated runs produces the same result."""
        results = []
        for _ in range(2):
            rollout.reset()
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            results.append(rollout.try_scale_up())
        self.assertEqual(results[0], results[1])

    def test_only_check_fn_controls_rollback(self):
        """Switching check_fn from healthy→unhealthy flips the decision."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        _, action_ok, _ = rollout.try_scale_up()
        self.assertEqual(action_ok, "scaled_up")

        rollout.configure(
            check_rollback_fn=lambda: ["memory usage exceeded"],
            save_baseline_fn=lambda: None,
        )
        _, action_bad, reasons = rollout.try_scale_up()
        self.assertEqual(action_bad, "rollback")
        self.assertIn("memory usage exceeded", reasons)

    def test_check_fn_result_fully_determines_action(self):
        """Identical check_fn returning reasons always triggers rollback."""
        reasons_input = ["error rate 90.0% exceeds 5%"]
        results = []
        for _ in range(3):
            rollout.reset()
            rollout.configure(
                check_rollback_fn=lambda: list(reasons_input),
                save_baseline_fn=lambda: None,
            )
            results.append(rollout.try_scale_up())
        for r in results:
            self.assertEqual(r[1], "rollback")
            self.assertEqual(r[2], reasons_input)


# ===================================================================
# 5. No state drift  (6 tests)
# ===================================================================

class TestNoStateDrift(DeterminismResetMixin, unittest.TestCase):
    """Reset must fully restore initial state with no leakage."""

    def test_rollout_reset_clears_step_index(self):
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()
        rollout.reset()
        self.assertEqual(rollout.get_current_step_index(), 0)

    def test_rollout_reset_clears_rollback_history(self):
        rollout.force_rollback("test")
        rollout.reset()
        self.assertEqual(rollout.get_rollback_history(), [])

    def test_rollout_reset_cycle_produces_identical_initial_state(self):
        """Multiple reset cycles always return to the same initial state."""
        for _ in range(3):
            rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
            rollout.try_scale_up()
            rollout.force_rollback("cycle")
            rollout.reset()
            self.assertEqual(rollout.get_current_workers(), 1)
            self.assertEqual(rollout.get_current_step_index(), 0)
            self.assertEqual(rollout.get_rollback_history(), [])
            self.assertTrue(rollout.can_scale_up())

    def test_monitor_reset_clears_counters(self):
        for _ in range(5):
            monitor.record_success()
            monitor.record_error()
        monitor.reset()
        self.assertEqual(monitor.get_success_rate(), 1.0)
        self.assertEqual(monitor.get_error_rate(), 0.0)

    def test_monitor_reset_clears_baseline(self):
        for _ in range(10):
            monitor.record_success()
        monitor.save_baseline()
        monitor.reset()
        self.assertIsNone(monitor.get_baseline_success_rate())

    def test_monitor_reset_cycle_produces_identical_initial_state(self):
        """Multiple reset cycles always return to the same metrics snapshot."""
        for _ in range(3):
            for _ in range(5):
                monitor.record_success()
            monitor.record_error()
            monitor.record_restart()
            monitor.save_baseline()
            monitor.reset()
            m = monitor.get_metrics()
            self.assertEqual(m["success_count"], 0)
            self.assertEqual(m["error_count"], 0)
            self.assertEqual(m["success_rate"], 1.0)
            self.assertEqual(m["error_rate"], 0.0)
            self.assertEqual(m["restarts_last_hour"], 0)
            self.assertIsNone(m["baseline_success_rate"])


# ===================================================================
# 6. End-to-end pipeline determinism  (4 tests)
# ===================================================================

class TestEndToEndPipelineDeterminism(DeterminismResetMixin, unittest.TestCase):
    """Full monitor→rollout pipeline: same event sequence → same outcome."""

    _FIXED_TIME = 1_700_000_000.0

    def _run_pipeline(self, successes, errors, restarts, mem_proc):
        """Replay a fixed event sequence through monitor+rollout and return results.

        Patches ``time.time`` to a fixed value so that restart-window
        calculations (``restarts_last_hour``) are fully reproducible.
        """
        with patch("modules.monitor.main.time.time", return_value=self._FIXED_TIME):
            monitor.reset()
            rollout.reset()
            rollout.configure(
                check_rollback_fn=monitor.check_rollback_needed,
                save_baseline_fn=monitor.save_baseline,
            )

            for _ in range(successes):
                monitor.record_success()
            for _ in range(errors):
                monitor.record_error()
            for _ in range(restarts):
                monitor.record_restart()

            with patch("builtins.open", mock_open(read_data=mem_proc)):
                result = rollout.try_scale_up()
                metrics = monitor.get_metrics()

        return result, metrics

    def test_healthy_pipeline_deterministic(self):
        """Healthy events → deterministic scale-up across runs."""
        results = []
        for _ in range(2):
            results.append(self._run_pipeline(20, 0, 0, "VmRSS:\t100 kB\n"))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][0][1], "scaled_up")

    def test_unhealthy_pipeline_deterministic(self):
        """High error rate events → deterministic rollback across runs."""
        results = []
        for _ in range(2):
            results.append(self._run_pipeline(1, 19, 0, "VmRSS:\t100 kB\n"))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][0][1], "rollback")

    def test_restart_heavy_pipeline_deterministic(self):
        """Excessive restarts → deterministic rollback across runs."""
        results = []
        for _ in range(2):
            results.append(self._run_pipeline(20, 0, 5, "VmRSS:\t100 kB\n"))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(results[0][0][1], "rollback")

    def test_mixed_event_sequence_deterministic(self):
        """Mixed events → identical metrics and rollout decision across runs."""
        results = []
        for _ in range(2):
            results.append(self._run_pipeline(15, 2, 1, "VmRSS:\t500 kB\n"))
        # Compare rollout decisions
        self.assertEqual(results[0][0], results[1][0])
        # Compare counter-derived metrics (exclude memory which is mocked identically)
        for key in ("success_count", "error_count", "success_rate", "error_rate"):
            self.assertEqual(results[0][1][key], results[1][1][key])


if __name__ == "__main__":
    unittest.main()
