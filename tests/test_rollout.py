"""Unit tests for :mod:`modules.rollout.main`."""

import os
import threading
import unittest

from modules.rollout import main as rollout_module
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

TEST_TIMEOUT_SECONDS = 1


def _clear_rollout_runtime_overrides():
    """Clear rollout runtime config overrides under the module lock.

    Tests never touch ``_runtime_*`` module-level state directly; all
    writes go through ``_lock`` to match the concurrency contract of
    ``modules/rollout/main.py`` (threading.Lock on shared state).
    """
    with rollout_module._lock:  # pylint: disable=protected-access
        rollout_module._runtime_max_worker_count = None  # pylint: disable=protected-access
        rollout_module._runtime_scale_steps = None  # pylint: disable=protected-access


class RolloutResetMixin:
    def setUp(self):
        _clear_rollout_runtime_overrides()
        reset()

    def tearDown(self):
        _clear_rollout_runtime_overrides()
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

        # After the TOCTOU fix, check_fn is called under the lock (serialized),
        # so we cannot use a threading.Barrier here.  Instead we verify the
        # invariant directly: concurrent callers must never push the step
        # index past the maximum.
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)

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

    def test_scale_steps_start_at_one(self):
        self.assertEqual(SCALE_STEPS[0], 1)

    def test_scale_steps_end_at_max_worker_count(self):
        # The final step must equal the configured cap — whether the cap came
        # from the MAX_WORKER_COUNT env var or the default (10).
        expected_cap = rollout_module._read_max_worker_count()  # pylint: disable=protected-access
        self.assertEqual(SCALE_STEPS[-1], expected_cap)

    def test_scale_steps_bounded_by_cap(self):
        expected_cap = rollout_module._read_max_worker_count()  # pylint: disable=protected-access
        for step in SCALE_STEPS:
            self.assertGreaterEqual(step, 1)
            self.assertLessEqual(step, expected_cap)


class TestConfigureMaxWorkers(RolloutResetMixin, unittest.TestCase):
    """Property-based tests for :func:`configure_max_workers`."""

    def _assert_valid_steps(self, steps, cap):
        self.assertGreater(len(steps), 0)
        self.assertEqual(steps[0], 1)
        self.assertEqual(steps[-1], cap)
        for prev, nxt in zip(steps, steps[1:]):
            self.assertLess(prev, nxt)
        for step in steps:
            self.assertLessEqual(step, cap)
            self.assertGreaterEqual(step, 1)

    def test_configure_rebuilds_scale_steps_for_each_cap(self):
        for cap in (1, 2, 3, 4, 5, 7, 10, 15, 25, 50):
            with self.subTest(cap=cap):
                rollout_module.configure_max_workers(cap)
                self._assert_valid_steps(rollout_module.SCALE_STEPS, cap)
                # reset() side-effect must hold for every cap, not just the
                # dedicated reset test below.
                self.assertEqual(get_current_step_index(), 0)
                self.assertEqual(get_current_workers(), 1)

    def test_configure_resets_state(self):
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()
        force_rollback("test")
        rollout_module.configure_max_workers(5)
        self.assertEqual(get_current_step_index(), 0)
        self.assertEqual(get_current_workers(), 1)
        self.assertEqual(get_rollback_history(), [])

    def test_configure_max_workers_one_makes_single_step(self):
        rollout_module.configure_max_workers(1)
        self.assertEqual(rollout_module.SCALE_STEPS, (1,))
        self.assertFalse(can_scale_up())
        workers, action, _ = try_scale_up()
        self.assertEqual(action, "at_max")
        self.assertEqual(workers, 1)

    def test_configure_rejects_out_of_range(self):
        for bad in (0, -1, 501, 1000):
            with self.subTest(n=bad):
                with self.assertRaises(ValueError):
                    rollout_module.configure_max_workers(bad)

    def test_configure_rejects_non_int(self):
        for bad in (1.0, "5", None, True):
            with self.subTest(n=bad):
                with self.assertRaises(TypeError):
                    rollout_module.configure_max_workers(bad)

    def test_configured_cap_persists_across_reset(self):
        """BLOCKER regression: reset() must rebuild from the runtime
        override, not fall back to env/default."""
        rollout_module.configure_max_workers(25)
        self.assertEqual(rollout_module.SCALE_STEPS[-1], 25)
        reset()
        self.assertEqual(rollout_module.SCALE_STEPS[-1], 25)
        self.assertEqual(rollout_module.SCALE_STEPS[0], 1)

    def test_configured_cap_persists_when_env_changes(self):
        """Runtime override wins over MAX_WORKER_COUNT env var after reset()."""
        rollout_module.configure_max_workers(15)
        os.environ["MAX_WORKER_COUNT"] = "4"
        try:
            reset()
            self.assertEqual(rollout_module.SCALE_STEPS[-1], 15)
        finally:
            os.environ.pop("MAX_WORKER_COUNT", None)


