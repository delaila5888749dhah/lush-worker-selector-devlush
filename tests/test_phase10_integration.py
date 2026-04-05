"""Phase 10 integration tests — Task 10.8.

End-to-end tests combining all modules in ``modules/delay/``.
"""
import threading
import unittest

from modules.delay.main import (
    PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY,
    BehaviorStateMachine, DelayEngine, MAX_STEP_DELAY,
    TemporalModel, BiometricProfile, wrap,
)


class TestFullPipeline(unittest.TestCase):
    """Persona → Engine → Temporal → Biometrics end-to-end."""

    def test_typing_pipeline(self):
        p = PersonaProfile(100)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        tm = TemporalModel(p)
        sm.transition("FILLING_FORM")

        raw = e.calculate_typing_delay(0)
        modified = tm.apply_temporal_modifier(raw, "typing")
        varied = tm.apply_micro_variation(modified)
        self.assertGreater(varied, 0.0)
        self.assertLessEqual(varied, MAX_TYPING_DELAY * 1.1 + 0.01)  # micro-var tolerance

    def test_thinking_pipeline(self):
        p = PersonaProfile(101)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        tm = TemporalModel(p)
        sm.transition("FILLING_FORM")

        raw = e.calculate_thinking_delay()
        fatigued = tm.apply_fatigue(raw, p.fatigue_threshold + 5)
        varied = tm.apply_micro_variation(fatigued)
        self.assertGreater(varied, 0.0)

    def test_biometric_keystroke_overlay(self):
        p = PersonaProfile(102)
        bio = BiometricProfile(p)
        pattern = bio.generate_4x4_pattern()
        self.assertEqual(len(pattern), 19)
        noisy = [bio.apply_noise(d) for d in pattern]
        for d in noisy:
            self.assertGreaterEqual(d, 0.0)

    def test_full_cycle_transitions(self):
        """IDLE → FILLING_FORM → PAYMENT → VBV → POST_ACTION → IDLE."""
        p = PersonaProfile(103)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)

        self.assertTrue(e.is_delay_permitted())
        sm.transition("FILLING_FORM")
        d1 = e.calculate_typing_delay(0)
        self.assertGreater(d1, 0.0)

        sm.transition("PAYMENT")
        d2 = e.calculate_typing_delay(1)
        self.assertGreater(d2, 0.0)

        sm.transition("VBV")
        self.assertEqual(e.calculate_typing_delay(0), 0.0)

        sm.transition("POST_ACTION")
        self.assertEqual(e.calculate_delay("thinking"), 0.0)

        sm.transition("IDLE")
        self.assertTrue(e.is_delay_permitted())

    def test_all_delay_types_in_single_cycle(self):
        """Exercise typing, click, and thinking in one step."""
        p = PersonaProfile(104)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")

        d_type = e.calculate_delay("typing")
        d_click = e.calculate_delay("click")
        d_think = e.calculate_delay("thinking")

        self.assertGreater(d_type, 0.0)
        self.assertEqual(d_click, 0.0)
        self.assertGreaterEqual(d_think, 0.0)
        self.assertLessEqual(e.get_step_accumulated_delay(), MAX_STEP_DELAY)


class TestCriticalSectionIntegration(unittest.TestCase):
    """Critical section flag integrates correctly with engine."""

    def test_critical_section_blocks_engine(self):
        p = PersonaProfile(110)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")

        self.assertTrue(e.is_delay_permitted())
        sm.set_critical_section(True)
        self.assertEqual(e.calculate_typing_delay(0), 0.0)
        self.assertEqual(e.calculate_thinking_delay(), 0.0)

        sm.set_critical_section(False)
        self.assertGreater(e.calculate_typing_delay(0), 0.0)

    def test_reset_clears_critical_flag(self):
        p = PersonaProfile(111)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.set_critical_section(True)
        self.assertFalse(e.is_delay_permitted())
        sm.reset()
        self.assertTrue(e.is_delay_permitted())


class TestEngineResetBetweenCycles(unittest.TestCase):
    """Accumulator resets correctly between cycles."""

    def test_accumulator_resets(self):
        p = PersonaProfile(120)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")

        for gi in range(4):
            e.calculate_typing_delay(gi)
        first_accum = e.get_step_accumulated_delay()
        self.assertGreater(first_accum, 0.0)

        e.reset_step_accumulator()
        self.assertEqual(e.get_step_accumulated_delay(), 0.0)

        e.calculate_typing_delay(0)
        self.assertGreater(e.get_step_accumulated_delay(), 0.0)
        self.assertLess(e.get_step_accumulated_delay(), first_accum)


class TestTemporalBiometricCombined(unittest.TestCase):
    """Temporal + biometric applied together."""

    def test_temporal_then_biometric_noise(self):
        p = PersonaProfile(130)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        tm = TemporalModel(p)
        bio = BiometricProfile(p)
        sm.transition("FILLING_FORM")

        raw = e.calculate_typing_delay(0)
        modified = tm.apply_temporal_modifier(raw, "typing")
        noisy = bio.apply_noise(modified)
        self.assertGreaterEqual(noisy, 0.0)


class TestWrapperEndToEnd(unittest.TestCase):
    def test_wrapped_task_returns_correctly(self):
        def task(wid):
            return {"status": "ok", "worker": wid}

        p = PersonaProfile(200)
        wrapped = wrap(task, p)
        result = wrapped("w-200")
        self.assertEqual(result, {"status": "ok", "worker": "w-200"})

    def test_multiple_calls_accumulate_differently(self):
        call_count = [0]

        def task(wid):
            call_count[0] += 1
            return call_count[0]

        p = PersonaProfile(201)
        wrapped = wrap(task, p)
        r1 = wrapped("w-1")
        r2 = wrapped("w-2")
        self.assertEqual(r1, 1)
        self.assertEqual(r2, 2)


class TestConcurrentIntegration(unittest.TestCase):
    def test_10_workers_no_crash(self):
        errors = []

        def task(wid):
            return wid

        def run(seed):
            try:
                p = PersonaProfile(seed)
                wrapped = wrap(task, p)
                sm = BehaviorStateMachine()
                e = DelayEngine(p, sm)
                sm.transition("FILLING_FORM")
                # exercise all components
                e.calculate_typing_delay(0)
                tm = TemporalModel(p)
                tm.apply_micro_variation(1.0)
                bio = BiometricProfile(p)
                bio.generate_4x4_pattern()
                wrapped(f"w-{seed}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TestModuleIsolation(unittest.TestCase):
    """Verify that delay module does not import from outside stdlib + modules/delay."""

    def test_no_cross_module_imports(self):
        import modules.delay.main as delay_mod

        source_file = delay_mod.__file__
        with open(source_file) as f:
            content = f.read()
        # Should not import from integration/ or other modules/
        self.assertNotIn("from integration", content)
        self.assertNotIn("from modules.behavior", content)
        self.assertNotIn("from modules.monitor", content)
        self.assertNotIn("from modules.rollout", content)


if __name__ == "__main__":
    unittest.main()
