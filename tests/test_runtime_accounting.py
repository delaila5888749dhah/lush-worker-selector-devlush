"""P0 — runtime accounting must not contaminate success_count.

When ``run_cycle()`` returns a non-complete action (``abort_cycle``,
``await_3ds``, ``retry``, ``retry_new_card``) the runtime must record
the cycle as an error — not a success.

These tests target the contract directly:

  * ``integration.worker_task.task_fn`` raises
    :class:`CycleDidNotCompleteError` on any non-complete action
    (including tuple-form actions like ``("retry_new_card", ...)``).
  * ``integration.runtime._worker_fn`` translates that exception into
    ``monitor.record_error``, does not call ``monitor.record_success``,
    and does not call ``autoscaler.record_failure`` again because
    ``run_cycle()`` already accounted the autoscaler outcome for
    non-complete actions. The branch also does not increment
    ``_pending_restarts`` because this is an expected per-cycle
    outcome, not a worker crash.

See issue: P0 success_count contamination on non-complete cycles.
"""
import unittest
from unittest.mock import MagicMock, patch

from integration.cycle_outcome import CycleDidNotCompleteError, normalize_action


def _make_bb_client():
    bb = MagicMock()
    bb.create_profile.return_value = "profile-1"
    bb.launch_profile.return_value = {"webdriver": "ws://127.0.0.1:9222/x"}
    bb.close_profile.return_value = None
    bb.delete_profile.return_value = None
    return bb


def _patches():
    """Common patches for exercising worker_task.task_fn end-to-end."""
    bb_client = _make_bb_client()
    return (
        bb_client,
        [
            patch("integration.worker_task.get_bitbrowser_client", return_value=bb_client),
            patch("integration.worker_task._build_remote_driver", return_value=MagicMock()),
            patch("modules.cdp.driver.GivexDriver", return_value=MagicMock()),
            patch("integration.worker_task.cdp"),
            patch("integration.runtime.probe_cdp_listener_support"),
            patch("integration.worker_task._get_current_ip_best_effort", return_value=None),
            patch("integration.worker_task.maxmind_lookup_zip", return_value=None),
        ],
    )


class TestTaskFnNormalization(unittest.TestCase):
    """task_fn must raise CycleDidNotCompleteError on non-complete actions."""

    def _run_with_action(self, action_return):
        from integration.worker_task import make_task_fn

        _, ctxes = _patches()
        with patch("integration.orchestrator.run_cycle",
                   return_value=(action_return, None, None)):
            for cm in ctxes:
                cm.start()
            try:
                task_fn = make_task_fn(task_source=MagicMock(return_value=MagicMock()))
                return task_fn("w-1")
            finally:
                for cm in ctxes:
                    cm.stop()

    def test_complete_records_success(self):
        """Action ``complete`` must NOT raise — runtime records success."""
        # No exception → runtime records_success path.
        result = self._run_with_action("complete")
        self.assertIsNone(result)

    def test_normalize_accepts_complete_action(self):
        self.assertEqual(normalize_action("complete"), "complete")

    def test_abort_cycle_action_raises(self):
        with self.assertRaises(CycleDidNotCompleteError) as cm:
            self._run_with_action("abort_cycle")
        self.assertEqual(cm.exception.action, "abort_cycle")

    def test_await_3ds_action_raises(self):
        with self.assertRaises(CycleDidNotCompleteError) as cm:
            self._run_with_action("await_3ds")
        self.assertEqual(cm.exception.action, "await_3ds")

    def test_retry_action_raises(self):
        with self.assertRaises(CycleDidNotCompleteError) as cm:
            self._run_with_action("retry")
        self.assertEqual(cm.exception.action, "retry")

    def test_tuple_action_normalized(self):
        """Tuple-form actions like ('retry_new_card', card) must be normalized."""
        sentinel_card = object()
        with self.assertRaises(CycleDidNotCompleteError) as cm:
            self._run_with_action(("retry_new_card", sentinel_card))
        # Normalised to leading string token.
        self.assertEqual(cm.exception.action, "retry_new_card")

    def test_tuple_action_with_non_tuple_token_fails_loud(self):
        with self.assertRaises(ValueError) as cm:
            normalize_action(("retry", object()))
        self.assertEqual(
            str(cm.exception),
            "run_cycle action token does not support tuple form",
        )

    def test_unknown_action_fails_loud(self):
        with self.assertRaises(ValueError) as cm:
            self._run_with_action("completed")
        # Do not echo invalid action tokens; keep malformed contract errors
        # free of arbitrary runtime data.
        self.assertEqual(str(cm.exception), "unknown run_cycle action token")

    def test_malformed_action_type_fails_loud(self):
        class UnstringifiableAction:
            stringified = False

            def __str__(self):
                self.stringified = True
                raise AssertionError("normalize_action must not stringify arbitrary objects")

        action = UnstringifiableAction()
        with self.assertRaises(ValueError) as cm:
            self._run_with_action(action)
        self.assertEqual(str(cm.exception), "malformed run_cycle action type")
        self.assertFalse(action.stringified)


