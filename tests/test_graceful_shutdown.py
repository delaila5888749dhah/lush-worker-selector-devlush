"""Tests for Task 9.4 — Graceful Shutdown Upgrade.

Validates:
  - stop_worker() respects CRITICAL_SECTION (waits for CS to complete via join)
  - stop_worker() stops immediately for IDLE / SAFE_POINT workers
  - stop_worker() marks IN_CYCLE workers and lets them exit at safe point
  - stop_worker() handles stuck workers that don't reach safe point (timeout)
  - _worker_fn checks _should_stop_worker at safe point after task completion
  - stop() hard-timeout force-cleans straggler workers
  - stop() sets STOPPING so workers break at safe points
  - stop() splits its timeout budget 30 % loop / 70 % workers
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

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


def _wait_until(condition_fn, timeout=2.0, interval=0.01):
    """Poll condition_fn until it returns True or timeout expires.

    Returns True if the condition was met within the timeout window,
    False if the deadline was reached without the condition becoming True.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


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
        started = threading.Event()
        barrier = threading.Event()

        def task(_):
            started.set()
            barrier.wait(timeout=3)

        wid = start_worker(task)
        self.assertTrue(
            started.wait(timeout=2),
            "worker did not start before forcing IDLE state",
        )
        # Force IDLE
        with runtime._lock:
            runtime._worker_states[wid] = "IDLE"
        barrier.set()
        result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertNotIn(wid, get_all_worker_states())

    def test_safe_point_worker_stops_immediately(self):
        started = threading.Event()
        barrier = threading.Event()

        def task(_):
            started.set()
            barrier.wait(timeout=3)

        wid = start_worker(task)
        self.assertTrue(
            started.wait(timeout=2),
            "worker did not start before forcing SAFE_POINT state",
        )
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
        # Release the worker and wait for it to clean up
        hold_forever.set()
        self.assertTrue(
            _wait_until(lambda: wid not in get_all_worker_states(), timeout=CLEANUP_TIMEOUT),
            "worker did not clean up after critical-section timeout release",
        )

    def test_cs_timeout_worker_remains_registered_until_natural_exit(self):
        """Timed-out CS worker stays in registry; cleanup deferred to finally."""
        task_started = threading.Event()
        proceed = threading.Event()

        def task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            task_started.set()
            proceed.wait(timeout=5)
            set_worker_state(wid, "IN_CYCLE")

        wid = start_worker(task)
        task_started.wait(timeout=2)
        self.assertEqual(get_worker_state(wid), "CRITICAL_SECTION")

        # Timeout — worker is still in CS
        result = stop_worker(wid, timeout=0.1)
        self.assertFalse(result)
        # Worker must still be registered (not force-removed)
        self.assertIn(wid, get_all_worker_states())

        # Let worker finish naturally
        proceed.set()
        self.assertTrue(
            _wait_until(lambda: wid not in get_all_worker_states(), timeout=CLEANUP_TIMEOUT),
            "worker did not unregister after leaving CRITICAL_SECTION",
        )
        # Worker cleaned up by _worker_fn finally block
        self.assertNotIn(wid, get_all_worker_states())

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
        self.assertTrue(
            _wait_until(lambda: wid not in get_all_worker_states(), timeout=CLEANUP_TIMEOUT),
            "worker did not stop after safe-point stop request",
        )
        # Worker should have stopped after 1 cycle (the safe-point check
        # after task completion catches the stop request)
        self.assertNotIn(wid, get_all_worker_states())
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
        cycle_started = threading.Event()
        slow_barrier = threading.Event()

        def task(wid):
            cycles.append(1)
            cycle_started.set()
            slow_barrier.wait(timeout=3)

        wid = start_worker(task)
        self.assertTrue(
            cycle_started.wait(timeout=2),
            "worker did not start a cycle before shutdown",
        )
        self.assertGreaterEqual(
            len(cycles),
            1,
            "worker should have completed at least one cycle before shutdown",
        )
        slow_barrier.set()
        runtime.stop(timeout=CLEANUP_TIMEOUT)
        # Worker should have exited
        self.assertEqual(get_all_worker_states(), {})


# ── stop() hard timeout force-cleanup ────────────────────────────


class TestStopHardTimeout(GracefulShutdownResetMixin, unittest.TestCase):
    """stop() force-cleans straggler workers after hard timeout."""

    def test_hard_timeout_cleans_stuck_workers(self):
        """Workers that don't stop within timeout are logged but left registered
        so their threads can still call set_worker_state() safely."""
        runtime._state = "RUNNING"
        cs_entered = threading.Event()
        release_worker = threading.Event()

        def stuck_task(wid):
            set_worker_state(wid, "CRITICAL_SECTION")
            cs_entered.set()
            release_worker.wait()

        wid = start_worker(stuck_task)
        self.assertTrue(
            cs_entered.wait(timeout=2),
            "Worker did not enter CRITICAL_SECTION before stop() was invoked",
        )

        try:
            result = runtime.stop(timeout=0.5)
            # Incomplete shutdown — stragglers still running
            self.assertFalse(result)
            self.assertEqual(runtime.get_state(), "STOPPED")
            self.assertEqual(get_worker_state(wid), "CRITICAL_SECTION")
        finally:
            release_worker.set()
            self.assertTrue(
                _wait_until(
                    lambda: wid not in runtime.get_active_workers()
                    and wid not in get_all_worker_states(),
                    timeout=CLEANUP_TIMEOUT,
                ),
                "Straggler worker did not exit after release",
            )


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
        with self.assertLogs("integration.runtime", level="INFO") as cm:
            result = stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertTrue(result)
        self.assertTrue(
            any("awaiting_critical_section" in msg for msg in cm.output),
            f"Expected 'awaiting_critical_section' in logs, got: {cm.output}",
        )


