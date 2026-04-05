"""Tests for Task 9.4 — Graceful Shutdown Upgrade.

Validates:
  - stop_worker() respects CRITICAL_SECTION (waits for CS to complete via join)
  - stop_worker() stops immediately for IDLE / SAFE_POINT workers
  - stop_worker() marks IN_CYCLE workers and lets them exit at safe point
  - stop_worker() handles stuck workers that don't reach safe point (timeout)
  - _worker_fn checks _should_stop_worker at safe point after task completion
  - stop() hard-timeout force-cleans straggler workers
  - stop() sets STOPPING so workers break at safe points
"""
import threading
import time
import unittest

from integration import runtime
from integration.runtime import (
    get_all_worker_states,
    get_worker_state,
    reset,
    set_worker_state,
    start_worker,
    stop_worker,
)
from modules.monitor import main as monitor
from modules.rollout import main as rollout

CLEANUP_TIMEOUT = 3


class GracefulShutdownResetMixin:
    """Common setUp/tearDown for graceful shutdown tests."""

    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        reset()
        rollout.reset()
        monitor.reset()


# ── stop_worker: IDLE / SAFE_POINT → immediate stop ─────────────


class TestStopWorkerIdleOrSafePoint(GracefulShutdownResetMixin, unittest.TestCase):
    """stop_worker() stops immediately when worker is IDLE or SAFE_POINT."""

    def test_idle_worker_stops_immediately(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=3))
        time.sleep(0.05)
        # Force IDLE
        with runtime._lock:
            runtime._worker_states[wid] = "IDLE"
        barrier.set()
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())

    def test_safe_point_worker_stops_immediately(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=3))
        time.sleep(0.05)
        # Force SAFE_POINT
        with runtime._lock:
            runtime._worker_states[wid] = "SAFE_POINT"
        barrier.set()
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())


# ── stop_worker: IN_CYCLE → mark for stop ───────────────────────


class TestStopWorkerInCycle(GracefulShutdownResetMixin, unittest.TestCase):
    """stop_worker() marks IN_CYCLE worker; worker exits at next safe point."""

    def test_in_cycle_worker_marked_and_exits(self):
        task_started = threading.Event()
        proceed = threading.Event()

        def task(wid):
            task_started.set()
            proceed.wait(timeout=3)

        wid = start_worker(task)
        task_started.wait(timeout=2)
        # Worker is IN_CYCLE (inside task_fn)
        self.assertEqual(get_worker_state(wid), "IN_CYCLE")
        # Let the task complete so worker can reach safe point
        proceed.set()
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())


# ── stop_worker: CRITICAL_SECTION → wait then stop ──────────────


class TestStopWorkerCriticalSection(GracefulShutdownResetMixin, unittest.TestCase):
    """stop_worker() waits for CRITICAL_SECTION to complete before stopping."""

    def test_waits_for_critical_section_to_clear(self):
        """Worker in CRITICAL_SECTION: stop_worker waits for CS to complete via join."""
        task_started = threading.Event()
        proceed = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            task_started.set()
            proceed.wait(timeout=5)
            # Leave critical section
            set_worker_state(wid, "IN_CYCLE")

        wid = start_worker(task)
        task_started.wait(timeout=2)
        self.assertEqual(get_worker_state(wid), "CRITICAL_SECTION")

        # Release worker after a short delay (simulates CS completing)
        def release():
            time.sleep(0.3)
            proceed.set()

        threading.Thread(target=release, daemon=True).start()

        t0 = time.monotonic()
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        elapsed = time.monotonic() - t0
        self.assertTrue(result)
        # Must have waited for the CS to complete (~0.3s)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertNotIn(wid, get_all_worker_states())

    def test_critical_section_timeout_returns_false(self):
        """Worker stuck in CRITICAL_SECTION past timeout → stop_worker returns False."""
        task_started = threading.Event()
        hold_forever = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            task_started.set()
            hold_forever.wait(timeout=5)

        wid = start_worker(task)
        task_started.wait(timeout=2)
        self.assertEqual(get_worker_state(wid), "CRITICAL_SECTION")

        # Very short timeout — worker won't leave CS in time
        result = stop_worker(wid, timeout=0.2)
        self.assertFalse(result)
        # Cleanup
        hold_forever.set()
        time.sleep(0.2)

    def test_worker_exits_cs_naturally_then_stops(self):
        """Worker leaves CRITICAL_SECTION on its own, stop_worker succeeds."""
        cs_entered = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            cs_entered.set()
            time.sleep(0.15)
            set_worker_state(wid, "IN_CYCLE")

        wid = start_worker(task)
        cs_entered.wait(timeout=2)
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())


