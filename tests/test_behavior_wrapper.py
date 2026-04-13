"""Tests for BehaviorWrapper — Task 10.5."""
import threading
import unittest
from unittest.mock import patch

from modules.delay.main import PersonaProfile, wrap
from modules.delay.state import BehaviorStateMachine
from modules.delay.engine import DelayEngine
from modules.delay.temporal import TemporalModel
from modules.delay.wrapper import inject_step_delay


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
            mock_sleep.assert_called()
            delay_arg = mock_sleep.call_args_list[0][0][0]
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
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            for delays, seed in [(delays_a, 77), (delays_b, 77)]:
                persona = PersonaProfile(seed)
                wrapped = wrap(_dummy_task, persona)
                with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
                    wrapped("w-1")
                    for c in mock_sleep.call_args_list:
                        delays.append(c[0][0])
        self.assertTrue(len(delays_a) > 0, "At least one delay should be generated")
        self.assertEqual(delays_a, delays_b, "Same seed must yield identical delay sequence")


class TestInjectStepDelay(unittest.TestCase):
    """Tests for the inject_step_delay() public helper (GAP-T1)."""

    def _make_engine_and_temporal(self, seed=42):
        persona = PersonaProfile(seed)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        temporal = TemporalModel(persona)
        sm.transition("FILLING_FORM")
        return engine, temporal, sm

    def test_typing_injects_positive_delay(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_step_delay(engine, temporal, "typing")
        mock_sleep.assert_called_once()
        self.assertGreater(result, 0.0)
        self.assertAlmostEqual(result, engine.get_step_accumulated_delay(), places=10)

    def test_thinking_injects_positive_delay(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        with (
            patch.object(TemporalModel, "get_time_state", return_value="DAY"),
            patch("modules.delay.wrapper.time.sleep") as mock_sleep,
        ):
            result = inject_step_delay(engine, temporal, "thinking")
        mock_sleep.assert_called_once()
        self.assertGreater(result, 0.0)
        self.assertGreaterEqual(result, 3.0)
        self.assertLessEqual(result, 5.0)

    def test_click_injects_micro_delay(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_step_delay(engine, temporal, "click")
        mock_sleep.assert_called_once()
        self.assertGreater(result, 0.0)
        self.assertLessEqual(result, 0.25)

    def test_no_delay_in_critical_context(self):
        engine, temporal, sm = self._make_engine_and_temporal()
        sm.set_critical_section(True)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_step_delay(engine, temporal, "typing")
        mock_sleep.assert_not_called()
        self.assertEqual(result, 0.0)

    def test_returns_actual_delay_value(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_step_delay(engine, temporal, "typing")
        if mock_sleep.called:
            self.assertAlmostEqual(result, mock_sleep.call_args[0][0], places=10)
            self.assertAlmostEqual(result, engine.get_step_accumulated_delay(), places=10)

    def test_stop_event_path_returns_requested_delay(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        stop_event = threading.Event()
        stop_event.set()
        with patch.object(stop_event, "wait", wraps=stop_event.wait) as mock_wait:
            result = inject_step_delay(engine, temporal, "typing", stop_event=stop_event)
        mock_wait.assert_called_once()
        self.assertAlmostEqual(result, mock_wait.call_args[1]["timeout"], places=10)

    def test_accumulator_headroom_caps_requested_delay(self):
        engine, temporal, _ = self._make_engine_and_temporal()
        engine.accumulate_delay(6.8)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_step_delay(engine, temporal, "typing")
        mock_sleep.assert_called_once()
        self.assertAlmostEqual(result, 0.2, places=10)
        self.assertAlmostEqual(engine.get_step_accumulated_delay(), 7.0, places=10)


class TestWrapInjectsBothDelayTypes(unittest.TestCase):
    """wrap() must inject typing AND thinking delays (GAP-T2)."""

    def test_wrap_calls_sleep_twice(self):
        """One sleep for typing (pre-form) and one for thinking (post-fill)."""
        persona = PersonaProfile(42)
        wrapped = wrap(_dummy_task, persona)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            wrapped("w-1")
        self.assertEqual(mock_sleep.call_count, 2, "Expected exactly 2 sleep calls")
        for c in mock_sleep.call_args_list:
            self.assertGreater(c[0][0], 0.0, "Each sleep call must use a positive delay")

    def test_no_thinking_delay_on_exception(self):
        """thinking delay must NOT be injected when task_fn raises."""
        persona = PersonaProfile(42)
        wrapped = wrap(_failing_task, persona)
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError):
                wrapped("w-1")
        # Only the pre-form typing sleep should have fired.
        self.assertEqual(mock_sleep.call_count, 1, "Only typing sleep expected on failure")


class TestMultiStepFormSimulation(unittest.TestCase):
    """Simulate per-field delay injection using inject_step_delay directly."""

    def _make_components(self, seed=42):
        persona = PersonaProfile(seed)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        temporal = TemporalModel(persona)
        return engine, temporal, sm

    def test_5_fields_inject_5_delays(self):
        """Simulate 5 form fields: 3 typing, 1 card typing, 1 thinking hesitation."""
        engine, temporal, sm = self._make_components()
        fields = ["typing", "typing", "typing", "typing", "thinking"]
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            for action in fields:
                sm.reset()
                sm.transition("FILLING_FORM")
                inject_step_delay(engine, temporal, action)
                engine.reset_step_accumulator()
        self.assertEqual(mock_sleep.call_count, 5, "Expected 5 sleep calls for 5 fields")

    def test_accumulator_reset_between_steps(self):
        """Resetting the accumulator between steps allows each step to inject a full delay."""
        engine, temporal, sm = self._make_components()
        sm.transition("FILLING_FORM")

        with patch("modules.delay.wrapper.time.sleep"):
            delay1 = inject_step_delay(engine, temporal, "typing")

        # Without reset, the second call may be blocked if accumulator is near limit.
        # After reset, a fresh positive delay should be possible.
        engine.reset_step_accumulator()

        with patch("modules.delay.wrapper.time.sleep"):
            delay2 = inject_step_delay(engine, temporal, "typing")

        self.assertGreater(delay1, 0.0, "First step should inject a positive delay")
        self.assertGreater(delay2, 0.0, "Second step should inject a positive delay after reset")


class StopEventEarlyExitTests(unittest.TestCase):
    """inject_step_delay() stop_event early-exit correctness."""

    def _make_components(self, seed=42):
        persona = PersonaProfile(seed)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        temporal = TemporalModel(persona)
        sm.transition("FILLING_FORM")
        return engine, temporal

    def test_stop_event_set_before_call_exits_early(self):
        """When stop_event is already set, wait() returns immediately."""
        import time as _time
        engine, temporal = self._make_components()
        stop_event = threading.Event()
        stop_event.set()  # pre-set: "already stopped"

        t0 = _time.monotonic()
        result = inject_step_delay(engine, temporal, "typing", stop_event=stop_event)
        elapsed = _time.monotonic() - t0

        # Result should still be the delay value (wait returns True immediately)
        self.assertGreater(result, 0.0)
        # Elapsed wall time should be well under the delay value (early exit)
        self.assertLess(
            elapsed,
            result,
            "stop_event.wait() should return immediately when pre-set",
        )

    def test_stop_event_not_set_uses_wait_with_positive_timeout(self):
        """When stop_event is not set, wait() is called with a positive timeout."""
        engine, temporal = self._make_components()
        stop_event = threading.Event()  # NOT set

        with patch.object(stop_event, "wait", wraps=stop_event.wait) as mock_wait:
            with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
                result = inject_step_delay(engine, temporal, "typing", stop_event=stop_event)

        mock_wait.assert_called_once()
        timeout_arg = mock_wait.call_args[1]["timeout"]
        self.assertGreater(timeout_arg, 0.0)
        self.assertAlmostEqual(result, timeout_arg, places=10)


class TestInjectCardEntryDelays(unittest.TestCase):
    """Tests for inject_card_entry_delays() — Phase 11 BiometricProfile wiring."""

    def _make_bio(self, seed=42):
        from modules.delay.biometrics import BiometricProfile
        persona = PersonaProfile(seed)
        return BiometricProfile(persona)

    def test_returns_19_delays(self):
        """generate_4x4_pattern() produces 19 values; all must be slept."""
        from modules.delay.wrapper import inject_card_entry_delays
        bio = self._make_bio()
        with patch("modules.delay.wrapper.time.sleep"):
            result = inject_card_entry_delays(bio)
        self.assertEqual(len(result), 19)

    def test_all_delays_positive(self):
        """All 19 delays must be positive floats."""
        from modules.delay.wrapper import inject_card_entry_delays
        bio = self._make_bio()
        with patch("modules.delay.wrapper.time.sleep"):
            result = inject_card_entry_delays(bio)
        for d in result:
            self.assertGreater(d, 0.0)

    def test_sleep_called_19_times(self):
        """time.sleep must be called once per keystroke delay."""
        from modules.delay.wrapper import inject_card_entry_delays
        bio = self._make_bio()
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            inject_card_entry_delays(bio)
        self.assertEqual(mock_sleep.call_count, 19)

    def test_stop_event_exits_early(self):
        """When stop_event is pre-set, injection stops after 0 keystrokes."""
        from modules.delay.wrapper import inject_card_entry_delays
        bio = self._make_bio()
        stop_event = threading.Event()
        stop_event.set()
        with patch("modules.delay.wrapper.time.sleep") as mock_sleep:
            result = inject_card_entry_delays(bio, stop_event=stop_event)
        mock_sleep.assert_not_called()
        self.assertEqual(result, [])

    def test_stop_event_uses_wait(self):
        """When stop_event is not set, wait() is called instead of sleep()."""
        from modules.delay.wrapper import inject_card_entry_delays
        bio = self._make_bio()
        stop_event = threading.Event()  # NOT set
        with (
            patch("modules.delay.wrapper.time.sleep") as mock_sleep,
            patch.object(stop_event, "wait", return_value=False) as mock_wait,
        ):
            inject_card_entry_delays(bio, stop_event=stop_event)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_wait.call_count, 19)
        for call in mock_wait.call_args_list:
            timeout = call.kwargs.get("timeout")
            if timeout is None and call.args:
                timeout = call.args[0]
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0.0)

    def test_deterministic_same_seed(self):
        """Same seed must produce identical delay list across two calls."""
        from modules.delay.wrapper import inject_card_entry_delays
        from modules.delay.biometrics import BiometricProfile
        bio1 = BiometricProfile(PersonaProfile(77))
        bio2 = BiometricProfile(PersonaProfile(77))
        with patch("modules.delay.wrapper.time.sleep"):
            r1 = inject_card_entry_delays(bio1)
            r2 = inject_card_entry_delays(bio2)
        self.assertEqual(r1, r2)

    def test_does_not_accumulate_in_engine(self):
        """inject_card_entry_delays must NOT affect DelayEngine accumulator."""
        from modules.delay.wrapper import inject_card_entry_delays
        from modules.delay.biometrics import BiometricProfile
        persona = PersonaProfile(42)
        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        bio = BiometricProfile(persona)
        acc_before = engine.get_step_accumulated_delay()
        with patch("modules.delay.wrapper.time.sleep"):
            inject_card_entry_delays(bio)
        acc_after = engine.get_step_accumulated_delay()
        self.assertEqual(acc_before, acc_after)

    def test_accessible_via_delay_main(self):
        """inject_card_entry_delays must be importable from modules.delay.main."""
        from modules.delay.main import inject_card_entry_delays as fn
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
