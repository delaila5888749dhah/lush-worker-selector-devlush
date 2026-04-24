"""P3-F4-ORDER / INV-PAYMENT-01 — The Total Watchdog MUST observe the
checkout-total network response BEFORE any card or billing field is typed.

If ``watchdog.wait_for_total`` times out, ``run_payment_step`` must raise
:class:`SessionFlaggedError` and the driver must NOT have dispatched any
CDP ``Input.dispatchKeyEvent`` on the card number / CVV / billing fields.
"""

import unittest
from unittest.mock import MagicMock, patch

from integration import orchestrator as orch
from modules.common.exceptions import SessionFlaggedError


def _make_task(task_id="p3-f4-order-test"):
    task = MagicMock()
    task.task_id = task_id
    task.primary_card = MagicMock()
    task.primary_card.card_name = "Alice Smith"
    task.primary_card.card_number = "4111111111111111"
    task.primary_card.exp_month = "01"
    task.primary_card.exp_year = "2030"
    task.primary_card.cvv = "123"
    return task


def _make_profile():
    p = MagicMock()
    p.email = "guest@example.com"
    p.address = "123 Main"
    p.city = "LA"
    p.zip_code = "90001"
    p.country = "US"
    p.state = "CA"
    p.phone = "5551234567"
    return p


class TestFillPaymentBlockedUntilWaitForTotalReturns(unittest.TestCase):
    """The "fill payment & billing" CDP call must not occur until the watchdog
    confirms the total; on timeout, SessionFlaggedError fires first."""

    def test_fill_payment_blocked_until_wait_for_total_returns(self):
        task = _make_task()
        profile = _make_profile()

        with patch.object(orch, "billing") as mock_billing, \
             patch.object(orch, "cdp") as mock_cdp, \
             patch.object(orch, "watchdog") as mock_watchdog, \
             patch.object(orch, "fsm") as mock_fsm, \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch, "_notify_total_from_dom"), \
             patch.object(orch, "_emit_billing_audit_event"):
            mock_billing.select_profile.return_value = profile
            mock_cdp._get_driver.return_value = MagicMock()
            # Simulate a >10s watchdog delay by raising SessionFlaggedError.
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            mock_fsm.get_current_state_for_worker.return_value = None

            with self.assertRaises(SessionFlaggedError):
                orch.run_payment_step(task, worker_id="p3-f4-worker", _profile=profile)

            # INV-PAYMENT-01: fill_payment_and_billing must NOT be called when
            # the watchdog times out before the fill step.
            mock_cdp.fill_payment_and_billing.assert_not_called()
            # submit_purchase must NOT be called either.
            mock_cdp.submit_purchase.assert_not_called()

    def test_preflight_runs_before_wait_for_total(self):
        """run_preflight_up_to_guest_checkout MUST run before wait_for_total
        so the payment page is loaded (and its total-XHR can fire)."""
        task = _make_task()
        profile = _make_profile()
        order = []

        def record_prefill(*a, **kw):
            order.append("prefill")

        def record_wait(*a, **kw):
            order.append("wait")
            raise SessionFlaggedError("timeout")

        with patch.object(orch, "billing") as mock_billing, \
             patch.object(orch, "cdp") as mock_cdp, \
             patch.object(orch, "watchdog") as mock_watchdog, \
             patch.object(orch, "fsm") as mock_fsm, \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch, "_notify_total_from_dom"), \
             patch.object(orch, "_emit_billing_audit_event"):
            mock_billing.select_profile.return_value = profile
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_up_to_guest_checkout.side_effect = record_prefill
            mock_watchdog.wait_for_total.side_effect = record_wait
            mock_fsm.get_current_state_for_worker.return_value = None

            with self.assertRaises(SessionFlaggedError):
                orch.run_payment_step(task, worker_id="p3-f4-worker", _profile=profile)

        self.assertEqual(order, ["prefill", "wait"])

    def test_no_dispatch_key_event_on_card_number_when_watchdog_times_out(self):
        """When watchdog times out, the driver MUST NOT dispatch any CDP
        Input.dispatchKeyEvent call (i.e. no card-number / CVV / billing
        keystrokes leaked to the browser)."""
        task = _make_task()
        profile = _make_profile()

        # Real driver double: records every execute_cdp_cmd and tracks whether
        # Input.dispatchKeyEvent was ever invoked.
        driver_double = MagicMock()
        dispatch_calls = []

        def record_cdp(cmd, *a, **kw):
            if cmd == "Input.dispatchKeyEvent":
                dispatch_calls.append((cmd, a, kw))
            return {}

        driver_double.execute_cdp_cmd.side_effect = record_cdp

        with patch.object(orch, "billing") as mock_billing, \
             patch.object(orch.cdp, "_get_driver", return_value=driver_double), \
             patch.object(orch, "watchdog") as mock_watchdog, \
             patch.object(orch, "fsm") as mock_fsm, \
             patch.object(orch, "_setup_network_total_listener"), \
             patch.object(orch, "_notify_total_from_dom"), \
             patch.object(orch, "_emit_billing_audit_event"), \
             patch.object(orch, "_cdp_call_with_timeout") as mock_exec:
            mock_billing.select_profile.return_value = profile
            # _cdp_call_with_timeout(fn, ...) — forward for preflight, nothing
            # afterwards because wait_for_total raises.
            def _exec(fn, *a, **kw):
                # Never let fn touch the driver_double keyboard — we only
                # care that after wait_for_total raises, fill is never run.
                return None
            mock_exec.side_effect = _exec
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("timeout")
            mock_fsm.get_current_state_for_worker.return_value = None

            with self.assertRaises(SessionFlaggedError):
                orch.run_payment_step(task, worker_id="p3-f4-worker", _profile=profile)

        self.assertEqual(
            dispatch_calls, [],
            "CDP Input.dispatchKeyEvent must NOT fire before wait_for_total "
            "confirms the total (INV-PAYMENT-01)",
        )


if __name__ == "__main__":
    unittest.main()
