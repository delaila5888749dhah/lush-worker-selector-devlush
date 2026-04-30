"""T-09 — CDP timeout between fill & submit → ``mark_submitted`` NOT called.

If ``cdp.run_preflight_and_fill`` (the pre-submit sequence) raises a
TimeoutError, the orchestrator must abort run_payment_step BEFORE the
``mark_submitted`` checkpoint and BEFORE ``cdp.submit_purchase`` — neither
must be invoked.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integration.orchestrator import run_payment_step  # noqa: E402
from modules.common.exceptions import SessionFlaggedError  # noqa: E402
from _e2e_harness import (  # noqa: E402
    E2EBase,
    _STORE_PATCH,
    make_billing_profile,
    make_task,
)


class TestT09CdpTimeoutBeforeSubmit(E2EBase):
    """T-09: CDP timeout between prepare/fill and submit → mark_submitted NOT called.

    After the Phase A reorder split, a CDP timeout can surface from either
    of the two split-phase points (run_pre_card_checkout_prepare or
    run_payment_card_fill).  Both sub-cases must abort run_payment_step
    BEFORE ``mark_submitted`` and BEFORE ``cdp.submit_purchase``.
    """

    def test_mark_submitted_not_called_on_card_fill_timeout(self):
        """Timeout from run_payment_card_fill (post Phase A) → no mark/submit."""
        task = make_task(task_id="t09-timeout-card-fill")
        profile = make_billing_profile()

        store = MagicMock()

        with patch("integration.orchestrator.cdp") as cdp_mod, \
             patch("integration.orchestrator.watchdog") as wd, \
             patch("integration.orchestrator._emit_billing_audit_event"), \
             patch("integration.orchestrator._setup_network_total_listener"), \
             patch(_STORE_PATCH, return_value=store):
            cdp_mod._get_driver.return_value = MagicMock()
            cdp_mod.run_payment_card_fill.side_effect = TimeoutError(
                "CDP call timed out during card fill",
            )
            wd.wait_for_total.return_value = None

            with self.assertRaises((TimeoutError, SessionFlaggedError)):
                run_payment_step(task, worker_id=self.worker_id, _profile=profile)

        store.mark_submitted.assert_not_called()
        cdp_mod.submit_purchase.assert_not_called()

    def test_mark_submitted_not_called_on_pre_card_prepare_timeout(self):
        """Timeout from run_pre_card_checkout_prepare (pre Phase A) → no mark/submit.

        With the Phase A reorder split, navigation/cart/guest-checkout happens
        before Phase A wait.  A timeout there must abort cleanly without
        running Phase A wait, card fill, mark_submitted, or submit.
        """
        task = make_task(task_id="t09-timeout-prepare")
        profile = make_billing_profile()

        store = MagicMock()

        with patch("integration.orchestrator.cdp") as cdp_mod, \
             patch("integration.orchestrator.watchdog") as wd, \
             patch("integration.orchestrator._emit_billing_audit_event"), \
             patch("integration.orchestrator._setup_network_total_listener"), \
             patch(_STORE_PATCH, return_value=store):
            cdp_mod._get_driver.return_value = MagicMock()
            cdp_mod.run_pre_card_checkout_prepare.side_effect = TimeoutError(
                "CDP call timed out during pre-card prepare",
            )
            wd.wait_for_total.return_value = None

            with self.assertRaises((TimeoutError, SessionFlaggedError)):
                run_payment_step(task, worker_id=self.worker_id, _profile=profile)

        # All downstream irreversible steps must not be reached.
        cdp_mod.run_payment_card_fill.assert_not_called()
        store.mark_submitted.assert_not_called()
        cdp_mod.submit_purchase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