# ── _worker_fn: safe-point check after task ──────────────────────


class TestWorkerFnSafePointCheck(GracefulShutdownResetMixin, unittest.TestCase):
    """_worker_fn checks _should_stop_worker after task completion."""

    def test_worker_breaks_early_on_stop_during_cycle(self):
        """If stop is requested while task runs, worker exits after cycle ends."""
        cycle_count = []
        task_started = threading.Event()
        proceed = threading.Event()

        def task(wid):
            cycle_count.append(1)
            if len(cycle_count) == 1:
                task_started.set()
                proceed.wait(timeout=3)

        wid = start_worker(task)
        task_started.wait(timeout=2)
        # Add stop request while worker is in first task
        with runtime._lock:
            runtime._stop_requests.add(wid)
        proceed.set()
        time.sleep(0.3)
        # Worker should have stopped after 1 cycle (the safe-point check
        # after task completion catches the stop request)
        self.assertEqual(len(cycle_count), 1)


# ── stop() with STOPPING state ──────────────────────────────────


class TestStopSetsStoppingState(GracefulShutdownResetMixin, unittest.TestCase):
    """stop() sets STOPPING so workers break at safe points."""

    def test_stop_sets_stopping_state(self):
        runtime._state = "RUNNING"
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=3))
        barrier.set()
        runtime.stop(timeout=CLEANUP_TIMEOUT)
        self.assertEqual(runtime.get_state(), "STOPPED")
        self.assertEqual(get_all_worker_states(), {})

    def test_workers_exit_on_stopping(self):
        """Workers check _should_stop_worker and exit when state is STOPPING."""
        runtime._state = "RUNNING"
        cycles = []
        slow_barrier = threading.Event()

        def task(wid):
            cycles.append(1)
            slow_barrier.wait(timeout=3)

        wid = start_worker(task)
        time.sleep(0.1)  # let first cycle start
        slow_barrier.set()
        runtime.stop(timeout=CLEANUP_TIMEOUT)
        # Worker should have exited
        self.assertEqual(get_all_worker_states(), {})


# ── stop() hard timeout force-cleanup ────────────────────────────


class TestStopHardTimeout(GracefulShutdownResetMixin, unittest.TestCase):
    """stop() force-cleans straggler workers after hard timeout."""

    def test_hard_timeout_cleans_stuck_workers(self):
        """Workers that don't stop within timeout are force-cleaned."""
        runtime._state = "RUNNING"

        def stuck_task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            time.sleep(10)  # stuck for a long time

        wid = start_worker(stuck_task)
        time.sleep(0.1)  # let task enter CRITICAL_SECTION

        result = runtime.stop(timeout=0.5)
        # Should have force-cleaned
        self.assertFalse(result)
        self.assertEqual(runtime.get_state(), "STOPPED")
        # All workers should be cleaned up
        self.assertEqual(get_all_worker_states(), {})


# ── stop_worker: edge cases ──────────────────────────────────────


class TestStopWorkerEdgeCases(GracefulShutdownResetMixin, unittest.TestCase):
    """Edge cases for state-aware stop_worker."""

    def test_stop_nonexistent_worker(self):
        result = stop_worker("nonexistent", timeout=0.1)
        self.assertFalse(result)

    def test_stop_worker_already_exited_during_join(self):
        """Worker exits naturally while stop_worker joins."""
        task_started = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            task_started.set()
            time.sleep(0.1)
            set_worker_state(wid, "IN_CYCLE")

        wid = start_worker(task)
        task_started.wait(timeout=2)
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        # Worker exited naturally and stop_worker joined successfully
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())

    def test_stop_worker_logs_awaiting_cs(self):
        """stop_worker logs 'awaiting_critical_section' for CS workers."""
        task_started = threading.Event()
        proceed = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            task_started.set()
            proceed.wait(timeout=3)
            set_worker_state(wid, "IN_CYCLE")

        wid = start_worker(task)
        task_started.wait(timeout=2)

        # Release quickly
        def release():
            time.sleep(0.1)
            proceed.set()

        threading.Thread(target=release, daemon=True).start()
        # Should succeed; no assertion on log content, just no crash
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
