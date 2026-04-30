"""Phase A reorder fix — run_payment_step order and failure isolation tests.

Verifies the new INV-PAYMENT-01 compliant execution order introduced to fix
the ``ALLOW_DOM_ONLY_WATCHDOG=1`` deadlock (DOM polling on ``about:blank``):

  1. _setup_network_total_listener  (arms DOM polling)
  2. run_pre_card_checkout_prepare  (navigate/form/cart/guest — no card data)
  3. watchdog.wait_for_total        (Phase A — blocks on real Givex page)
  4. set_expected_total
  5. watchdog.enable_network_monitor (re-arm for Phase C)
  6. run_payment_card_fill           (types card/billing fields — INV-PAYMENT-01 gate)
  7. mark_submitted
  8. submit_purchase
  9. Phase C wait_for_total

Tests
-----
  OrderTest          — strict call-order invariant for the full happy path.
  PhaseATimeoutTest  — Phase A timeout aborts before card fill.
  PrepareFailureTest — pre-card prepare failure aborts before Phase A wait/fill.
  BackwardCompatTest — cdp.run_preflight_and_fill alias still invokes both new functions.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from integration.orchestrator import run_payment_step
from modules.common.exceptions import SessionFlaggedError
from modules.common.types import CardInfo, State, WorkerTask


def _make_task():
    return WorkerTask(
        recipient_email="buyer@example.com",
        amount=50,
        primary_card=CardInfo(
            card_number="4111111111111111",
            exp_month="01",
            exp_year="2030",
            cvv="123",
        ),
        order_queue=(),
    )


# ── Helper context manager ────────────────────────────────────────────────────

def _std_patch():
    """Return the standard set of patches needed by most tests in this module."""
    return (
        patch("integration.orchestrator.billing"),
        patch("integration.orchestrator.cdp"),
        patch("integration.orchestrator.watchdog"),
        patch("integration.orchestrator.fsm"),
    )


# ── 1. Order test ─────────────────────────────────────────────────────────────

class OrderTest(unittest.TestCase):
    """run_payment_step must call steps in strict order per the Phase A reorder spec."""

    def test_strict_call_order(self):
        """Full happy-path order:
        _setup_network_total_listener → run_pre_card_checkout_prepare →
        wait_for_total (Phase A) → enable_network_monitor (re-arm) →
        run_payment_card_fill → mark_submitted → submit_purchase.
        """
        call_order = []

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._setup_network_total_listener",
                  side_effect=lambda *a, **kw: call_order.append("setup_listener")),
            patch("integration.orchestrator._get_idempotency_store") as mock_store_factory,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_pre_card_checkout_prepare.side_effect = (
                lambda *a, **kw: call_order.append("pre_card_prepare")
            )
            mock_cdp.run_payment_card_fill.side_effect = (
                lambda *a, **kw: call_order.append("card_fill")
            )
            mock_cdp.submit_purchase.side_effect = (
                lambda *a, **kw: call_order.append("submit")
            )
            mock_watchdog.wait_for_total.side_effect = (
                lambda *a, **kw: (call_order.append("wait_for_total"), 49.99)[1]
            )
            mock_watchdog.enable_network_monitor.side_effect = (
                lambda *a, **kw: call_order.append("enable_monitor")
            )
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            store = MagicMock()
            store.mark_submitted.side_effect = (
                lambda tid: call_order.append("mark_submitted")
            )
            mock_store_factory.return_value = store

            run_payment_step(_make_task(), worker_id="order-test-worker")

        # setup_listener must come before pre_card_prepare
        self.assertLess(
            call_order.index("setup_listener"),
            call_order.index("pre_card_prepare"),
            f"setup_listener must precede pre_card_prepare: {call_order}",
        )
        # pre_card_prepare must come before Phase A wait
        self.assertLess(
            call_order.index("pre_card_prepare"),
            call_order.index("wait_for_total"),
            f"pre_card_prepare must precede wait_for_total: {call_order}",
        )
        # Phase A wait must come before card fill
        self.assertLess(
            call_order.index("wait_for_total"),
            call_order.index("card_fill"),
            f"wait_for_total (Phase A) must precede card_fill: {call_order}",
        )
        # card fill must come before mark_submitted
        self.assertLess(
            call_order.index("card_fill"),
            call_order.index("mark_submitted"),
            f"card_fill must precede mark_submitted: {call_order}",
        )
        # mark_submitted must come before submit
        self.assertLess(
            call_order.index("mark_submitted"),
            call_order.index("submit"),
            f"mark_submitted must precede submit_purchase: {call_order}",
        )
        # re-arm enable_monitor must come after Phase A wait
        rearm_indices = [i for i, v in enumerate(call_order) if v == "enable_monitor"]
        # At least one enable_monitor after wait_for_total
        self.assertTrue(
            any(idx > call_order.index("wait_for_total") for idx in rearm_indices),
            f"enable_network_monitor must be re-armed after Phase A wait: {call_order}",
        )


# ── 2. Phase A timeout AFTER prepare ─────────────────────────────────────────

class PhaseATimeoutTest(unittest.TestCase):
    """Phase A timeout must abort before any card field is typed."""

    def test_phase_a_timeout_aborts_before_card_fill(self):
        """If wait_for_total raises SessionFlaggedError, run_payment_card_fill,
        mark_submitted, and submit_purchase must NOT be called.
        run_pre_card_checkout_prepare may (and must) have been called once.
        """
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._get_idempotency_store") as mock_store_factory,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError(
                "Timeout (20.0s) waiting for total amount"
            )
            store = MagicMock()
            mock_store_factory.return_value = store

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(), worker_id="phase-a-timeout-worker")

        # pre_card_prepare must have been called (navigation precedes Phase A)
        mock_cdp.run_pre_card_checkout_prepare.assert_called_once()
        # card fill, mark_submitted, and submit must NOT be called
        mock_cdp.run_payment_card_fill.assert_not_called()
        store.mark_submitted.assert_not_called()
        mock_cdp.submit_purchase.assert_not_called()

    def test_phase_a_timeout_logs_before_fill_message(self):
        """Phase A timeout must log a message containing 'BEFORE fill' so that
        existing log-grep alert patterns continue to fire."""
        warning_messages = []

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._logger") as mock_logger,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            mock_logger.warning.side_effect = (
                lambda fmt, *a, **kw: warning_messages.append(fmt % a if a else fmt)
            )

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(), worker_id="log-test-worker")

        before_fill_msgs = [m for m in warning_messages if "BEFORE fill" in m]
        self.assertTrue(
            len(before_fill_msgs) >= 1,
            f"Expected 'BEFORE fill' warning message; got: {warning_messages}",
        )


# ── 3. Pre-card prepare failure ───────────────────────────────────────────────

class PrepareFailureTest(unittest.TestCase):
    """Failure in run_pre_card_checkout_prepare must abort before Phase A wait."""

    def test_prepare_failure_aborts_before_phase_a_wait(self):
        """If run_pre_card_checkout_prepare raises, wait_for_total (Phase A),
        run_payment_card_fill, mark_submitted, and submit_purchase must NOT be called.
        """
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._get_idempotency_store") as mock_store_factory,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            # Use SessionFlaggedError to avoid incrementing the global orphaned_threads counter
            # (TimeoutError is an alias of concurrent.futures.TimeoutError in Python 3.11+).
            mock_cdp.run_pre_card_checkout_prepare.side_effect = SessionFlaggedError(
                "navigate_to_egift failed during prepare"
            )
            store = MagicMock()
            mock_store_factory.return_value = store

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(), worker_id="prepare-fail-worker")

        # Phase A wait must not have been called
        mock_watchdog.wait_for_total.assert_not_called()
        # card fill, mark_submitted, and submit must not be called
        mock_cdp.run_payment_card_fill.assert_not_called()
        store.mark_submitted.assert_not_called()
        mock_cdp.submit_purchase.assert_not_called()

    def test_prepare_failure_stops_dom_polling(self):
        """Pre-card prepare failure must stop the Phase A DOM polling thread.

        Otherwise the polling thread armed by ``_setup_network_total_listener``
        keeps querying the (still-stalled) page until its own deadline expires,
        wasting cycle time and leaking a background thread per failed attempt.
        """
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog"),
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._stop_phase_a_dom_polling") as mock_stop,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_pre_card_checkout_prepare.side_effect = SessionFlaggedError(
                "prepare failed"
            )

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(), worker_id="dom-cleanup-worker")

        mock_stop.assert_called_with("dom-cleanup-worker")


# ── 4. Backward-compat alias ──────────────────────────────────────────────────

class BackwardCompatAliasTest(unittest.TestCase):
    """cdp.run_preflight_and_fill (backward-compat alias) must call both new functions."""

    def test_run_preflight_and_fill_calls_both_new_functions(self):
        """Calling cdp.run_preflight_and_fill must invoke both
        driver.run_pre_card_checkout_prepare and driver.run_payment_card_fill
        on the registered driver.
        """
        import modules.cdp.main as cdp_main

        stub_driver = MagicMock()
        stub_driver._geo_checked_this_cycle = False
        # Provide email to pass validation inside run_pre_card_checkout_prepare
        billing_profile = MagicMock()
        billing_profile.email = "test@example.com"

        worker_id = "compat-alias-worker"
        cdp_main.register_driver(worker_id, stub_driver)
        try:
            task = _make_task()
            cdp_main.run_preflight_and_fill(task, billing_profile, worker_id=worker_id)
        finally:
            cdp_main.unregister_driver(worker_id)

        stub_driver.run_pre_card_checkout_prepare.assert_called_once_with(
            task, billing_profile
        )
        stub_driver.run_payment_card_fill.assert_called_once_with(
            task.primary_card, billing_profile
        )

    def test_run_preflight_and_fill_orchestrator_does_not_call_it(self):
        """The orchestrator itself must NOT call run_preflight_and_fill —
        it must use the two split functions (run_pre_card_checkout_prepare and
        run_payment_card_fill) directly so that Phase A is waited between them.
        """
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.return_value = 49.99
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(_make_task(), worker_id="no-alias-worker")

        # Orchestrator must NOT call run_preflight_and_fill directly
        mock_cdp.run_preflight_and_fill.assert_not_called()
        # Orchestrator must call the two split functions
        mock_cdp.run_pre_card_checkout_prepare.assert_called_once()
        mock_cdp.run_payment_card_fill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
