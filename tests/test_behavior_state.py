"""Tests for Task 10.2 — BehaviorState FSM (Context-Aware State Machine).

Validates:
  - BEHAVIOR_STATES has exactly 5 states
  - _VALID_BEHAVIOR_TRANSITIONS enforces strict transition rules
  - BehaviorStateMachine(initial_state) defaults to IDLE
  - transition() returns True for valid, False for invalid
  - get_state() returns current state under lock
  - is_critical_context() True for VBV/POST_ACTION or critical-section flag
  - is_safe_for_delay() True for IDLE/FILLING_FORM/PAYMENT
  - is_safe_for_delay() False for VBV/POST_ACTION
  - reset() restores IDLE
  - Thread safety via threading.Lock
  - Invalid initial state raises ValueError
"""

import threading
import unittest

from modules.delay.main import (
    BEHAVIOR_STATES,
    BehaviorStateMachine,
)
from modules.delay.state import _VALID_BEHAVIOR_TRANSITIONS


# ── BEHAVIOR_STATES constants ───────────────────────────────────


class TestBehaviorStates(unittest.TestCase):
    """BEHAVIOR_STATES must have exactly 5 mandatory states."""

    def test_has_five_states(self):
        self.assertEqual(len(BEHAVIOR_STATES), 5)

    def test_contains_idle(self):
        self.assertIn("IDLE", BEHAVIOR_STATES)

    def test_contains_filling_form(self):
        self.assertIn("FILLING_FORM", BEHAVIOR_STATES)

    def test_contains_payment(self):
        self.assertIn("PAYMENT", BEHAVIOR_STATES)

    def test_contains_vbv(self):
        self.assertIn("VBV", BEHAVIOR_STATES)

    def test_contains_post_action(self):
        self.assertIn("POST_ACTION", BEHAVIOR_STATES)

    def test_is_a_set(self):
        self.assertIsInstance(BEHAVIOR_STATES, set)


# ── _VALID_BEHAVIOR_TRANSITIONS ─────────────────────────────────


class TestValidBehaviorTransitions(unittest.TestCase):
    """_VALID_BEHAVIOR_TRANSITIONS must cover every state with a set of
    allowed targets."""

    def test_all_states_have_transitions(self):
        for state in BEHAVIOR_STATES:
            self.assertIn(state, _VALID_BEHAVIOR_TRANSITIONS)

    def test_transition_targets_are_subsets(self):
        for source, targets in _VALID_BEHAVIOR_TRANSITIONS.items():
            self.assertIsInstance(targets, set)
            self.assertTrue(targets.issubset(BEHAVIOR_STATES))

    def test_idle_can_reach_filling_form(self):
        self.assertIn("FILLING_FORM", _VALID_BEHAVIOR_TRANSITIONS["IDLE"])

    def test_filling_form_can_reach_payment(self):
        self.assertIn("PAYMENT", _VALID_BEHAVIOR_TRANSITIONS["FILLING_FORM"])

    def test_payment_can_reach_vbv(self):
        self.assertIn("VBV", _VALID_BEHAVIOR_TRANSITIONS["PAYMENT"])

    def test_payment_can_reach_post_action(self):
        self.assertIn("POST_ACTION", _VALID_BEHAVIOR_TRANSITIONS["PAYMENT"])

    def test_vbv_can_reach_post_action(self):
        self.assertIn("POST_ACTION", _VALID_BEHAVIOR_TRANSITIONS["VBV"])

    def test_post_action_can_reach_idle(self):
        self.assertIn("IDLE", _VALID_BEHAVIOR_TRANSITIONS["POST_ACTION"])

    def test_no_self_transitions(self):
        for source, targets in _VALID_BEHAVIOR_TRANSITIONS.items():
            self.assertNotIn(source, targets)


# ── BehaviorStateMachine — init ──────────────────────────────────


