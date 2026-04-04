"""Tests for modules.behavior.main — scaling decision engine.

Validates that the behavior system:
  - Correctly evaluates trigger conditions (error rate, workload, restarts)
  - Makes proper scaling decisions (scale_up, scale_down, hold)
  - Respects cooldown periods
  - Maintains thread safety
  - Does not cause instability
"""

import threading
import unittest

from modules.behavior.main import (
    COOLDOWN_SECONDS,
    ERROR_RATE_THRESHOLD,
    HOLD,
    RESTART_RATE_THRESHOLD,
    SCALE_DOWN,
    SCALE_UP,
    SUCCESS_RATE_DROP_THRESHOLD,
    SUCCESS_RATE_MIN,
    VALID_DECISIONS,
    evaluate,
    expire_cooldown_for_testing,
    get_decision_history,
    get_last_decision_time,
    get_status,
    reset,
)


class BehaviorResetMixin:
    """Common setUp/tearDown for behavior tests."""

    def setUp(self):
        reset()

    def tearDown(self):
        reset()


def _healthy_metrics():
    """Return a metrics dict representing a completely healthy system."""
    return {
        "error_rate": 0.0,
        "success_rate": 1.0,
        "restarts_last_hour": 0,
        "baseline_success_rate": 1.0,
    }


def _unhealthy_error_rate():
    """Return metrics with error rate above threshold."""
    return {
        "error_rate": 0.10,
        "success_rate": 0.90,
        "restarts_last_hour": 0,
        "baseline_success_rate": 1.0,
    }


def _unhealthy_restarts():
    """Return metrics with excessive restarts."""
    return {
        "error_rate": 0.01,
        "success_rate": 0.99,
        "restarts_last_hour": 5,
        "baseline_success_rate": 1.0,
    }


def _unhealthy_success_drop():
    """Return metrics with a success rate drop from baseline."""
    return {
        "error_rate": 0.02,
        "success_rate": 0.75,
        "restarts_last_hour": 0,
        "baseline_success_rate": 1.0,
    }


# ── Decision rules ────────────────────────────────────────────────


class TestScaleUpDecision(BehaviorResetMixin, unittest.TestCase):
    """Rule 4: Scale up when all metrics are healthy and not at max."""

    def test_scale_up_when_healthy(self):
        action, reasons = evaluate(_healthy_metrics(), 0, 3)
        self.assertEqual(action, SCALE_UP)
        self.assertIn("all_metrics_healthy", reasons)

    def test_scale_up_at_intermediate_step(self):
        action, reasons = evaluate(_healthy_metrics(), 1, 3)
        self.assertEqual(action, SCALE_UP)

    def test_scale_up_one_below_max(self):
        action, reasons = evaluate(_healthy_metrics(), 2, 3)
        self.assertEqual(action, SCALE_UP)

    def test_no_scale_up_at_max(self):
        action, reasons = evaluate(_healthy_metrics(), 3, 3)
        self.assertEqual(action, HOLD)
        self.assertIn("at_max_scale", reasons)

    def test_scale_up_no_baseline(self):
        metrics = _healthy_metrics()
        metrics["baseline_success_rate"] = None
        action, reasons = evaluate(metrics, 0, 3)
        self.assertEqual(action, SCALE_UP)


class TestScaleDownErrorRate(BehaviorResetMixin, unittest.TestCase):
    """Rule 1: Scale down on high error rate."""

    def test_scale_down_high_error_rate(self):
        action, reasons = evaluate(_unhealthy_error_rate(), 2, 3)
        self.assertEqual(action, SCALE_DOWN)
        self.assertTrue(any("error_rate" in r for r in reasons))

    def test_scale_down_at_threshold_boundary(self):
        metrics = _healthy_metrics()
        metrics["error_rate"] = ERROR_RATE_THRESHOLD + 0.001
        metrics["success_rate"] = 1.0 - metrics["error_rate"]
        action, _ = evaluate(metrics, 2, 3)
        self.assertEqual(action, SCALE_DOWN)

    def test_no_scale_down_at_threshold(self):
        metrics = _healthy_metrics()
        metrics["error_rate"] = ERROR_RATE_THRESHOLD
        action, _ = evaluate(metrics, 0, 3)
        self.assertEqual(action, SCALE_UP)


class TestScaleDownRestarts(BehaviorResetMixin, unittest.TestCase):
    """Rule 2: Scale down on excessive restarts."""

    def test_scale_down_excessive_restarts(self):
        action, reasons = evaluate(_unhealthy_restarts(), 2, 3)
        self.assertEqual(action, SCALE_DOWN)
        self.assertTrue(any("restarts" in r for r in reasons))

    def test_restarts_at_threshold_no_scale_down(self):
        metrics = _healthy_metrics()
        metrics["restarts_last_hour"] = RESTART_RATE_THRESHOLD
        action, _ = evaluate(metrics, 0, 3)
        self.assertEqual(action, SCALE_UP)

    def test_restarts_above_threshold_scale_down(self):
        metrics = _healthy_metrics()
        metrics["restarts_last_hour"] = RESTART_RATE_THRESHOLD + 1
        action, _ = evaluate(metrics, 2, 3)
        self.assertEqual(action, SCALE_DOWN)


