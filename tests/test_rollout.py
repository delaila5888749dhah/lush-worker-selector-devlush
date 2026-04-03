import threading
import unittest

from modules.rollout.main import (
    SCALE_STEPS,
    can_scale_up,
    check_health,
    configure,
    force_rollback,
    get_current_step_index,
    get_current_workers,
    get_rollback_history,
    get_status,
    reset,
    try_scale_up,
)


class RolloutResetMixin:
    def setUp(self):
        reset()

    def tearDown(self):
        reset()


class TestInitialState(RolloutResetMixin, unittest.TestCase):
    def test_starts_at_one_worker(self):
        self.assertEqual(get_current_workers(), 1)

    def test_starts_at_step_zero(self):
        self.assertEqual(get_current_step_index(), 0)

    def test_can_scale_up_initially(self):
        self.assertTrue(can_scale_up())

    def test_empty_rollback_history(self):
        self.assertEqual(get_rollback_history(), [])


class TestConfigure(RolloutResetMixin, unittest.TestCase):
    def test_configure_sets_callbacks(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        # Should work without errors; no way to directly inspect callbacks
        count, action, reasons = try_scale_up()
        self.assertEqual(action, "scaled_up")

    def test_configure_with_none(self):
        configure(check_rollback_fn=None, save_baseline_fn=None)
        count, action, reasons = try_scale_up()
        self.assertEqual(action, "scaled_up")


class TestScaleUp(RolloutResetMixin, unittest.TestCase):
    def test_scale_up_steps_in_order(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        expected = list(SCALE_STEPS[1:])
        for expected_count in expected:
            count, action, reasons = try_scale_up()
            self.assertEqual(count, expected_count)
            self.assertEqual(action, "scaled_up")
            self.assertEqual(reasons, [])

    def test_at_max_when_fully_scaled(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        for _ in range(len(SCALE_STEPS) - 1):
            try_scale_up()
        count, action, reasons = try_scale_up()
        self.assertEqual(count, SCALE_STEPS[-1])
        self.assertEqual(action, "at_max")

    def test_cannot_scale_up_at_max(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        for _ in range(len(SCALE_STEPS) - 1):
            try_scale_up()
        self.assertFalse(can_scale_up())

    def test_save_baseline_called_on_scale_up(self):
        calls = []
        configure(
            check_rollback_fn=lambda: [],
            save_baseline_fn=lambda: calls.append(1),
        )
        try_scale_up()
        self.assertEqual(len(calls), 1)


class TestRollback(RolloutResetMixin, unittest.TestCase):
    def test_rollback_on_bad_metrics(self):
        # Scale up first (configure with healthy, then switch to bad)
        reset()
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # 1 → 3
        self.assertEqual(get_current_workers(), 3)

        # Now configure bad metrics
        configure(
            check_rollback_fn=lambda: ["error rate 50.0% exceeds 5%"],
            save_baseline_fn=lambda: None,
        )
        count, action, reasons = try_scale_up()
        self.assertEqual(action, "rollback")
        self.assertEqual(count, 1)
        self.assertTrue(any("error rate" in r for r in reasons))

    def test_rollback_at_step_zero_stays_at_zero(self):
        configure(
            check_rollback_fn=lambda: ["memory usage exceeds limit"],
            save_baseline_fn=lambda: None,
        )
        count, action, reasons = try_scale_up()
        self.assertEqual(action, "rollback")
        self.assertEqual(count, 1)
        self.assertEqual(get_current_step_index(), 0)

    def test_rollback_records_history(self):
        configure(
            check_rollback_fn=lambda: ["worker restarts exceeded"],
            save_baseline_fn=lambda: None,
        )
        try_scale_up()
        history = get_rollback_history()
        self.assertEqual(len(history), 1)
        self.assertIn("worker restarts exceeded", history[0]["reasons"])

    def test_save_baseline_not_called_on_rollback(self):
        calls = []
        configure(
            check_rollback_fn=lambda: ["error rate too high"],
            save_baseline_fn=lambda: calls.append(1),
        )
        try_scale_up()
        self.assertEqual(len(calls), 0)


class TestForceRollback(RolloutResetMixin, unittest.TestCase):
    def test_force_rollback_decrements_step(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # 1 → 3
        count = force_rollback("test reason")
        self.assertEqual(count, 1)
        self.assertEqual(get_current_workers(), 1)

    def test_force_rollback_at_zero_stays_at_zero(self):
        count = force_rollback("at bottom")
        self.assertEqual(count, 1)
        self.assertEqual(get_current_step_index(), 0)

    def test_force_rollback_records_reason(self):
        force_rollback("manual override")
        history = get_rollback_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reasons"], ["manual override"])

    def test_rollback_history_isolation(self):
        force_rollback("manual override")
        history = get_rollback_history()
        history[0]["reasons"].append("mutated")
        history[0]["from_step"] = 99
        fresh_history = get_rollback_history()
        self.assertEqual(fresh_history[0]["reasons"], ["manual override"])
        self.assertEqual(fresh_history[0]["from_step"], 0)


class TestCheckHealth(RolloutResetMixin, unittest.TestCase):
    def test_healthy_returns_empty(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        self.assertEqual(check_health(), [])

    def test_unhealthy_returns_reasons(self):
        configure(
            check_rollback_fn=lambda: ["memory too high"],
            save_baseline_fn=lambda: None,
        )
        reasons = check_health()
        self.assertEqual(reasons, ["memory too high"])

    def test_no_callback_returns_empty(self):
        self.assertEqual(check_health(), [])


class TestGetStatus(RolloutResetMixin, unittest.TestCase):
    def test_initial_status(self):
        status = get_status()
        self.assertEqual(status["current_workers"], 1)
        self.assertEqual(status["step_index"], 0)
        self.assertEqual(status["max_step_index"], len(SCALE_STEPS) - 1)
        self.assertTrue(status["can_scale_up"])
        self.assertEqual(status["rollback_count"], 0)

    def test_status_after_scale_up(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()
        status = get_status()
        self.assertEqual(status["current_workers"], 3)
        self.assertEqual(status["step_index"], 1)


class TestThreadSafety(RolloutResetMixin, unittest.TestCase):
    def test_concurrent_scale_and_rollback(self):
        errors = []
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)

        def scale_worker():
            try:
                for _ in range(20):
                    try_scale_up()
            except Exception as e:
                errors.append(e)

        def rollback_worker():
            try:
                for _ in range(20):
                    force_rollback("concurrent")
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=scale_worker) for _ in range(3)]
            + [threading.Thread(target=rollback_worker) for _ in range(3)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # Step index must be within valid bounds
        idx = get_current_step_index()
        self.assertGreaterEqual(idx, 0)
        self.assertLess(idx, len(SCALE_STEPS))

    def test_concurrent_try_scale_up_does_not_exceed_max_scale_step(self):
        errors = []
        actions = []
        thread_count = 8
        barrier = threading.Barrier(thread_count)

        def check_healthy():
            barrier.wait()
            return []

        configure(check_rollback_fn=check_healthy, save_baseline_fn=lambda: None)

        def scale_worker():
            try:
                actions.append(try_scale_up()[1])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=scale_worker) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertLessEqual(get_current_step_index(), len(SCALE_STEPS) - 1)
        self.assertEqual(get_current_workers(), SCALE_STEPS[-1])
        self.assertTrue(all(action in ("scaled_up", "at_max") for action in actions))


class TestReset(RolloutResetMixin, unittest.TestCase):
    def test_reset_clears_all(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()
        force_rollback("test")
        reset()
        self.assertEqual(get_current_workers(), 1)
        self.assertEqual(get_current_step_index(), 0)
        self.assertEqual(get_rollback_history(), [])
        self.assertTrue(can_scale_up())


class TestScaleSteps(unittest.TestCase):
    def test_scale_steps_are_ascending(self):
        for i in range(len(SCALE_STEPS) - 1):
            self.assertLess(SCALE_STEPS[i], SCALE_STEPS[i + 1])

    def test_scale_steps_match_spec(self):
        self.assertEqual(SCALE_STEPS, (1, 3, 5, 10))


if __name__ == "__main__":
    unittest.main()
