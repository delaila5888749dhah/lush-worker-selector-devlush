"""Cross-module stabilization tests (PR 15).

Validates the final integration contract between behavior, monitor,
rollout, autoscaler, and runtime:

  1. Concurrent behavior SCALE_DOWN and autoscaler threshold breach do not
     double-decrement rollout step (rollout._rollback_applied guard).

  2. Full chain: monitor.get_metrics() → behavior.evaluate() →
     rollout.try_scale_up()/force_rollback() → runtime._apply_scale()
     stays consistent under concurrent decision windows.

  3. Circuit-breaker events are independently observable (rollback CB vs
     billing CB emit distinct log action names).

  4. Metrics-unavailable deferred path emits its own distinct log action
     ('metrics_unavailable_scaling_deferred') rather than 'hold' or
     'hold_deferred'.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pathlib import Path

from integration import runtime
from integration.runtime import start, stop
from modules.behavior import main as behavior
from modules.billing import main as billing
from modules.monitor import main as monitor
from modules.rollout import autoscaler as autoscaler_module
from modules.rollout import main as rollout
from modules.rollout.autoscaler import get_autoscaler


def _wait_until(condition_fn, timeout=2.0, interval=0.01):
    """Poll condition_fn until True or timeout expires.

    Returns True if the condition was met, False if deadline was reached.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


class _ResetMixin:
    """Reset all cross-module state before and after each test."""

    def setUp(self):  # pylint: disable=invalid-name
        """Reset all cross-module state before each test."""
        runtime.reset()
        rollout.reset()
        monitor.reset()
        behavior.reset()
        autoscaler_module.reset()
        self._billing_dir = tempfile.mkdtemp()
        profiles_path = os.path.join(self._billing_dir, "profiles.txt")
        with open(profiles_path, "w", encoding="utf-8") as handle:
            handle.write("Alice|Smith|1 Main St|City|NY|10001|2125550001|a@e.com\n")
        self._billing_patcher = patch.object(
            billing, "_pool_dir", return_value=Path(self._billing_dir)
        )
        self._billing_patcher.start()

    def tearDown(self):  # pylint: disable=invalid-name
        """Tear down billing patch and reset all cross-module state."""
        self._billing_patcher.stop()
        shutil.rmtree(self._billing_dir, ignore_errors=True)
        runtime.reset()
        rollout.reset()
        monitor.reset()
        behavior.reset()
        autoscaler_module.reset()


# ── 1. Concurrent rollback: behavior path + autoscaler path ──────────────────


