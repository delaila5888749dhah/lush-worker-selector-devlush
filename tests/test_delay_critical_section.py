"""Phase 5A (PR B) — delay module: CRITICAL_SECTION wiring tests.

Verifies the orchestrator-level CRITICAL_SECTION wiring and the
ContextVar-based SM sharing introduced in the delay module:

* ``integration.orchestrator.run_payment_step`` wraps both
  ``wait_for_total`` calls with ``set_critical_section(True/False)``.
* ``integration.orchestrator.refill_after_vbv_reload`` brackets the
  entire reload chain with ``set_critical_section(True/False)``.
* ``modules.delay.state`` exposes ``get_current_sm`` / ``set_current_sm``
  / ``reset_current_sm`` via a ``ContextVar`` so callers running inside
  a behaviour wrapper share a single SM instance without import-time
  coupling.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from modules.common.types import CardInfo
from modules.delay.state import BehaviorStateMachine


class TestRefillAfterVbvCriticalSection(unittest.TestCase):
    def test_refill_after_vbv_reload_brackets_critical_section(self):
        """refill_after_vbv_reload must flip CS True/False around the reload chain."""
        from integration.orchestrator import refill_after_vbv_reload
        from modules.delay.state import set_current_sm, reset_current_sm
        from modules.common.types import CycleContext

        sm = BehaviorStateMachine()
        flag_calls: list[bool] = []
        real_flag = sm.set_critical_section

        def _flag(active: bool) -> None:
            flag_calls.append(bool(active))
            real_flag(active)

        sm.set_critical_section = _flag  # type: ignore[assignment]

        billing = MagicMock(email="x@y.z")
        task = MagicMock()
        ctx = CycleContext(
            cycle_id="c-1", worker_id="w-1",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()
        new_card = CardInfo(
            card_number="4111111111111111", exp_month="12", exp_year="2027",
            cvv="123", card_name="Jane Doe",
        )

        token = set_current_sm(sm)
        try:
            refill_after_vbv_reload(driver, ctx, new_card)
        finally:
            reset_current_sm(token)

        self.assertEqual(flag_calls, [True, False])


class TestWaitForTotalCriticalSection(unittest.TestCase):
    def test_set_critical_section_called_around_wait_for_total(self):
        """run_payment_step wraps both wait_for_total calls with CS True/False."""
        from integration import orchestrator
        from modules.delay.state import set_current_sm, reset_current_sm

        sm = BehaviorStateMachine()
        flag_calls: list[bool] = []
        real_flag = sm.set_critical_section

        def _flag(active: bool) -> None:
            flag_calls.append(bool(active))
            real_flag(active)

        sm.set_critical_section = _flag  # type: ignore[assignment]

        # Provide a fake driver in the cdp registry so the early lookup succeeds.
        fake_driver = MagicMock()
        fake_driver.cdp_listeners = []
        fake_billing_profile = MagicMock(zip_code="00000")

        token = set_current_sm(sm)
        try:
            with patch.object(orchestrator.cdp, "_get_driver", return_value=fake_driver), \
                 patch.object(orchestrator, "_setup_network_total_listener"), \
                 patch.object(orchestrator, "_select_profile_with_audit",
                               return_value=fake_billing_profile), \
                 patch.object(orchestrator.watchdog, "enable_network_monitor"), \
                 patch.object(orchestrator.watchdog, "wait_for_total",
                               return_value=49.99), \
                 patch.object(orchestrator, "_cdp_call_with_timeout"), \
                 patch.object(orchestrator, "_get_idempotency_store"), \
                 patch.object(orchestrator.cdp, "detect_page_state",
                               return_value="success"), \
                 patch.object(orchestrator.fsm, "transition_for_worker"), \
                 patch.object(orchestrator, "_notify_total_from_dom"):
                task = MagicMock(task_id="t-1", amount=49.99)
                orchestrator.run_payment_step(task, worker_id="w-1")
        finally:
            reset_current_sm(token)

        # Two wait_for_total wrappers → at least 2 True flips and 2 False flips.
        self.assertGreaterEqual(flag_calls.count(True), 2)
        self.assertGreaterEqual(flag_calls.count(False), 2)
        # Order must alternate: first call is True, every True is followed by a False.
        self.assertEqual(flag_calls[0], True)
        for true_idx in [i for i, v in enumerate(flag_calls) if v]:
            self.assertLess(true_idx, len(flag_calls) - 1,
                            "every True must be followed by a False")
            self.assertEqual(flag_calls[true_idx + 1], False)


class TestContextVarSmHelpers(unittest.TestCase):
    def test_get_current_sm_returns_none_outside_context(self):
        """get_current_sm returns None when no SM has been published."""
        from modules.delay.state import get_current_sm
        self.assertIsNone(get_current_sm())

    def test_set_and_reset_current_sm(self):
        """set_current_sm/reset_current_sm form a balanced try/finally pair."""
        from modules.delay.state import get_current_sm, set_current_sm, reset_current_sm
        sm = BehaviorStateMachine()
        token = set_current_sm(sm)
        try:
            self.assertIs(get_current_sm(), sm)
        finally:
            reset_current_sm(token)
        self.assertIsNone(get_current_sm())


if __name__ == "__main__":
    unittest.main()