class TestBehaviorStateMachineInit(unittest.TestCase):
    """Constructor must validate initial state."""

    def test_default_initial_state_is_idle(self):
        sm = BehaviorStateMachine()
        self.assertEqual(sm.get_state(), "IDLE")

    def test_custom_initial_state(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        self.assertEqual(sm.get_state(), "PAYMENT")

    def test_invalid_initial_state_raises(self):
        with self.assertRaises(ValueError):
            BehaviorStateMachine(initial_state="BOGUS")

    def test_invalid_initial_state_empty(self):
        with self.assertRaises(ValueError):
            BehaviorStateMachine(initial_state="")


# ── BehaviorStateMachine — transition ────────────────────────────


class TestTransition(unittest.TestCase):
    """transition() must enforce _VALID_BEHAVIOR_TRANSITIONS."""

    def test_valid_transition_returns_true(self):
        sm = BehaviorStateMachine()
        self.assertTrue(sm.transition("FILLING_FORM"))

    def test_valid_transition_updates_state(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        self.assertEqual(sm.get_state(), "FILLING_FORM")

    def test_invalid_transition_returns_false(self):
        sm = BehaviorStateMachine()
        self.assertFalse(sm.transition("VBV"))

    def test_invalid_transition_preserves_state(self):
        sm = BehaviorStateMachine()
        sm.transition("VBV")
        self.assertEqual(sm.get_state(), "IDLE")

    def test_unknown_state_returns_false(self):
        sm = BehaviorStateMachine()
        self.assertFalse(sm.transition("UNKNOWN"))

    def test_full_happy_path(self):
        sm = BehaviorStateMachine()
        self.assertTrue(sm.transition("FILLING_FORM"))
        self.assertTrue(sm.transition("PAYMENT"))
        self.assertTrue(sm.transition("VBV"))
        self.assertTrue(sm.transition("POST_ACTION"))
        self.assertTrue(sm.transition("IDLE"))

    def test_full_path_no_vbv(self):
        sm = BehaviorStateMachine()
        self.assertTrue(sm.transition("FILLING_FORM"))
        self.assertTrue(sm.transition("PAYMENT"))
        self.assertTrue(sm.transition("POST_ACTION"))
        self.assertTrue(sm.transition("IDLE"))

    def test_abort_from_filling_form(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        self.assertTrue(sm.transition("IDLE"))

    def test_abort_from_payment(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        self.assertTrue(sm.transition("IDLE"))

    def test_abort_from_vbv(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        self.assertTrue(sm.transition("IDLE"))


# ── BehaviorStateMachine — is_critical_context ───────────────────


class TestIsCriticalContext(unittest.TestCase):
    """is_critical_context() reflects FSM critical states and flag state."""

    def test_idle_not_critical(self):
        sm = BehaviorStateMachine()
        self.assertFalse(sm.is_critical_context())

    def test_filling_form_not_critical(self):
        sm = BehaviorStateMachine(initial_state="FILLING_FORM")
        self.assertFalse(sm.is_critical_context())

    def test_payment_not_critical(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        self.assertFalse(sm.is_critical_context())

    def test_vbv_is_critical(self):
        sm = BehaviorStateMachine(initial_state="VBV")
        self.assertTrue(sm.is_critical_context())

    def test_post_action_is_critical(self):
        sm = BehaviorStateMachine(initial_state="POST_ACTION")
        self.assertTrue(sm.is_critical_context())

    def test_critical_section_flag_is_critical(self):
        """is_critical_context() returns True when critical-section flag is set."""
        state_machine = BehaviorStateMachine(initial_state="FILLING_FORM")
        state_machine.set_critical_section(True)
        self.assertTrue(state_machine.is_critical_context())


# ── BehaviorStateMachine — is_safe_for_delay ─────────────────────


class TestIsSafeForDelay(unittest.TestCase):
    """is_safe_for_delay() depends on behavior state + critical-section flag."""

    def test_idle_safe(self):
        sm = BehaviorStateMachine()
        self.assertTrue(sm.is_safe_for_delay())

    def test_filling_form_safe(self):
        sm = BehaviorStateMachine(initial_state="FILLING_FORM")
        self.assertTrue(sm.is_safe_for_delay())

    def test_payment_safe(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        self.assertTrue(sm.is_safe_for_delay())

    def test_vbv_not_safe(self):
        sm = BehaviorStateMachine(initial_state="VBV")
        self.assertFalse(sm.is_safe_for_delay())

    def test_post_action_not_safe(self):
        sm = BehaviorStateMachine(initial_state="POST_ACTION")
        self.assertFalse(sm.is_safe_for_delay())

    def test_safe_state_but_critical_context_via_vbv(self):
        sm = BehaviorStateMachine(initial_state="VBV")
        self.assertFalse(sm.is_safe_for_delay())

    def test_safe_after_returning_to_idle(self):
        sm = BehaviorStateMachine(initial_state="VBV")
        sm.transition("IDLE")
        self.assertTrue(sm.is_safe_for_delay())

    def test_critical_state_post_action(self):
        sm = BehaviorStateMachine(initial_state="POST_ACTION")
        self.assertFalse(sm.is_safe_for_delay())


# ── BehaviorStateMachine — reset ─────────────────────────────────


class TestReset(unittest.TestCase):
    """reset() must restore IDLE and clear critical-section flag."""

    def test_reset_to_idle(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.reset()
        self.assertEqual(sm.get_state(), "IDLE")

    def test_reset_clears_state_to_idle(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        sm.reset()
        self.assertTrue(sm.is_safe_for_delay())

    def test_reset_allows_new_cycle(self):
        sm = BehaviorStateMachine()
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("POST_ACTION")
        sm.reset()
        self.assertTrue(sm.transition("FILLING_FORM"))


# ── Thread safety ────────────────────────────────────────────────


class TestThreadSafety(unittest.TestCase):
    """Concurrent transitions must not corrupt state."""

    def test_concurrent_transitions(self):
        sm = BehaviorStateMachine()
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=2)
                for _ in range(100):
                    sm.transition("FILLING_FORM")
                    state = sm.get_state()
                    if state not in BEHAVIOR_STATES:
                        errors.append(f"bad state: {state}")
                    sm.reset()
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], f"threads still alive: {alive}")
        self.assertEqual(errors, [])

    def test_concurrent_state_checks(self):
        sm = BehaviorStateMachine()
        errors = []
        barrier = threading.Barrier(10)

        def worker(use_vbv):
            try:
                barrier.wait(timeout=2)
                for _ in range(100):
                    if use_vbv:
                        ok = sm.transition("FILLING_FORM")
                        if ok:
                            ok = sm.transition("PAYMENT")
                        if ok:
                            sm.transition("VBV")
                    # is_safe_for_delay must not raise
                    sm.is_safe_for_delay()
                    sm.reset()
            except Exception as exc:
                errors.append(str(exc))

        threads = [
            threading.Thread(target=worker, args=(i % 2 == 0,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        alive = [t.name for t in threads if t.is_alive()]
        self.assertEqual(alive, [], f"threads still alive: {alive}")
        self.assertEqual(errors, [])


# ── Phase 9 compatibility ───────────────────────────────────────


class TestPhase9Compatibility(unittest.TestCase):
    """BehaviorStateMachine must not conflict with Phase 9 worker states."""

    def test_behavior_states_disjoint_from_worker_states(self):
        # Phase 9 ALLOWED_WORKER_STATES
        worker_states = {"IDLE", "IN_CYCLE", "CRITICAL_SECTION", "SAFE_POINT"}
        # Only IDLE overlaps — that's intentional (both layers start IDLE)
        overlap = BEHAVIOR_STATES & worker_states
        self.assertEqual(overlap, {"IDLE"})

    def test_behavior_states_no_in_cycle(self):
        self.assertNotIn("IN_CYCLE", BEHAVIOR_STATES)

    def test_behavior_states_no_critical_section(self):
        self.assertNotIn("CRITICAL_SECTION", BEHAVIOR_STATES)

    def test_behavior_states_no_safe_point(self):
        self.assertNotIn("SAFE_POINT", BEHAVIOR_STATES)


# ── CRITICAL_SECTION constant (BEH-CS-CONST) ────────────────────


class TestCriticalSectionConstant(unittest.TestCase):
    """Validate the module-level ``CRITICAL_SECTION`` constant
    (Blueprint §8.3, INV-DELAY-02, audit claim BEH-CS-CONST)."""

    def test_critical_section_exists(self):
        from modules.delay.state import CRITICAL_SECTION  # noqa: F401

    def test_critical_section_is_frozenset(self):
        from modules.delay.state import CRITICAL_SECTION
        self.assertIsInstance(CRITICAL_SECTION, frozenset)

    def test_critical_section_value(self):
        from modules.delay.state import CRITICAL_SECTION
        self.assertEqual(CRITICAL_SECTION, frozenset({"VBV", "POST_ACTION"}))

    def test_critical_section_subset_of_behavior_states(self):
        from modules.delay.state import CRITICAL_SECTION
        self.assertTrue(CRITICAL_SECTION.issubset(BEHAVIOR_STATES))

    def test_critical_section_disjoint_with_safe_contexts(self):
        from modules.delay.state import CRITICAL_SECTION, _SAFE_CONTEXTS
        self.assertTrue(CRITICAL_SECTION.isdisjoint(_SAFE_CONTEXTS))

    def test_critical_section_exported_via_main(self):
        from modules.delay.main import CRITICAL_SECTION
        self.assertEqual(CRITICAL_SECTION, frozenset({"VBV", "POST_ACTION"}))

    def test_is_critical_context_uses_constant(self):
        from modules.delay.state import CRITICAL_SECTION
        for state in CRITICAL_SECTION:
            sm = BehaviorStateMachine(initial_state=state)
            self.assertTrue(
                sm.is_critical_context(),
                f"is_critical_context() should be True for state {state!r}",
            )


# ── CRITICAL_SECTION_ZONES whitelist (Blueprint §8.3) ────────────


class TestCriticalSectionZones(unittest.TestCase):
    """CRITICAL_SECTION_ZONES is the canonical 4-zone whitelist."""

    def test_has_four_zones(self):
        from modules.delay.state import CRITICAL_SECTION_ZONES
        self.assertEqual(
            CRITICAL_SECTION_ZONES,
            frozenset({"payment_submit", "vbv_iframe", "api_wait", "page_reload"}),
        )

    def test_critical_section_zones_exported_via_main(self):
        from modules.delay.main import CRITICAL_SECTION_ZONES
        self.assertEqual(
            CRITICAL_SECTION_ZONES,
            frozenset({"payment_submit", "vbv_iframe", "api_wait", "page_reload"}),
        )

    def test_enter_critical_zone_rejects_unknown(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        with self.assertRaises(ValueError):
            sm.enter_critical_zone("not_a_real_zone")
        # SM must remain delay-safe after a rejected enter
        self.assertTrue(sm.is_safe_for_delay())
        self.assertIsNone(sm.get_active_zone())

    def test_enter_critical_zone_records_active_zone(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        sm.enter_critical_zone("payment_submit")
        self.assertEqual(sm.get_active_zone(), "payment_submit")
        self.assertTrue(sm.is_critical_context())
        self.assertFalse(sm.is_safe_for_delay())

    def test_exit_critical_zone_clears_state(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        sm.enter_critical_zone("api_wait")
        sm.exit_critical_zone()
        self.assertIsNone(sm.get_active_zone())
        self.assertTrue(sm.is_safe_for_delay())

    def test_legacy_set_critical_section_alias_still_works(self):
        """Backward compat: legacy set_critical_section(bool) toggles the flag."""
        sm = BehaviorStateMachine(initial_state="FILLING_FORM")
        sm.set_critical_section(True)
        self.assertTrue(sm.is_critical_context())
        self.assertFalse(sm.is_safe_for_delay())
        sm.set_critical_section(False)
        self.assertTrue(sm.is_safe_for_delay())
        self.assertIsNone(sm.get_active_zone())

    def test_reset_clears_active_zone(self):
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        sm.enter_critical_zone("vbv_iframe")
        sm.reset()
        self.assertIsNone(sm.get_active_zone())
        self.assertTrue(sm.is_safe_for_delay())

    def test_all_four_cs_zones_block_delay(self):
        """All four canonical zones must block delay injection in a SAFE FSM state."""
        from modules.delay.state import CRITICAL_SECTION_ZONES
        for zone in CRITICAL_SECTION_ZONES:
            sm = BehaviorStateMachine(initial_state="PAYMENT")
            self.assertTrue(
                sm.is_safe_for_delay(),
                f"PAYMENT must be delay-safe before entering zone {zone!r}",
            )
            sm.enter_critical_zone(zone)
            self.assertFalse(
                sm.is_safe_for_delay(),
                f"zone {zone!r} must block delay injection",
            )
            self.assertTrue(
                sm.is_critical_context(),
                f"zone {zone!r} must mark critical context",
            )
            self.assertEqual(sm.get_active_zone(), zone)
            sm.exit_critical_zone()
            self.assertTrue(
                sm.is_safe_for_delay(),
                f"exit_critical_zone() must restore delay safety after zone {zone!r}",
            )
            self.assertIsNone(sm.get_active_zone())

    def test_nested_zones_release_in_lifo_order(self):
        """Nested critical zones must be tracked re-entrantly.

        Entering an inner zone while an outer zone is still active must
        NOT clear the outer zone on the inner exit; the critical-section
        flag must remain set until the outermost zone is also exited.
        """
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        sm.enter_critical_zone("api_wait")           # outer
        self.assertEqual(sm.get_active_zone(), "api_wait")
        sm.enter_critical_zone("payment_submit")     # inner
        self.assertEqual(sm.get_active_zone(), "payment_submit")
        self.assertTrue(sm.is_critical_context())
        # Exit inner — outer must still be active.
        sm.exit_critical_zone()
        self.assertEqual(sm.get_active_zone(), "api_wait")
        self.assertTrue(sm.is_critical_context())
        self.assertFalse(sm.is_safe_for_delay())
        # Exit outer — flag finally clears.
        sm.exit_critical_zone()
        self.assertIsNone(sm.get_active_zone())
        self.assertFalse(sm.is_critical_context())
        self.assertTrue(sm.is_safe_for_delay())

    def test_exit_critical_zone_is_noop_when_stack_empty(self):
        """Defensive: extra exit_critical_zone() calls must not raise."""
        sm = BehaviorStateMachine(initial_state="PAYMENT")
        sm.exit_critical_zone()  # no-op
        self.assertTrue(sm.is_safe_for_delay())
        self.assertIsNone(sm.get_active_zone())


if __name__ == "__main__":
    unittest.main()