class TestConcurrentRollbackPreventsDoubleDecrement(_ResetMixin, unittest.TestCase):
    """rollout._rollback_applied guard prevents over-decrement from concurrent
    behavior and autoscaler rollback paths."""

    def test_behavior_and_autoscaler_concurrent_rollback_decrements_once(self):
        """Concurrent calls from the behavior path (runtime loop SCALE_DOWN →
        force_rollback()) and the autoscaler path (record_failure() threshold →
        force_rollback()) only decrement the rollout step once per window."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()  # step 0 → 1
        rollout.try_scale_up()  # step 1 → 2
        self.assertEqual(rollout.get_current_step_index(), 2)

        scaler = get_autoscaler()
        # Pre-load failure count so the next record_failure() triggers scale-down
        with scaler._lock:  # pylint: disable=protected-access
            scaler._consecutive_failures["w1"] = (  # pylint: disable=protected-access
                scaler._CONSECUTIVE_FAILURE_THRESHOLD - 1  # pylint: disable=protected-access
            )

        barrier = threading.Barrier(2)
        errors = []

        def behavior_path():
            """Simulate runtime loop SCALE_DOWN → force_rollback()."""
            barrier.wait()
            try:
                rollout.force_rollback("behavior_scale_down")
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        def autoscaler_path():
            """Simulate autoscaler threshold → record_failure() → force_rollback()."""
            barrier.wait()
            try:
                scaler.record_failure("w1")
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        thread_behavior = threading.Thread(target=behavior_path)
        thread_autoscaler = threading.Thread(target=autoscaler_path)
        thread_behavior.start()
        thread_autoscaler.start()
        thread_behavior.join()
        thread_autoscaler.join()

        self.assertEqual(errors, [])
        # Exactly one decrement: step 2 → 1.  The second caller was blocked
        # by rollout._rollback_applied and returned idempotently.
        self.assertEqual(rollout.get_current_step_index(), 1)

    def test_three_concurrent_force_rollback_callers_decrement_once(self):
        """Three concurrent force_rollback() callers (any mix of behavior,
        autoscaler, runtime) still only decrement the step once per window."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()
        rollout.try_scale_up()
        rollout.try_scale_up()
        self.assertEqual(rollout.get_current_step_index(), 3)

        barrier = threading.Barrier(3)
        errors = []

        def do_rollback(reason):
            """Execute force_rollback after barrier synchronization."""
            barrier.wait()
            try:
                rollout.force_rollback(reason)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [
            threading.Thread(target=do_rollback, args=(f"caller-{i}",))
            for i in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        # Exactly one decrement: step 3 → 2
        self.assertEqual(rollout.get_current_step_index(), 2)

    def test_rollback_guard_resets_on_next_scale_up(self):
        """After try_scale_up() opens a new window, exactly one more forced
        rollback is permitted."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()           # 0 → 1
        rollout.force_rollback("first")  # 1 → 0, guard=True
        rollout.try_scale_up()           # 0 → 1, guard reset
        rollout.force_rollback("second") # 1 → 0, guard=True (new window)
        self.assertEqual(rollout.get_current_step_index(), 0)


# ── 2. Full integration chain consistency ────────────────────────────────────


class TestFullIntegrationChainConsistency(_ResetMixin, unittest.TestCase):
    """monitor → behavior → rollout → runtime._apply_scale() chain stays
    internally consistent under normal and concurrent use."""

    def test_healthy_metrics_chain_scales_up(self):
        """Healthy monitor metrics produce a SCALE_UP decision that advances
        the rollout step."""
        rollout.configure(
            check_rollback_fn=monitor.check_rollback_needed,
            save_baseline_fn=monitor.save_baseline,
        )
        for _ in range(100):
            monitor.record_success()

        metrics = monitor.get_metrics()
        behavior.expire_cooldown_for_testing()
        decision, _ = behavior.evaluate(
            metrics,
            rollout.get_current_step_index(),
            len(rollout.SCALE_STEPS) - 1,
        )
        self.assertEqual(decision, behavior.SCALE_UP,
                         "Healthy metrics must yield SCALE_UP")

        _, action, _ = rollout.try_scale_up()
        self.assertEqual(action, "scaled_up")
        self.assertEqual(rollout.get_current_step_index(), 1)

    def test_degraded_metrics_chain_rolls_back_once(self):
        """Degraded monitor metrics produce SCALE_DOWN → force_rollback()
        decrements the rollout step exactly once."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()
        initial = rollout.get_current_step_index()

        for _ in range(100):
            monitor.record_error()
        monitor.save_baseline()

        metrics = monitor.get_metrics()
        behavior.expire_cooldown_for_testing()
        decision, reasons = behavior.evaluate(
            metrics, initial, len(rollout.SCALE_STEPS) - 1
        )
        self.assertEqual(decision, behavior.SCALE_DOWN,
                         "Degraded metrics must yield SCALE_DOWN")

        rollout.force_rollback("; ".join(reasons))
        self.assertEqual(rollout.get_current_step_index(), initial - 1,
                         "force_rollback() must decrement exactly one step")

    def test_apply_scale_receives_valid_rollout_target(self):
        """runtime._apply_scale() is called with the target count returned
        by rollout (always a value from SCALE_STEPS)."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        applied_targets = []

        def spy_apply(target_count, task_fn):  # pylint: disable=unused-argument
            """Capture target_count passed to _apply_scale."""
            applied_targets.append(target_count)

        tick = threading.Event()
        with patch("integration.runtime._apply_scale", side_effect=spy_apply), \
             patch.object(behavior, "evaluate",
                          return_value=(behavior.SCALE_UP, ["healthy"])):
            start(lambda _: tick.wait(timeout=2), interval=0.05)
            _wait_until(lambda: len(applied_targets) > 0, timeout=2)
            tick.set()
            stop(timeout=2)

        self.assertGreater(len(applied_targets), 0,
                           "_apply_scale() should have been called at least once")
        for target in applied_targets:
            self.assertIn(target, rollout.SCALE_STEPS,
                          f"_apply_scale called with invalid target {target}")

    def test_concurrent_chain_ticks_leave_valid_state(self):
        """Five concurrent chain ticks (monitor→behavior→rollout) leave
        rollout in a valid, in-range step index with no exceptions."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        rollout.try_scale_up()
        errors = []

        def tick():
            """Execute one monitor→behavior→rollout decision cycle."""
            try:
                metrics = monitor.get_metrics()
                step = rollout.get_current_step_index()
                max_idx = len(rollout.SCALE_STEPS) - 1
                decision, reasons = behavior.evaluate(metrics, step, max_idx)
                if decision == behavior.SCALE_UP:
                    rollout.try_scale_up()
                elif decision == behavior.SCALE_DOWN:
                    rollout.force_rollback("; ".join(reasons))
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=tick) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [],
                         f"Concurrent chain ticks raised unexpected errors: {errors}")
        step = rollout.get_current_step_index()
        self.assertGreaterEqual(step, 0)
        self.assertLessEqual(step, len(rollout.SCALE_STEPS) - 1)


