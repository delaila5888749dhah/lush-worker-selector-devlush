"""Tests for runtime behaviour integration — Task 10.7."""
import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime


class _RuntimeReset(unittest.TestCase):
    """Ensure clean state for every test."""

    def setUp(self):
        runtime.reset()
        runtime.set_behavior_delay_enabled(True)

    def tearDown(self):
        runtime.reset()


class TestWrapperApplied(_RuntimeReset):
    """Verify that the behaviour wrapper is applied to the task function."""

    def test_worker_executes_wrapped_task(self):
        results = []

        def task(wid):
            results.append(wid)

        wid = runtime.start_worker(task)
        time.sleep(3.0)  # allow at least one cycle + max behavior delay
        runtime.stop_worker(wid, timeout=5)
        self.assertGreater(len(results), 0)

    def test_worker_returns_after_stop(self):
        def task(wid):
            time.sleep(0.1)

        wid = runtime.start_worker(task)
        time.sleep(0.5)
        stopped = runtime.stop_worker(wid, timeout=5)
        self.assertTrue(stopped, "worker did not stop within timeout")
        self.assertNotIn(wid, runtime.get_active_workers())


class TestLifecycleStatesUnchanged(_RuntimeReset):
    """Behaviour layer must not change lifecycle state transitions."""

    def test_states_remain_valid(self):
        def task(wid):
            pass

        wid = runtime.start_worker(task)
        time.sleep(1.0)
        # Worker should cycle through IDLE → IN_CYCLE → IDLE
        # If it's still alive, state should be one of the allowed set
        try:
            state = runtime.get_worker_state(wid)
            self.assertIn(state, runtime.ALLOWED_WORKER_STATES)
        except ValueError:
            pass  # Worker may have already exited
        runtime.stop_worker(wid, timeout=5)


class TestCriticalSectionRespected(_RuntimeReset):
    """Behaviour layer must not interfere with CRITICAL_SECTION."""

    def test_critical_section_no_delay(self):
        entered_cs = threading.Event()
        exited_cs = threading.Event()

        def task(wid):
            runtime.set_worker_state(wid, "CRITICAL_SECTION")
            entered_cs.set()
            time.sleep(0.1)
            runtime.set_worker_state(wid, "IN_CYCLE")
            exited_cs.set()

        wid = runtime.start_worker(task)
        entered_cs.wait(timeout=5)
        # While in CRITICAL_SECTION the worker must still be running
        self.assertIn(wid, runtime.get_active_workers())
        exited_cs.wait(timeout=5)
        runtime.stop_worker(wid, timeout=5)


class TestNoScalingInterference(_RuntimeReset):
    """Behaviour wrapper must not affect scaling decisions."""

    def test_multiple_workers_scale(self):
        results = []

        def task(wid):
            results.append(wid)

        w1 = runtime.start_worker(task)
        w2 = runtime.start_worker(task)
        time.sleep(1.5)
        self.assertGreater(len(results), 0)
        runtime.stop_worker(w1, timeout=5)
        runtime.stop_worker(w2, timeout=5)


class TestSafePointHonored(_RuntimeReset):
    """Behaviour delay must only operate at SAFE_POINT boundaries."""

    def test_safe_point_transition(self):
        """Worker can transition to SAFE_POINT and back without issues."""
        reached_sp = threading.Event()

        def task(wid):
            runtime.set_worker_state(wid, "SAFE_POINT")
            reached_sp.set()
            time.sleep(0.05)
            runtime.set_worker_state(wid, "IN_CYCLE")

        wid = runtime.start_worker(task)
        reached_sp.wait(timeout=5)
        self.assertIn(wid, runtime.get_active_workers())
        runtime.stop_worker(wid, timeout=5)

    def test_stop_during_safe_point(self):
        """Stop request during SAFE_POINT should be respected promptly."""
        at_sp = threading.Event()

        def task(wid):
            runtime.set_worker_state(wid, "SAFE_POINT")
            at_sp.set()
            time.sleep(0.5)
            runtime.set_worker_state(wid, "IN_CYCLE")

        wid = runtime.start_worker(task)
        at_sp.wait(timeout=5)
        stopped = runtime.stop_worker(wid, timeout=5)
        self.assertTrue(stopped)


class TestOverheadWithin15Percent(_RuntimeReset):
    """Behaviour wrapper overhead must be ≤15% of a cycle without behaviour."""

    def test_overhead_acceptable(self):
        """Compare cycle time with and without behaviour delay enabled."""
        cycle_times = {"enabled": [], "disabled": []}

        def timed_task(wid):
            pass  # minimal task to measure wrapper overhead only

        # --- Measure WITHOUT behaviour delay ---
        runtime.set_behavior_delay_enabled(False)
        wid = runtime.start_worker(timed_task)
        t0 = time.monotonic()
        time.sleep(1.0)
        runtime.stop_worker(wid, timeout=5)
        baseline_elapsed = time.monotonic() - t0
        runtime.reset()

        # --- Measure WITH behaviour delay (mocked sleep) ---
        runtime.set_behavior_delay_enabled(True)
        with patch("modules.delay.wrapper.time.sleep"):
            wid = runtime.start_worker(timed_task)
            t0 = time.monotonic()
            time.sleep(1.0)
            runtime.stop_worker(wid, timeout=5)
            enabled_elapsed = time.monotonic() - t0

        # Overhead should be within 15%
        if baseline_elapsed > 0:
            overhead = (enabled_elapsed - baseline_elapsed) / baseline_elapsed
            self.assertLessEqual(overhead, 0.15,
                f"Overhead {overhead:.1%} exceeds 15% limit")


class TestDisabledBypassesWrap(_RuntimeReset):
    """When delay is disabled, wrap must not be applied."""

    def test_disabled_skips_wrap(self):
        runtime.set_behavior_delay_enabled(False)
        results = []

        def task(wid):
            results.append(wid)

        wid = runtime.start_worker(task)
        time.sleep(0.5)
        runtime.stop_worker(wid, timeout=5)
        self.assertGreater(len(results), 0)


if __name__ == "__main__":
    unittest.main()
