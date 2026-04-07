"""Tests for BehaviorWrapper — Task 10.5."""
import time
import unittest
from unittest.mock import patch

from modules.delay.main import PersonaProfile, wrap
from modules.delay.state import BehaviorStateMachine
from modules.delay.engine import DelayEngine


def _dummy_task(worker_id):
    """Simple task that returns a known value."""
    return f"ok-{worker_id}"


def _failing_task(worker_id):
    raise RuntimeError("boom")


class TestWrapPreservesReturnValue(unittest.TestCase):
    def test_return_value_unchanged(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        result = wrapped("w-1")
        self.assertEqual(result, "ok-w-1")

    def test_preserves_function_name(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        self.assertEqual(wrapped.__name__, "_dummy_task")


class TestWrapAddsDelay(unittest.TestCase):
    def test_measurable_delay(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            wrapped("w-1")
            mock_sleep.assert_called_once()
            delay_arg = mock_sleep.call_args[0][0]
            self.assertGreater(delay_arg, 0.0, "sleep should be called with positive delay")


class TestWrapPropagatesExceptions(unittest.TestCase):
    def test_exception_propagated(self):
        persona = PersonaProfile(42)
        wrapped = wrap(_failing_task, persona)
        with self.assertRaises(RuntimeError):
            wrapped("w-1")

    def test_cleanup_runs_on_exception(self):
        """BUG-001: engine.reset_step_accumulator() and sm.reset() must run even on exception."""
        persona = PersonaProfile(42)
        with (
            patch("modules.delay.wrapper.DelayEngine.reset_step_accumulator") as mock_reset_acc,
            patch("modules.delay.wrapper.BehaviorStateMachine.reset") as mock_sm_reset,
        ):
            wrapped = wrap(_failing_task, persona)
            with self.assertRaises(RuntimeError):
                wrapped("w-1")
        mock_reset_acc.assert_called_once()
        mock_sm_reset.assert_called_once()


class TestCriticalSectionBypass(unittest.TestCase):
    """Verify zero delay when BehaviorStateMachine is in a critical context."""

    def test_vbv_state_skips_delay(self):
        """When FSM is in VBV (critical), is_delay_permitted() returns False → no sleep."""
        persona = PersonaProfile(42)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        # Transition to VBV: IDLE → FILLING_FORM → PAYMENT → VBV
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        self.assertFalse(engine.is_delay_permitted())

    def test_critical_section_flag_skips_delay(self):
        """When set_critical_section(True), delay is not permitted even in SAFE context."""
        persona = PersonaProfile(42)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        sm.transition("FILLING_FORM")
        sm.set_critical_section(True)
        self.assertFalse(engine.is_delay_permitted())

    def test_wrap_no_sleep_in_critical_section(self):
        """wrap() must not call time.sleep when critical section is active."""
        persona = PersonaProfile(42)
        # Manually test: when is_delay_permitted returns False, sleep is not called
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            with patch("modules.delay.engine.DelayEngine.is_delay_permitted", return_value=False):
                wrapped = wrap(_dummy_task, persona)
                result = wrapped("w-1")
                mock_sleep.assert_not_called()
                self.assertEqual(result, "ok-w-1")


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_return(self):
        p1 = PersonaProfile(99)
        p2 = PersonaProfile(99)
        w1 = wrap(_dummy_task, p1)
        w2 = wrap(_dummy_task, p2)
        self.assertEqual(w1("w-1"), w2("w-1"))

    def test_same_seed_same_delay_sequence(self):
        """Same seed must produce identical delay values across two independent wraps."""
        delays_a = []
        delays_b = []
        for delays, seed in [(delays_a, 77), (delays_b, 77)]:
            persona = PersonaProfile(seed)
            wrapped = wrap(_dummy_task, persona)
            with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
                wrapped("w-1")
                if mock_sleep.called:
                    delays.append(mock_sleep.call_args[0][0])
        self.assertTrue(len(delays_a) > 0, "At least one delay should be generated")
        self.assertEqual(delays_a, delays_b, "Same seed must yield identical delay sequence")


if __name__ == "__main__":
    unittest.main()
