"""Phase 5A — cdp-half review-fix tests for GivexDriver / shared SM wiring.

Covers the four review blockers raised on PR #276:

* [F1] ``submit_purchase()`` must only advance the FSM to ``POST_ACTION``
  when the underlying submit click actually succeeds.  Click failures
  must clear the critical section but leave the FSM untouched so future
  delay decisions reflect real progress through the checkout flow.
* [F2] ``handle_vbv_challenge()`` must only advance the FSM to
  ``POST_ACTION`` on the ``"cancelled"`` (success) path.  The benign
  ``"iframe_missing"`` and the failure ``"cdp_fail"`` / ``"error"``
  paths must clear the critical section but leave the FSM in its
  current (e.g. ``VBV`` / ``PAYMENT``) state.
* [F3] Rejected ``BehaviorStateMachine.transition()`` calls in driver
  code must be surfaced as WARNINGs so silent FSM drift is observable.
* [F4] ``GivexDriver.__init__`` must adopt the ``BehaviorStateMachine``
  published via ``modules.delay.state.set_current_sm`` and
  ``_realistic_type_field`` must skip biometric-pattern generation when
  ``DelayEngine.is_delay_permitted()`` returns ``False`` so the
  biometric RNG is not advanced inside CRITICAL_SECTION / VBV /
  POST_ACTION zones.
"""
from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from modules.cdp.driver import GivexDriver
from modules.delay.persona import PersonaProfile
from modules.delay.state import (
    BehaviorStateMachine,
    reset_current_sm,
    set_current_sm,
)


def _make_selenium() -> MagicMock:
    sel = MagicMock()
    sel.current_url = "https://example.com/page"
    sel.find_elements.return_value = []
    return sel


def _drive_to_payment(gd: GivexDriver) -> None:
    """Walk the SM into PAYMENT, the typical state when submit_purchase fires."""
    assert gd._sm is not None
    gd._sm.transition("FILLING_FORM")
    gd._sm.transition("PAYMENT")


# ── [F4] shared-SM adoption ──────────────────────────────────────────────


class TestSharedSmAdoption(unittest.TestCase):
    """``__init__`` adopts the SM published in the current context."""

    def test_init_adopts_published_current_sm(self) -> None:
        published = BehaviorStateMachine()
        token = set_current_sm(published)
        try:
            gd = GivexDriver(_make_selenium(), persona=PersonaProfile(42))
        finally:
            reset_current_sm(token)
        self.assertIs(gd._sm, published)
        # The DelayEngine must consult the same SM, otherwise the driver
        # and engine see different views of the FSM (the bug Phase 5A is
        # designed to eliminate).
        self.assertIs(gd._engine._state_machine, published)

    def test_init_falls_back_to_local_sm_outside_context(self) -> None:
        gd = GivexDriver(_make_selenium(), persona=PersonaProfile(42))
        self.assertIsNotNone(gd._sm)
        self.assertIs(gd._engine._state_machine, gd._sm)

    def test_no_persona_leaves_sm_none(self) -> None:
        gd = GivexDriver(_make_selenium())
        self.assertIsNone(gd._sm)
        self.assertIsNone(gd._engine)


# ── [F1] submit_purchase POST_ACTION timing ──────────────────────────────


