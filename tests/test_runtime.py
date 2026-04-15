"""Tests for the integration.runtime module."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import runtime
from modules.billing import main as billing
from modules.cdp import proxy as proxy_mod
from modules.monitor import main as monitor
from modules.rollout import main as rollout
from integration.runtime import (
    ALLOWED_STATES,
    ConfigError,
    _apply_scale,
    get_active_workers,
    get_deployment_status,
    get_state,
    get_status,
    get_trace_id,
    is_running,
    reset,
    start,
    start_worker,
    stop,
    stop_worker,
    verify_deployment,
)

WORKER_BLOCK_TIMEOUT = 1
CLEANUP_TIMEOUT = 2
WARMUP_DELAY = 0.2
INSUFFICIENT_TIMEOUT = 0.01


class RuntimeResetMixin:
    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()
        self._billing_pool_dir = tempfile.mkdtemp()
        _pool_profile = os.path.join(self._billing_pool_dir, "profiles.txt")
        with open(_pool_profile, "w", encoding="utf-8") as handle:
            handle.write("Alice|Smith|1 Main St|City|NY|10001|2125550001|a@e.com\n")
        self._billing_pool_patcher = patch.object(
            billing, "_pool_dir",
            return_value=Path(self._billing_pool_dir),
        )
        self._billing_pool_patcher.start()

    def tearDown(self):
        self._billing_pool_patcher.stop()
        shutil.rmtree(self._billing_pool_dir, ignore_errors=True)
        reset()
        rollout.reset()
        monitor.reset()

    def _poll_until(self, predicate, timeout=CLEANUP_TIMEOUT, interval=0.05):
        """Poll *predicate* until it returns True or *timeout* expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()


# ── Worker control ────────────────────────────────────────────────


class TestStartWorker(RuntimeResetMixin, unittest.TestCase):
    def test_returns_worker_id(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=1))
        self.assertTrue(wid.startswith("worker-"))
        barrier.set()

    def test_worker_appears_in_active_list(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=1))
        self.assertIn(wid, get_active_workers())
        barrier.set()

    def test_multiple_workers(self):
        barrier = threading.Event()
        ids = [start_worker(lambda _: barrier.wait(timeout=1)) for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)
        barrier.set()

    def test_start_worker_continues_when_proxy_list_file_not_set(self):
        """start_worker succeeds when PROXY_LIST_FILE is unset (empty pool)."""
        barrier = threading.Event()
        with patch.dict(os.environ, {}, clear=True):
            proxy_mod._default_pool = None  # pylint: disable=protected-access
            wid = start_worker(lambda _: barrier.wait(timeout=1))
        self.assertTrue(wid.startswith("worker-"))
        barrier.set()


class TestStopWorker(RuntimeResetMixin, unittest.TestCase):
    def test_stop_running_worker(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=1))
        barrier.set()
        result = stop_worker(wid, timeout=2)
        self.assertTrue(result)
        self.assertNotIn(wid, get_active_workers())

    def test_stop_nonexistent_worker(self):
        self.assertFalse(stop_worker("no-such-worker"))

    def test_stop_running_worker_timeout_removes_zombie(self):
        barrier = threading.Event()
        entered = threading.Event()

        def _blocking_task(_wid):
            entered.set()
            barrier.wait(timeout=WORKER_BLOCK_TIMEOUT)

        wid = start_worker(_blocking_task)
        # Wait until the worker is actually inside the blocking call so that
        # stop_worker's insufficient timeout genuinely expires.
        self.assertTrue(
            entered.wait(timeout=CLEANUP_TIMEOUT),
            "worker did not enter blocking task before timeout",
        )
        try:
            self.assertFalse(stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT))
            # Worker stays registered until thread naturally exits
            self.assertIn(wid, get_active_workers())
        finally:
            barrier.set()
            # Poll until worker cleans itself up via _worker_fn finally block
            deadline = time.monotonic() + CLEANUP_TIMEOUT
            while time.monotonic() < deadline:
                if wid not in get_active_workers():
                    break
                time.sleep(0.01)
            self.assertNotIn(wid, get_active_workers())


# ── Scale up / down ──────────────────────────────────────────────


class TestApplyScale(RuntimeResetMixin, unittest.TestCase):
    def _noop(self, _):
        time.sleep(0.01)

    def test_scale_up(self):
        from integration import runtime
        runtime._state = "RUNNING"
        _apply_scale(3, self._noop)
        self.assertEqual(len(get_active_workers()), 3)
        runtime._state = "INIT"
        time.sleep(0.1)

    def test_scale_down(self):
        from integration import runtime
        runtime._state = "RUNNING"
        _apply_scale(3, self._noop)
        self.assertEqual(len(get_active_workers()), 3)
        _apply_scale(1, self._noop)
        self.assertEqual(len(get_active_workers()), 1)
        runtime._state = "INIT"
        time.sleep(0.1)

    def test_scale_to_zero(self):
        from integration import runtime
        runtime._state = "RUNNING"
        _apply_scale(2, self._noop)
        _apply_scale(0, self._noop)
        self.assertEqual(len(get_active_workers()), 0)
        runtime._state = "INIT"


# ── Worker crash handling ────────────────────────────────────────


class TestWorkerCrash(RuntimeResetMixin, unittest.TestCase):
    def test_crash_removes_worker_from_active_set(self):
        """A failed standalone worker exits cleanly and is deregistered."""
        crash_event = threading.Event()

        def crashing_fn(_):
            crash_event.set()
            raise RuntimeError("boom")

        from integration import runtime
        runtime._state = "RUNNING"
        start_worker(crashing_fn)
        crash_event.wait(timeout=2)
        time.sleep(0.1)
        runtime._state = "INIT"
        self.assertEqual(get_active_workers(), [])

    def test_crash_does_not_stop_other_workers(self):
        """One crashing worker must not kill another."""
        from integration import runtime
        runtime._state = "RUNNING"
        good_barrier = threading.Event()
        start_worker(lambda _: good_barrier.wait(timeout=2))

        def bad_fn(_):
            raise RuntimeError("fail")

        start_worker(bad_fn)
        time.sleep(0.2)
        # Good worker should still be in the active list
        self.assertGreaterEqual(len(get_active_workers()), 1)
        good_barrier.set()
        runtime._state = "INIT"
        time.sleep(0.1)


# ── Runtime loop (start / stop) ──────────────────────────────────


