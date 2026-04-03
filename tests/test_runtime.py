import threading
import time
import unittest
from unittest.mock import patch

from integration import runtime
from modules.monitor import main as monitor
from modules.rollout import main as rollout
from integration.runtime import (
    _apply_scale,
    get_active_workers,
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


if __name__ == "__main__":
    unittest.main()
