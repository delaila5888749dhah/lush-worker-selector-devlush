"""Phase 3A Task 2 — Pre-fill ``wait_for_total`` ordering (INV-PAYMENT-01).

Verifies the new Phase A / Phase B / Phase C ordering of
``run_payment_step``:

* Phase A — ``watchdog.wait_for_total`` MUST block BEFORE any card field is
  filled.  If it times out, ``SessionFlaggedError`` is raised and neither
  ``fill_payment_and_billing`` nor ``submit_purchase`` is invoked.
* Phase B — fill + ``mark_submitted`` + ``submit_purchase`` only run after
  Phase A succeeds.
* Phase C — post-submit ``wait_for_total`` is best-effort; a timeout there
  must NOT raise (the submit is irreversible) — the task is marked
  unconfirmed (TTL) and the call returns normally.

Audit finding [F4] / Blueprint §5 / ``spec/contracts/section5_payment.yaml``.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from integration.orchestrator import run_payment_step
from modules.common.exceptions import SessionFlaggedError
from modules.common.types import CardInfo, State, WorkerTask


def _make_task():
    return WorkerTask(
        recipient_email="x@example.com",
        amount=50,
        primary_card=CardInfo(
            card_number="4111111111111111",
            exp_month="07",
            exp_year="27",
            cvv="123",
        ),
        order_queue=(),
    )


class WaitForTotalBlocksBeforeFill(unittest.TestCase):
    """Phase A precedes Phase B."""

    def test_wait_for_total_blocks_before_fill(self):
        """preflight wait_for_total must come before run_preflight_and_fill."""
        call_order = []
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()

            def _fill(*a, **kw):
                call_order.append("fill")
            def _submit(*a, **kw):
                call_order.append("submit")
            def _wait(*a, **kw):
                call_order.append("wait")
                return 49.99

            mock_cdp.run_preflight_and_fill.side_effect = _fill
            mock_cdp.submit_purchase.side_effect = _submit
            mock_watchdog.wait_for_total.side_effect = _wait
            mock_fsm.get_current_state_for_worker.return_value = State("success")

            run_payment_step(_make_task())

        # First wait must precede first fill.
        self.assertEqual(call_order[0], "wait", f"order: {call_order}")
        self.assertIn("fill", call_order)
        self.assertIn("submit", call_order)
        self.assertLess(call_order.index("wait"), call_order.index("fill"))
        self.assertLess(call_order.index("fill"), call_order.index("submit"))


class PreflightTotalTimeoutAbortsBeforeCardFill(unittest.TestCase):

    def test_preflight_total_timeout_aborts_before_card_fill(self):
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm"),
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_watchdog.wait_for_total.side_effect = SessionFlaggedError("preflight timeout")

            with self.assertRaises(SessionFlaggedError):
                run_payment_step(_make_task(), worker_id="w-pre")

        mock_cdp.run_preflight_and_fill.assert_not_called()
        mock_cdp.submit_purchase.assert_not_called()


class PostSubmitTotalOptional(unittest.TestCase):

    def test_post_submit_total_optional_does_not_block_success(self):
        """Phase C timeout must NOT raise; should mark unconfirmed and return."""
        with (
            patch("integration.orchestrator.billing") as mock_billing,
            patch("integration.orchestrator.cdp") as mock_cdp,
            patch("integration.orchestrator.watchdog") as mock_watchdog,
            patch("integration.orchestrator.fsm") as mock_fsm,
            patch("integration.orchestrator._get_idempotency_store") as mock_store_factory,
        ):
            mock_billing.select_profile.return_value = MagicMock()
            mock_cdp._get_driver.return_value = MagicMock()
            mock_fsm.get_current_state_for_worker.return_value = State("success")
            store = MagicMock()
            mock_store_factory.return_value = store
            # Phase A succeeds; Phase C times out.
            mock_watchdog.wait_for_total.side_effect = [
                49.99,
                SessionFlaggedError("post-submit timeout"),
            ]

            # No exception bubbles up.
            state, total = run_payment_step(_make_task(), worker_id="w-post")

        self.assertIsNone(total, "post-submit timeout returns total=None")
        store.mark_submitted.assert_called_once()
        store.mark_unconfirmed.assert_called_once()


class CdpTimeoutContractDocSays10s(unittest.TestCase):
    """spec/cdp-timeout-contract.md must reflect the live 10s timeout."""

    def test_cdp_timeout_contract_doc_says_10s(self):
        path = (
            Path(__file__).resolve().parent.parent
            / "spec" / "cdp-timeout-contract.md"
        )
        text = path.read_text(encoding="utf-8")
        # The §5 network rule line must say timeout=10.
        self.assertRegex(
            text,
            r"timeout\s*=\s*10",
            "spec/cdp-timeout-contract.md must declare timeout=10 (Blueprint §5)",
        )
        # And must NOT mention the stale 30-second value for the §5 rule.
        # We allow other "30" uses elsewhere — only target the §5 paragraph.
        section = re.search(
            r"### Network Response Timeout.*?(?=\n###|\Z)",
            text, flags=re.DOTALL,
        )
        self.assertIsNotNone(section)
        self.assertNotRegex(
            section.group(0),
            r"timeout=30|30 seconds",
            "stale 30s reference in §5 Network Response Timeout block",
        )


if __name__ == "__main__":
    unittest.main()
