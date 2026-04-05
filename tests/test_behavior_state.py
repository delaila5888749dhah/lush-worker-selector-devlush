"""Tests for Task 10.2 — BehaviorState FSM (Context-Aware State Machine).

Validates:
  - BEHAVIOR_STATES has exactly 5 states
  - _VALID_BEHAVIOR_TRANSITIONS enforces strict transition rules
  - BehaviorStateMachine(initial_state) defaults to IDLE
  - transition() returns True for valid, False for invalid
  - get_state() returns current state under lock
  - is_critical_context() True for VBV/POST_ACTION, False otherwise
  - is_safe_for_delay() True for IDLE/FILLING_FORM/PAYMENT when not in CS
  - is_safe_for_delay() False when critical-section flag is set
  - reset() restores IDLE and clears critical-section flag
  - Thread safety via threading.Lock
  - Invalid initial state raises ValueError
"""

import threading
import unittest

from modules.delay.state import (
    BEHAVIOR_STATES,
    BehaviorStateMachine,
    _VALID_BEHAVIOR_TRANSITIONS,
)


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
    """is_critical_context() True for VBV and POST_ACTION only."""

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

    def test_safe_state_but_critical_section_active(self):
        sm = BehaviorStateMachine()
        sm.set_critical_section(True)
        self.assertFalse(sm.is_safe_for_delay())

    def test_safe_after_critical_section_cleared(self):
        sm = BehaviorStateMachine()
        sm.set_critical_section(True)
        sm.set_critical_section(False)
        self.assertTrue(sm.is_safe_for_delay())

    def test_critical_state_with_critical_section_flag(self):
        sm = BehaviorStateMachine(initial_state="VBV")
        sm.set_critical_section(True)
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

    def test_reset_clears_critical_section(self):
        sm = BehaviorStateMachine()
        sm.set_critical_section(True)
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
        self.assertEqual(errors, [])

    def test_concurrent_critical_section_flag(self):
        sm = BehaviorStateMachine()
        errors = []
        barrier = threading.Barrier(10)

        def worker(flag_value):
            try:
                barrier.wait(timeout=2)
                for _ in range(100):
                    sm.set_critical_section(flag_value)
                    # is_safe_for_delay must not raise
                    sm.is_safe_for_delay()
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


if __name__ == "__main__":
    unittest.main()
