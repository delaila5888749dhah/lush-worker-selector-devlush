"""Tests for Task 2: Scaling Execution Layer — behavior→runtime→rollout integration.

Validates that:
  - behavior.evaluate() is called in the runtime loop with correct metrics
  - SCALE_UP decision → rollout.try_scale_up() is called
  - SCALE_DOWN decision → rollout.force_rollback() is called
  - HOLD decision → no scaling change
  - consecutive_rollbacks tracked correctly (only cleared on scale_up)
  - No race conditions
  - Lifecycle (INIT/RUNNING/STOPPED) not broken
  - behavior state reset when runtime.reset() is called
"""

import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime
from integration.runtime import (
    get_status,
    is_running,
    reset,
    start,
    stop,
)
from modules.behavior import main as behavior
from modules.monitor import main as monitor
from modules.rollout import main as rollout


class ScalingResetMixin:
    """Common setUp/tearDown for scaling execution tests."""

    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()
        behavior.reset()

    def tearDown(self):
        reset()
        rollout.reset()
        monitor.reset()
        behavior.reset()


# ── Decision routing ────────────────────────────────────────────────


class TestDecisionRouting(ScalingResetMixin, unittest.TestCase):
    """Verify that behavior decisions route to correct rollout actions."""

    def test_scale_up_routes_to_try_scale_up(self):
        """SCALE_UP decision calls rollout.try_scale_up()."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)
        tick = threading.Event()

        def task_fn(_):
            tick.wait(timeout=2)

        # Healthy metrics → behavior should decide SCALE_UP
        start(task_fn, interval=0.05)
        time.sleep(0.4)
        # Should have scaled up from step 0
        step = rollout.get_current_step_index()
        self.assertGreater(step, 0, "Expected rollout to advance past step 0")
        tick.set()
        stop(timeout=2)

    def test_scale_down_routes_to_force_rollback(self):
        """SCALE_DOWN decision calls rollout.force_rollback()."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)
        # First scale up manually
        rollout.try_scale_up()
        initial_step = rollout.get_current_step_index()
        self.assertGreater(initial_step, 0)

        # Inject high error rate so behavior decides SCALE_DOWN
        for _ in range(100):
            monitor.record_error()

        behavior.expire_cooldown_for_testing()

        tick = threading.Event()

        def task_fn(_):
            tick.wait(timeout=2)

        start(task_fn, interval=0.05)
        time.sleep(0.3)
        # Should have rolled back
        step_after = rollout.get_current_step_index()
        self.assertLess(step_after, initial_step,
                        "Expected rollback to reduce step index")
        tick.set()
        stop(timeout=2)

    def test_hold_does_not_change_scaling(self):
        """HOLD decision keeps current worker count unchanged."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)

        with patch.object(behavior, "evaluate",
                          return_value=(behavior.HOLD, ["cooldown_active"])):
            tick = threading.Event()

            def task_fn(_):
                tick.wait(timeout=2)

            start(task_fn, interval=0.05)
            time.sleep(0.2)
            step = rollout.get_current_step_index()
            self.assertEqual(step, 0, "HOLD should not change step index")
            tick.set()
            stop(timeout=2)


# ── Consecutive rollback tracking ─────────────────────────────────


class TestConsecutiveRollbacks(ScalingResetMixin, unittest.TestCase):
    """Verify consecutive_rollbacks counter behavior with behavior engine."""

    def test_rollback_increments_counter(self):
        """SCALE_DOWN increments consecutive_rollbacks."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)
        # Scale up first
        rollout.try_scale_up()

        # Force unhealthy metrics
        for _ in range(100):
            monitor.record_error()
        behavior.expire_cooldown_for_testing()

        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.3)
        status = get_status()
        self.assertGreater(status["consecutive_rollbacks"], 0)
        stop(timeout=2)

    def test_scale_up_clears_counter(self):
        """Successful scale_up clears consecutive_rollbacks."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)

        # Manually set consecutive rollbacks
        with runtime._lock:
            runtime._consecutive_rollbacks = 3

        # Healthy metrics → scale up
        tick = threading.Event()

        def task_fn(_):
            tick.wait(timeout=2)

        start(task_fn, interval=0.05)
        time.sleep(0.3)
        status = get_status()
        self.assertEqual(status["consecutive_rollbacks"], 0,
                         "SCALE_UP should clear consecutive_rollbacks")
        tick.set()
        stop(timeout=2)

    def test_hold_does_not_clear_counter(self):
        """HOLD does not reset consecutive_rollbacks."""
        with patch.object(behavior, "evaluate",
                          return_value=(behavior.HOLD, ["cooldown_active"])):
            with runtime._lock:
                runtime._consecutive_rollbacks = 2

            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(0.2)
            status = get_status()
            self.assertEqual(status["consecutive_rollbacks"], 2,
                             "HOLD should not change consecutive_rollbacks")
            stop(timeout=2)


# ── Lifecycle integrity ──────────────────────────────────────────


class TestLifecycleIntegrity(ScalingResetMixin, unittest.TestCase):
    """Behavior integration must not break lifecycle transitions."""

    def test_init_to_running(self):
        """start() transitions INIT → RUNNING."""
        self.assertEqual(runtime.get_state(), "INIT")
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(runtime.get_state(), "RUNNING")
        stop(timeout=2)

    def test_running_to_stopped(self):
        """stop() transitions RUNNING → STOPPED."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertEqual(runtime.get_state(), "STOPPED")

    def test_behavior_reset_on_runtime_reset(self):
        """runtime.reset() also resets behavior state."""
        behavior.evaluate(
            {"error_rate": 0.0, "success_rate": 1.0,
             "restarts_last_hour": 0, "baseline_success_rate": 1.0},
            0, 3,
        )
        self.assertGreater(len(behavior.get_decision_history()), 0)
        reset()
        self.assertEqual(len(behavior.get_decision_history()), 0)
        self.assertEqual(behavior.get_last_decision_time(), 0.0)


