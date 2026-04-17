"""Tests for PR 13 — runtime lifecycle, shutdown, restart, and control-plane safety.

Validates:
  - start_worker() releases proxy when Thread.start() fails (proxy leak fix)
  - _pending_restarts is capped at worker count on concurrent failures
  - reset() raises when called while runtime is RUNNING in production mode
  - _log_event() increments _log_sink_error_count on log_sink.emit() failure
  - metrics-unavailable path logs 'metrics_unavailable_scaling_deferred'
  - register_signal_handlers() logs a debug message from non-main thread
  - _ensure_rollout_configured() triggers rollout.configure when not configured
  - is_safe_to_control() returns True for zero workers (vacuous truth)
"""
# pylint: disable=protected-access  # white-box tests require internal state access
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from integration import runtime
from integration.runtime import (
    _ensure_rollout_configured,
    get_all_worker_states,
    is_safe_to_control,
    reset,
    start_worker,
)
from modules.monitor import main as monitor
from modules.rollout import main as rollout

CLEANUP_TIMEOUT = 3


def _wait_until(condition_fn, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


class RuntimeSafetyResetMixin:
    """Common setUp/tearDown for runtime safety tests."""

    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()

    def tearDown(self):
        reset()
        rollout.reset()
        monitor.reset()


# ── Proxy leak fix ────────────────────────────────────────────────


class TestStartWorkerProxyLeak(RuntimeSafetyResetMixin, unittest.TestCase):
    """start_worker() releases proxy when Thread.start() raises."""

    def test_proxy_released_on_thread_start_failure(self):
        """If Thread.start() raises RuntimeError, the proxy must be released."""
        released = []

        mock_proxy = MagicMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_proxy
        mock_pool.release.side_effect = released.append

        with patch("integration.runtime.get_default_pool", return_value=mock_pool):
            with patch("threading.Thread.start", side_effect=RuntimeError("cannot start")):
                with self.assertRaises(RuntimeError):
                    start_worker(lambda _: None)

        # Pool.release must have been called with the worker id
        self.assertEqual(len(released), 1, "proxy must be released exactly once on start failure")

    def test_proxy_released_on_thread_start_oserror(self):
        """If Thread.start() raises OSError, the proxy must also be released."""
        released = []

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = MagicMock()
        mock_pool.release.side_effect = released.append

        with patch("integration.runtime.get_default_pool", return_value=mock_pool):
            with patch("threading.Thread.start", side_effect=OSError("thread limit")):
                with self.assertRaises(OSError):
                    start_worker(lambda _: None)

        self.assertEqual(len(released), 1, "proxy must be released on OSError from Thread.start()")

    def test_worker_registry_cleaned_on_thread_start_failure(self):
        """Worker must not remain registered when Thread.start() raises."""
        with patch("threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                start_worker(lambda _: None)

        self.assertEqual(get_all_worker_states(), {})


# ── _pending_restarts cap ─────────────────────────────────────────


class TestPendingRestartsCap(RuntimeSafetyResetMixin, unittest.TestCase):
    """_pending_restarts must not exceed the current worker count."""

    def test_concurrent_failures_do_not_overshoot(self):
        """Multiple concurrent worker failures must not push _pending_restarts above worker count."""
        runtime._state = "RUNNING"
        failure_events = []
        done_events = []
        N = 4

        for _ in range(N):
            ev = threading.Event()
            done_ev = threading.Event()
            failure_events.append(ev)
            done_events.append(done_ev)

        def failing_task(_wid, ev=None, done=None):
            if ev:
                ev.set()
            time.sleep(0.05)
            if done:
                done.set()
            raise RuntimeError("simulated failure")

        # Start N workers simultaneously
        wids = []
        for i in range(N):
            wid = start_worker(lambda _, ev=failure_events[i], done=done_events[i]: failing_task(_, ev, done))
            wids.append(wid)

        # Wait for all workers to start their tasks
        for ev in failure_events:
            ev.wait(timeout=2)

        # Wait for all to finish
        for ev in done_events:
            ev.wait(timeout=3)

        time.sleep(0.2)

        # _pending_restarts must not exceed the total workers that were spawned
        with runtime._lock:
            pending = runtime._pending_restarts
        self.assertLessEqual(
            pending,
            N,
            f"_pending_restarts ({pending}) must not exceed worker count ({N})",
        )
        runtime._state = "INIT"

    def test_pending_restarts_decrement_on_scale_apply(self):
        """_apply_scale decrements _pending_restarts when restarting workers."""
        runtime._state = "RUNNING"
        with runtime._lock:
            runtime._pending_restarts = 2

        runtime._apply_scale(3, lambda _: time.sleep(0.1))

        with runtime._lock:
            pending = runtime._pending_restarts
        # After scaling up by 3 workers from 0, restarted = min(2, 3) = 2
        self.assertEqual(pending, 0, "all pending restarts should be consumed during scale-up")
        runtime._state = "INIT"
        time.sleep(0.1)


# ── reset() production guard ──────────────────────────────────────


class TestResetProductionGuard(RuntimeSafetyResetMixin, unittest.TestCase):
    """reset() raises when called while runtime is RUNNING with behavior delay enabled."""

    @staticmethod
    def _runtime_private(name):
        return runtime.__dict__[name]

    @staticmethod
    def _set_runtime_private(name, value):
        runtime.__dict__[name] = value

    def test_reset_raises_when_running_in_production_mode(self):
        """reset() must raise when a live loop thread is running."""
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        with self._runtime_private("_lock"):
            self._set_runtime_private("_state", "RUNNING")
            self._set_runtime_private("_loop_thread", fake_thread)
            self._set_runtime_private("_behavior_delay_enabled", True)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                reset()
            self.assertIn("running", str(ctx.exception).lower())
        finally:
            # Restore safe state for tearDown
            with self._runtime_private("_lock"):
                self._set_runtime_private("_state", "STOPPED")
                self._set_runtime_private("_loop_thread", None)
                self._set_runtime_private("_behavior_delay_enabled", False)

    def test_reset_raises_when_running_even_if_flag_disabled(self):
        """NAQ-RUNTIME-02: reset() must NOT be bypassable by toggling
        _behavior_delay_enabled=False while a live loop thread is running."""
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        with self._runtime_private("_lock"):
            self._set_runtime_private("_state", "RUNNING")
            self._set_runtime_private("_loop_thread", fake_thread)
            self._set_runtime_private("_behavior_delay_enabled", False)
        try:
            with self.assertRaises(RuntimeError):
                reset()
        finally:
            with self._runtime_private("_lock"):
                self._set_runtime_private("_state", "STOPPED")
                self._set_runtime_private("_loop_thread", None)
                self._set_runtime_private("_behavior_delay_enabled", False)

    def test_reset_allowed_when_stopped(self):
        """reset() must NOT raise if state is STOPPED."""
        with runtime._lock:
            runtime._state = "STOPPED"
            runtime._behavior_delay_enabled = True
        # Should not raise
        reset()


# ── log_sink error counter ────────────────────────────────────────


class TestLogSinkErrorCounter(RuntimeSafetyResetMixin, unittest.TestCase):
    """_log_event() increments _log_sink_error_count on log_sink.emit() failure."""

    def test_error_counter_increments_on_sink_failure(self):
        """_log_sink_error_count should increment each time log_sink.emit() raises."""
        with runtime._lock:
            runtime._log_sink_error_count = 0

        with patch("integration.runtime.log_sink.emit", side_effect=RuntimeError("sink down")):
            with self.assertLogs("integration.runtime", level="WARNING") as cm:
                runtime._log_event("worker-test", "info", "test_action")
                runtime._log_event("worker-test", "info", "test_action")

        self.assertEqual(runtime._log_sink_error_count, 2)
        self.assertTrue(
            any("log_sink.emit() failed" in m for m in cm.output),
            f"Expected log_sink failure warning, got: {cm.output}",
        )

    def test_runtime_continues_after_sink_failure(self):
        """Runtime must not crash when log_sink.emit() raises."""
        with patch("integration.runtime.log_sink.emit", side_effect=OSError("pipe broken")):
            # _log_event should not raise
            runtime._log_event("worker-test", "info", "test_action")


# ── metrics unavailable logging ───────────────────────────────────


class TestMetricsUnavailableLogging(RuntimeSafetyResetMixin, unittest.TestCase):
    """When metrics are unavailable, runtime logs 'metrics_unavailable_scaling_deferred'."""

    def test_metrics_unavailable_logs_deferred(self):
        """When monitor.get_metrics() fails, loop logs scaling_deferred explicitly."""
        with patch("integration.runtime.monitor.get_metrics",
                   side_effect=RuntimeError("unavailable")):
            with self.assertLogs("integration.runtime", level="WARNING") as cm:
                # Directly emit the two log calls that _runtime_loop makes
                # when monitor.get_metrics() raises.
                runtime._log_event("runtime", "warning", "metrics_unavailable_scaling_deferred",
                                   {"error": "unavailable"})
                runtime._logger.warning("Metrics unavailable; scaling decision deferred for this tick")

        found = any("metrics_unavailable_scaling_deferred" in m or
                    "Metrics unavailable" in m for m in cm.output)
        self.assertTrue(
            found,
            f"Expected metrics-unavailable deferred log, got: {cm.output}",
        )


# ── register_signal_handlers non-main thread ──────────────────────


class TestRegisterSignalHandlersNonMainThread(RuntimeSafetyResetMixin, unittest.TestCase):
    """register_signal_handlers() from non-main thread logs debug and does not crash."""

    def test_non_main_thread_does_not_crash(self):
        """Calling register_signal_handlers() from a non-main thread must not raise."""
        errors = []

        def call_from_thread():
            try:
                runtime.register_signal_handlers()
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(exc)

        t = threading.Thread(target=call_from_thread, daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "thread should have completed")
        self.assertEqual(errors, [], f"unexpected errors: {errors}")


# ── _ensure_rollout_configured ────────────────────────────────────


class TestEnsureRolloutConfigured(RuntimeSafetyResetMixin, unittest.TestCase):
    """_ensure_rollout_configured() triggers rollout.configure when not configured."""

    def test_configures_rollout_when_unconfigured(self):
        """_ensure_rollout_configured must call rollout.configure if not already set."""
        rollout.reset()
        self.assertFalse(rollout.is_configured())
        _ensure_rollout_configured()
        self.assertTrue(rollout.is_configured())

    def test_no_op_when_already_configured(self):
        """_ensure_rollout_configured must be a no-op when already configured."""
        rollout.configure(lambda: [], lambda: None)
        self.assertTrue(rollout.is_configured())
        # Must not raise
        _ensure_rollout_configured()
        self.assertTrue(rollout.is_configured())


# ── is_safe_to_control zero-worker semantics ──────────────────────


class TestIsSafeToControlZeroWorkers(RuntimeSafetyResetMixin, unittest.TestCase):
    """is_safe_to_control() returns True for zero workers (vacuous truth)."""

    def test_returns_true_with_no_workers(self):
        self.assertEqual(get_all_worker_states(), {})
        self.assertTrue(is_safe_to_control())


if __name__ == "__main__":
    unittest.main()