class TestScaleDownSuccessDrop(BehaviorResetMixin, unittest.TestCase):
    """Rule 3: Scale down on success rate drop from baseline."""

    def test_scale_down_success_drop(self):
        action, reasons = evaluate(_unhealthy_success_drop(), 2, 3)
        self.assertEqual(action, SCALE_DOWN)
        self.assertTrue(any("success_rate dropped" in r for r in reasons))

    def test_no_scale_down_small_drop(self):
        metrics = _healthy_metrics()
        metrics["success_rate"] = 0.95
        metrics["baseline_success_rate"] = 1.0
        action, _ = evaluate(metrics, 0, 3)
        # 5% drop is below 10% threshold
        self.assertEqual(action, SCALE_UP)

    def test_no_baseline_ignores_drop_rule(self):
        metrics = _healthy_metrics()
        metrics["baseline_success_rate"] = None
        metrics["success_rate"] = 0.50
        metrics["error_rate"] = 0.0
        action, _ = evaluate(metrics, 0, 3)
        # No baseline means rule 3 doesn't fire, but low success_rate < 70% means no scale up
        self.assertNotEqual(action, SCALE_UP)


class TestHoldAtMinScale(BehaviorResetMixin, unittest.TestCase):
    """Rule 5: Never scale below step 0."""

    def test_hold_at_min_when_unhealthy(self):
        action, reasons = evaluate(_unhealthy_error_rate(), 0, 3)
        self.assertEqual(action, HOLD)
        self.assertIn("already_at_min_scale", reasons)


class TestMarginalMetrics(BehaviorResetMixin, unittest.TestCase):
    """HOLD when metrics are marginal — not bad enough to scale down,
    not good enough to scale up."""

    def test_low_success_rate_no_errors(self):
        metrics = {
            "error_rate": 0.0,
            "success_rate": 0.60,
            "restarts_last_hour": 0,
            "baseline_success_rate": None,
        }
        action, reasons = evaluate(metrics, 1, 3)
        self.assertEqual(action, HOLD)
        self.assertIn("metrics_marginal", reasons)


# ── Cooldown ──────────────────────────────────────────────────────


class TestCooldown(BehaviorResetMixin, unittest.TestCase):
    """Cooldown guard prevents rapid scaling oscillation."""

    def test_cooldown_blocks_second_decision(self):
        action1, _ = evaluate(_healthy_metrics(), 0, 3)
        self.assertEqual(action1, SCALE_UP)
        action2, reasons2 = evaluate(_healthy_metrics(), 1, 3)
        self.assertEqual(action2, HOLD)
        self.assertIn("cooldown_active", reasons2)

    def test_cooldown_expires(self):
        action1, _ = evaluate(_healthy_metrics(), 0, 3)
        self.assertEqual(action1, SCALE_UP)
        # Manually expire cooldown
        expire_cooldown_for_testing()
        action2, _ = evaluate(_healthy_metrics(), 1, 3)
        self.assertEqual(action2, SCALE_UP)


# ── Decision history ──────────────────────────────────────────────


class TestDecisionHistory(BehaviorResetMixin, unittest.TestCase):
    """Decision history is recorded and queryable."""

    def test_history_recorded(self):
        evaluate(_healthy_metrics(), 0, 3)
        history = get_decision_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["action"], SCALE_UP)
        self.assertIn("time", history[0])
        self.assertIn("reasons", history[0])
        self.assertIn("metrics_snapshot", history[0])

    def test_history_bounded(self):
        for i in range(110):
            expire_cooldown_for_testing()
            evaluate(_healthy_metrics(), 0, 3)
        history = get_decision_history()
        self.assertLessEqual(len(history), 100)

    def test_history_copy_is_independent(self):
        evaluate(_healthy_metrics(), 0, 3)
        h1 = get_decision_history()
        h1.clear()
        h2 = get_decision_history()
        self.assertEqual(len(h2), 1)


# ── Status ────────────────────────────────────────────────────────


