import unittest
from unittest.mock import MagicMock, patch

import integration.orchestrator as orch
from integration.session_outcome import SessionLostError
from modules.common.types import CardInfo, State, WorkerTask


def _make_task(task_id: str) -> WorkerTask:
    return WorkerTask(
        recipient_email="a@b.com",
        amount=10,
        primary_card=CardInfo(
            card_number="TEST-CARD-NOT-A-PAN",
            exp_month="12",
            exp_year="2030",
            cvv="XXX",
        ),
        order_queue=(),
        task_id=task_id,
    )


class OrchestratorSessionLostIdempotencyTests(unittest.TestCase):
    def test_session_lost_inside_run_payment_step_marks_unconfirmed(self):
        task = _make_task("inside-run-payment-step")
        fake_store = MagicMock()

        with patch.object(orch, "_get_idempotency_store", return_value=fake_store), \
             patch.object(
                 orch, "_cdp_call_with_timeout",
                 side_effect=[None, None, SessionLostError("invalid_session_id")],
             ), \
             patch.object(orch.cdp, "_get_driver", return_value=MagicMock()), \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch, "_notify_total_from_dom"), \
             patch.object(orch.watchdog, "enable_network_monitor"), \
             patch.object(orch.watchdog, "reset_session"), \
             patch.object(orch.watchdog, "wait_for_total", return_value=49.99), \
             patch.object(orch, "_alerting"):
            with self.assertRaises(SessionLostError):
                orch.run_payment_step(
                    task,
                    zip_code="12345",
                    worker_id="w-inside",
                    _profile=MagicMock(),
                )

        fake_store.mark_submitted.assert_called_once_with("inside-run-payment-step")
        fake_store.mark_unconfirmed.assert_called_once_with(
            "inside-run-payment-step",
            ttl_seconds=orch._UNCONFIRMED_TTL_SECONDS,
        )

    def test_session_lost_in_handle_outcome_marks_unconfirmed_when_submitted(self):
        task = _make_task("outside-run-payment-step")
        fake_store = MagicMock()
        fake_store.is_duplicate.return_value = False
        fake_store.is_submitted.return_value = True

        with patch.object(orch, "_get_idempotency_store", return_value=fake_store), \
             patch.object(orch, "_select_profile_with_audit", return_value=MagicMock()), \
             patch.object(orch, "initialize_cycle"), \
             patch.object(orch, "run_payment_step", return_value=(State("vbv_3ds"), None)), \
             patch.object(
                 orch, "handle_outcome",
                 side_effect=SessionLostError("session_probe_failed_pre_vbv"),
             ), \
             patch.object(orch.watchdog, "reset_session") as mock_reset_session, \
             patch.object(orch.cdp, "unregister_driver"), \
             patch.object(orch.fsm, "cleanup_worker"):
            with self.assertRaises(SessionLostError):
                orch.run_cycle(task, zip_code="12345", worker_id="w-outside")

        fake_store.mark_unconfirmed.assert_called_once_with(
            "outside-run-payment-step",
            ttl_seconds=orch._UNCONFIRMED_TTL_SECONDS,
        )
        mock_reset_session.assert_called_once_with("w-outside")

    def test_session_lost_in_handle_outcome_skips_unconfirmed_without_submitted_checkpoint(self):
        task = _make_task("outside-not-submitted")
        fake_store = MagicMock()
        fake_store.is_duplicate.return_value = False
        fake_store.is_submitted.return_value = False

        with patch.object(orch, "_get_idempotency_store", return_value=fake_store), \
             patch.object(orch, "_select_profile_with_audit", return_value=MagicMock()), \
             patch.object(orch, "initialize_cycle"), \
             patch.object(orch, "run_payment_step", return_value=(State("vbv_3ds"), None)), \
             patch.object(
                 orch, "handle_outcome",
                 side_effect=SessionLostError("session_probe_failed_pre_vbv"),
             ), \
             patch.object(orch.watchdog, "reset_session") as mock_reset_session, \
             patch.object(orch.cdp, "unregister_driver"), \
             patch.object(orch.fsm, "cleanup_worker"):
            with self.assertRaises(SessionLostError):
                orch.run_cycle(task, zip_code="12345", worker_id="w-outside")

        fake_store.mark_unconfirmed.assert_not_called()
        mock_reset_session.assert_called_once_with("w-outside")


if __name__ == "__main__":
    unittest.main()
