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

    def test_stop_running_worker_timeout_keeps_worker_active(self):
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
        try:
            self.assertFalse(stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT))
            self.assertIn(wid, get_active_workers())
            self.assertTrue(runtime._workers[wid].is_alive())
        finally:
            barrier.set()
            stop_worker(wid, timeout=CLEANUP_TIMEOUT)


# ── Scale up / down ──────────────────────────────────────────────


class TestApplyScale(RuntimeResetMixin, unittest.TestCase):
    def _noop(self, _):
        time.sleep(0.01)

    def test_scale_up(self):
        from integration import runtime
        runtime._running = True
        _apply_scale(3, self._noop)
        self.assertEqual(len(get_active_workers()), 3)
        runtime._running = False
        time.sleep(0.1)

    def test_scale_down(self):
        from integration import runtime
        runtime._running = True
        _apply_scale(3, self._noop)
        self.assertEqual(len(get_active_workers()), 3)
        _apply_scale(1, self._noop)
        self.assertEqual(len(get_active_workers()), 1)
        runtime._running = False
        time.sleep(0.1)

    def test_scale_to_zero(self):
        from integration import runtime
        runtime._running = True
        _apply_scale(2, self._noop)
        _apply_scale(0, self._noop)
        self.assertEqual(len(get_active_workers()), 0)
        runtime._running = False


# ── Worker crash handling ────────────────────────────────────────


class TestWorkerCrash(RuntimeResetMixin, unittest.TestCase):
    def test_crash_removes_worker_from_active_set(self):
        """A failed standalone worker exits cleanly and is deregistered."""
        crash_event = threading.Event()

        def crashing_fn(_):
            crash_event.set()
            raise RuntimeError("boom")

        from integration import runtime
        runtime._running = True
        start_worker(crashing_fn)
        crash_event.wait(timeout=2)
        time.sleep(0.1)
        runtime._running = False
        self.assertEqual(get_active_workers(), [])

    def test_crash_does_not_stop_other_workers(self):
        """One crashing worker must not kill another."""
        from integration import runtime
        runtime._running = True
        good_barrier = threading.Event()
        start_worker(lambda _: good_barrier.wait(timeout=2))

        def bad_fn(_):
            raise RuntimeError("fail")

        start_worker(bad_fn)
        time.sleep(0.2)
        # Good worker should still be in the active list
        self.assertGreaterEqual(len(get_active_workers()), 1)
        good_barrier.set()
        runtime._running = False
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
            self.assertNotEqual(get_active_workers(), [])
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


# ── Zombie worker cleanup audit ──────────────────────────────────


class TestZombieWorkerCleanup(RuntimeResetMixin, unittest.TestCase):
    """Validate no zombie workers exist after stop/timeout/crash scenarios."""

    def test_timeout_worker_eventually_cleaned_up(self):
        """Worker that times out on stop_worker cleans up once its task ends."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
        # stop_worker times out
        self.assertFalse(stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT))
        self.assertIn(wid, get_active_workers())
        # Unblock the worker so it can exit naturally
        barrier.set()
        time.sleep(0.2)
        # Worker must have cleaned itself up via the finally block
        self.assertNotIn(wid, get_active_workers())
        self.assertNotIn(wid, runtime._stop_requests)

    def test_stop_requests_cleaned_after_timeout_worker_exits(self):
        """_stop_requests has no stale entries after a timed-out worker exits."""
        barrier = threading.Event()
        wid = start_worker(lambda _: barrier.wait(timeout=WORKER_BLOCK_TIMEOUT))
        stop_worker(wid, timeout=INSUFFICIENT_TIMEOUT)
        self.assertIn(wid, runtime._stop_requests)
        barrier.set()
        time.sleep(0.2)
        self.assertEqual(runtime._stop_requests, set())

    def test_rapid_start_stop_no_zombies(self):
        """Rapid start/stop cycles must leave zero workers in registry."""
        for _ in range(10):
            barrier = threading.Event()
            wid = start_worker(lambda _: barrier.wait(timeout=1))
            barrier.set()
            stop_worker(wid, timeout=CLEANUP_TIMEOUT)
        self.assertEqual(get_active_workers(), [])
        self.assertEqual(runtime._stop_requests, set())

    def test_multiple_crashes_no_zombies(self):
        """Multiple crashed workers must all be deregistered."""
        runtime._running = True
        events = []
        for _ in range(5):
            ev = threading.Event()
            events.append(ev)

            def crash_fn(_, e=ev):
                e.set()
                raise RuntimeError("boom")

            start_worker(crash_fn)
        # Wait for all crashes to fire
        for ev in events:
            ev.wait(timeout=2)
        time.sleep(0.2)
        runtime._running = False
        self.assertEqual(get_active_workers(), [])
        self.assertEqual(runtime._stop_requests, set())

    def test_crash_cleans_stop_requests(self):
        """A worker that was asked to stop but crashes has no stale _stop_requests."""
        runtime._running = True
        barrier = threading.Event()

        def delayed_crash(_):
            barrier.wait(timeout=2)
            raise RuntimeError("late crash")

        wid = start_worker(delayed_crash)
        # Mark worker for stop
        with runtime._lock:
            runtime._stop_requests.add(wid)
        barrier.set()
        time.sleep(0.3)
        runtime._running = False
        self.assertNotIn(wid, get_active_workers())
        self.assertNotIn(wid, runtime._stop_requests)

    def test_stop_runtime_timeout_eventual_cleanup(self):
        """Workers from stop() timeout path clean up once they finish."""
        worker_block = threading.Event()
        with patch("integration.runtime.rollout.try_scale_up",
                    return_value=(2, "at_max", [])):
            start(lambda _: worker_block.wait(timeout=WORKER_BLOCK_TIMEOUT),
                  interval=1)
            time.sleep(WARMUP_DELAY)
            # stop() will timeout, leaving workers active
            self.assertFalse(stop(timeout=INSUFFICIENT_TIMEOUT))
            self.assertNotEqual(get_active_workers(), [])
            # Unblock workers so they can exit naturally
            worker_block.set()
            time.sleep(0.5)
            self.assertEqual(get_active_workers(), [])
            self.assertEqual(runtime._stop_requests, set())

    def test_start_worker_thread_failure_no_zombie(self):
        """If thread.start() fails, worker must not linger in _workers."""
        with patch("threading.Thread.start", side_effect=RuntimeError("no resources")):
            with self.assertRaises(RuntimeError):
                start_worker(lambda _: None)
        self.assertEqual(get_active_workers(), [])
        self.assertEqual(runtime._stop_requests, set())

    def test_concurrent_start_stop_no_zombies(self):
        """Concurrent start and stop operations must leave a consistent registry."""
        errors = []

        def worker_task(_):
            time.sleep(0.02)

        def start_stop_cycle():
            try:
                wid = start_worker(worker_task)
                time.sleep(0.01)
                stop_worker(wid, timeout=CLEANUP_TIMEOUT)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start_stop_cycle) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        time.sleep(0.3)
        self.assertEqual(errors, [])
        self.assertEqual(get_active_workers(), [])
        self.assertEqual(runtime._stop_requests, set())


if __name__ == "__main__":
    unittest.main()