class TestStartStop(RuntimeResetMixin, unittest.TestCase):
    def test_start_returns_true(self):
        result = start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertTrue(result)
        self.assertTrue(is_running())
        stop(timeout=2)

    def test_double_start_returns_false(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertFalse(start(lambda _: None, interval=0.05))
        stop(timeout=2)

    def test_stop_returns_true(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertTrue(stop(timeout=2))
        self.assertFalse(is_running())

    def test_stop_when_not_running(self):
        self.assertFalse(stop(timeout=1))

    def test_stop_timeout_returns_false_when_worker_still_alive(self):
        worker_block = threading.Event()
        with patch("integration.runtime.rollout.try_scale_up",
                   return_value=(1, "at_max", [])):
            start(lambda _: worker_block.wait(timeout=WORKER_BLOCK_TIMEOUT), interval=1)
            time.sleep(WARMUP_DELAY)
            self.assertFalse(stop(timeout=INSUFFICIENT_TIMEOUT))
            self.assertFalse(is_running())
            # Straggler workers stay registered until threads naturally exit
            worker_block.set()
            deadline = time.monotonic() + CLEANUP_TIMEOUT
            while time.monotonic() < deadline:
                if get_active_workers() == []:
                    break
                time.sleep(0.01)
            self.assertEqual(get_active_workers(), [])


# ── Runtime loop integration ─────────────────────────────────────


class TestRuntimeLoop(RuntimeResetMixin, unittest.TestCase):
    def test_loop_restarts_crashed_worker(self):
        calls = []
        wait_event = threading.Event()

        def task_fn(_):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            wait_event.wait(timeout=1)

        with patch("integration.runtime.rollout.try_scale_up",
                   return_value=(1, "at_max", [])):
            start(task_fn, interval=0.05)
            time.sleep(0.3)
            self.assertGreater(monitor.get_restarts_last_hour(), 0)
            self.assertEqual(len(get_active_workers()), 1)
            wait_event.set()
            stop(timeout=2)

    def test_loop_scales_workers(self):
        """Runtime loop should scale workers based on rollout."""
        rollout.configure(check_rollback_fn=lambda: [],
                          save_baseline_fn=lambda: None)
        tick = threading.Event()

        def task_fn(_):
            tick.wait(timeout=2)

        start(task_fn, interval=0.05)
        time.sleep(0.3)
        # After a few ticks, rollout should have advanced and workers scaled
        status = get_status()
        self.assertTrue(status["running"])
        self.assertGreater(status["worker_count"], 0)
        tick.set()
        stop(timeout=2)

    def test_loop_handles_rollback(self):
        """When rollout triggers rollback, consecutive counter increases."""
        rollout.configure(
            check_rollback_fn=lambda: ["error too high"],
            save_baseline_fn=lambda: None,
        )
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.3)
        status = get_status()
        self.assertGreater(status["consecutive_rollbacks"], 0)
        stop(timeout=2)


class TestRuntimeMonitorUnavailable(RuntimeResetMixin, unittest.TestCase):
    def test_loop_survives_monitor_failure(self):
        """Runtime loop must not crash when monitor.get_metrics raises."""
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            start(lambda _: time.sleep(0.5), interval=0.05)
            time.sleep(0.2)
            self.assertTrue(is_running())
            stop(timeout=2)


# ── get_status / is_running ──────────────────────────────────────


class TestStatus(RuntimeResetMixin, unittest.TestCase):
    def test_initial_status(self):
        status = get_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["worker_count"], 0)
        self.assertEqual(status["consecutive_rollbacks"], 0)

    def test_status_during_run(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        self.assertTrue(is_running())
        stop(timeout=2)


class TestReset(RuntimeResetMixin, unittest.TestCase):
    def test_reset_clears_all(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        reset()
        self.assertFalse(is_running())
        self.assertEqual(get_active_workers(), [])
        status = get_status()
        self.assertEqual(status["worker_count"], 0)


# ── Concurrency stress tests ─────────────────────────────────────


class TestSingleLoopThreadInvariant(RuntimeResetMixin, unittest.TestCase):
    """Only one loop thread may exist at a time."""

    def test_concurrent_start_only_one_succeeds(self):
        """Multiple threads calling start() concurrently — exactly one wins."""
        results = []
        barrier = threading.Barrier(10)

        def try_start():
            barrier.wait()
            results.append(start(lambda _: time.sleep(0.5), interval=0.05))

        threads = [threading.Thread(target=try_start) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 9)
        stop(timeout=2)

    def test_start_blocked_during_stopping(self):
        """start() must return False while stop() is in progress."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        with runtime._lock:
            runtime._state = "STOPPING"
        self.assertFalse(start(lambda _: None, interval=0.05))
        with runtime._lock:
            runtime._state = "RUNNING"
        stop(timeout=2)


class TestNoZombieWorkers(RuntimeResetMixin, unittest.TestCase):
    """Worker registry must never contain stale entries."""

    def test_rapid_start_stop_no_zombies(self):
        """Rapidly starting and stopping workers leaves no zombies."""
        runtime._state = "RUNNING"
        for _ in range(20):
            wid = start_worker(lambda _: time.sleep(0.01))
            stop_worker(wid, timeout=2)
        self.assertEqual(get_active_workers(), [])
        runtime._state = "INIT"

    def test_crashed_workers_cleaned_up(self):
        """All crashed workers are removed from registry."""
        runtime._state = "RUNNING"
        events = []
        for _ in range(5):
            ev = threading.Event()
            events.append(ev)

            def crash_fn(_, e=ev):
                e.set()
                raise RuntimeError("boom")

            start_worker(crash_fn)
        for ev in events:
            ev.wait(timeout=2)
        time.sleep(0.2)
        self.assertEqual(get_active_workers(), [])
        runtime._state = "INIT"


class TestStartStopRace(RuntimeResetMixin, unittest.TestCase):
    """start() and stop() racing must not corrupt state."""

    def test_start_stop_interleaved(self):
        """Repeated start/stop cycles must always end in a clean state."""
        for _ in range(10):
            started = start(lambda _: time.sleep(0.5), interval=0.05)
            if started:
                stop(timeout=2)
            self.assertFalse(is_running())
            self.assertEqual(get_active_workers(), [])

    def test_concurrent_stop_only_one_succeeds(self):
        """Multiple threads calling stop() — at most one returns True."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        results = []
        barrier = threading.Barrier(5)

        def try_stop():
            barrier.wait()
            results.append(stop(timeout=2))

        threads = [threading.Thread(target=try_stop) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertLessEqual(results.count(True), 1)
        self.assertFalse(is_running())

    def test_no_duplicate_loop_thread_during_stopping(self):
        """_loop_thread must not be replaced while state is STOPPING."""
        started = start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertTrue(started)
        time.sleep(0.1)
        with runtime._lock:
            runtime._state = "STOPPING"
            self.assertIsNotNone(runtime._loop_thread)
            original_thread = runtime._loop_thread
        # Attempt start while STOPPING — must be rejected
        self.assertFalse(start(lambda _: None, interval=0.05))
        with runtime._lock:
            self.assertIs(runtime._loop_thread, original_thread)
            runtime._state = "RUNNING"
        stop(timeout=2)

    def test_concurrent_start_stop_deterministic(self):
        """Racing start() vs stop() — if both succeed, runtime must not remain RUNNING."""
        for _ in range(10):
            start_result = [None]
            stop_result = [None]
            barrier = threading.Barrier(2)

            def do_start():
                barrier.wait()
                start_result[0] = start(lambda _: time.sleep(0.5), interval=0.05)

            def do_stop():
                barrier.wait()
                stop_result[0] = stop(timeout=2)

            t1 = threading.Thread(target=do_start)
            t2 = threading.Thread(target=do_stop)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)
            # Assert threads actually completed (not timed-out)
            self.assertFalse(t1.is_alive(), "start thread did not finish")
            self.assertFalse(t2.is_alive(), "stop thread did not finish")
            # Assert results are booleans, not None (threads ran their targets)
            self.assertIsInstance(start_result[0], bool)
            self.assertIsInstance(stop_result[0], bool)
            # Mutual exclusion: if both start() and stop() claim success,
            # the runtime must NOT be left in RUNNING — that would mean
            # stop() reported success while the lifecycle is still active.
            if start_result[0] and stop_result[0]:
                self.assertNotEqual(get_state(), "RUNNING")
            # Clean up for next iteration
            if is_running():
                stop(timeout=2)
            if get_state() != "INIT":
                reset()


class TestWorkerRegistryConsistency(RuntimeResetMixin, unittest.TestCase):
    """Worker registry must stay consistent under concurrent operations."""

    def test_concurrent_worker_spawn(self):
        """Spawning workers from multiple threads yields unique IDs."""
        runtime._state = "RUNNING"
        ids = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def spawn():
            barrier.wait()
            wid = start_worker(lambda _: time.sleep(0.5))
            with lock:
                ids.append(wid)

        threads = [threading.Thread(target=spawn) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10)
        runtime._state = "INIT"


class TestLifecycleStateModel(RuntimeResetMixin, unittest.TestCase):
    """Lifecycle state transitions are deterministic."""

    def test_state_after_init(self):
        self.assertEqual(runtime._state, "INIT")

    def test_state_after_start(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(runtime._state, "RUNNING")
        stop(timeout=2)

    def test_state_after_stop(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertEqual(runtime._state, "STOPPED")

    def test_state_after_reset(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        reset()
        self.assertEqual(runtime._state, "INIT")

    def test_restart_after_stop(self):
        """start() succeeds after a clean stop() cycle."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertTrue(start(lambda _: time.sleep(0.5), interval=0.05))
        stop(timeout=2)
# ── Lifecycle state machine audit ────────────────────────────────


class TestLifecycleStateMachine(RuntimeResetMixin, unittest.TestCase):
    """Phase 6 — validate INIT → RUNNING → STOPPING → STOPPED transitions."""

    def test_allowed_states_set(self):
        self.assertEqual(ALLOWED_STATES, {"INIT", "RUNNING", "STOPPING", "STOPPED"})

    def test_initial_state_is_init(self):
        self.assertEqual(get_state(), "INIT")

    def test_start_transitions_to_running(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(get_state(), "RUNNING")
        stop(timeout=2)

    def test_stop_transitions_to_stopped(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")

    def test_start_allowed_from_init(self):
        self.assertEqual(get_state(), "INIT")
        self.assertTrue(start(lambda _: time.sleep(0.5), interval=0.05))
        stop(timeout=2)

    def test_start_allowed_from_stopped(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")
        self.assertTrue(start(lambda _: time.sleep(0.5), interval=0.05))
        stop(timeout=2)

    def test_start_blocked_while_running(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertFalse(start(lambda _: None, interval=0.05))
        self.assertEqual(get_state(), "RUNNING")
        stop(timeout=2)

    def test_stopping_blocks_start(self):
        """Verify STOPPING state blocks start()."""
        with runtime._lock:
            runtime._state = "STOPPING"
        self.assertFalse(start(lambda _: None, interval=0.05))
        with runtime._lock:
            runtime._state = "INIT"

    def test_stop_only_from_running(self):
        self.assertFalse(stop(timeout=1))
        self.assertEqual(get_state(), "INIT")

    def test_stop_from_stopped_returns_false(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")
        self.assertFalse(stop(timeout=1))

    def test_restart_no_state_leak(self):
        """Validate restart cycle does not leak state."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")
        self.assertEqual(get_active_workers(), [])
        status = get_status()
        self.assertEqual(status["worker_count"], 0)
        self.assertFalse(status["running"])
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(get_state(), "RUNNING")
        self.assertTrue(is_running())
        stop(timeout=2)

    def test_reset_returns_to_init(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(0.1)
        reset()
        self.assertEqual(get_state(), "INIT")

    def test_get_status_includes_state(self):
        status = get_status()
        self.assertIn("state", status)
        self.assertEqual(status["state"], "INIT")

    def test_deterministic_full_cycle(self):
        """INIT → RUNNING → STOPPED → RUNNING → STOPPED → INIT."""
        self.assertEqual(get_state(), "INIT")
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(get_state(), "RUNNING")
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")
        start(lambda _: time.sleep(0.5), interval=0.05)
        self.assertEqual(get_state(), "RUNNING")
        stop(timeout=2)
        self.assertEqual(get_state(), "STOPPED")
        reset()
        self.assertEqual(get_state(), "INIT")


class TestZombieWorkerCleanup(RuntimeResetMixin, unittest.TestCase):
    """Validate no zombie workers remain across stop/timeout/crash scenarios."""

    def test_timeout_worker_eventually_cleaned_up(self):
        """Worker cleans up from _workers after its blocking task completes."""
        barrier = threading.Event()
        entered = threading.Event()

        def _blocking_task(_wid):
            entered.set()
            barrier.wait(timeout=WORKER_BLOCK_TIMEOUT)

        wid = start_worker(_blocking_task)
        self.assertTrue(
            entered.wait(timeout=CLEANUP_TIMEOUT),
            "worker did not enter blocking task before timeout",
        )
        with runtime._lock:
            worker_thread = runtime._workers[wid]
        # Force a timeout on stop
        stop_result = stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT)
        self.assertFalse(stop_result)
        # Worker stays registered until thread naturally exits
        self.assertIn(wid, get_active_workers())
        # Let the blocking task finish naturally and verify the thread exits
        barrier.set()
        worker_thread.join(timeout=CLEANUP_TIMEOUT)
        self.assertFalse(worker_thread.is_alive())
        self.assertNotIn(wid, get_active_workers())

    def test_stop_requests_cleaned_after_timeout_worker_exits(self):
        """No stale _stop_requests entries after timed-out worker exits."""
        barrier = threading.Event()
        entered = threading.Event()

        def _blocking_task(_wid):
            entered.set()
            barrier.wait(timeout=WORKER_BLOCK_TIMEOUT)

        wid = start_worker(_blocking_task)
        self.assertTrue(
            entered.wait(timeout=CLEANUP_TIMEOUT),
            "worker did not enter blocking task before timeout",
        )
        with runtime._lock:
            worker_thread = runtime._workers[wid]
        stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT)
        barrier.set()
        worker_thread.join(timeout=CLEANUP_TIMEOUT)
        with runtime._lock:
            self.assertNotIn(wid, runtime._stop_requests)

    def test_multiple_crashes_no_zombies(self):
        """5 concurrent crashes all deregister cleanly."""
        events = []
        for _ in range(5):
            ev = threading.Event()
            events.append(ev)

            def crash_fn(_, e=ev):
                e.set()
                raise RuntimeError("crash")

            start_worker(crash_fn)
        for ev in events:
            ev.wait(timeout=2)
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "crashed workers still in registry",
        )

    def test_crash_cleans_stop_requests(self):
        """Crash with pending stop request leaves no stale entry."""
        gate = threading.Event()

        def crash_fn(_):
            gate.wait(timeout=2)
            raise RuntimeError("boom")

        wid = start_worker(crash_fn)
        # Add a stop request before the crash to simulate stop racing with crash
        with runtime._lock:
            runtime._stop_requests.add(wid)
        gate.set()
        self.assertTrue(
            self._poll_until(lambda: wid not in get_active_workers()),
            "crashed worker still in registry",
        )
        with runtime._lock:
            self.assertNotIn(wid, runtime._stop_requests)
            self.assertNotIn(wid, runtime._workers)

    def test_stop_runtime_timeout_eventual_cleanup(self):
        """stop() timeout returns False and the original worker thread later exits."""
        barrier = threading.Event()
        with patch("integration.runtime.rollout.try_scale_up",
                   return_value=(1, "at_max", [])):
            start(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT), interval=60)
            time.sleep(WARMUP_DELAY)
            with runtime._lock:
                self.assertEqual(len(runtime._workers), 1)
                worker_thread = next(iter(runtime._workers.values()))
            self.assertIsInstance(worker_thread, threading.Thread)
            # Stop with very short timeout — may not join all workers yet.
            self.assertFalse(stop(timeout=INSUFFICIENT_TIMEOUT))
            barrier.set()
            worker_thread.join(timeout=CLEANUP_TIMEOUT)
            self.assertFalse(worker_thread.is_alive())

    def test_start_worker_thread_failure_no_zombie(self):
        """thread.start() failure leaves no zombie in _workers."""
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no resources")):
            with self.assertRaises(RuntimeError):
                start_worker(lambda _: None)
        self.assertEqual(get_active_workers(), [])

    def test_concurrent_start_stop_no_zombies(self):
        """10 concurrent start/stop threads leave consistent registry."""
        errors = []

        def start_stop_cycle():
            try:
                wid = start_worker(lambda _: time.sleep(0.01))
                stop_worker(wid, timeout=2)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start_stop_cycle) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "workers still in registry after concurrent start/stop",
        )


