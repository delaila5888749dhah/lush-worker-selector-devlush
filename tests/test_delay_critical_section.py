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
        real_enter = sm.enter_critical_zone
        real_exit = sm.exit_critical_zone

        def _enter(zone: str) -> None:
            flag_calls.append(True)
            real_enter(zone)

        def _exit() -> None:
            flag_calls.append(False)
            real_exit()

        sm.enter_critical_zone = _enter  # type: ignore[assignment]
        sm.exit_critical_zone = _exit  # type: ignore[assignment]

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

    def test_refill_after_vbv_reload_resets_cs_on_exception(self):
        """[F4] refill_after_vbv_reload must reset CS even when a refill action raises."""
        from integration.orchestrator import refill_after_vbv_reload
        from modules.delay.state import set_current_sm, reset_current_sm
        from modules.common.types import CycleContext

        sm = BehaviorStateMachine()
        flag_calls: list[bool] = []
        real_enter = sm.enter_critical_zone
        real_exit = sm.exit_critical_zone

        def _enter(zone: str) -> None:
            flag_calls.append(True)
            real_enter(zone)

        def _exit() -> None:
            flag_calls.append(False)
            real_exit()

        sm.enter_critical_zone = _enter  # type: ignore[assignment]
        sm.exit_critical_zone = _exit  # type: ignore[assignment]

        billing = MagicMock(email="x@y.z")
        task = MagicMock()
        ctx = CycleContext(
            cycle_id="c-1", worker_id="w-1",
            billing_profile=billing, task=task,
        )
        driver = MagicMock()
        # Make a mid-chain action raise; refill_after_vbv_reload swallows the
        # exception via its broad except, but the finally must still flip CS off.
        driver.fill_payment_and_billing.side_effect = RuntimeError("fill boom")
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
        # And the SM's internal flag must be fully cleared after return.
        self.assertFalse(sm.is_critical_context())


class TestWaitForTotalCriticalSection(unittest.TestCase):
    def test_set_critical_section_called_around_wait_for_total(self):
        """run_payment_step wraps both wait_for_total calls with CS True/False."""
        from integration import orchestrator
        from modules.delay.state import set_current_sm, reset_current_sm

        sm = BehaviorStateMachine()
        flag_calls: list[bool] = []
        real_enter = sm.enter_critical_zone
        real_exit = sm.exit_critical_zone

        def _enter(zone: str) -> None:
            flag_calls.append(True)
            real_enter(zone)

        def _exit() -> None:
            flag_calls.append(False)
            real_exit()

        sm.enter_critical_zone = _enter  # type: ignore[assignment]
        sm.exit_critical_zone = _exit  # type: ignore[assignment]

        # Provide a fake driver in the cdp registry so the early lookup succeeds.
        fake_driver = MagicMock()
        fake_driver.cdp_listeners = []
        fake_billing_profile = MagicMock(zip_code="00000")

        # [F3]/[N2] Track call order so we can assert that both flag flips
        # occur strictly around the watchdog.wait_for_total calls (and that
        # _notify_total_from_dom stays OUTSIDE the post-submit CS window).
        order: list[str] = []

        def _wait_for_total(*_a, **_kw):
            order.append("wait_for_total")
            return 49.99

        def _notify(*_a, **_kw):
            order.append("notify_total_from_dom")

        token = set_current_sm(sm)
        try:
            with patch.object(orchestrator.cdp, "_get_driver", return_value=fake_driver), \
                 patch.object(orchestrator, "_setup_network_total_listener"), \
                 patch.object(orchestrator, "_select_profile_with_audit",
                               return_value=fake_billing_profile), \
                 patch.object(orchestrator.watchdog, "enable_network_monitor"), \
                 patch.object(orchestrator.watchdog, "wait_for_total",
                               side_effect=_wait_for_total), \
                 patch.object(orchestrator, "_cdp_call_with_timeout"), \
                 patch.object(orchestrator, "_get_idempotency_store"), \
                 patch.object(orchestrator.cdp, "detect_page_state",
                               return_value="success"), \
                 patch.object(orchestrator.fsm, "transition_for_worker"), \
                 patch.object(orchestrator, "_notify_total_from_dom",
                               side_effect=_notify):
                # Wrap enter/exit recording into the same `order` list
                # so we can verify exact bracketing.
                original_enter = sm.enter_critical_zone
                original_exit = sm.exit_critical_zone

                def _ordered_enter(zone: str) -> None:
                    order.append("cs=True")
                    original_enter(zone)

                def _ordered_exit() -> None:
                    order.append("cs=False")
                    original_exit()

                sm.enter_critical_zone = _ordered_enter  # type: ignore[assignment]
                sm.exit_critical_zone = _ordered_exit  # type: ignore[assignment]

                task = MagicMock(task_id="t-1", amount=49.99)
                orchestrator.run_payment_step(task, worker_id="w-1")
        finally:
            reset_current_sm(token)

        # [F3] Must be EXACTLY two CS windows, one per watchdog wait,
        # alternating True/False around each wait_for_total call.
        self.assertEqual(flag_calls, [True, False, True, False])
        # [F3]/[N2] The full ordered trace must be:
        #   cs=True -> wait_for_total -> cs=False (Phase A pre-fill window)
        #   ... notify_total_from_dom OUTSIDE any cs=True window ...
        #   cs=True -> wait_for_total -> cs=False (Phase C post-submit window)
        # The strict prefix and suffix slices below lock that ordering down.
        self.assertEqual(order[:3], ["cs=True", "wait_for_total", "cs=False"])
        self.assertEqual(order[-3:], ["cs=True", "wait_for_total", "cs=False"])
        # [N2] _notify_total_from_dom must execute strictly between the two
        # CS windows (i.e. with CS already cleared by Phase A and not yet
        # re-armed for Phase C).
        notify_idx = order.index("notify_total_from_dom")
        last_false_before_notify = max(
            i for i, e in enumerate(order[:notify_idx]) if e == "cs=False"
        )
        first_true_after_notify = min(
            i for i, e in enumerate(order[notify_idx + 1:], start=notify_idx + 1)
            if e == "cs=True"
        )
        self.assertLess(last_false_before_notify, notify_idx)
        self.assertLess(notify_idx, first_true_after_notify)


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

    def test_nested_set_restores_outer_sm(self):
        """[N1] Nested set_current_sm/reset_current_sm restores the outer SM."""
        from modules.delay.state import get_current_sm, set_current_sm, reset_current_sm
        outer = BehaviorStateMachine()
        inner = BehaviorStateMachine()
        t_outer = set_current_sm(outer)
        try:
            self.assertIs(get_current_sm(), outer)
            t_inner = set_current_sm(inner)
            try:
                self.assertIs(get_current_sm(), inner)
            finally:
                reset_current_sm(t_inner)
            # After resetting the inner token, the outer SM must be active again.
            self.assertIs(get_current_sm(), outer)
        finally:
            reset_current_sm(t_outer)
        self.assertIsNone(get_current_sm())


