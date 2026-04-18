"""Tests for DelayEngine — Task 10.3."""
import unittest

from modules.delay.main import (
    PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY,
    BehaviorStateMachine, DelayEngine,
    MAX_HESITATION_DELAY, MAX_STEP_DELAY, WATCHDOG_HEADROOM,
)
from modules.delay.config import WATCHDOG_HEADROOM as CONFIG_WATCHDOG_HEADROOM


class _EngineSetup(unittest.TestCase):
    """Common setup: persona + state machine + engine."""

    def setUp(self):
        self.persona = PersonaProfile(42)
        self.sm = BehaviorStateMachine()
        self.engine = DelayEngine(self.persona, self.sm)


class TestTypingDelay(_EngineSetup):
    def test_within_bounds(self):
        self.sm.transition("FILLING_FORM")
        for gi in range(5):
            d = self.engine.calculate_typing_delay(gi)
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, MAX_TYPING_DELAY)

    def test_zero_in_critical_context(self):
        self.sm.transition("FILLING_FORM")
        self.sm.transition("PAYMENT")
        self.sm.transition("VBV")
        self.assertEqual(self.engine.calculate_typing_delay(0), 0.0)


class TestClickDelay(_EngineSetup):
    def test_click_delay_positive(self):
        from modules.delay.config import MAX_CLICK_DELAY
        d = self.engine.calculate_click_delay()
        self.assertGreater(d, 0.0)
        self.assertLessEqual(d, MAX_CLICK_DELAY)

    def test_click_delay_does_not_accumulate(self):
        acc_before = self.engine.get_step_accumulated_delay()
        self.engine.calculate_click_delay()
        acc_after = self.engine.get_step_accumulated_delay()
        self.assertEqual(acc_before, acc_after)

    def test_click_delay_deterministic(self):
        p2 = PersonaProfile(42)
        sm2 = BehaviorStateMachine()
        e2 = DelayEngine(p2, sm2)
        self.assertEqual(
            self.engine.calculate_click_delay(),
            e2.calculate_click_delay(),
        )

    def test_click_delay_positive_in_critical_context(self):
        self.sm.transition("FILLING_FORM")
        self.sm.transition("PAYMENT")
        self.sm.transition("VBV")
        d = self.engine.calculate_click_delay()
        self.assertGreater(d, 0.0)


class TestThinkingDelay(_EngineSetup):
    def test_within_bounds(self):
        self.sm.transition("FILLING_FORM")
        d = self.engine.calculate_thinking_delay()
        self.assertGreaterEqual(d, 3.0)
        self.assertLessEqual(d, MAX_HESITATION_DELAY)

    def test_zero_in_critical(self):
        self.sm.transition("FILLING_FORM")
        self.sm.transition("PAYMENT")
        self.sm.transition("VBV")
        self.assertEqual(self.engine.calculate_thinking_delay(), 0.0)


class TestDispatcher(_EngineSetup):
    def test_typing_dispatch(self):
        self.sm.transition("FILLING_FORM")
        d = self.engine.calculate_delay("typing")
        self.assertGreater(d, 0.0)

    def test_click_dispatch(self):
        from modules.delay.config import MAX_CLICK_DELAY
        d = self.engine.calculate_delay("click")
        self.assertGreater(d, 0.0)
        self.assertLessEqual(d, MAX_CLICK_DELAY)

    def test_unknown_dispatch(self):
        self.assertEqual(self.engine.calculate_delay("unknown"), 0.0)


class TestAccumulator(_EngineSetup):
    def test_accumulation(self):
        self.sm.transition("FILLING_FORM")
        self.engine.calculate_delay("typing")
        self.assertGreater(self.engine.get_step_accumulated_delay(), 0.0)

    def test_reset_accumulator(self):
        self.sm.transition("FILLING_FORM")
        self.engine.calculate_delay("typing")
        self.engine.reset_step_accumulator()
        self.assertEqual(self.engine.get_step_accumulated_delay(), 0.0)

    def test_accumulator_respects_ceiling(self):
        self.sm.transition("FILLING_FORM")
        total = 0.0
        for _ in range(20):
            d = self.engine.calculate_delay("thinking")
            total += d
        self.assertLessEqual(self.engine.get_step_accumulated_delay(), MAX_STEP_DELAY)


