"""Phase 5A — CriticalSection wiring & dual-SM unification tests.

Verifies the production wiring described in the Phase 5A issue:

* ``GivexDriver.submit_purchase`` brackets the ``bounding_box_click`` of
  the Complete Purchase button with ``set_critical_section(True/False)``
  and transitions the FSM to ``POST_ACTION`` on success.
* ``GivexDriver.handle_vbv_challenge`` brackets the iframe interaction
  with ``set_critical_section(True/False)``, transitions through
  ``VBV`` and ends in ``POST_ACTION``.
* ``integration.orchestrator.run_payment_step`` wraps ``wait_for_total``
  with ``set_critical_section(True/False)``.
* ``GivexDriver._realistic_type_field`` skips the slow biometric 4×4
  pattern when the engine reports the worker is in a critical section.
* The behaviour wrapper publishes its ``BehaviorStateMachine`` into a
  shared ``ContextVar`` so a ``GivexDriver`` constructed inside the
  wrapper adopts the same SM instance.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from modules.cdp.driver import GivexDriver, SEL_COMPLETE_PURCHASE
from modules.common.types import CardInfo
from modules.delay.persona import PersonaProfile
from modules.delay.state import BehaviorStateMachine
from modules.delay.wrapper import wrap as _behavior_wrap


# ── helpers ────────────────────────────────────────────────────────────────


def _make_selenium() -> MagicMock:
    d = MagicMock()
    d.current_url = "https://example.com/checkout"
    d.find_elements.return_value = []
    body = MagicMock()
    body.text = ""
    d.find_element.return_value = body
    return d


def _spy_sm(driver: GivexDriver) -> tuple[list[bool], list[str]]:
    """Wrap driver._sm.set_critical_section and .transition with spies.

    Returns ``(flag_calls, transition_calls)`` lists capturing every
    flag flip and transition target observed for the test.
    """
    sm = driver._sm  # pylint: disable=protected-access
    assert sm is not None, "driver constructed without persona — no SM"
    flag_calls: list[bool] = []
    transition_calls: list[str] = []
    real_flag = sm.set_critical_section
    real_transition = sm.transition

    def _flag(active: bool) -> None:
        flag_calls.append(bool(active))
        real_flag(active)

    def _transition(state: str) -> bool:
        transition_calls.append(state)
        return real_transition(state)

    sm.set_critical_section = _flag  # type: ignore[assignment]
    sm.transition = _transition  # type: ignore[assignment]
    return flag_calls, transition_calls


# ── tests ──────────────────────────────────────────────────────────────────


class TestSubmitPurchaseCriticalSection(unittest.TestCase):
    def test_set_critical_section_called_around_payment_submit(self):
        """Spy: True flipped before bounding_box_click, False after."""
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(11))
        flag_calls, transition_calls = _spy_sm(driver)

        order: list[str] = []

        def fake_click(sel: str) -> None:
            order.append(f"click:{sel}")

        # Capture a snapshot of CS state at click time.
        cs_at_click = {"value": None}
        real_click = fake_click

        def click_wrapped(sel: str) -> None:
            cs_at_click["value"] = driver._sm.is_critical_context()  # pylint: disable=protected-access
            real_click(sel)

        with patch.object(driver, "_hesitate_before_submit"), \
             patch.object(driver, "bounding_box_click", side_effect=click_wrapped), \
             patch.object(driver, "find_elements", return_value=[]):
            driver.submit_purchase()

        # The CS flag must be True while the click happens, and False after.
        self.assertTrue(flag_calls, "set_critical_section was never called")
        self.assertEqual(flag_calls[0], True, "first flip must be True before click")
        self.assertEqual(flag_calls[-1], False, "last flip must be False after click")
        self.assertTrue(cs_at_click["value"], "CS must be active during the click")
        self.assertFalse(driver._sm.is_critical_context())  # pylint: disable=protected-access
        # Sanity: the click did happen.
        self.assertIn(f"click:{SEL_COMPLETE_PURCHASE}", order)

    def test_behavior_sm_transitions_to_post_action_after_submit(self):
        """submit_purchase must transition the SM to POST_ACTION after click."""
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(12))
        # Move SM to PAYMENT (the legal predecessor) so POST_ACTION is valid.
        driver._sm.transition("FILLING_FORM")  # pylint: disable=protected-access
        driver._sm.transition("PAYMENT")  # pylint: disable=protected-access
        _flags, transitions = _spy_sm(driver)

        with patch.object(driver, "_hesitate_before_submit"), \
             patch.object(driver, "bounding_box_click"), \
             patch.object(driver, "find_elements", return_value=[]):
            driver.submit_purchase()

        self.assertIn("POST_ACTION", transitions)
        self.assertEqual(
            driver._sm.get_state(),  # pylint: disable=protected-access
            "POST_ACTION",
        )


class TestVBVCriticalSection(unittest.TestCase):
    def test_set_critical_section_called_around_vbv_iframe(self):
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(21))
        flag_calls, _transitions = _spy_sm(driver)

        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            driver.handle_vbv_challenge()

        self.assertTrue(flag_calls, "set_critical_section was never called")
        self.assertEqual(flag_calls[0], True)
        self.assertEqual(flag_calls[-1], False)

    def test_behavior_sm_transitions_to_vbv_during_challenge(self):
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(22))
        _flags, transitions = _spy_sm(driver)

        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            driver.handle_vbv_challenge()

        self.assertIn("VBV", transitions)
        self.assertIn("POST_ACTION", transitions)


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


class TestBiometricRespectsCriticalSection(unittest.TestCase):
    def test_biometric_respects_critical_section_in_production_path(self):
        """When CS is active, _realistic_type_field must NOT call generate_4x4_pattern."""
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(31))
        # Ensure card field selector resolves to one element.
        element = MagicMock()
        with patch.object(driver, "find_elements", return_value=[element]), \
             patch("modules.cdp.driver._type_value") as mock_type, \
             patch.object(driver._bio, "generate_4x4_pattern") as mock_4x4, \
             patch.object(driver._bio, "generate_burst_pattern") as mock_burst:
            driver._sm.set_critical_section(True)  # pylint: disable=protected-access
            driver._realistic_type_field(  # pylint: disable=protected-access
                "input.card", "4111111111111111", use_burst=True,
                field_kind="card_number",
            )
        mock_4x4.assert_not_called()
        mock_burst.assert_not_called()
        # type_value still invoked, but with delays=None.
        mock_type.assert_called_once()
        self.assertIsNone(mock_type.call_args.kwargs.get("delays"))

    def test_biometric_active_when_critical_section_off(self):
        """Sanity: with CS off the production path still generates the 4x4 pattern."""
        driver = GivexDriver(_make_selenium(), persona=PersonaProfile(32))
        element = MagicMock()
        with patch.object(driver, "find_elements", return_value=[element]), \
             patch("modules.cdp.driver._type_value"), \
             patch.object(driver._bio, "generate_4x4_pattern",
                          return_value=[0.05] * 16) as mock_4x4:
            driver._realistic_type_field(  # pylint: disable=protected-access
                "input.card", "4111111111111111", use_burst=True,
                field_kind="card_number",
            )
        mock_4x4.assert_called_once()


class TestSharedSMAcrossDriverAndWrapper(unittest.TestCase):
    def test_single_sm_instance_across_driver_and_wrapper(self):
        """GivexDriver constructed inside wrap() must adopt the wrapper's SM."""
        persona = PersonaProfile(99)
        captured: dict[str, object] = {}

        def task_fn(_worker_id: str) -> None:
            d = GivexDriver(_make_selenium(), persona=persona)
            captured["driver_sm"] = d._sm  # pylint: disable=protected-access

        wrapped = _behavior_wrap(task_fn, persona)
        with patch("modules.delay.wrapper.time.sleep"):
            wrapped("w-1")

        wrapper_sm = wrapped.behavior_sm  # type: ignore[attr-defined]
        self.assertIsNotNone(captured.get("driver_sm"))
        self.assertIs(captured["driver_sm"], wrapper_sm)

    def test_driver_outside_wrapper_creates_fresh_sm(self):
        """Outside any wrapper context, GivexDriver creates its own SM."""
        persona = PersonaProfile(98)
        d1 = GivexDriver(_make_selenium(), persona=persona)
        d2 = GivexDriver(_make_selenium(), persona=persona)
        self.assertIsNotNone(d1._sm)  # pylint: disable=protected-access
        self.assertIsNotNone(d2._sm)  # pylint: disable=protected-access
        self.assertIsNot(d1._sm, d2._sm)  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
