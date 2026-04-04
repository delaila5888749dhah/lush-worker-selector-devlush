import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime
from modules.monitor import main as monitor
from modules.rollout import main as rollout
from integration.runtime import (
    ALLOWED_STATES,
    _apply_scale,
    get_active_workers,
    get_state,
    get_status,
    is_running,
    reset,
    start,
    start_worker,
    stop,
    stop_worker,
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

    def tearDown(self):
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
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
        try:
            self.assertFalse(stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT))
            # Zombie cleaned from registry even though thread is alive
            self.assertNotIn(wid, get_active_workers())
        finally:
            barrier.set()
            time.sleep(0.2)


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
            # Zombie workers cleaned from registry
            self.assertEqual(get_active_workers(), [])
            worker_block.set()
            time.sleep(1.1)


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
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
        with runtime._lock:
            worker_thread = runtime._workers[wid]
        # Force a timeout on stop
        stop_result = stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT)
        self.assertFalse(stop_result)
        self.assertNotIn(wid, get_active_workers())
        # Let the blocking task finish naturally and verify the thread exits
        barrier.set()
        worker_thread.join(timeout=CLEANUP_TIMEOUT)
        self.assertFalse(worker_thread.is_alive())

    def test_stop_requests_cleaned_after_timeout_worker_exits(self):
        """No stale _stop_requests entries after timed-out worker exits."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
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


if __name__ == "__main__":
    unittest.main()