class TestSubmitPurchasePostActionTiming(unittest.TestCase):
    """Click success advances POST_ACTION; click failure does not."""

    def _make(self) -> GivexDriver:
        gd = GivexDriver(_make_selenium(), persona=PersonaProfile(42))
        _drive_to_payment(gd)
        return gd

    def test_post_action_set_only_on_click_success(self) -> None:
        gd = self._make()
        with patch.object(gd, "_hesitate_before_submit"), \
             patch.object(gd, "bounding_box_click"):
            gd.submit_purchase()
        self.assertEqual(gd._sm.get_state(), "POST_ACTION")
        # CS flag must be cleared regardless of result.
        self.assertFalse(gd._sm._in_critical_section)

    def test_click_failure_keeps_state_in_payment(self) -> None:
        gd = self._make()
        with patch.object(gd, "_hesitate_before_submit"), \
             patch.object(gd, "bounding_box_click", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                gd.submit_purchase()
        # FSM must remain in PAYMENT — the click never landed.
        self.assertEqual(gd._sm.get_state(), "PAYMENT")
        # CS flag must still be cleared so a retry is not blocked.
        self.assertFalse(gd._sm._in_critical_section)

    def test_critical_section_armed_only_around_click(self) -> None:
        gd = self._make()
        states: list[bool] = []

        def _spy_click(_sel: str) -> None:
            # Snapshot the flag at the moment of the click.
            states.append(gd._sm.is_critical_context())

        with patch.object(gd, "_hesitate_before_submit"), \
             patch.object(gd, "bounding_box_click", side_effect=_spy_click):
            gd.submit_purchase()
        self.assertEqual(states, [True])
        # And cleared again after the click returns.
        self.assertFalse(gd._sm._in_critical_section)


# ── [F2] handle_vbv_challenge POST_ACTION timing ────────────────────────


class TestHandleVbvChallengePostActionTiming(unittest.TestCase):
    """POST_ACTION fires on the cancelled path only; failure paths preserve VBV."""

    def _make(self) -> GivexDriver:
        gd = GivexDriver(_make_selenium(), persona=PersonaProfile(42))
        _drive_to_payment(gd)
        return gd

    def test_cancelled_path_advances_to_post_action(self) -> None:
        gd = self._make()
        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            self.assertEqual(gd.handle_vbv_challenge(), "cancelled")
        self.assertEqual(gd._sm.get_state(), "POST_ACTION")
        self.assertFalse(gd._sm._in_critical_section)

    def test_iframe_missing_keeps_state_in_vbv(self) -> None:
        gd = self._make()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=NoSuchElementException("no iframe")):
            self.assertEqual(gd.handle_vbv_challenge(), "iframe_missing")
        self.assertEqual(gd._sm.get_state(), "VBV")
        self.assertFalse(gd._sm._in_critical_section)

    def test_iframe_missing_on_stale_keeps_state_in_vbv(self) -> None:
        gd = self._make()
        with patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element",
                   side_effect=StaleElementReferenceException("stale")):
            self.assertEqual(gd.handle_vbv_challenge(), "iframe_missing")
        self.assertEqual(gd._sm.get_state(), "VBV")
        self.assertFalse(gd._sm._in_critical_section)

    def test_cdp_fail_keeps_state_in_vbv(self) -> None:
        gd = self._make()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=WebDriverException("cdp gone")):
            self.assertEqual(gd.handle_vbv_challenge(), "cdp_fail")
        self.assertEqual(gd._sm.get_state(), "VBV")
        self.assertFalse(gd._sm._in_critical_section)

    def test_unexpected_error_keeps_state_in_vbv(self) -> None:
        gd = self._make()
        with patch("modules.cdp.driver.vbv_dynamic_wait",
                   side_effect=ValueError("unexpected")):
            self.assertEqual(gd.handle_vbv_challenge(), "error")
        self.assertEqual(gd._sm.get_state(), "VBV")
        self.assertFalse(gd._sm._in_critical_section)

    def test_critical_section_armed_during_iframe_interaction(self) -> None:
        gd = self._make()
        seen: list[bool] = []

        def _record_wait(*_a: object, **_kw: object) -> None:
            seen.append(gd._sm.is_critical_context())

        with patch("modules.cdp.driver.vbv_dynamic_wait", side_effect=_record_wait), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            gd.handle_vbv_challenge()
        self.assertEqual(seen, [True])


# ── [F3] rejected transitions surface as warnings ───────────────────────


class TestRejectedTransitionWarnings(unittest.TestCase):
    """Driver-side ``transition()`` results must not be silently swallowed."""

    def test_vbv_transition_from_post_action_is_warned(self) -> None:
        gd = GivexDriver(_make_selenium(), persona=PersonaProfile(42))
        gd._sm.transition("FILLING_FORM")
        gd._sm.transition("PAYMENT")
        gd._sm.transition("POST_ACTION")  # POST_ACTION → VBV is invalid.
        with self.assertLogs("modules.cdp.driver", level=logging.WARNING) as cm, \
             patch("modules.cdp.driver.vbv_dynamic_wait"), \
             patch("modules.cdp.driver.cdp_click_iframe_element"), \
             patch("modules.cdp.driver.handle_something_wrong_popup"):
            gd.handle_vbv_challenge()
        joined = "\n".join(cm.output)
        self.assertIn("rejected VBV transition", joined)


# ── [F4] safe-zone typing skips biometric pattern generation ────────────


class TestRealisticTypeFieldSkipsBiometric(unittest.TestCase):
    """When delay is not permitted, biometric pattern generation is skipped."""

    def _make_driver_in_critical_section(self) -> GivexDriver:
        sel = _make_selenium()
        sel.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(sel, persona=PersonaProfile(42))
        _drive_to_payment(gd)
        gd._sm.set_critical_section(True)
        return gd

    def test_biometric_generators_not_called_when_delay_blocked(self) -> None:
        gd = self._make_driver_in_critical_section()
        bio = MagicMock()
        bio.generate_4x4_pattern.return_value = [0.1] * 16
        bio.generate_burst_pattern.return_value = [0.05] * 4
        gd._bio = bio
        with patch("modules.cdp.driver._type_value") as mock_tv:
            gd._realistic_type_field("#field", "abcd", field_kind="text")
        bio.generate_4x4_pattern.assert_not_called()
        bio.generate_burst_pattern.assert_not_called()
        # _type_value still receives the field, but with delays=None so no
        # per-keystroke sleep is generated either.
        self.assertIsNone(mock_tv.call_args.kwargs["delays"])

    def test_biometric_generators_called_on_normal_path(self) -> None:
        sel = _make_selenium()
        sel.find_elements.return_value = [MagicMock()]
        gd = GivexDriver(sel, persona=PersonaProfile(42))
        # No critical section: SM stays in IDLE which is delay-safe.
        bio = MagicMock()
        bio.generate_4x4_pattern.return_value = [0.1] * 16
        bio.generate_burst_pattern.return_value = [0.05] * 4
        gd._bio = bio
        with patch("modules.cdp.driver._type_value") as mock_tv:
            gd._realistic_type_field(
                "#card", "4111111111111111", use_burst=True, field_kind="card_number",
            )
        bio.generate_4x4_pattern.assert_called_once()
        delays = mock_tv.call_args.kwargs["delays"]
        self.assertEqual(delays, [0.1] * 16)


if __name__ == "__main__":
    unittest.main()