class TestRuntimeAccounting(unittest.TestCase):
    """_worker_fn translates CycleDidNotCompleteError into record_error."""

    def setUp(self):
        from modules.monitor import main as monitor
        monitor.reset()
        from modules.rollout import autoscaler as _autoscaler_mod
        _autoscaler_mod.reset()
        # Reset runtime restart backoff so successive tests don't sleep,
        # disable behaviour delays so the cycle completes immediately,
        # and clear any worker bookkeeping leaked from previous tests.
        from integration import runtime
        runtime.set_behavior_delay_enabled(False)
        with runtime._lock:
            runtime._restart_delay = 0
            runtime._pending_restarts = 0
            runtime._consecutive_billing_failures = 0
            runtime._stop_requests.clear()
            runtime._workers.clear()
            runtime._worker_states.clear()

    def tearDown(self):
        from integration import runtime
        runtime.set_behavior_delay_enabled(True)

    def _run_one_cycle(self, side_effect):
        """Drive exactly one cycle of ``_worker_fn`` via ``start_worker``.

        ``task_fn`` requests a stop on the very same call that produces
        the cycle outcome, so the runtime records success/error exactly
        once and then breaks out of the loop at the next safe-point check.
        """
        import threading
        from integration import runtime
        from modules.monitor import main as monitor

        runtime._state = "RUNNING"
        done_evt = threading.Event()
        original_success = monitor.record_success
        original_error = monitor.record_error

        def _spy_success(*a, **kw):
            original_success(*a, **kw)
            done_evt.set()

        def _spy_error(*a, **kw):
            original_error(*a, **kw)
            done_evt.set()

        def task_fn(worker_id):
            # Mark stop BEFORE raising/returning so the runtime exits the
            # loop immediately after the single accounting decision.
            with runtime._lock:
                runtime._stop_requests.add(worker_id)
            if isinstance(side_effect, BaseException):
                raise side_effect
            if callable(side_effect):
                return side_effect(worker_id)
            return None

        with patch.object(monitor, "record_success", side_effect=_spy_success), \
             patch.object(monitor, "record_error", side_effect=_spy_error):
            wid = runtime.start_worker(task_fn)
            self.assertTrue(
                done_evt.wait(timeout=2),
                "runtime did not record success or error within timeout",
            )
            runtime.stop_worker(wid, timeout=2)
        runtime._state = "INIT"
        return wid

    def test_retry_action_records_error(self):
        from modules.monitor import main as monitor

        self._run_one_cycle(CycleDidNotCompleteError(action="retry"))
        m = monitor.get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertEqual(m["error_count"], 1)

    def test_abort_cycle_records_error(self):
        from modules.monitor import main as monitor
        self._run_one_cycle(CycleDidNotCompleteError(action="abort_cycle"))
        m = monitor.get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertEqual(m["error_count"], 1)

    def test_await_3ds_does_not_record_success(self):
        from modules.monitor import main as monitor
        self._run_one_cycle(CycleDidNotCompleteError(action="await_3ds"))
        m = monitor.get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertGreaterEqual(m["error_count"], 1)

    def test_complete_records_success(self):
        from modules.monitor import main as monitor

        # task_fn returns normally → runtime records success.
        self._run_one_cycle(lambda _wid: None)
        m = monitor.get_metrics()
        self.assertGreaterEqual(m["success_count"], 1)
        self.assertEqual(m["error_count"], 0)

    # ── Regression tests for the runtime accounting contract ─────────────

    def test_runtime_branch_does_not_call_autoscaler_failure(self):
        """Runtime CycleDidNotCompleteError branch must NOT call
        autoscaler.record_failure — run_cycle() already did it."""
        from integration import runtime
        from unittest.mock import MagicMock

        mock_autoscaler = MagicMock()
        with patch.object(runtime, "get_autoscaler", return_value=mock_autoscaler):
            self._run_one_cycle(CycleDidNotCompleteError(action="retry"))
        mock_autoscaler.record_failure.assert_not_called()
        # And it must not have been recorded as success either.
        mock_autoscaler.record_success.assert_not_called()

    def test_non_complete_resets_billing_streak(self):
        """CycleDidNotCompleteError must reset _consecutive_billing_failures."""
        from integration import runtime

        with runtime._lock:
            runtime._consecutive_billing_failures = 1
        self._run_one_cycle(CycleDidNotCompleteError(action="abort_cycle"))
        self.assertEqual(runtime._consecutive_billing_failures, 0)

    def test_non_complete_resets_restart_delay(self):
        """CycleDidNotCompleteError must clear stale crash restart backoff."""
        from integration import runtime

        with runtime._lock:
            runtime._restart_delay = 8
        self._run_one_cycle(CycleDidNotCompleteError(action="abort_cycle"))
        with runtime._lock:
            self.assertEqual(runtime._restart_delay, 0)

    def test_non_complete_does_not_increment_pending_restarts(self):
        """CycleDidNotCompleteError is an expected per-cycle outcome — NOT a
        crash — so it must NOT increment _pending_restarts. This locks down
        the lifecycle distinction from CycleExhaustedError / generic Exception
        handlers, which DO increment _pending_restarts."""
        from integration import runtime

        with runtime._lock:
            runtime._pending_restarts = 0

        self._run_one_cycle(CycleDidNotCompleteError(action="abort_cycle"))

        with runtime._lock:
            self.assertEqual(runtime._pending_restarts, 0)

    def test_real_path_no_double_count(self):
        """End-to-end: worker_task → run_cycle → runtime must increment
        consecutive_failures by exactly 1, not 2.

        Mirrors the real ``run_cycle`` contract by recording autoscaler
        failure before returning a non-complete action; if the runtime
        branch incorrectly double-counted, this would observe 2.
        """
        from integration import runtime
        from modules.monitor import main as monitor
        from modules.rollout.autoscaler import get_autoscaler

        recorded_wid = []

        def fake_task(worker_id):
            # Mirror real run_cycle: autoscaler accounting before "return".
            get_autoscaler().record_failure(worker_id)
            recorded_wid.append(worker_id)
            raise CycleDidNotCompleteError(action="retry")

        import threading
        runtime._state = "RUNNING"
        done_evt = threading.Event()
        original_error = monitor.record_error

        def _spy_error(*a, **kw):
            original_error(*a, **kw)
            done_evt.set()

        def task_fn(worker_id):
            with runtime._lock:
                runtime._stop_requests.add(worker_id)
            fake_task(worker_id)

        with patch.object(monitor, "record_error", side_effect=_spy_error):
            wid = runtime.start_worker(task_fn)
            self.assertTrue(done_evt.wait(timeout=2))
            runtime.stop_worker(wid, timeout=2)
        runtime._state = "INIT"

        # If the runtime branch double-counted, this would be 2.
        self.assertEqual(get_autoscaler().get_consecutive_failures(wid), 1)


if __name__ == "__main__":
    unittest.main()