class TestCriticalSectionBypass(_EngineSetup):
    def test_vbv_zero_delay(self):
        self.sm.transition("FILLING_FORM")
        self.sm.transition("PAYMENT")
        self.sm.transition("VBV")
        self.assertFalse(self.engine.is_delay_permitted())
        self.assertEqual(self.engine.calculate_delay("typing"), 0.0)
        self.assertEqual(self.engine.calculate_delay("thinking"), 0.0)

    def test_post_action_zero_delay(self):
        self.sm.transition("FILLING_FORM")
        self.sm.transition("PAYMENT")
        self.sm.transition("POST_ACTION")
        self.assertFalse(self.engine.is_delay_permitted())

    def test_phase9_critical_section_flag(self):
        """Phase 9 CRITICAL_SECTION flag → zero delay."""
        self.sm.transition("FILLING_FORM")
        self.assertTrue(self.engine.is_delay_permitted())
        self.sm.set_critical_section(True)
        self.assertFalse(self.engine.is_delay_permitted())
        self.assertEqual(self.engine.calculate_delay("typing"), 0.0)
        self.assertEqual(self.engine.calculate_delay("thinking"), 0.0)
        self.sm.set_critical_section(False)
        self.assertTrue(self.engine.is_delay_permitted())


class TestDeterminism(_EngineSetup):
    def test_same_seed_same_delays(self):
        p2 = PersonaProfile(42)
        sm2 = BehaviorStateMachine()
        e2 = DelayEngine(p2, sm2)
        self.sm.transition("FILLING_FORM")
        sm2.transition("FILLING_FORM")
        d1 = self.engine.calculate_typing_delay(0)
        d2 = e2.calculate_typing_delay(0)
        self.assertEqual(d1, d2)


class TestWatchdogHeadroom(unittest.TestCase):
    def test_headroom_constant(self):
        self.assertEqual(WATCHDOG_HEADROOM, 3.0)

    def test_max_step_plus_headroom(self):
        from modules.delay.config import _STEP_BUDGET_TOTAL
        self.assertLessEqual(MAX_STEP_DELAY + WATCHDOG_HEADROOM, _STEP_BUDGET_TOTAL)


class TestEngineModuleImport(unittest.TestCase):
    """Verify engine.py is importable as a standalone module."""

    def test_direct_import(self):
        from modules.delay.engine import DelayEngine as DE
        from modules.delay.engine import (
            MAX_HESITATION_DELAY as MHD,
            MAX_STEP_DELAY as MSD,
        )
        from modules.delay.persona import PersonaProfile as PP
        from modules.delay.persona import MAX_TYPING_DELAY as MTD
        from modules.delay.state import BehaviorStateMachine as BSM
        self.assertEqual(MHD, 5.0)
        self.assertEqual(MSD, 7.0)
        self.assertEqual(CONFIG_WATCHDOG_HEADROOM, 3.0)
        p = PP(99)
        sm = BSM()
        e = DE(p, sm)
        sm.transition("FILLING_FORM")
        d = e.calculate_typing_delay(0)
        self.assertGreaterEqual(d, 0.0)
        self.assertLessEqual(d, MTD)


class TestBoundaryConditions(_EngineSetup):
    """Edge cases and boundary conditions."""

    def test_idle_state_permits_typing(self):
        """IDLE is a safe context — typing delay is positive."""
        d = self.engine.calculate_delay("typing")
        self.assertGreater(d, 0.0)

    def test_accumulator_exact_ceiling(self):
        """Accumulator stops exactly at MAX_STEP_DELAY."""
        self.sm.transition("FILLING_FORM")
        for _ in range(50):
            self.engine.calculate_delay("thinking")
        acc = self.engine.get_step_accumulated_delay()
        self.assertLessEqual(acc, MAX_STEP_DELAY)
        self.assertEqual(self.engine.calculate_delay("thinking"), 0.0)

    def test_reset_then_accumulate_again(self):
        """After reset, accumulator allows new delays."""
        self.sm.transition("FILLING_FORM")
        self.engine.calculate_delay("typing")
        self.engine.reset_step_accumulator()
        d = self.engine.calculate_delay("typing")
        self.assertGreater(d, 0.0)

    def test_click_always_returns_reaction_delay_regardless_of_state(self):
        """Click delay is a small non-zero value, even in critical states."""
        from modules.delay.config import MAX_CLICK_DELAY
        self.sm.transition("FILLING_FORM")
        d1 = self.engine.calculate_click_delay()
        self.assertGreater(d1, 0.0)
        self.assertLessEqual(d1, MAX_CLICK_DELAY)
        self.sm.transition("PAYMENT")
        self.sm.transition("VBV")
        d2 = self.engine.calculate_click_delay()
        self.assertGreater(d2, 0.0)
        self.assertLessEqual(d2, MAX_CLICK_DELAY)


if __name__ == "__main__":
    unittest.main()
