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
    """T-09: CDP timeout between fill & submit → mark_submitted NOT called."""

    def test_mark_submitted_not_called_on_fill_timeout(self):
        task = make_task(task_id="t09-timeout-001")
        profile = make_billing_profile()

        store = MagicMock()

        with patch("integration.orchestrator.cdp") as cdp_mod, \
             patch("integration.orchestrator.watchdog") as wd, \
             patch("integration.orchestrator._emit_billing_audit_event"), \
             patch("integration.orchestrator._setup_network_total_listener"), \
             patch(_STORE_PATCH, return_value=store):
            cdp_mod._get_driver.return_value = MagicMock()
            # Simulate a CDP timeout during the fill phase.  The orchestrator's
            # _cdp_call_with_timeout converts the underlying TimeoutError into
            # a SessionFlaggedError before propagating out of run_payment_step.
            cdp_mod.run_preflight_up_to_guest_checkout.side_effect = TimeoutError(
                "CDP call timed out during preflight",
            )
            wd.wait_for_total.return_value = None

            with self.assertRaises((TimeoutError, SessionFlaggedError)):
                run_payment_step(task, worker_id=self.worker_id, _profile=profile)

        # The irreversible submit and its checkpoint must NOT have happened.
        store.mark_submitted.assert_not_called()
        cdp_mod.submit_purchase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
