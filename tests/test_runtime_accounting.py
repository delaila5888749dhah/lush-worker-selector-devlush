"""P0 — runtime accounting must not contaminate success_count.

When ``run_cycle()`` returns a non-complete action (``failure``,
``abort_cycle``, ``await_3ds``, ``retry``, etc.) the runtime must
record the cycle as an error — not a success.

These tests target the contract directly:

  * ``integration.worker_task.task_fn`` raises
    :class:`CycleDidNotCompleteError` on any non-complete action
    (including tuple-form actions like ``("retry_new_card", ...)``).
  * ``integration.runtime._worker_fn`` translates that exception into
    ``monitor.record_error`` + ``autoscaler.record_failure``, never
    ``record_success``.

See issue: P0 success_count contamination on non-complete cycles.
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.common.exceptions import CycleDidNotCompleteError


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

    def test_failure_action_raises(self):
        with self.assertRaises(CycleDidNotCompleteError) as cm:
            self._run_with_action("failure")
        self.assertEqual(cm.exception.action, "failure")

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

    def test_failure_action_records_error(self):
        from modules.monitor import main as monitor
        from modules.rollout.autoscaler import get_autoscaler

        wid = self._run_one_cycle(CycleDidNotCompleteError(action="failure"))
        m = monitor.get_metrics()
        self.assertEqual(m["success_count"], 0)
        self.assertEqual(m["error_count"], 1)
        self.assertEqual(get_autoscaler().get_consecutive_failures(wid), 1)

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


if __name__ == "__main__":
    unittest.main()
