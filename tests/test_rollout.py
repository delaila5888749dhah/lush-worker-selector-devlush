import unittest
from unittest.mock import patch

from integration.rollout import (
    ROLLOUT_STEPS,
    advance,
    can_advance,
    evaluate,
    get_active_workers,
    get_current_step,
    get_rollback_reasons,
    get_status,
    is_rollback_active,
    reset,
    rollback,
    set_active_workers,
)
from modules.monitor import main as monitor


class RolloutResetMixin:
    def setUp(self):
        reset()
        monitor.reset()

    def tearDown(self):
        reset()
        monitor.reset()


class TestRolloutSteps(RolloutResetMixin, unittest.TestCase):
    def test_steps_are_correct(self):
        self.assertEqual(ROLLOUT_STEPS, (1, 3, 5, 10))

    def test_initial_step_is_one(self):
        self.assertEqual(get_current_step(), 1)


class TestActiveWorkers(RolloutResetMixin, unittest.TestCase):
    def test_initial_active_workers_zero(self):
        self.assertEqual(get_active_workers(), 0)

    def test_set_active_workers(self):
        set_active_workers(3)
        self.assertEqual(get_active_workers(), 3)


class TestCanAdvance(RolloutResetMixin, unittest.TestCase):
    def test_can_advance_when_healthy(self):
        for _ in range(10):
            monitor.record_success()
        self.assertTrue(can_advance())

    def test_cannot_advance_at_final_step(self):
        for _ in range(10):
            monitor.record_success()
        # Advance through all steps
        for _ in range(len(ROLLOUT_STEPS) - 1):
            advance()
        self.assertFalse(can_advance())

    def test_cannot_advance_when_rollback_active(self):
        for _ in range(10):
            monitor.record_success()
        # Force a rollback
        for _ in range(4):
            monitor.record_restart()
        evaluate()
        self.assertFalse(can_advance())


class TestAdvance(RolloutResetMixin, unittest.TestCase):
    def test_advance_increments_step(self):
        for _ in range(10):
            monitor.record_success()
        result = advance()
        self.assertEqual(result, 3)
        self.assertEqual(get_current_step(), 3)

    def test_advance_saves_baseline(self):
        for _ in range(20):
            monitor.record_success()
        record_before = monitor.get_success_rate()
        advance()
        self.assertAlmostEqual(monitor.get_baseline_success_rate(), record_before)

    def test_advance_returns_none_when_unhealthy(self):
        # Only errors → high error rate
        for _ in range(10):
            monitor.record_error()
        result = advance()
        self.assertIsNone(result)

    def test_full_rollout_sequence(self):
        for _ in range(10):
            monitor.record_success()
        self.assertEqual(advance(), 3)
        self.assertEqual(advance(), 5)
        self.assertEqual(advance(), 10)
        self.assertIsNone(advance())  # Already at final step


class TestEvaluate(RolloutResetMixin, unittest.TestCase):
    def test_healthy_evaluation(self):
        for _ in range(10):
            monitor.record_success()
        healthy, reasons = evaluate()
        self.assertTrue(healthy)
        self.assertEqual(reasons, [])

    def test_unhealthy_sets_rollback_active(self):
        for _ in range(4):
            monitor.record_restart()
        for _ in range(10):
            monitor.record_success()
        healthy, reasons = evaluate()
        self.assertFalse(healthy)
        self.assertTrue(is_rollback_active())
        self.assertGreater(len(reasons), 0)

    def test_rollback_reasons_stored(self):
        for _ in range(4):
            monitor.record_restart()
        for _ in range(10):
            monitor.record_success()
        evaluate()
        stored = get_rollback_reasons()
        self.assertTrue(any("worker restarts" in r for r in stored))


class TestRollback(RolloutResetMixin, unittest.TestCase):
    def test_rollback_from_step_two(self):
        for _ in range(10):
            monitor.record_success()
        advance()  # step 0 → 1 (target: 3)
        result = rollback()
        self.assertEqual(result, 1)
        self.assertFalse(is_rollback_active())

    def test_rollback_at_step_zero_stays(self):
        result = rollback()
        self.assertEqual(result, 1)
        self.assertEqual(get_current_step(), 1)

    def test_rollback_clears_reasons(self):
        for _ in range(4):
            monitor.record_restart()
        for _ in range(10):
            monitor.record_success()
        evaluate()
        self.assertTrue(len(get_rollback_reasons()) > 0)
        rollback()
        self.assertEqual(get_rollback_reasons(), [])


class TestGetStatus(RolloutResetMixin, unittest.TestCase):
    def test_initial_status(self):
        status = get_status()
        self.assertEqual(status["step_index"], 0)
        self.assertEqual(status["target_workers"], 1)
        self.assertEqual(status["active_workers"], 0)
        self.assertFalse(status["rollback_active"])
        self.assertEqual(status["rollback_reasons"], [])
        self.assertFalse(status["is_final_step"])

    def test_status_after_advance(self):
        for _ in range(10):
            monitor.record_success()
        advance()
        set_active_workers(3)
        status = get_status()
        self.assertEqual(status["step_index"], 1)
        self.assertEqual(status["target_workers"], 3)
        self.assertEqual(status["active_workers"], 3)

    def test_final_step_flag(self):
        for _ in range(10):
            monitor.record_success()
        for _ in range(len(ROLLOUT_STEPS) - 1):
            advance()
        status = get_status()
        self.assertTrue(status["is_final_step"])


class TestReset(RolloutResetMixin, unittest.TestCase):
    def test_reset_clears_state(self):
        for _ in range(10):
            monitor.record_success()
        advance()
        set_active_workers(5)
        reset()
        self.assertEqual(get_current_step(), 1)
        self.assertEqual(get_active_workers(), 0)
        self.assertFalse(is_rollback_active())


if __name__ == "__main__":
    unittest.main()