class TestStatus(BehaviorResetMixin, unittest.TestCase):
    """get_status() returns correct snapshot."""

    def test_initial_status(self):
        status = get_status()
        self.assertEqual(status["decision_count"], 0)
        self.assertEqual(status["cooldown_seconds"], COOLDOWN_SECONDS)
        self.assertIn("thresholds", status)

    def test_status_after_decision(self):
        evaluate(_healthy_metrics(), 0, 3)
        status = get_status()
        self.assertEqual(status["decision_count"], 1)
        self.assertGreater(status["last_decision_time"], 0)

    def test_thresholds_match_constants(self):
        thresholds = get_status()["thresholds"]
        self.assertEqual(thresholds["error_rate"], ERROR_RATE_THRESHOLD)
        self.assertEqual(thresholds["success_rate_min"], SUCCESS_RATE_MIN)
        self.assertEqual(thresholds["restart_rate"], RESTART_RATE_THRESHOLD)
        self.assertEqual(thresholds["success_rate_drop"], SUCCESS_RATE_DROP_THRESHOLD)


# ── Reset ─────────────────────────────────────────────────────────


class TestReset(BehaviorResetMixin, unittest.TestCase):
    """reset() clears all state."""

    def test_reset_clears_history(self):
        evaluate(_healthy_metrics(), 0, 3)
        self.assertGreater(len(get_decision_history()), 0)
        reset()
        self.assertEqual(len(get_decision_history()), 0)

    def test_reset_clears_cooldown(self):
        evaluate(_healthy_metrics(), 0, 3)
        self.assertGreater(get_last_decision_time(), 0)
        reset()
        self.assertEqual(get_last_decision_time(), 0.0)


# ── Thread safety ─────────────────────────────────────────────────


class TestThreadSafety(BehaviorResetMixin, unittest.TestCase):
    """Concurrent evaluate() calls must not corrupt state."""

    def test_concurrent_evaluations(self):
        errors = []

        def worker(idx):
            try:
                expire_cooldown_for_testing()
                action, reasons = evaluate(_healthy_metrics(), 0, 3)
                if action not in VALID_DECISIONS:
                    errors.append(f"Thread {idx}: invalid action {action}")
            except Exception as exc:
                errors.append(f"Thread {idx}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])


# ── Multiple trigger combinations ─────────────────────────────────


class TestMultipleTriggers(BehaviorResetMixin, unittest.TestCase):
    """Multiple bad signals should all appear in reasons."""

    def test_error_and_restarts_combined(self):
        metrics = {
            "error_rate": 0.10,
            "success_rate": 0.90,
            "restarts_last_hour": 5,
            "baseline_success_rate": None,
        }
        action, reasons = evaluate(metrics, 2, 3)
        self.assertEqual(action, SCALE_DOWN)
        self.assertTrue(any("error_rate" in r for r in reasons))
        self.assertTrue(any("restarts" in r for r in reasons))

    def test_all_triggers_fire(self):
        metrics = {
            "error_rate": 0.20,
            "success_rate": 0.50,
            "restarts_last_hour": 10,
            "baseline_success_rate": 1.0,
        }
        action, reasons = evaluate(metrics, 2, 3)
        self.assertEqual(action, SCALE_DOWN)
        # All three rules should trigger
        self.assertTrue(any("error_rate" in r for r in reasons))
        self.assertTrue(any("restarts" in r for r in reasons))
        self.assertTrue(any("success_rate dropped" in r for r in reasons))


# ── Valid decisions ───────────────────────────────────────────────


class TestValidDecisions(BehaviorResetMixin, unittest.TestCase):
    """All decisions must be from VALID_DECISIONS set."""

    def test_valid_decisions_constant(self):
        self.assertEqual(VALID_DECISIONS, {SCALE_UP, SCALE_DOWN, HOLD})

    def test_decision_always_valid(self):
        scenarios = [
            (_healthy_metrics(), 0, 3),
            (_healthy_metrics(), 3, 3),
            (_unhealthy_error_rate(), 0, 3),
            (_unhealthy_error_rate(), 2, 3),
            (_unhealthy_restarts(), 1, 3),
            (_unhealthy_success_drop(), 2, 3),
        ]
        for i, (m, idx, mx) in enumerate(scenarios):
            expire_cooldown_for_testing()
            action, reasons = evaluate(m, idx, mx)
            self.assertIn(action, VALID_DECISIONS,
                          f"Scenario {i}: {action} not in VALID_DECISIONS")
            self.assertIsInstance(reasons, list)
            self.assertGreater(len(reasons), 0)


# ── Missing / partial metrics ─────────────────────────────────────


class TestPartialMetrics(BehaviorResetMixin, unittest.TestCase):
    """evaluate() handles missing keys gracefully via .get() defaults."""

    def test_empty_metrics(self):
        action, reasons = evaluate({}, 0, 3)
        self.assertIn(action, VALID_DECISIONS)

    def test_partial_metrics(self):
        action, reasons = evaluate({"error_rate": 0.0}, 0, 3)
        self.assertIn(action, VALID_DECISIONS)


if __name__ == "__main__":
    unittest.main()