# ── Thread safety ────────────────────────────────────────────────


class TestScalingThreadSafety(ScalingResetMixin, unittest.TestCase):
    """Behavior + runtime + rollout concurrent access must not corrupt state."""

    def test_concurrent_start_stop_with_behavior(self):
        """Rapid start/stop cycles with behavior engine don't crash."""
        errors = []

        def cycle():
            try:
                for _ in range(5):
                    started = start(lambda _: time.sleep(0.01), interval=0.05)
                    if started:
                        time.sleep(0.05)
                        stop(timeout=2)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=cycle) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

    def test_behavior_evaluate_during_runtime(self):
        """External behavior.evaluate() calls during runtime don't crash."""
        errors = []
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)

        tick = threading.Event()

        def task_fn(_):
            tick.wait(timeout=2)

        start(task_fn, interval=0.05)

        def external_evaluate():
            try:
                for _ in range(20):
                    behavior.expire_cooldown_for_testing()
                    action, reasons = behavior.evaluate(
                        {"error_rate": 0.0, "success_rate": 1.0,
                         "restarts_last_hour": 0, "baseline_success_rate": 1.0},
                        0, 3,
                    )
                    if action not in behavior.VALID_DECISIONS:
                        errors.append(f"Invalid decision: {action}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=external_evaluate) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        tick.set()
        stop(timeout=2)
        self.assertEqual(errors, [])


# ── Monitor unavailable ──────────────────────────────────────────


class TestMonitorUnavailableWithBehavior(ScalingResetMixin, unittest.TestCase):
    """Runtime loop survives when monitor is unavailable."""

    def test_loop_continues_on_monitor_failure(self):
        """When monitor.get_metrics() fails, loop continues without crashing."""
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(0.2)
            self.assertTrue(is_running())
            stop(timeout=2)


# ── behavior.evaluate called with correct args ──────────────────


class TestBehaviorCalledCorrectly(ScalingResetMixin, unittest.TestCase):
    """Verify behavior.evaluate is called with proper metrics/indices."""

    def test_evaluate_receives_metrics_and_indices(self):
        """behavior.evaluate() is called with monitor metrics and rollout indices."""
        captured = []

        def spy_evaluate(metrics, step_index, max_index):
            captured.append({
                "metrics": metrics,
                "step_index": step_index,
                "max_index": max_index,
            })
            return behavior.HOLD, ["test_hold"]

        with patch.object(behavior, "evaluate", side_effect=spy_evaluate):
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(0.2)
            stop(timeout=2)

        self.assertGreater(len(captured), 0, "evaluate() should have been called")
        call = captured[0]
        self.assertIn("error_rate", call["metrics"])
        self.assertIn("success_rate", call["metrics"])
        self.assertIsInstance(call["step_index"], int)
        self.assertIsInstance(call["max_index"], int)
        self.assertEqual(call["max_index"], len(rollout.SCALE_STEPS) - 1)


if __name__ == "__main__":
    unittest.main()