class TestRegistryConcurrency(RuntimeResetMixin, unittest.TestCase):
    """Worker registry consistency under concurrent operations."""

    def test_concurrent_spawn_unique_ids(self):
        """20 concurrent start_worker calls all produce unique IDs."""
        runtime._state = "RUNNING"
        ids = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def spawn():
            barrier.wait()
            wid = start_worker(lambda _: time.sleep(0.01))
            with lock:
                ids.append(wid)

        threads = [threading.Thread(target=spawn) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20, "duplicate worker IDs detected")
        for wid in ids:
            stop_worker(wid, timeout=2)
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "registry not empty after concurrent spawn cleanup",
        )

    def test_concurrent_spawn_registry_integrity(self):
        """All concurrently spawned workers appear in the active registry."""
        runtime._state = "RUNNING"
        ids = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)
        barriers = [threading.Event() for _ in range(10)]

        def spawn(idx):
            barrier.wait()
            wid = start_worker(lambda _, b=barriers[idx]: b.wait(timeout=2))
            with lock:
                ids.append(wid)

        threads = [threading.Thread(target=spawn, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        active = get_active_workers()
        for wid in ids:
            self.assertIn(wid, active, f"{wid} missing from active registry")
        for b in barriers:
            b.set()
        for wid in ids:
            stop_worker(wid, timeout=2)
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "registry not empty after concurrent spawn cleanup",
        )

    def test_concurrent_add_remove(self):
        """Interleaved start/stop from multiple threads doesn't corrupt the registry."""
        runtime._state = "RUNNING"
        errors = []

        def add_remove():
            try:
                wid = start_worker(lambda _: time.sleep(0.01))
                stop_worker(wid, timeout=2)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add_remove) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "registry not empty after concurrent add/remove",
        )
        runtime._state = "INIT"

    def test_registry_reflects_runtime_after_completion(self):
        """After workers exit via exception, they are properly deregistered."""
        runtime._state = "RUNNING"
        events = []
        for _ in range(5):
            ev = threading.Event()
            events.append(ev)

            def crash_fn(_, e=ev):
                e.set()
                raise RuntimeError("boom")

            start_worker(crash_fn)
        for ev in events:
            ev.wait(timeout=2)
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
            "crashed workers remain in registry",
        )
        runtime._state = "INIT"

    def test_stop_worker_on_not_yet_started_thread(self):
        """stop_worker handles thread registered but not yet started without raising.

        Direct state manipulation is required because the public start_worker()
        API calls t.start() immediately, making the narrow registration-to-start
        window impossible to hit deterministically through the public interface.
        """
        with runtime._lock:
            runtime._worker_counter += 1
            wid = f"worker-{runtime._worker_counter}"
            t = threading.Thread(
                target=runtime._worker_fn,
                args=(wid, lambda _: None),
                daemon=True,
            )
            runtime._workers[wid] = t
        # Thread is registered but not started (ident is None); stop_worker must not raise
        self.assertIsNone(t.ident)
        result = stop_worker(wid, timeout=0.1)
        # Thread was never alive so cleanup succeeds
        self.assertTrue(result)
        self.assertNotIn(wid, get_active_workers())


