"""Tests for runtime behaviour integration — Task 10.7."""
import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
