"""E3 audit: cross-check DOM Order Total vs watchdog total before submit.

Covers the orchestrator-level wiring point and the ``modules.cdp.main``
wrapper that forwards the captured Phase A total to the registered driver
so :meth:`GivexDriver.submit_purchase` can verify the on-page DOM total
right before the irreversible COMPLETE PURCHASE click (Spec §5 line 287).
"""
import unittest
from unittest.mock import MagicMock, patch

from modules.common.types import CardInfo, State, WorkerTask
from modules.fsm.main import cleanup_worker, reset_registry
from modules.watchdog.main import reset as _reset_watchdog

from integration.orchestrator import (
    _completed_task_ids,
    _idempotency_lock,
    _in_flight_task_ids,
    _network_listener_lock,
    _notified_workers_this_cycle,
    _submitted_task_ids,
    run_payment_step,
)


def _make_task():
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
        order_queue=(),
    )


def _clear_idempotency():
    with _idempotency_lock:
        _completed_task_ids.clear()
        _in_flight_task_ids.clear()
        _submitted_task_ids.clear()
    with _network_listener_lock:
        _notified_workers_this_cycle.clear()


class CdpMainSetExpectedTotalTests(unittest.TestCase):
    """``modules.cdp.main.set_expected_total`` forwards to the registered driver."""

    def test_forwards_value_to_driver(self):
        from modules.cdp import main as cdp_main
        stub_driver = MagicMock()
        with patch.object(cdp_main, "_get_driver", return_value=stub_driver):
            cdp_main.set_expected_total("w1", 49.99)
        stub_driver.set_expected_total.assert_called_once_with(49.99)

    def test_forwards_none(self):
        from modules.cdp import main as cdp_main
        stub_driver = MagicMock()
        with patch.object(cdp_main, "_get_driver", return_value=stub_driver):
            cdp_main.set_expected_total("w1", None)
        stub_driver.set_expected_total.assert_called_once_with(None)


class OrchestratorWiringTests(unittest.TestCase):
    """run_payment_step wires the Phase A total into the driver via cdp.set_expected_total."""

    def setUp(self):
        _clear_idempotency()
        _reset_watchdog()
        reset_registry()
        cleanup_worker("e3-worker")

    def tearDown(self):
        _clear_idempotency()
        cleanup_worker("e3-worker")

    def test_set_expected_total_called_with_phase_a_total_before_submit(self):
        """cdp.set_expected_total must be called between wait_for_total and submit_purchase."""
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
            mock_cdp.set_expected_total.side_effect = (
                lambda wid, val: call_order.append(("set_expected_total", wid, val))
            )
            mock_cdp.run_preflight_and_fill.side_effect = (
                lambda *a, **kw: call_order.append("prefill")
            )
            mock_cdp.submit_purchase.side_effect = (
                lambda *a, **kw: call_order.append("submit")
            )
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(task, worker_id="e3-worker")

        # set_expected_total fires with the Phase A total (50.0) and the
        # correct worker_id, BEFORE the irreversible submit_purchase click.
        names = [c[0] if isinstance(c, tuple) else c for c in call_order]
        self.assertIn(("set_expected_total"), names)
        idx_set = names.index("set_expected_total")
        idx_submit = names.index("submit")
        self.assertLess(
            idx_set, idx_submit,
            f"set_expected_total must precede submit_purchase; got {call_order}",
        )
        # Value passed = the Phase A total returned by wait_for_total.
        ev = next(c for c in call_order if isinstance(c, tuple) and c[0] == "set_expected_total")
        self.assertEqual(ev[1], "e3-worker")
        self.assertEqual(ev[2], 50.0)

    def test_set_expected_total_failure_does_not_abort_cycle(self):
        """A wiring failure must be best-effort: cycle continues, submit still runs."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.set_expected_total.side_effect = RuntimeError(
                "driver does not support set_expected_total"
            )
            mock_watchdog.wait_for_total.return_value = 50.0
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            # Must NOT raise — failure is best-effort.
            run_payment_step(task, worker_id="e3-worker")

        mock_cdp.submit_purchase.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
