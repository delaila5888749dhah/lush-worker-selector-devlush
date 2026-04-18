"""PR-05 tests: orchestrator full purchase sequence (F-02) and idempotency ordering (U-07).

Covers:
  F-02 — orchestrator drives the complete Givex purchase sequence, not just fill.
  U-07 — mark_submitted (idempotency checkpoint) is persisted BEFORE the irreversible
          submit_purchase action; crash between those two must not double-charge.

Test categories:
  FullSequenceCallOrderTests  — unit: cdp.run_preflight_and_fill → mark_submitted →
                                       cdp.submit_purchase call order
  ExceptionRoutingTests       — unit: exceptions before/after mark_submitted produce
                                       correct log messages; mark_submitted call state
  IdempotencyCrashTests       — unit: simulated crash after mark_submitted / before
                                       submit blocks re-execution (no double-charge)
  L3IntegrationTests          — integration (stub driver): full orchestrator-driver
                                       flow exercised; observability assertions
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch

from modules.common.exceptions import SessionFlaggedError
from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry
from modules.watchdog.main import reset as _reset_watchdog

from integration.orchestrator import (
    _FileIdempotencyStore,
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _submitted_task_ids,
    handle_outcome,
    run_cycle,
    run_payment_step,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_task(order_queue=None):
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="07",
        exp_year="27",
        cvv="123",
    )
    return WorkerTask(
        recipient_email="buyer@example.com",
        amount=50,
        primary_card=card,
        order_queue=tuple(order_queue) if order_queue else (),
    )


def _clear_idempotency():
    with _idempotency_lock:
        _completed_task_ids.clear()
        _in_flight_task_ids.clear()
        _submitted_task_ids.clear()
    with _network_listener_lock:
        _notified_workers_this_cycle.clear()


# ── F-02: Full sequence call order ─────────────────────────────────────────────

class FullSequenceCallOrderTests(unittest.TestCase):
    """run_payment_step must call run_preflight_and_fill → mark_submitted → submit_purchase."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("seq-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("seq-worker")

    def test_run_preflight_and_fill_called_before_submit(self):
        """cdp.run_preflight_and_fill must be called before cdp.submit_purchase."""
        task = _make_task()
        call_order = []

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.side_effect = lambda *a, **kw: call_order.append("prefill")
            mock_cdp.submit_purchase.side_effect = lambda *a, **kw: call_order.append("submit")
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="seq-worker")

        self.assertIn("prefill", call_order)
        self.assertIn("submit", call_order)
        self.assertLess(
            call_order.index("prefill"),
            call_order.index("submit"),
            "run_preflight_and_fill must be called before submit_purchase",
        )

    def test_mark_submitted_called_between_prefill_and_submit(self):
        """mark_submitted must be called AFTER run_preflight_and_fill and BEFORE submit_purchase."""
        task = _make_task()
        call_order = []

        store_mock = MagicMock()

        def record_prefill(*a, **kw):
            call_order.append("prefill")

        def record_submit(*a, **kw):
            call_order.append("submit")

        store_mock.mark_submitted.side_effect = lambda tid: call_order.append("mark_submitted")

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.side_effect = record_prefill
            mock_cdp.submit_purchase.side_effect = record_submit
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="seq-worker")

        self.assertEqual(
            call_order, ["prefill", "mark_submitted", "submit"],
            f"Expected prefill → mark_submitted → submit, got: {call_order}",
        )

    def test_fill_payment_and_billing_not_called_directly(self):
        """Orchestrator must not call cdp.fill_payment_and_billing directly (F-02 refactor)."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="seq-worker")

        mock_cdp.fill_payment_and_billing.assert_not_called()

    def test_run_preflight_and_fill_receives_task_and_profile(self):
        """cdp.run_preflight_and_fill must receive the task and profile arguments."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            profile = MagicMock()
            mock_billing.select_profile.return_value = profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="seq-worker")

        args, kwargs = mock_cdp.run_preflight_and_fill.call_args
        self.assertIs(args[0], task)
        self.assertIs(args[1], profile)
        self.assertEqual(kwargs.get("worker_id"), "seq-worker")

    def test_submit_purchase_receives_worker_id(self):
        """cdp.submit_purchase must receive the correct worker_id."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="seq-worker")

        _, kwargs = mock_cdp.submit_purchase.call_args
        self.assertEqual(kwargs.get("worker_id"), "seq-worker")


# ── Exception routing ─────────────────────────────────────────────────────────

class ExceptionRoutingTests(unittest.TestCase):
    """Exceptions before/after mark_submitted must produce distinct log messages."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("exc-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("exc-worker")

    def test_exception_in_prefill_logs_before_submission(self):
        """SessionFlaggedError from run_preflight_and_fill must log 'BEFORE payment submission'."""
        task = _make_task()
        log_messages = []

        def capture_error(fmt, *args, **kwargs):
            log_messages.append(fmt % args if args else fmt)

        store_mock = MagicMock()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog"),
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.side_effect = SessionFlaggedError("geo failed")
            mock_logger.error.side_effect = capture_error

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(task, worker_id="exc-worker")

        pre_msgs = [m for m in log_messages if "BEFORE payment submission" in m]
        self.assertTrue(
            len(pre_msgs) >= 1,
            f"Expected 'BEFORE payment submission' log, got: {log_messages}",
        )
        store_mock.mark_submitted.assert_not_called()

    def test_exception_in_submit_logs_after_submission(self):
        """SessionFlaggedError from cdp.submit_purchase must log 'AFTER payment submission'."""
        task = _make_task()
        log_messages = []

        def capture_error(fmt, *args, **kwargs):
            log_messages.append(fmt % args if args else fmt)

        store_mock = MagicMock()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog"),
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.return_value = None  # succeeds
            mock_cdp.submit_purchase.side_effect = SessionFlaggedError("click failed")
            mock_logger.error.side_effect = capture_error

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(task, worker_id="exc-worker")

        post_msgs = [m for m in log_messages if "AFTER payment submission" in m]
        self.assertTrue(
            len(post_msgs) >= 1,
            f"Expected 'AFTER payment submission' log, got: {log_messages}",
        )
        store_mock.mark_submitted.assert_called_once_with(task.task_id)

    def test_watchdog_timeout_after_submit_logs_after_submission(self):
        """SessionFlaggedError from watchdog must log 'AFTER payment submission'."""
        task = _make_task()
        log_messages = []

        def capture_error(fmt, *args, **kwargs):
            log_messages.append(fmt % args if args else fmt)

        store_mock = MagicMock()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.return_value = None
            mock_cdp.submit_purchase.return_value = None
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            mock_logger.error.side_effect = capture_error

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(task, worker_id="exc-worker")

        post_msgs = [m for m in log_messages if "AFTER payment submission" in m]
        self.assertTrue(
            len(post_msgs) >= 1,
            f"Expected 'AFTER payment submission' log after watchdog timeout, got: {log_messages}",
        )
        store_mock.mark_submitted.assert_called_once_with(task.task_id)


