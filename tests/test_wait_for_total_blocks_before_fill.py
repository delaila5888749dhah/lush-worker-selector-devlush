"""Phase 3A Task 2 — wait_for_total ordering (INV-PAYMENT-01).

Verifies that ``integration.orchestrator.run_payment_step`` blocks on
``watchdog.wait_for_total`` BEFORE any card field is typed, that a
timeout in that pre-fill phase aborts before ``run_preflight_and_fill``
is invoked, and that a post-submit (Phase C) timeout is swallowed
(optional confirmation).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import integration.orchestrator as orch
from modules.common.exceptions import SessionFlaggedError
from modules.common.types import CardInfo, WorkerTask
from modules.fsm.main import State


def _make_task():
    card = CardInfo(
        card_number="4111111111111111",
        exp_month="12",
        exp_year="2030",
        cvv="123",
        card_name="Jane Doe",
    )
    return WorkerTask(
        recipient_email="buyer@example.com",
        amount=50,
        primary_card=card,
        order_queue=(card,),
    )


class WaitForTotalOrderingTests(unittest.TestCase):
    """INV-PAYMENT-01 — pricing watchdog gates BEFORE card fill."""

    def test_wait_for_total_blocks_before_fill(self):
        """wait_for_total must be called BEFORE run_preflight_and_fill."""
        task = _make_task()
        call_order = []

        def record(name):
            def fn(*a, **kw):
                call_order.append(name)
                return MagicMock()
            return fn

        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.side_effect = record("fill")
            mock_cdp.submit_purchase.side_effect = record("submit")
            mock_watchdog.wait_for_total.side_effect = (
                lambda *a, **kw: call_order.append("wait") or 50.0
            )
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            run_payment_step(task, worker_id="ord-worker")

        # First call must be "wait" (Phase A).
        self.assertEqual(call_order[0], "wait", f"got: {call_order}")
        # Fill must come AFTER the first wait.
        self.assertLess(call_order.index("wait"), call_order.index("fill"))
        self.assertLess(call_order.index("fill"), call_order.index("submit"))

    def test_preflight_total_timeout_aborts_before_card_fill(self):
        """Phase A timeout raises BEFORE run_preflight_and_fill / submit_purchase."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
            patch("integration.orchestrator._get_idempotency_store") as mock_store_f,
        ):
            store = MagicMock()
            mock_store_f.return_value = store
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            # Phase A wait_for_total times out on the FIRST call.
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("preflight timeout")

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(task, worker_id="abort-worker")

        mock_cdp.run_preflight_and_fill.assert_not_called()
        mock_cdp.submit_purchase.assert_not_called()
        store.mark_submitted.assert_not_called()

    def test_post_submit_total_optional_does_not_block_success(self):
        """Phase C timeout is swallowed and preflight total is returned."""
        task = _make_task()
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_cdp.run_preflight_and_fill.return_value = None
            mock_cdp.submit_purchase.return_value = None
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            # Phase A OK; Phase C times out.
            mock_watchdog.wait_for_total.side_effect = [
                99.0,  # Phase A preflight total
                SessionFlaggedError("post-submit timeout"),
            ]

            state, total = orch.run_payment_step(task, worker_id="phc-worker")

        self.assertEqual(total, 99.0)
        self.assertEqual(state.name, "success")


class CDPTimeoutContractDocTests(unittest.TestCase):
    """spec/cdp-timeout-contract.md must state timeout=10 (not 30)."""

    def test_cdp_timeout_contract_doc_says_10s(self):
        import re
        from pathlib import Path
        doc = Path(__file__).resolve().parents[1] / "spec" / "cdp-timeout-contract.md"
        text = doc.read_text(encoding="utf-8")
        # Must mention 10-second network total timeout explicitly.
        self.assertRegex(text, r"timeout\s*=\s*10\b")
        # Must not claim 30s for the network total watchdog.
        self.assertNotRegex(text, r"timeout\s*=\s*30\b")

    def test_payment_watchdog_timeout_default_is_10s(self):
        """The orchestrator's compiled-in payment watchdog timeout is 10s."""
        import os
        # Baseline: with no operator override, default is 10s (see
        # _load_payment_watchdog_timeout).  Skip if a CI override is active.
        if os.environ.get("PAYMENT_WATCHDOG_TIMEOUT_S"):
            self.skipTest("PAYMENT_WATCHDOG_TIMEOUT_S override is set")
        self.assertEqual(orch._WATCHDOG_TIMEOUT_PAYMENT, 10.0)


if __name__ == "__main__":
    unittest.main()