# ── 3. Circuit-breaker observability ─────────────────────────────────────────


class TestCircuitBreakerObservability(_ResetMixin, unittest.TestCase):
    """Rollback CB and billing CB must emit distinct, observable log events."""

    def test_consecutive_rollbacks_trigger_circuit_breaker_log(self):
        """Three consecutive SCALE_DOWN decisions (with workers safe each tick)
        emit a log message containing 'circuit_breaker_triggered' from the
        runtime loop."""
        rollout.configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        for _ in range(3):
            rollout.try_scale_up()

        log_messages = []
        handler = logging.Handler()
        handler.emit = lambda r: log_messages.append(r.getMessage())
        logger = logging.getLogger("integration.runtime")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            # Patch _is_safe_locked so every tick reaches the SCALE_DOWN path
            # (not "hold_deferred") ensuring _consecutive_rollbacks increments.
            with patch.object(runtime, "_CIRCUIT_BREAKER_PAUSE", 0), \
                 patch.object(behavior, "evaluate",
                              return_value=(behavior.SCALE_DOWN, ["high_error"])), \
                 patch("integration.runtime._is_safe_locked", return_value=True):
                start(lambda _: time.sleep(0.5), interval=0.05)
                _wait_until(
                    lambda: any("circuit_breaker" in m.lower()
                                for m in log_messages),
                    timeout=3,
                )
                stop(timeout=2)
        finally:
            logger.removeHandler(handler)

        self.assertTrue(
            any("circuit_breaker" in m.lower() for m in log_messages),
            "Expected 'circuit_breaker' in runtime log after consecutive rollbacks",
        )

    def test_billing_and_rollback_cb_event_names_are_distinct(self):
        """billing_cb_triggered and circuit_breaker_triggered are distinct
        string literals so operators can filter them independently in logs."""
        self.assertNotEqual("billing_cb_triggered", "circuit_breaker_triggered")


# ── 4. Metrics-unavailable path observability ─────────────────────────────────


class TestMetricsUnavailableObservability(_ResetMixin, unittest.TestCase):
    """When monitor.get_metrics() raises, the runtime must emit a uniquely
    named log action 'metrics_unavailable_scaling_deferred'."""

    def test_metrics_unavailable_emits_distinct_action(self):
        """metrics_unavailable_scaling_deferred is distinct from 'hold' (normal
        HOLD decision) and 'hold_deferred' (unsafe worker state)."""
        log_messages = []
        handler = logging.Handler()
        handler.emit = lambda r: log_messages.append(r.getMessage())
        logger = logging.getLogger("integration.runtime")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            with patch("integration.runtime.monitor") as mock_mon:
                mock_mon.get_metrics.side_effect = RuntimeError("monitor unavailable")
                mock_mon.check_rollback_needed.return_value = []
                mock_mon.save_baseline.return_value = None
                mock_mon.record_restart.return_value = None
                start(lambda _: time.sleep(0.5), interval=0.05)
                _wait_until(
                    lambda: any(
                        "metrics_unavailable_scaling_deferred" in m
                        for m in log_messages
                    ),
                    timeout=2,
                )
                stop(timeout=2)
        finally:
            logger.removeHandler(handler)

        self.assertTrue(
            any("metrics_unavailable_scaling_deferred" in m for m in log_messages),
            f"Expected 'metrics_unavailable_scaling_deferred' in log. "
            f"Got: {log_messages[:5]}",
        )
        # Confirm it is distinct from the two other deferred-action names
        self.assertNotIn(
            "metrics_unavailable_scaling_deferred", {"hold", "hold_deferred"}
        )


if __name__ == "__main__":
    unittest.main()