# ── U-07: Idempotency crash simulation ────────────────────────────────────────

class IdempotencyCrashTests(unittest.TestCase):
    """Crash simulation: mark_submitted before submit prevents double-charge on restart."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("crash-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("crash-worker")

    def test_submitted_task_blocks_reexecution_simulating_crash_before_submit(self):
        """Task marked submitted (crash before submit) must not re-execute on restart."""
        task = _make_task()
        # Simulate: process marked task as submitted then crashed before submit_purchase.
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp"),
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            action, state, total = run_cycle(task, worker_id="crash-worker")

        # run_cycle returns early ("complete", None, None) — billing never called
        self.assertEqual(action, "complete")
        self.assertIsNone(state)
        self.assertIsNone(total)
        mock_billing.select_profile.assert_not_called()

    def test_mark_submitted_persisted_before_submit_purchase_is_called(self):
        """mark_submitted must be persisted before cdp.submit_purchase executes.

        This ensures that if the process crashes after mark_submitted but before
        submit_purchase, the task_id is in the submitted state and re-execution
        is blocked (preventing a double-charge attempt).
        """
        task = _make_task()
        mark_submitted_happened = threading.Event()
        submit_called_after_mark = threading.Event()

        store_mock = MagicMock()

        def mark_submitted_side_effect(tid):
            mark_submitted_happened.set()

        def submit_side_effect(**kw):
            if mark_submitted_happened.is_set():
                submit_called_after_mark.set()

        store_mock.mark_submitted.side_effect = mark_submitted_side_effect

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.return_value = None
            mock_cdp.submit_purchase.side_effect = submit_side_effect
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="crash-worker")

        self.assertTrue(
            mark_submitted_happened.is_set(),
            "mark_submitted must be called before submit_purchase",
        )
        self.assertTrue(
            submit_called_after_mark.is_set(),
            "submit_purchase must be called after mark_submitted",
        )

    def test_no_double_charge_on_restart_after_submitted_state(self):
        """restart with submitted task_id must not invoke billing or CDP."""
        task = _make_task()
        with _idempotency_lock:
            _submitted_task_ids[task.task_id] = time.monotonic()

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_cycle(task, worker_id="crash-worker")

        mock_cdp.run_preflight_and_fill.assert_not_called()
        mock_cdp.submit_purchase.assert_not_called()
        mock_billing.select_profile.assert_not_called()


# ── L3 Integration: stub driver, full orchestrator-driver flow ────────────────

class L3IntegrationStubTests(unittest.TestCase):
    """L3 integration tests: stub Givex driver, assert full flow is exercised.

    These tests use a fake driver (stub) registered via cdp.register_driver
    to verify that the orchestrator invokes the complete purchase sequence.
    No real browser or network is used.
    """

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("l3-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("l3-worker")
        # Clean up driver registry
        import modules.cdp.main as cdp_main
        cdp_main.unregister_driver("l3-worker")

    def _make_stub_driver(self, page_state="success"):
        """Build a minimal stub driver that records method calls and returns safe values."""
        stub = MagicMock()
        stub.execute_script.return_value = "50.00"
        stub.execute_cdp_cmd.return_value = None
        return stub

    def test_full_sequence_methods_invoked_on_driver(self):
        """All purchase-sequence methods must be invoked on the registered driver."""
        import modules.cdp.main as cdp_main
        from modules.fsm.main import transition_for_worker

        stub = self._make_stub_driver()
        cdp_main.register_driver("l3-worker", stub)

        # We drive the sequence through cdp module functions (not the driver directly)
        # to verify the cdp.main delegation layer works end-to-end.
        profile = MagicMock()
        profile.email = "billing@example.com"
        profile.first_name = "Alice"
        profile.last_name = "Smith"
        profile.address = "123 Main St"
        profile.country = "US"
        profile.state = "CA"
        profile.city = "Los Angeles"
        profile.zip_code = "90001"
        profile.phone = None

        task = _make_task()

        cdp_main.run_preflight_and_fill(task, profile, "l3-worker")
        stub.preflight_geo_check.assert_called_once()
        stub.navigate_to_egift.assert_called_once()
        stub.fill_egift_form.assert_called_once_with(task, profile)
        stub.add_to_cart_and_checkout.assert_called_once()
        stub.select_guest_checkout.assert_called_once_with(profile.email)
        stub.fill_payment_and_billing.assert_called_once_with(task.primary_card, profile)

        cdp_main.submit_purchase("l3-worker")
        stub.submit_purchase.assert_called_once()

    def test_run_full_purchase_flow_convenience_wrapper(self):
        """run_full_purchase_flow must delegate to driver.run_full_cycle."""
        import modules.cdp.main as cdp_main

        stub = self._make_stub_driver()
        stub.run_full_cycle.return_value = "success"
        cdp_main.register_driver("l3-worker", stub)

        profile = MagicMock()
        profile.email = "billing@example.com"
        task = _make_task()

        result = cdp_main.run_full_purchase_flow(task, profile, "l3-worker")
        stub.run_full_cycle.assert_called_once_with(task, profile)
        self.assertEqual(result, "success")

    def test_l3_orchestrator_run_payment_step_success_path(self):
        """run_payment_step with a full stub driver must return (state, total)."""
        import modules.cdp.main as cdp_main

        stub = self._make_stub_driver()
        stub.execute_script.return_value = "50.00"
        cdp_main.register_driver("l3-worker", stub)

        task = _make_task()

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            profile = MagicMock()
            profile.email = "billing@example.com"
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            state, total = run_payment_step(task, worker_id="l3-worker")

        stub.preflight_geo_check.assert_called_once()
        stub.navigate_to_egift.assert_called_once()
        stub.submit_purchase.assert_called_once()
        self.assertIsNotNone(state)
        self.assertEqual(total, 50.0)

    def test_l3_orchestrator_decline_path(self):
        """run_cycle with declined state returns 'retry' or 'retry_new_card'."""
        import modules.cdp.main as cdp_main

        stub = self._make_stub_driver()
        cdp_main.register_driver("l3-worker", stub)

        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            profile = MagicMock()
            profile.email = "billing@example.com"
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("declined")

            action, state, total = run_cycle(task, worker_id="l3-worker")

        self.assertIn(action, ("retry", "retry_new_card"))

    def test_l3_observability_mark_submitted_called_before_submit(self):
        """run_payment_step must call mark_submitted before submit_purchase (observability)."""
        import modules.cdp.main as cdp_main

        stub = self._make_stub_driver()
        cdp_main.register_driver("l3-worker", stub)

        task = _make_task()
        call_order = []

        original_submit = stub.submit_purchase

        def record_submit():
            call_order.append("submit_purchase")

        stub.submit_purchase.side_effect = record_submit

        store_mock = MagicMock()

        def record_mark(tid):
            call_order.append("mark_submitted")

        store_mock.mark_submitted.side_effect = record_mark

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._get_idempotency_store", return_value=store_mock),
        ):
            profile = MagicMock()
            profile.email = "billing@example.com"
            mock_billing.select_profile.return_value = profile
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="l3-worker")

        self.assertIn("mark_submitted", call_order)
        self.assertIn("submit_purchase", call_order)
        self.assertLess(
            call_order.index("mark_submitted"),
            call_order.index("submit_purchase"),
            f"mark_submitted must precede submit_purchase; got: {call_order}",
        )


if __name__ == "__main__":
    unittest.main()
