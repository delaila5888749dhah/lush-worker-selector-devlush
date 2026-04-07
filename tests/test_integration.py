import unittest
from unittest.mock import MagicMock, patch

from modules.common.exceptions import CycleExhaustedError, SessionFlaggedError
from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import get_current_state, reset_states
from modules.watchdog.main import reset as _reset_watchdog
from integration.orchestrator import (
    handle_outcome,
    initialize_cycle,
    run_cycle,
    run_payment_step,
)


def _make_task(order_queue=None):
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="07",
        exp_year="27",
        cvv="123",
    )
    return WorkerTask(
        recipient_email="test@example.com",
        amount=100,
        primary_card=card,
        order_queue=order_queue or [],
    )


class InitializeCycleTests(unittest.TestCase):
    def setUp(self):
        reset_states()

    def test_registers_all_states(self):
        initialize_cycle()
        from modules.fsm.main import transition_to
        for name in ("ui_lock", "success", "vbv_3ds", "declined"):
            state = transition_to(name)
            self.assertEqual(state.name, name)

    def test_is_idempotent(self):
        initialize_cycle()
        initialize_cycle()
        self.assertIsNone(get_current_state())

    def test_resets_current_state(self):
        initialize_cycle()
        from modules.fsm.main import transition_to
        transition_to("success")
        initialize_cycle()
        self.assertIsNone(get_current_state())

    def test_configures_rollout_with_monitor_callbacks(self):
        with (
            patch("integration.orchestrator.rollout") as mock_rollout,
            patch("integration.orchestrator.monitor") as mock_monitor,
        ):
            initialize_cycle()
        mock_rollout.configure.assert_called_once_with(
            mock_monitor.check_rollback_needed,
            mock_monitor.save_baseline,
        )


class HandleOutcomeTests(unittest.TestCase):
    def test_none_state_returns_retry(self):
        self.assertEqual(handle_outcome(None, []), "retry")

    def test_success_returns_complete(self):
        self.assertEqual(handle_outcome(State("success"), []), "complete")

    def test_declined_with_queue_returns_retry_new_card(self):
        queue = [MagicMock()]
        self.assertEqual(handle_outcome(State("declined"), queue), "retry_new_card")

    def test_declined_empty_queue_returns_retry(self):
        self.assertEqual(handle_outcome(State("declined"), []), "retry")

    def test_ui_lock_returns_retry(self):
        self.assertEqual(handle_outcome(State("ui_lock"), []), "retry")

    def test_vbv_3ds_clears_fields_and_returns_await_3ds(self):
        with patch("integration.orchestrator.cdp") as mock_cdp:
            result = handle_outcome(State("vbv_3ds"), [])
        self.assertEqual(result, "await_3ds")
        mock_cdp.clear_card_fields.assert_called_once()

    def test_vbv_3ds_cdp_failure_still_returns_await_3ds(self):
        """BUG-003: CDP error in clear_card_fields must be swallowed; await_3ds returned."""
        with patch("integration.orchestrator.cdp") as mock_cdp:
            mock_cdp.clear_card_fields.side_effect = RuntimeError("browser crashed")
            result = handle_outcome(State("vbv_3ds"), [])
        self.assertEqual(result, "await_3ds")

    def test_unknown_state_returns_retry(self):
        self.assertEqual(handle_outcome(State("unknown_state"), []), "retry")


class RunPaymentStepTests(unittest.TestCase):
    def setUp(self):
        _reset_watchdog()
        reset_states()

    def test_raises_not_implemented_from_cdp(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.return_value = MagicMock()
            with self.assertRaises(NotImplementedError):
                run_payment_step(_make_task())

    def test_raises_cycle_exhausted_from_billing(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.side_effect = CycleExhaustedError("empty")
            with self.assertRaises(CycleExhaustedError):
                run_payment_step(_make_task())

    def test_zip_code_forwarded_to_select_profile(self):
        with patch("integration.orchestrator.billing") as mock_billing:
            mock_billing.select_profile.side_effect = CycleExhaustedError("empty")
            with self.assertRaises(CycleExhaustedError):
                run_payment_step(_make_task(), zip_code="90210")
        mock_billing.select_profile.assert_called_once_with("90210")



    def test_fill_card_called_with_primary_card(self):
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 25.0
            mock_fsm.get_current_state.return_value = None
            run_payment_step(task)
        mock_cdp.fill_card.assert_called_once_with(task.primary_card)

    def test_raises_session_flagged_on_watchdog_timeout(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task())

    def test_returns_state_and_total(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state.return_value = State("success")
            state, total = run_payment_step(_make_task())
        self.assertEqual(total, 49.99)
        self.assertEqual(state.name, "success")


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        _reset_watchdog()
        reset_states()

    def test_run_cycle_complete_on_success(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 99.0
            mock_fsm.get_current_state.return_value = State("success")
            action, state, total = run_cycle(_make_task())
        self.assertEqual(action, "complete")
        self.assertEqual(total, 99.0)

    def test_run_cycle_retry_when_no_state(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state.return_value = None
            action, state, total = run_cycle(_make_task())
        self.assertEqual(action, "retry")
        self.assertIsNone(state)

    def test_run_cycle_propagates_session_flagged_error(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            with self.assertRaises(SessionFlaggedError):
                run_cycle(_make_task())

    def test_run_cycle_initializes_fsm_before_payment(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 1.0
            mock_fsm.get_current_state.return_value = None
            run_cycle(_make_task())
        mock_fsm.reset_states.assert_called_once()


class WorkerTaskFrozenTests(unittest.TestCase):
    """WorkerTask must be immutable (frozen=True) to prevent accidental mutation."""

    def test_worker_task_is_frozen(self):
        task = _make_task()
        with self.assertRaises(AttributeError):
            task.amount = 200

    def test_worker_task_created_with_correct_values(self):
        task = _make_task()
        self.assertEqual(task.recipient_email, "test@example.com")
        self.assertEqual(task.amount, 100)
        self.assertEqual(task.primary_card.card_number, "4111111111111111")
        self.assertEqual(task.order_queue, [])


class WorkerIdPropagationTests(unittest.TestCase):
    """Verify worker_id is correctly threaded through the orchestrator calls."""

    def test_worker_id_forwarded_to_watchdog(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 10.0
            mock_fsm.get_current_state.return_value = None
            run_payment_step(_make_task(), worker_id="worker-42")
        mock_watchdog.enable_network_monitor.assert_called_once_with("worker-42")
        mock_watchdog.wait_for_total.assert_called_once_with(
            "worker-42", timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
