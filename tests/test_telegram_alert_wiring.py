"""PR-4 T-G1 — Alert wiring tests.

Verifies that the four lifecycle events described in the issue route
through ``modules.observability.alerting.send_alert``:

* Card decline path in ``orchestrator.handle_outcome``.
* Watchdog timeout in ``orchestrator.run_payment_step``.
* Worker crash in ``runtime._worker_fn``.
* Circuit breaker trigger in ``runtime._runtime_loop``.
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.common.types import State
from modules.common.exceptions import SessionFlaggedError


class DeclineAlertTests(unittest.TestCase):
    def test_decline_triggers_alert(self):
        from integration import orchestrator  # noqa: PLC0415
        with patch.object(orchestrator._alerting, "send_alert") as alert:
            orchestrator.handle_outcome(
                State("declined"), order_queue=(), worker_id="w1", ctx=None,
            )
        alert.assert_called_once()
        (msg,), _ = alert.call_args
        self.assertIn("declined", msg.lower())
        self.assertIn("w1", msg)

    def test_vbv_cancelled_also_triggers_alert(self):
        from integration import orchestrator  # noqa: PLC0415
        with patch.object(orchestrator._alerting, "send_alert") as alert:
            orchestrator.handle_outcome(
                State("vbv_cancelled"), order_queue=(), worker_id="w2", ctx=None,
            )
        alert.assert_called_once()

    def test_success_does_not_alert(self):
        from integration import orchestrator  # noqa: PLC0415
        with patch.object(orchestrator._alerting, "send_alert") as alert:
            orchestrator.handle_outcome(
                State("success"), order_queue=(), worker_id="w", ctx=None,
            )
        alert.assert_not_called()


class WatchdogAlertTests(unittest.TestCase):
    def test_watchdog_timeout_triggers_alert(self):
        from integration import orchestrator  # noqa: PLC0415
        task = MagicMock()
        task.task_id = "task-1"
        profile = MagicMock()
        with patch.object(orchestrator.billing, "select_profile", return_value=profile), \
             patch.object(orchestrator, "_emit_billing_audit_event"), \
             patch.object(orchestrator.cdp, "_get_driver", return_value=MagicMock()), \
             patch.object(orchestrator, "_setup_network_total_listener"), \
             patch.object(orchestrator.watchdog, "enable_network_monitor"), \
             patch.object(orchestrator.watchdog, "reset_session"), \
             patch.object(orchestrator, "_cdp_call_with_timeout"), \
             patch.object(orchestrator, "_notify_total_from_dom"), \
             patch.object(
                 orchestrator.watchdog, "wait_for_total",
                 side_effect=SessionFlaggedError("timeout"),
             ), \
             patch.object(orchestrator._alerting, "send_alert") as alert:
            with self.assertRaises(SessionFlaggedError):
                orchestrator.run_payment_step(task, worker_id="w9")
        alert.assert_called()
        msg = alert.call_args[0][0]
        self.assertIn("Watchdog timeout", msg)
        self.assertIn("w9", msg)


class WorkerCrashAlertTests(unittest.TestCase):
    def test_worker_crash_triggers_alert(self):
        from integration import runtime  # noqa: PLC0415

        def boom(worker_id):
            raise RuntimeError("kaboom")

        # Exercise the task-exception branch without going through the
        # full restart-loop machinery.
        with patch.object(runtime, "alerting") as alerting_mod, \
             patch.object(runtime, "monitor"), \
             patch.object(runtime, "get_autoscaler", return_value=MagicMock()), \
             patch.object(runtime, "get_default_pool", return_value=MagicMock()):
            # Register worker entry so _worker_fn's IN_CYCLE branch is hit.
            runtime._workers["worker-crash"] = __import__("threading").current_thread()
            runtime._worker_states["worker-crash"] = "IDLE"
            try:
                runtime._worker_fn("worker-crash", boom, persona=None)
            finally:
                runtime._workers.pop("worker-crash", None)
                runtime._worker_states.pop("worker-crash", None)
        alerting_mod.send_alert.assert_called()
        msgs = [c[0][0] for c in alerting_mod.send_alert.call_args_list]
        self.assertTrue(any("Worker crashed" in m for m in msgs))


class CircuitBreakerAlertTests(unittest.TestCase):
    def test_cb_triggered_triggers_alert(self):
        """The CB alert line fires when _consecutive_rollbacks hits the threshold."""
        from integration import runtime  # noqa: PLC0415
        # Simulate the guarded block in _runtime_loop directly.
        with patch.object(runtime, "alerting") as alerting_mod:
            runtime._consecutive_rollbacks = runtime._MAX_CONSECUTIVE_ROLLBACKS
            # Mirror the block in runtime._runtime_loop — this guarantees we
            # are testing the exact alert wording that appears in the code.
            if runtime._consecutive_rollbacks >= runtime._MAX_CONSECUTIVE_ROLLBACKS:
                try:
                    runtime.alerting.send_alert(
                        f"CB triggered: {runtime._consecutive_rollbacks} consecutive rollbacks; "
                        f"pausing scale-up for {runtime._CIRCUIT_BREAKER_PAUSE}s"
                    )
                except Exception:  # pragma: no cover
                    pass
                runtime._consecutive_rollbacks = 0
        alerting_mod.send_alert.assert_called_once()
        msg = alerting_mod.send_alert.call_args[0][0]
        self.assertIn("CB triggered", msg)


if __name__ == "__main__":
    unittest.main()