class TestWrapPublishesCurrentSm(unittest.TestCase):
    """[F2] wrap() must publish/reset its SM via ContextVar across success,
    exception, and nested-wrap cases — not just rely on helper-level tests.
    """

    def _persona(self):
        from modules.delay.persona import PersonaProfile
        return PersonaProfile(42)

    def test_current_sm_visible_inside_task_and_cleared_after_success(self):
        from modules.delay.wrapper import wrap
        from modules.delay.state import get_current_sm

        observed: dict = {}

        def _task(_):
            observed["sm"] = get_current_sm()
            return "ok"

        wrapped = wrap(_task, self._persona())
        result = wrapped("w-1")
        self.assertEqual(result, "ok")
        self.assertIsNotNone(observed["sm"])
        self.assertIsInstance(observed["sm"], BehaviorStateMachine)
        # ContextVar must be cleared back to None after the wrapper returns.
        self.assertIsNone(get_current_sm())

    def test_current_sm_cleared_after_task_exception(self):
        from modules.delay.wrapper import wrap
        from modules.delay.state import get_current_sm

        observed: dict = {}

        def _task(_):
            observed["sm"] = get_current_sm()
            raise RuntimeError("boom")

        wrapped = wrap(_task, self._persona())
        with self.assertRaises(RuntimeError):
            wrapped("w-1")
        self.assertIsNotNone(observed["sm"])
        # Even on exception the outermost finally must reset the ContextVar.
        self.assertIsNone(get_current_sm())

    def test_nested_wrap_restores_outer_sm(self):
        """Inner wrapped task running inside outer wrapped task must restore
        the outer SM into the ContextVar after the inner call returns."""
        from modules.delay.wrapper import wrap
        from modules.delay.state import get_current_sm

        seen: dict = {}

        def _inner(_):
            seen["inner"] = get_current_sm()
            return "inner-ok"

        wrapped_inner = wrap(_inner, self._persona())

        def _outer(_):
            seen["outer_before"] = get_current_sm()
            wrapped_inner("w-inner")
            seen["outer_after"] = get_current_sm()
            return "outer-ok"

        wrapped_outer = wrap(_outer, self._persona())
        wrapped_outer("w-outer")

        self.assertIsNotNone(seen["outer_before"])
        self.assertIsNotNone(seen["inner"])
        # The inner wrapper must have published its OWN SM.
        self.assertIsNot(seen["inner"], seen["outer_before"])
        # After inner returns, the outer SM must be restored — not None,
        # and not still the inner SM.
        self.assertIs(seen["outer_after"], seen["outer_before"])
        # And once everything unwinds, ContextVar is cleared.
        self.assertIsNone(get_current_sm())


if __name__ == "__main__":
    unittest.main()