# ── Failure Mode Audit ────────────────────────────────────────────


class TestFailureModeAudit(RuntimeResetMixin, unittest.TestCase):
    """Task 6 — ensure all failure modes are handled, no silent failures."""

    def test_crash_with_monitor_error_still_cleans_up(self):
        """Worker cleanup completes even when monitor fails during crash."""
        from integration import runtime

        runtime._state = "RUNNING"

        def crashing_fn(_):
            raise RuntimeError("task boom")

        with patch.object(monitor, "record_error", side_effect=Exception("monitor boom")):
            wid = start_worker(crashing_fn)
            self.assertTrue(
                self._poll_until(lambda: wid not in get_active_workers()),
                "worker should be cleaned up even when monitor.record_error() fails",
            )
        runtime._state = "INIT"

    def test_crash_with_monitor_error_still_logs_task_error(self):
        """Original task error is always logged, even when monitor fails."""
        from integration import runtime

        runtime._state = "RUNNING"
        logged = threading.Event()

        def crashing_fn(_):
            raise RuntimeError("original error")

        original_log_event = runtime._log_event

        def spy_log_event(wid, state, action, metrics=None):
            original_log_event(wid, state, action, metrics)
            if action == "task_failed":
                logged.set()

        with patch.object(monitor, "record_error", side_effect=Exception("monitor boom")):
            with patch.object(runtime, "_log_event", side_effect=spy_log_event):
                start_worker(crashing_fn)
                self.assertTrue(
                    logged.wait(timeout=2),
                    "task_failed must be logged even when monitor.record_error() fails",
                )
        runtime._state = "INIT"

    def test_success_with_monitor_failure_continues_worker(self):
        """Worker survives monitor.record_success() failure and keeps running."""
        from integration import runtime

        runtime._state = "RUNNING"
        call_count = {"n": 0}
        barrier = threading.Event()

        def counting_fn(_):
            call_count["n"] += 1
            if call_count["n"] >= 3:
                barrier.set()

        with patch.object(monitor, "record_success", side_effect=Exception("monitor boom")):
            wid = start_worker(counting_fn)
            self.assertTrue(
                barrier.wait(timeout=2),
                "worker should keep running despite monitor.record_success() failure",
            )
        # Stop the worker cleanly
        self.assertTrue(stop_worker(wid, timeout=WORKER_BLOCK_TIMEOUT))
        self.assertTrue(
            self._poll_until(lambda: wid not in get_active_workers()),
        )
        self.assertGreaterEqual(call_count["n"], 3)
        runtime._state = "INIT"

    def test_unexpected_exception_logged(self):
        """Catch-all logs unexpected errors that escape inner handlers."""
        from integration import runtime

        runtime._state = "RUNNING"
        logged = threading.Event()

        original_error = runtime._logger.error

        def spy_error(msg, *args, **kwargs):
            original_error(msg, *args, **kwargs)
            if "Unexpected error" in str(msg):
                logged.set()

        # Simulate unexpected error: patch _log_event to raise on "running"/"start"
        original_log = runtime._log_event

        def exploding_log(wid, state, action, metrics=None):
            if state == "running" and action == "start":
                raise RuntimeError("unexpected log failure")
            original_log(wid, state, action, metrics)

        with patch.object(runtime, "_log_event", side_effect=exploding_log):
            with patch.object(runtime._logger, "error", side_effect=spy_error):
                wid = start_worker(lambda _: None)
                self.assertTrue(
                    logged.wait(timeout=2),
                    "unexpected exception must be logged by catch-all handler",
                )
        self.assertTrue(
            self._poll_until(lambda: wid not in get_active_workers()),
        )
        runtime._state = "INIT"

    def test_timeout_stop_deterministic_state(self):
        """State is consistent after stop timeout."""
        from integration import runtime

        runtime._state = "RUNNING"
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=5))

        self.assertTrue(
            self._poll_until(lambda: wid in get_active_workers()),
        )
        # Stop with very short timeout — worker won't finish in time
        result = stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT)
        # Whether or not it timed out, the state must be clean
        barrier.set()
        self.assertTrue(
            self._poll_until(lambda: wid not in get_active_workers()),
        )
        # Worker must not remain in _stop_requests forever
        with runtime._lock:
            self.assertNotIn(wid, runtime._stop_requests)
        runtime._state = "INIT"

    def test_crash_recovery_state(self):
        """System restarts worker after crash via _pending_restarts."""
        from integration import runtime

        runtime._state = "RUNNING"
        crash_event = threading.Event()

        def crashing_fn(_):
            crash_event.set()
            raise RuntimeError("crash")

        start_worker(crashing_fn)
        crash_event.wait(timeout=2)
        self.assertTrue(
            self._poll_until(lambda: get_active_workers() == []),
        )
        # _pending_restarts should be incremented
        with runtime._lock:
            self.assertGreaterEqual(runtime._pending_restarts, 1)
        runtime._state = "INIT"