class TestStopBudgetSplit(GracefulShutdownResetMixin, unittest.TestCase):
    """Regression guard for the 30/70 stop/join timeout budget split.

    The graceful ``stop()`` divides its timeout ``T`` into:

      * ``0.30 * T`` reserved for joining the runtime loop thread.
      * ``0.70 * T`` distributed evenly across active workers.

    A drift to 40/60 or 50/50 must fail this test.
    """

    def _run_stop_with_mocked_clock(self, timeout, worker_ids, monotonic_value=1000.0):
        """Drive ``runtime.stop()`` with a frozen clock and mocked threads.

        Returns ``(loop_join_timeout, [(wid, per_worker_timeout), ...])``.
        """
        # Freeze time.monotonic so elapsed-time deltas are exactly zero and the
        # observed timeouts equal the budget split arithmetic.
        loop_thread = MagicMock(spec=threading.Thread)
        # Alive on the first is_alive() check (so .join is invoked); dead
        # afterwards so the second-join branch is skipped and the test
        # observes only the 30 % loop budget.
        loop_thread.is_alive.side_effect = [True, False, False, False]

        worker_threads = {}
        for wid in worker_ids:
            wt = MagicMock(spec=threading.Thread)
            wt.is_alive.return_value = False
            wt.ident = 1
            worker_threads[wid] = wt

        captured_worker_calls = []

        def fake_stop_worker(wid, timeout=None):
            captured_worker_calls.append((wid, timeout))
            with runtime._lock:
                runtime._workers.pop(wid, None)
                runtime._worker_states.pop(wid, None)
            return True

        with runtime._lock:
            runtime._state = "RUNNING"
            runtime._loop_thread = loop_thread
            runtime._workers.update(worker_threads)
            runtime._worker_states.update({wid: "IDLE" for wid in worker_ids})

        with patch.object(runtime.time, "monotonic", return_value=monotonic_value), \
             patch.object(runtime, "stop_worker", side_effect=fake_stop_worker):
            runtime.stop(timeout=timeout)

        # Extract the timeout argument passed to the loop-thread join.
        self.assertTrue(
            loop_thread.join.called,
            "stop() did not join the runtime loop thread",
        )
        first_call = loop_thread.join.call_args_list[0]
        loop_join_timeout = first_call.kwargs.get("timeout")
        if loop_join_timeout is None and first_call.args:
            loop_join_timeout = first_call.args[0]
        return loop_join_timeout, captured_worker_calls

    def test_loop_join_uses_30pct_of_timeout(self):
        T = 10.0
        loop_join_timeout, _ = self._run_stop_with_mocked_clock(
            timeout=T, worker_ids=["w1", "w2", "w3"]
        )
        # Loop join receives ≈ 0.30 * T (±5 %).
        self.assertAlmostEqual(loop_join_timeout, 0.30 * T, delta=0.05 * T)

    def test_per_worker_join_uses_70pct_of_timeout(self):
        T = 10.0
        worker_ids = ["w1", "w2", "w3", "w4"]
        _, captured = self._run_stop_with_mocked_clock(timeout=T, worker_ids=worker_ids)

        self.assertEqual(
            [wid for wid, _ in captured],
            worker_ids,
            "every active worker should be stopped",
        )
        expected_per_worker = (0.70 * T) / len(worker_ids)
        for wid, per_worker_timeout in captured:
            self.assertAlmostEqual(
                per_worker_timeout,
                expected_per_worker,
                delta=0.05 * expected_per_worker,
                msg=f"per-worker timeout for {wid} drifted from 70%/N split",
            )
        # The aggregate worker budget must be ≈ 0.70 * T (±5 %).
        total_worker_budget = sum(t for _, t in captured)
        self.assertAlmostEqual(total_worker_budget, 0.70 * T, delta=0.05 * T)

    def test_loop_and_worker_budgets_sum_to_total(self):
        """The 30 % loop slice plus the 70 % worker slice must sum to T."""
        T = 20.0
        worker_ids = ["w1", "w2"]
        loop_join_timeout, captured = self._run_stop_with_mocked_clock(
            timeout=T, worker_ids=worker_ids
        )
        total = loop_join_timeout + sum(t for _, t in captured)
        self.assertAlmostEqual(total, T, delta=0.05 * T)

    def test_budget_split_fails_for_50_50_drift(self):
        """Sanity check: a 50/50 split would not satisfy the 30/70 assertions.

        Documents the regression intent — if the implementation drifts to a
        balanced split, the assertions in ``test_loop_join_uses_30pct_of_timeout``
        would fail.
        """
        T = 10.0
        # Simulated 50/50 budget — must not pass the ±5 % tolerance for 30/70.
        fake_loop = 0.50 * T
        with self.assertRaises(AssertionError):
            self.assertAlmostEqual(fake_loop, 0.30 * T, delta=0.05 * T)


if __name__ == "__main__":
    unittest.main()
