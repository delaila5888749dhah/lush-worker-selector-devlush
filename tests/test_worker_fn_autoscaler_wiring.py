"""Spec §14.1 — autoscaler.record_failure() wiring inside _worker_fn.

The autoscaler emergency-rollback path must be driven by production worker
failures, not just by the behavior path (``_consecutive_billing_failures``
and ``_pending_restarts``).  These tests assert the wiring inside
``integration.runtime._worker_fn`` and verify ``rollout.force_rollback`` is
idempotent across both code paths.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from integration import runtime
from integration.runtime import (
    get_active_workers,
    reset,
    start_worker,
)
from modules.billing import main as billing
from modules.common.exceptions import CycleExhaustedError
from modules.monitor import main as monitor
from modules.rollout import autoscaler as autoscaler_module
from modules.rollout import main as rollout

CLEANUP_TIMEOUT = 2


class _RuntimeAutoscalerMixin:
    def setUp(self):
        reset()
        rollout.reset()
        monitor.reset()
        autoscaler_module.reset()
        self._billing_pool_dir = tempfile.mkdtemp()
        pool_profile = os.path.join(self._billing_pool_dir, "profiles.txt")
        with open(pool_profile, "w", encoding="utf-8") as handle:
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
        autoscaler_module.reset()

    def _wait_until_worker_exited(self, wid):
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        while time.monotonic() < deadline:
            if wid not in get_active_workers():
                return True
            time.sleep(0.02)
        return wid not in get_active_workers()

    @staticmethod
    def _preload_failure_threshold_minus_one(scaler, worker_id):
        """Set a worker's autoscaler counter to threshold-1."""
        with scaler._lock:  # pylint: disable=protected-access
            scaler._consecutive_failures[worker_id] = (  # pylint: disable=protected-access
                scaler._CONSECUTIVE_FAILURE_THRESHOLD - 1  # pylint: disable=protected-access
            )

    @staticmethod
    def _register_current_thread_worker(worker_id):
        """Register the current thread as the owner of a worker id."""
        with runtime._lock:
            runtime._workers[worker_id] = threading.current_thread()
            runtime._worker_states[worker_id] = "IDLE"

    @staticmethod
    def _clear_worker_registration(worker_id):
        """Remove any explicit worker registration created by a test."""
        with runtime._lock:
            runtime._workers.pop(worker_id, None)
            runtime._worker_states.pop(worker_id, None)
            runtime._stop_requests.discard(worker_id)


class TestWorkerFnRecordsAutoscalerFailure(_RuntimeAutoscalerMixin, unittest.TestCase):
    """``_worker_fn`` must invoke ``autoscaler.record_failure`` for both
    billing (``CycleExhaustedError``) and generic task failures."""

    def test_billing_failure_records_autoscaler_failure(self):
        runtime._state = "RUNNING"
        try:
            scaler = autoscaler_module.get_autoscaler()
            with (
                patch.object(scaler, "_scale_down_worker"),
                patch.object(
                    scaler,
                    "record_failure",
                    wraps=scaler.record_failure,
                ) as record_failure,
            ):
                def billing_fail(_):
                    raise CycleExhaustedError("pool empty")

                wid = start_worker(billing_fail)
                self.assertTrue(self._wait_until_worker_exited(wid))
                record_failure.assert_called_once_with(wid)
                self.assertGreaterEqual(
                    scaler.get_consecutive_failures(wid), 1,
                    "billing failure must increment autoscaler counter for the worker",
                )
        finally:
            runtime._state = "INIT"

    def test_generic_failure_records_autoscaler_failure(self):
        runtime._state = "RUNNING"
        try:
            scaler = autoscaler_module.get_autoscaler()
            with (
                patch.object(scaler, "_scale_down_worker"),
                patch.object(
                    scaler,
                    "record_failure",
                    wraps=scaler.record_failure,
                ) as record_failure,
            ):
                def generic_fail(_):
                    raise RuntimeError("boom")

                wid = start_worker(generic_fail)
                self.assertTrue(self._wait_until_worker_exited(wid))
                record_failure.assert_called_once_with(wid)
                self.assertGreaterEqual(
                    scaler.get_consecutive_failures(wid), 1,
                    "generic failure must increment autoscaler counter for the worker",
                )
        finally:
            runtime._state = "INIT"

    def test_success_resets_autoscaler_failure_counter(self):
        runtime._state = "RUNNING"
        try:
            scaler = autoscaler_module.get_autoscaler()
            ran = threading.Event()

            def succeed(_):
                ran.set()

            with patch.object(
                scaler,
                "record_success",
                wraps=scaler.record_success,
            ) as record_success:
                wid = start_worker(succeed)
                self.assertTrue(ran.wait(timeout=CLEANUP_TIMEOUT))
                # Worker may keep looping; stop it cleanly.
                with runtime._lock:
                    runtime._stop_requests.add(wid)
                self.assertTrue(self._wait_until_worker_exited(wid))
                record_success.assert_called_with(wid)
                self.assertEqual(
                    scaler.get_consecutive_failures(wid), 0,
                    "successful task execution must keep autoscaler counter at 0",
                )
        finally:
            runtime._state = "INIT"