# ── Observability Audit (Task 8) ──────────────────────────────────


class TestTraceIdLifecycle(RuntimeResetMixin, unittest.TestCase):
    """Trace-id is generated on start, persists through stop, cleared on reset."""

    def test_trace_id_none_before_start(self):
        self.assertIsNone(get_trace_id())

    def test_trace_id_generated_on_start(self):
        started = start(lambda _: time.sleep(0.5), interval=0.1)
        self.assertTrue(started)
        tid = get_trace_id()
        self.assertIsNotNone(tid)
        self.assertEqual(len(tid), 12)
        self.assertRegex(tid, r'^[0-9a-f]{12}$')

    def test_trace_id_published_before_thread_start(self):
        from integration import runtime

        observed = {}
        original_thread = runtime.threading.Thread

        def thread_factory(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            original_start = thread.start

            def wrapped_start():
                observed["trace_id"] = runtime.get_trace_id()
                observed["state"] = runtime.get_state()
                return original_start()

            thread.start = wrapped_start
            return thread

        with patch("integration.runtime.threading.Thread", side_effect=thread_factory):
            started = runtime.start(lambda _: time.sleep(0.1), interval=0.1)

        self.assertTrue(started)
        self.assertIsNotNone(observed["trace_id"])
        self.assertEqual(observed["state"], "RUNNING")
        self.assertEqual(runtime.get_trace_id(), observed["trace_id"])

    def test_trace_id_persists_through_stop(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        tid = get_trace_id()
        stop(timeout=2)
        self.assertEqual(get_trace_id(), tid)

    def test_trace_id_cleared_on_reset(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        self.assertIsNotNone(get_trace_id())
        reset()
        self.assertIsNone(get_trace_id())

    def test_new_trace_id_on_restart(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        first_tid = get_trace_id()
        stop(timeout=2)
        reset()
        start(lambda _: time.sleep(0.5), interval=0.1)
        second_tid = get_trace_id()
        self.assertNotEqual(first_tid, second_tid)

    def test_trace_id_in_status(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        status = get_status()
        self.assertIn("trace_id", status)
        self.assertEqual(status["trace_id"], get_trace_id())


class TestStructuredLogFormat(RuntimeResetMixin, unittest.TestCase):
    """Log format must have exactly 6 pipe-separated fields including trace_id."""

    def _capture_log_event(self, worker_id, state, action):
        """Capture _log_event calls and return a list of formatted log lines."""
        from integration import runtime

        captured = []
        original_info = runtime._logger.info

        def capture_info(msg, *args, **kwargs):
            captured.append(msg % args)
            original_info(msg, *args, **kwargs)

        with patch.object(runtime._logger, "info", side_effect=capture_info):
            runtime._log_event(worker_id, state, action)
        return captured

    def test_log_contains_trace_id(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        captured = self._capture_log_event("test-worker", "running", "test_action")
        self.assertTrue(len(captured) > 0, "Expected at least one log line")
        self.assertIn(get_trace_id(), captured[0])

    def test_log_format_has_six_fields(self):
        start(lambda _: time.sleep(0.5), interval=0.1)
        captured = self._capture_log_event("test-worker", "running", "test_action")
        self.assertTrue(len(captured) > 0, "Expected at least one log line")
        fields = captured[0].split(" | ")
        self.assertEqual(len(fields), 6, f"Expected 6 fields, got {len(fields)}: {captured[0]}")


# ── Deployment status ────────────────────────────────────────────


class TestDeploymentStatus(RuntimeResetMixin, unittest.TestCase):
    """Tests for get_deployment_status() production health snapshot."""

    def test_initial_deployment_status(self):
        ds = get_deployment_status()
        self.assertFalse(ds["running"])
        self.assertEqual(ds["state"], "INIT")
        self.assertEqual(ds["worker_count"], 0)
        self.assertEqual(ds["active_workers"], [])
        self.assertEqual(ds["consecutive_rollbacks"], 0)
        self.assertIsNotNone(ds["metrics"])
        self.assertEqual(ds["metrics"]["success_count"], 0)
        self.assertEqual(ds["metrics"]["error_count"], 0)
        self.assertEqual(ds["metrics"]["restarts_last_hour"], 0)

    def test_deployment_status_during_run(self):
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        ds = get_deployment_status()
        self.assertTrue(ds["running"])
        self.assertEqual(ds["state"], "RUNNING")
        self.assertGreater(ds["worker_count"], 0)
        self.assertIsNotNone(ds["trace_id"])
        self.assertIsNotNone(ds["metrics"])
        stop(timeout=2)

    def test_deployment_status_includes_error_rate(self):
        monitor.record_success()
        monitor.record_error()
        ds = get_deployment_status()
        self.assertAlmostEqual(ds["metrics"]["error_rate"], 0.5)
        self.assertAlmostEqual(ds["metrics"]["success_rate"], 0.5)

    def test_deployment_status_includes_restarts(self):
        monitor.record_restart()
        monitor.record_restart()
        ds = get_deployment_status()
        self.assertEqual(ds["metrics"]["restarts_last_hour"], 2)

    def test_deployment_status_survives_monitor_failure(self):
        """get_deployment_status must not crash when monitor is unavailable."""
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            ds = get_deployment_status()
            self.assertFalse(ds["running"])
            self.assertIsNone(ds["metrics"])


class TestVerifyDeployment(RuntimeResetMixin, unittest.TestCase):
    """Tests for verify_deployment() production health verification."""

    def test_verify_fails_when_not_started(self):
        """verify_deployment must fail when service is not running."""
        result = verify_deployment()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["service_running"])
        self.assertFalse(result["checks"]["workers_active"])
        self.assertFalse(result["checks"]["no_startup_errors"])
        self.assertGreater(len(result["errors"]), 0)

    def test_verify_passes_when_healthy(self):
        """verify_deployment must pass when service is running and healthy."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        result = verify_deployment()
        self.assertTrue(result["passed"])
        self.assertTrue(result["checks"]["service_running"])
        self.assertTrue(result["checks"]["workers_active"])
        self.assertTrue(result["checks"]["no_startup_errors"])
        self.assertEqual(result["errors"], [])
        stop(timeout=2)

    def test_verify_returns_required_keys(self):
        """Result must contain passed, checks, and errors keys."""
        result = verify_deployment()
        self.assertIn("passed", result)
        self.assertIn("checks", result)
        self.assertIn("errors", result)
        self.assertIn("service_running", result["checks"])
        self.assertIn("workers_active", result["checks"])
        self.assertIn("no_startup_errors", result["checks"])

    def test_verify_detects_high_error_rate(self):
        """verify_deployment must detect error_rate above 5% threshold."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        for _ in range(10):
            monitor.record_error()
        result = verify_deployment()
        self.assertFalse(result["checks"]["no_startup_errors"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("error rate" in e.lower() for e in result["errors"]))
        stop(timeout=2)

    def test_verify_detects_excessive_restarts(self):
        """verify_deployment must detect restarts above 3/hr threshold."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        for _ in range(4):
            monitor.record_restart()
        result = verify_deployment()
        self.assertFalse(result["checks"]["no_startup_errors"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("restarts" in e.lower() for e in result["errors"]))
        stop(timeout=2)

    def test_verify_detects_consecutive_rollbacks(self):
        """verify_deployment must detect consecutive rollbacks."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        with runtime._lock:
            runtime._consecutive_rollbacks = 2
        result = verify_deployment()
        self.assertFalse(result["checks"]["no_startup_errors"])
        self.assertTrue(any("rollback" in e.lower() for e in result["errors"]))
        stop(timeout=2)

    def test_verify_handles_monitor_failure(self):
        """verify_deployment must flag when monitor is unavailable while running."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        with patch("integration.runtime.monitor") as mock_mon:
            mock_mon.get_metrics.side_effect = RuntimeError("unavailable")
            result = verify_deployment()
            self.assertFalse(result["checks"]["no_startup_errors"])
            self.assertTrue(
                any(
                    "monitor metrics unavailable while service running" in e.lower()
                    for e in result["errors"]
                )
            )
        stop(timeout=2)

    def test_verify_after_stop(self):
        """verify_deployment must fail after stop (service not running)."""
        start(lambda _: time.sleep(0.5), interval=0.05)
        time.sleep(WARMUP_DELAY)
        stop(timeout=2)
        result = verify_deployment()
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["service_running"])
        self.assertFalse(result["checks"]["no_startup_errors"])


if __name__ == "__main__":
    unittest.main()


# ── Billing Circuit Breaker ──────────────────────────────────────


class TestBillingCircuitBreaker(RuntimeResetMixin, unittest.TestCase):
    """Billing-specific circuit breaker: pause after consecutive CycleExhaustedError."""

    def test_billing_cb_triggers_after_threshold(self):
        """After _BILLING_CB_THRESHOLD consecutive billing failures, throttle is active."""
        from modules.common.exceptions import CycleExhaustedError

        runtime._state = "RUNNING"
        original_threshold = runtime._BILLING_CB_THRESHOLD
        original_pause = runtime._BILLING_CB_PAUSE
        try:
            runtime._BILLING_CB_THRESHOLD = 2
            runtime._BILLING_CB_PAUSE = 60

            call_count = {"n": 0}

            def billing_fail(_):
                call_count["n"] += 1
                raise CycleExhaustedError("pool empty")

            # Launch 2 workers that fail with CycleExhaustedError
            wid1 = start_worker(billing_fail)
            self.assertTrue(
                self._poll_until(lambda: wid1 not in get_active_workers()),
            )
            wid2 = start_worker(billing_fail)
            self.assertTrue(
                self._poll_until(lambda: wid2 not in get_active_workers()),
            )
            # After 2 billing failures, circuit breaker should be throttled
            with runtime._lock:
                throttled = runtime._is_billing_throttled()
            self.assertTrue(throttled, "billing circuit breaker should be active after threshold failures")
        finally:
            runtime._BILLING_CB_THRESHOLD = original_threshold
            runtime._BILLING_CB_PAUSE = original_pause
            runtime._state = "INIT"

    def test_billing_cb_status_exposed(self):
        """get_status() exposes billing_throttled and consecutive_billing_failures."""
        runtime._state = "RUNNING"
        try:
            with runtime._lock:
                runtime._billing_throttled_until = time.monotonic() + 60
                runtime._consecutive_billing_failures = 2
            status = get_status()
            self.assertTrue(status["billing_throttled"])
            self.assertEqual(status["consecutive_billing_failures"], 2)
        finally:
            runtime._state = "INIT"

    def test_billing_cb_worker_pauses_during_throttle(self):
        """Worker waits during billing throttle instead of executing task."""
        from modules.common.exceptions import CycleExhaustedError

        runtime._state = "RUNNING"
        original_pause = runtime._BILLING_CB_PAUSE
        try:
            runtime._BILLING_CB_PAUSE = 300  # long pause
            with runtime._lock:
                runtime._billing_throttled_until = time.monotonic() + 300

            executed = threading.Event()

            def should_not_run(_):
                executed.set()

            wid = start_worker(should_not_run)
            # Give worker time to enter its loop; it should NOT execute the task
            time.sleep(0.5)
            self.assertFalse(executed.is_set(), "worker should NOT execute task during billing throttle")
            # Stop the worker
            stop_worker(wid, timeout=WORKER_BLOCK_TIMEOUT)
            self.assertTrue(
                self._poll_until(lambda: wid not in get_active_workers()),
            )
        finally:
            runtime._BILLING_CB_PAUSE = original_pause
            runtime._state = "INIT"

    def test_non_billing_failure_does_not_trigger_billing_cb(self):
        """Non-billing failures do not increment billing circuit breaker counter."""
        runtime._state = "RUNNING"
        original_threshold = runtime._BILLING_CB_THRESHOLD
        try:
            runtime._BILLING_CB_THRESHOLD = 2

            def generic_fail(_):
                raise RuntimeError("generic error")

            wid1 = start_worker(generic_fail)
            self.assertTrue(
                self._poll_until(lambda: wid1 not in get_active_workers()),
            )
            wid2 = start_worker(generic_fail)
            self.assertTrue(
                self._poll_until(lambda: wid2 not in get_active_workers()),
            )
            with runtime._lock:
                throttled = runtime._is_billing_throttled()
            self.assertFalse(throttled, "billing CB must not trigger for non-billing failures")
            with runtime._lock:
                self.assertEqual(runtime._consecutive_billing_failures, 0)
        finally:
            runtime._BILLING_CB_THRESHOLD = original_threshold
            runtime._state = "INIT"

    def test_billing_cb_resets_on_success(self):
        """Successful task execution resets billing failure counter."""
        runtime._state = "RUNNING"
        original_threshold = runtime._BILLING_CB_THRESHOLD
        try:
            runtime._BILLING_CB_THRESHOLD = 5  # high threshold so it won't trigger

            # Directly set billing failures to simulate prior failures
            with runtime._lock:
                runtime._consecutive_billing_failures = 2

            # Now a success should reset the counter
            success_done = threading.Event()

            def success_task(_):
                success_done.set()

            wid = start_worker(success_task)
            self.assertTrue(success_done.wait(timeout=2))
            stop_worker(wid, timeout=WORKER_BLOCK_TIMEOUT)
            self.assertTrue(self._poll_until(lambda: wid not in get_active_workers()))
            with runtime._lock:
                self.assertEqual(runtime._consecutive_billing_failures, 0)
        finally:
            runtime._BILLING_CB_THRESHOLD = original_threshold
            runtime._state = "INIT"

    def test_billing_cb_configurable_threshold(self):
        """Circuit breaker respects configured threshold value."""
        from modules.common.exceptions import CycleExhaustedError

        runtime._state = "RUNNING"
        original_threshold = runtime._BILLING_CB_THRESHOLD
        original_pause = runtime._BILLING_CB_PAUSE
        try:
            runtime._BILLING_CB_THRESHOLD = 1  # trigger after just 1 failure
            runtime._BILLING_CB_PAUSE = 30

            def billing_fail(_):
                raise CycleExhaustedError("pool empty")

            wid = start_worker(billing_fail)
            self.assertTrue(self._poll_until(lambda: wid not in get_active_workers()))
            with runtime._lock:
                throttled = runtime._is_billing_throttled()
            self.assertTrue(throttled, "billing CB should trigger after 1 failure when threshold=1")
        finally:
            runtime._BILLING_CB_THRESHOLD = original_threshold
            runtime._BILLING_CB_PAUSE = original_pause
            runtime._state = "INIT"


# ── Billing Pool Preflight Validation ───────────────────────────────


class TestBillingPoolPreflightValidation(RuntimeResetMixin, unittest.TestCase):
    """Billing pool preflight validation: startup must fail-fast if pool is invalid."""

    # Tests legitimately access billing module internals
    # pylint: disable=protected-access

    def setUp(self):
        super().setUp()
        self._tmpdirs = []

    def tearDown(self):
        for tmp_dir in self._tmpdirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        super().tearDown()

    def _make_pool_dir(self, with_txt=False):
        tmpdir = tempfile.mkdtemp()
        self._tmpdirs.append(tmpdir)
        if with_txt:
            path = os.path.join(tmpdir, "profiles.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Alice|Smith|1 Main St|City|NY|10001|2125550001|a@e.com\n")
        return tmpdir

    def test_start_fails_if_pool_dir_missing(self):
        """start() must raise RuntimeError if BILLING_POOL_DIR does not exist."""
        missing = Path(tempfile.gettempdir()) / "_nonexistent_billing_pool_dir_xyz"
        with patch.object(billing, "_pool_dir", return_value=missing):
            with self.assertRaises(RuntimeError) as ctx:
                start(lambda _: None, interval=0.05)
        self.assertIn("does not exist", str(ctx.exception))

    def test_start_fails_if_pool_dir_empty(self):
        """start() must raise RuntimeError if pool dir has no .txt files."""
        tmpdir = self._make_pool_dir(with_txt=False)
        with patch.object(billing, "_pool_dir", return_value=Path(tmpdir)):
            with self.assertRaises(RuntimeError) as ctx:
                start(lambda _: None, interval=0.05)
        self.assertIn("no .txt files", str(ctx.exception))

    def test_start_fails_if_pool_below_min_threshold(self):
        """start() must raise RuntimeError if pool has fewer profiles than MIN_BILLING_PROFILES."""
        tmpdir = self._make_pool_dir(with_txt=True)
        original_min = billing._MIN_BILLING_PROFILES
        try:
            billing._MIN_BILLING_PROFILES = 999
            with patch.object(billing, "_pool_dir", return_value=Path(tmpdir)):
                with self.assertRaises(RuntimeError) as ctx:
                    start(lambda _: None, interval=0.05)
            self.assertIn("below minimum threshold", str(ctx.exception))
        finally:
            billing._MIN_BILLING_PROFILES = original_min

    def test_start_succeeds_with_valid_pool(self):
        """start() succeeds when pool dir exists with at least one valid .txt file."""
        tmpdir = self._make_pool_dir(with_txt=True)
        with patch.object(billing, "_pool_dir", return_value=Path(tmpdir)):
            result = start(lambda _: None, interval=0.05)
        self.assertTrue(result)
        self.assertEqual(runtime.get_state(), "RUNNING")
        stop()

    def test_no_payment_attempt_when_preflight_fails(self):
        """task_fn must never be called when preflight validation fails."""
        missing = Path(tempfile.gettempdir()) / "_nonexistent_billing_pool_dir_xyz"
        called = []
        with patch.object(billing, "_pool_dir", return_value=missing):
            try:
                start(lambda _: called.append(1), interval=0.05)
            except RuntimeError:
                pass
        self.assertEqual(called, [], "task_fn must not be called when preflight fails")

    def test_runtime_state_unchanged_after_preflight_fail(self):
        """_state must remain INIT after preflight failure (not RUNNING)."""
        missing = Path(tempfile.gettempdir()) / "_nonexistent_billing_pool_dir_xyz"
        with patch.object(billing, "_pool_dir", return_value=missing):
            with self.assertRaises(RuntimeError):
                start(lambda _: None, interval=0.05)
        self.assertNotEqual(runtime.get_state(), "RUNNING")
        self.assertIn(runtime.get_state(), ("INIT", "STOPPED"))

    def test_start_returns_false_when_already_running(self):
        """start() must preserve its False return when runtime is already running."""
        self.assertTrue(start(lambda _: None, interval=0.05))
        missing = Path(tempfile.gettempdir()) / "_nonexistent_billing_pool_dir_xyz"
        try:
            with patch.object(billing, "_pool_dir", return_value=missing):
                self.assertFalse(start(lambda _: None, interval=0.05))
        finally:
            stop()


class TestStartupConfigValidation(RuntimeResetMixin, unittest.TestCase):
    def test_start_raises_config_error_for_zero_worker_count(self):
        with patch.dict(os.environ, {"WORKER_COUNT": "0", "GIVEX_ENDPOINT": "https://example.test"}):
            with self.assertRaises(ConfigError):
                start(lambda _: None, interval=0.05)

    def test_start_raises_config_error_for_non_integer_worker_count(self):
        with patch.dict(os.environ, {"WORKER_COUNT": "abc", "GIVEX_ENDPOINT": "https://example.test"}):
            with self.assertRaises(ConfigError):
                start(lambda _: None, interval=0.05)

    def test_start_warns_when_worker_count_missing(self):
        env = dict(os.environ)
        env.pop("WORKER_COUNT", None)
        env["GIVEX_ENDPOINT"] = "https://example.test"
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("integration.runtime", level="WARNING") as logs:
                self.assertTrue(start(lambda _: None, interval=0.05))
            self.assertTrue(
                any("WORKER_COUNT not set" in line for line in logs.output),
                "Expected WORKER_COUNT warning at startup",
            )
            stop()