class TestSetScaleSteps(RolloutResetMixin, unittest.TestCase):
    def test_installs_custom_steps(self):
        rollout_module.set_scale_steps((1, 2, 4, 8))
        self.assertEqual(rollout_module.SCALE_STEPS, (1, 2, 4, 8))
        self.assertEqual(get_current_workers(), 1)
        self.assertEqual(get_current_step_index(), 0)

    def test_single_step_allowed(self):
        rollout_module.set_scale_steps((1,))
        self.assertEqual(rollout_module.SCALE_STEPS, (1,))
        self.assertFalse(can_scale_up())

    def test_rejects_non_ascending(self):
        with self.assertRaises(ValueError):
            rollout_module.set_scale_steps((1, 3, 2))

    def test_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            rollout_module.set_scale_steps((1, 3, 3, 5))

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            rollout_module.set_scale_steps(())

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            rollout_module.set_scale_steps((0, 2, 5))

    def test_rejects_exceeds_cap(self):
        with self.assertRaises(ValueError):
            rollout_module.set_scale_steps((1, 10, 1000))

    def test_rejects_non_int_element(self):
        with self.assertRaises(TypeError):
            rollout_module.set_scale_steps((1, 2.5, 4))

    def test_rejects_not_starting_at_one(self):
        """MAJOR regression: rollout invariant requires steps[0] == 1."""
        for bad in ((2, 4), (3, 5, 10), (5,)):
            with self.subTest(steps=bad):
                with self.assertRaises(ValueError):
                    rollout_module.set_scale_steps(bad)

    def test_custom_steps_persist_across_reset(self):
        """Custom steps installed via set_scale_steps() must survive reset()."""
        rollout_module.set_scale_steps((1, 2, 4, 8))
        reset()
        self.assertEqual(rollout_module.SCALE_STEPS, (1, 2, 4, 8))


class TestResetRereadsEnv(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MAX_WORKER_COUNT", None)
        _clear_rollout_runtime_overrides()
        reset()

    def test_reset_rebuilds_scale_steps_from_env(self):
        os.environ["MAX_WORKER_COUNT"] = "4"
        reset()
        self.assertEqual(rollout_module.SCALE_STEPS[-1], 4)
        self.assertEqual(rollout_module.SCALE_STEPS[0], 1)
        os.environ["MAX_WORKER_COUNT"] = "15"
        reset()
        self.assertEqual(rollout_module.SCALE_STEPS[-1], 15)


class TestTOCTOURacePrevention(RolloutResetMixin, unittest.TestCase):
    """Verify that try_scale_up() holds the lock during health check + step increment.

    Before the fix, the lock was released between health check and step
    increment, allowing a concurrent caller to also scale up (TOCTOU race).
    After the fix, check_fn() is called under the lock.
    """

    def test_concurrent_scale_up_produces_exactly_expected_steps(self):
        """Multiple threads calling try_scale_up() should never produce more
        'scaled_up' actions than there are available scaling steps."""
        scaled_up_count = []
        errors = []
        thread_count = 10

        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)

        def worker():
            try:
                count, action, reasons = try_scale_up()
                scaled_up_count.append(action)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        n_scaled = sum(1 for a in scaled_up_count if a == "scaled_up")
        n_at_max = sum(1 for a in scaled_up_count if a == "at_max")
        # Can only scale up (len(SCALE_STEPS) - 1) times total
        self.assertLessEqual(n_scaled, len(SCALE_STEPS) - 1)
        self.assertEqual(n_scaled + n_at_max, thread_count)