class TestWorkerFnAndBehaviorRollbackIdempotent(_RuntimeAutoscalerMixin, unittest.TestCase):
    """When the autoscaler path (driven by ``_worker_fn`` failures crossing
    the consecutive-failure threshold) and the behavior path (runtime loop
    SCALE_DOWN) both fire in the same scale-up window, ``force_rollback``
    must decrement ``_current_step_index`` exactly once.
    """

    def test_force_rollback_called_once_across_both_paths(self):
        runtime._state = "RUNNING"
        try:
            # Open a scale-up window with two steps available so a single
            # rollback is observable as a 2 -> 1 decrement.
            rollout.configure(
                check_rollback_fn=lambda: [],
                save_baseline_fn=lambda: None,
            )
            rollout.try_scale_up()  # step 0 -> 1
            rollout.try_scale_up()  # step 1 -> 2
            self.assertEqual(rollout.get_current_step_index(), 2)

            scaler = autoscaler_module.get_autoscaler()

            # Pre-load the autoscaler counter to threshold-1 so a single
            # _worker_fn failure crosses the threshold.
            target_wid = "w-target"
            self._preload_failure_threshold_minus_one(scaler, target_wid)

            # Synchronize the behavior-path force_rollback with the
            # autoscaler-path force_rollback so they race.
            barrier = threading.Barrier(2)
            errors = []

            def behavior_path():
                """Simulate runtime loop SCALE_DOWN -> force_rollback()."""
                try:
                    barrier.wait(timeout=CLEANUP_TIMEOUT)
                    rollout.force_rollback("behavior_scale_down")
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(exc)

            def autoscaler_path():
                """Drive the autoscaler from a real _worker_fn failure."""
                try:
                    self._register_current_thread_worker(target_wid)

                    def fail_after_barrier(_):
                        """Block until the behavior path is ready, then fail."""
                        barrier.wait(timeout=CLEANUP_TIMEOUT)
                        raise RuntimeError("boom")

                    runtime._worker_fn(target_wid, fail_after_barrier, None)
                except Exception as exc:  # pylint: disable=broad-except
                    errors.append(exc)
                finally:
                    self._clear_worker_registration(target_wid)

            t_behavior = threading.Thread(target=behavior_path)
            t_autoscaler = threading.Thread(target=autoscaler_path)
            t_behavior.start()
            t_autoscaler.start()
            t_behavior.join(timeout=CLEANUP_TIMEOUT)
            t_autoscaler.join(timeout=CLEANUP_TIMEOUT)

            self.assertEqual(errors, [])
            # Exactly one decrement: 2 -> 1.  The second caller hits the
            # _ROLLBACK_APPLIED guard inside force_rollback.
            self.assertEqual(
                rollout.get_current_step_index(), 1,
                "force_rollback must decrement at most once per scale-up window",
            )
        finally:
            runtime._state = "INIT"

    def test_worker_fn_failure_drives_force_rollback_at_threshold(self):
        """A real _worker_fn failure path increments the autoscaler counter
        and, when threshold is reached, triggers force_rollback exactly once.
        """
        runtime._state = "RUNNING"
        try:
            rollout.configure(
                check_rollback_fn=lambda: [],
                save_baseline_fn=lambda: None,
            )
            rollout.try_scale_up()  # step 0 -> 1
            rollout.try_scale_up()  # step 1 -> 2
            start_step = rollout.get_current_step_index()
            self.assertEqual(start_step, 2)

            scaler = autoscaler_module.get_autoscaler()

            # Force every failure to be attributed to the same worker id
            # so the autoscaler counter accumulates across calls.
            # ``_worker_fn``'s ``finally`` block removes the worker from
            # ``_workers`` on exit, so we re-register the same id between
            # invocations.  This bypasses start_worker (which generates a
            # new id per restart) but exercises the real ``_worker_fn``
            # failure handler that is the subject of Spec §14.1.
            wid = "worker-shared"
            threshold = scaler._CONSECUTIVE_FAILURE_THRESHOLD  # pylint: disable=protected-access

            def _boom(_w):
                raise RuntimeError("boom")

            for _ in range(threshold):
                self._register_current_thread_worker(wid)
                try:
                    runtime._worker_fn(wid, _boom, None)
                finally:
                    self._clear_worker_registration(wid)

            # force_rollback fired exactly once at the threshold crossing.
            end_step = rollout.get_current_step_index()
            self.assertEqual(start_step - end_step, 1,
                             "crossing the consecutive-failure threshold via "
                             "_worker_fn must trigger force_rollback exactly "
                             "once for the current scale-up window")
        finally:
            runtime._state = "INIT"


if __name__ == "__main__":
    unittest.main()