class TestRollbackAtomicity(RolloutResetMixin, unittest.TestCase):
    """Verify rollback idempotency and try_scale_up() save-failure recovery."""

    def test_rollback_is_idempotent_within_scale_window(self):
        """Sequential force_rollback() calls within the same scale-up window
        must only decrement once (second call is a no-op)."""
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # step 0 → 1
        try_scale_up()  # step 1 → 2
        self.assertEqual(get_current_step_index(), 2)

        force_rollback("first")   # step 2 → 1 (applied)
        force_rollback("second")  # skipped – same window
        self.assertEqual(get_current_step_index(), 1)

    def test_rollback_window_resets_after_scale_up(self):
        """After a successful scale-up, a new window opens so force_rollback()
        can decrement again."""
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # step 0 → 1
        force_rollback("first")  # step 1 → 0
        # scale back up to open a fresh window
        try_scale_up()           # step 0 → 1
        force_rollback("second") # step 1 → 0 (allowed, new window)
        self.assertEqual(get_current_step_index(), 0)

    def test_concurrent_rollback_does_not_double_decrement(self):
        """Two near-simultaneous force_rollback() calls must only decrement
        once; the second caller is idempotent."""
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # step 0 → 1
        try_scale_up()  # step 1 → 2 (5 workers)
        self.assertEqual(get_current_step_index(), 2)

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def do_rollback():
            """Run force_rollback after the barrier and capture any error."""
            barrier.wait()
            try:
                results.append(force_rollback("concurrent-event"))
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=do_rollback) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        # Only one decrement must have happened: step 2 → 1, NOT 2 → 1 → 0.
        self.assertEqual(get_current_step_index(), 1)

    def test_scale_up_reverts_index_on_save_failure(self):
        """If save_fn() raises, _current_step_index is restored to its
        pre-increment value."""
        def failing_save():
            """Simulate a persistence failure."""
            raise RuntimeError("persistence unavailable")

        configure(check_rollback_fn=lambda: [], save_baseline_fn=failing_save)

        with self.assertRaises(RuntimeError):
            try_scale_up()

        self.assertEqual(get_current_step_index(), 0)
        self.assertEqual(get_current_workers(), SCALE_STEPS[0])

    def test_scale_up_save_failure_does_not_block_rollback_guard(self):
        """When save_fn() fails and the index is reverted, the rollback guard
        is also restored so force_rollback() can still fire."""
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # step 0 → 1
        force_rollback("first")  # step 1 → 0, guard = True

        # Now try a scale-up that fails its save; guard should be restored
        # to True (we're back in the old window).
        def failing_save():
            """Simulate a persistence failure."""
            raise RuntimeError("save error")

        configure(check_rollback_fn=lambda: [], save_baseline_fn=failing_save)
        with self.assertRaises(RuntimeError):
            try_scale_up()

        # Index reverted; we're still at 0
        self.assertEqual(get_current_step_index(), 0)
        # A second force_rollback() call should also be idempotent (guard restored)
        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        force_rollback("second")  # guard still True → no-op
        self.assertEqual(get_current_step_index(), 0)

    def test_failed_scale_up_restores_guard_after_concurrent_rollback(self):
        """A rollback during a failing save must not consume the restored
        window's rollback guard."""
        save_started = threading.Event()
        allow_failure = threading.Event()
        errors = []

        def blocking_failing_save():
            """Block until the test triggers a save failure."""
            save_started.set()
            if not allow_failure.wait(timeout=TEST_TIMEOUT_SECONDS):
                raise RuntimeError("timed out waiting to trigger save failure")
            raise RuntimeError("save error")

        configure(check_rollback_fn=lambda: [], save_baseline_fn=lambda: None)
        try_scale_up()  # step 0 → 1
        self.assertEqual(get_current_step_index(), 1)

        configure(check_rollback_fn=lambda: [], save_baseline_fn=blocking_failing_save)

        def scale_worker():
            """Run try_scale_up() and capture the expected save failure."""
            try:
                try_scale_up()
            except RuntimeError as exc:
                errors.append(exc)

        thread = threading.Thread(target=scale_worker)
        thread.start()
        self.assertTrue(save_started.wait(timeout=TEST_TIMEOUT_SECONDS))

        # Roll back the tentative step 1 → 2 scale-up while save_fn() is pending.
        force_rollback("concurrent-rollback")
        self.assertEqual(get_current_step_index(), 1)

        allow_failure.set()
        thread.join()

        self.assertEqual(len(errors), 1)
        self.assertEqual(str(errors[0]), "save error")
        self.assertEqual(get_current_step_index(), 1)

        # The original step-1 window should still allow one real rollback.
        force_rollback("post-failure-rollback")
        self.assertEqual(get_current_step_index(), 0)

    def test_concurrent_scale_up_index_stays_consistent(self):
        """Concurrent try_scale_up() calls must not corrupt the index:
        the final index equals the number of successful save_fn() calls."""
        save_lock = threading.Lock()
        save_count = [0]

        def counting_save():
            """Increment save_count under a lock."""
            with save_lock:
                save_count[0] += 1

        configure(check_rollback_fn=lambda: [], save_baseline_fn=counting_save)

        errors = []
        thread_count = 5  # more threads than steps to exercise at_max path

        def scale_worker():
            """Call try_scale_up() and capture any unexpected exception."""
            try:
                try_scale_up()
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        threads = [threading.Thread(target=scale_worker) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        idx = get_current_step_index()
        self.assertGreaterEqual(idx, 0)
        self.assertLess(idx, len(SCALE_STEPS))
        # Every increment must be matched by exactly one successful save.
        self.assertEqual(idx, save_count[0])


class _MaxWorkerCountEnvTestCase(unittest.TestCase):
    """Shared MAX_WORKER_COUNT env setup/teardown helpers."""

    def setUp(self):
        self._saved_env = os.environ.get("MAX_WORKER_COUNT")
        reset()

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("MAX_WORKER_COUNT", None)
        else:
            os.environ["MAX_WORKER_COUNT"] = self._saved_env
        reset()

    @staticmethod
    def _reload_steps(value):
        """Set (or clear) MAX_WORKER_COUNT, rebuild SCALE_STEPS, and return it."""
        if value is None:
            os.environ.pop("MAX_WORKER_COUNT", None)
        else:
            os.environ["MAX_WORKER_COUNT"] = value
        reset()
        return rollout_module.SCALE_STEPS


class TestMaxWorkerCountEnvParsing(_MaxWorkerCountEnvTestCase):
    """Verify parsing and exact SCALE_STEPS generation for MAX_WORKER_COUNT."""

    def test_default_behavior_unchanged_when_env_unset(self):
        """Unset env → default (1, 3, 5, 10)."""
        steps = self._reload_steps(None)
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_default_behavior_unchanged_when_env_empty(self):
        """Empty env → default (1, 3, 5, 10)."""
        steps = self._reload_steps("")
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_env_equal_to_default_keeps_default(self):
        """MAX_WORKER_COUNT=10 → (1, 3, 5, 10)."""
        steps = self._reload_steps("10")
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_env_invalid_falls_back_to_default(self):
        """Non-numeric env is rejected and falls back to the default cap."""
        steps = self._reload_steps("not-a-number")
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_env_zero_falls_back_to_default(self):
        """MAX_WORKER_COUNT=0 is invalid and falls back to the default cap."""
        steps = self._reload_steps("0")
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_env_negative_falls_back_to_default(self):
        """MAX_WORKER_COUNT=-5 is invalid and falls back to the default cap."""
        steps = self._reload_steps("-5")
        self.assertEqual(steps, (1, 3, 5, 10))

    def test_env_one_caps_at_one(self):
        """MAX_WORKER_COUNT=1 → (1,): pool cannot scale up at all."""
        steps = self._reload_steps("1")
        self.assertEqual(steps, (1,))
        self.assertFalse(rollout_module.can_scale_up())

    def test_env_two_caps_at_two(self):
        """MAX_WORKER_COUNT=2 → (1, 2): exact operator-configured cap."""
        steps = self._reload_steps("2")
        self.assertEqual(steps, (1, 2))

    def test_env_four_caps_at_four(self):
        """MAX_WORKER_COUNT=4 → (1, 3, 4): progressive but never exceeds 4."""
        steps = self._reload_steps("4")
        self.assertEqual(steps, (1, 3, 4))

    def test_env_seven_caps_at_seven(self):
        """MAX_WORKER_COUNT=7 → (1, 3, 5, 7): progressive but never exceeds 7."""
        steps = self._reload_steps("7")
        self.assertEqual(steps, (1, 3, 5, 7))

    def test_env_twelve_extends_past_default(self):
        """MAX_WORKER_COUNT=12 → (1, 3, 5, 10, 12): keeps default prefix, adds cap."""
        steps = self._reload_steps("12")
        self.assertEqual(steps, (1, 3, 5, 10, 12))

    def test_env_50_extends_steps_to_50(self):
        """MAX_WORKER_COUNT=50 → (1, 3, 5, 10, 20, 50)."""
        steps = self._reload_steps("50")
        self.assertEqual(steps, (1, 3, 5, 10, 20, 50))
        self.assertEqual(rollout_module.get_current_workers(), 1)
        self.assertTrue(rollout_module.can_scale_up())

    def test_env_100_extends_steps_to_100(self):
        """MAX_WORKER_COUNT=100 → (1, 3, 5, 10, 20, 50, 100)."""
        steps = self._reload_steps("100")
        self.assertEqual(steps, (1, 3, 5, 10, 20, 50, 100))

    def test_env_500_extends_steps_across_decades(self):
        """MAX_WORKER_COUNT=500 progresses through the decade series."""
        steps = self._reload_steps("500")
        self.assertEqual(steps, (1, 3, 5, 10, 20, 50, 100, 200, 500))

    def test_env_20_extends_minimally(self):
        """MAX_WORKER_COUNT=20 → (1, 3, 5, 10, 20)."""
        steps = self._reload_steps("20")
        self.assertEqual(steps, (1, 3, 5, 10, 20))

    def test_env_non_canonical_cap_is_appended(self):
        """A cap that isn't on the canonical progression is still the final step."""
        steps = self._reload_steps("30")
        self.assertEqual(steps, (1, 3, 5, 10, 20, 30))

    def test_steps_strictly_ascending_for_large_cap(self):
        """Regardless of cap, SCALE_STEPS must be strictly ascending."""
        steps = self._reload_steps("1000")
        for i in range(len(steps) - 1):
            self.assertLess(steps[i], steps[i + 1])
        self.assertEqual(steps[-1], 1000)


class TestMaxWorkerCountEnvRollout(_MaxWorkerCountEnvTestCase):
    """Verify rollout behavior respects the configured MAX_WORKER_COUNT cap."""

    def test_rollout_never_exceeds_configured_cap(self):
        """Rollout progression must never scale above MAX_WORKER_COUNT."""
        for cap_str, cap_int in (("2", 2), ("4", 4), ("7", 7), ("50", 50)):
            with self.subTest(cap=cap_str):
                self._reload_steps(cap_str)
                rollout_module.configure(
                    check_rollback_fn=lambda: [],
                    save_baseline_fn=lambda: None,
                )
                last_count = rollout_module.get_current_workers()
                while rollout_module.can_scale_up():
                    last_count, action, _ = rollout_module.try_scale_up()
                    self.assertEqual(action, "scaled_up")
                    self.assertLessEqual(last_count, cap_int)
                self.assertEqual(last_count, cap_int)

    def test_can_scale_all_the_way_up_to_50(self):
        """End-to-end: with MAX_WORKER_COUNT=50, rollout reaches exactly 50."""
        self._reload_steps("50")
        rollout_module.configure(
            check_rollback_fn=lambda: [],
            save_baseline_fn=lambda: None,
        )
        last_count = None
        for _ in range(len(rollout_module.SCALE_STEPS) - 1):
            last_count, action, _ = rollout_module.try_scale_up()
            self.assertEqual(action, "scaled_up")
        self.assertEqual(last_count, 50)
        self.assertFalse(rollout_module.can_scale_up())


if __name__ == "__main__":
    unittest.main()
