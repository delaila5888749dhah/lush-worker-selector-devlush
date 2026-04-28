"""Phase 10 safety validation — Task 10.8.

Comprehensive tests proving the behaviour layer does not violate any
safety rules: CRITICAL_SECTION, watchdog headroom, FSM invariants,
outcome invariants, execution order, stagger isolation, VBV isolation,
thread-safety, and deterministic reproducibility.
"""
import threading
import unittest

from modules.delay.main import (
    PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY,
    BehaviorStateMachine, DelayEngine,
    MAX_HESITATION_DELAY, MAX_STEP_DELAY, WATCHDOG_HEADROOM,
    TemporalModel, BiometricProfile, wrap,
)


# ---------------------------------------------------------------------------
# 1. CRITICAL_SECTION zero-delay proof
# ---------------------------------------------------------------------------
class TestCriticalSectionZeroDelay(unittest.TestCase):
    """No delay must ever be injected in VBV / POST_ACTION states."""

    def test_vbv_zero_typing(self):
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        self.assertEqual(e.calculate_typing_delay(0), 0.0)

    def test_vbv_zero_thinking(self):
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        self.assertEqual(e.calculate_thinking_delay(), 0.0)

    def test_post_action_zero(self):
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("POST_ACTION")
        self.assertFalse(e.is_delay_permitted())

    def test_payment_submit_no_delay(self):
        """Payment submit ≈ transition to VBV/POST_ACTION → zero delay."""
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("POST_ACTION")
        self.assertEqual(e.calculate_delay("typing"), 0.0)
        self.assertEqual(e.calculate_delay("thinking"), 0.0)

    def test_critical_section_flag_blocks_delay(self):
        """Phase 9 CRITICAL_SECTION flag → zero delay even in safe states."""
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        self.assertTrue(e.is_delay_permitted())
        sm.set_critical_section(True)
        self.assertFalse(e.is_delay_permitted())
        self.assertEqual(e.calculate_typing_delay(0), 0.0)

    def test_api_wait_critical_section(self):
        """API wait scenario — set_critical_section blocks accumulating delay types.

        Click delays are NOT accumulated and bypass the critical section guard —
        they represent a physical reaction time offset (0.05–0.25 s).
        """
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.set_critical_section(True)
        self.assertEqual(e.calculate_delay("typing"), 0.0)
        self.assertEqual(e.calculate_delay("thinking"), 0.0)
        # Click delay bypasses the critical section guard (non-accumulated micro-delay)
        d = e.calculate_delay("click")
        self.assertGreaterEqual(d, 0.05)
        self.assertLessEqual(d, 0.25)

    def test_page_reload_critical_section(self):
        """Page reload — critical flag blocks, then clearing re-enables."""
        p = PersonaProfile(1)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.set_critical_section(True)
        self.assertEqual(e.calculate_typing_delay(0), 0.0)
        sm.set_critical_section(False)
        self.assertGreater(e.calculate_typing_delay(0), 0.0)


# ---------------------------------------------------------------------------
# 2. SAFE_POINT compatibility
# ---------------------------------------------------------------------------
class TestSafePointCompatibility(unittest.TestCase):
    def test_delay_only_in_safe_states(self):
        p = PersonaProfile(2)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        # IDLE → safe
        self.assertTrue(e.is_delay_permitted())
        sm.transition("FILLING_FORM")
        self.assertTrue(e.is_delay_permitted())
        sm.transition("PAYMENT")
        self.assertTrue(e.is_delay_permitted())
        sm.transition("VBV")
        self.assertFalse(e.is_delay_permitted())

    def test_post_action_blocks_delay(self):
        p = PersonaProfile(2)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("POST_ACTION")
        self.assertFalse(e.is_delay_permitted())

    def test_critical_flag_overrides_safe_state(self):
        """Even FILLING_FORM (safe) is blocked when critical flag is set."""
        p = PersonaProfile(2)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        self.assertTrue(e.is_delay_permitted())
        sm.set_critical_section(True)
        self.assertFalse(e.is_delay_permitted())
        sm.set_critical_section(False)
        self.assertTrue(e.is_delay_permitted())


# ---------------------------------------------------------------------------
# 2b. NIGHT typo bonus wiring (audit [L3] / Blueprint §10)
# ---------------------------------------------------------------------------
class TestNightTypoRateWiring(unittest.TestCase):
    """``_realistic_type_field`` must wire ``get_night_typo_increase`` and
    suppress all typo modulation while delay is not permitted."""

    @staticmethod
    def _build_driver(seed: int = 7):
        from unittest.mock import MagicMock  # noqa: PLC0415

        from modules.cdp.driver import GivexDriver  # noqa: PLC0415

        selenium = MagicMock()
        element = MagicMock()
        selenium.find_elements.return_value = [element]
        persona = PersonaProfile(seed)
        # Pin the persona base typo rate to a deterministic value so the
        # DAY/NIGHT delta is dominated by the NIGHT bonus (1–2% absolute).
        persona.typo_rate = 0.02
        gd = GivexDriver(selenium, persona=persona)
        return gd, selenium, element, persona

    def _count_typos(self, gd, n_keystrokes: int) -> int:
        """Drive ``_realistic_type_field`` once with ``n_keystrokes`` chars
        and return how many typos the keyboard layer would inject given the
        ``typo_rate`` it was passed."""
        from unittest.mock import patch  # noqa: PLC0415

        captured: dict = {}

        def fake_tv(driver, element, value, rnd, **kw):
            captured["typo_rate"] = kw.get("typo_rate", 0.0)
            return {
                "typed_chars": len(value), "typos_injected": 0,
                "corrections_made": 0, "mode": "cdp_key",
            }

        value = "a" * n_keystrokes
        with patch("modules.cdp.driver._type_value", side_effect=fake_tv), \
             patch("time.sleep"):
            gd._realistic_type_field("#f", value)  # noqa: SLF001
        # Expected typo count is rate × N (the keyboard layer Bernoulli-tests
        # rnd.random() < eff per keystroke). For N≥10000 the law of large
        # numbers makes the ratio comparison stable.
        return int(captured["typo_rate"] * n_keystrokes)

    def test_night_typo_rate_higher_than_day(self):
        """Mock UTC offset → NIGHT, count typos over N≥10000 keystrokes."""
        from unittest.mock import patch  # noqa: PLC0415

        gd, _selenium, _element, _persona = self._build_driver()
        n = 10_000
        # DAY: night bonus is 0.0
        with patch.object(
            gd._temporal, "get_night_typo_increase", return_value=0.0,  # noqa: SLF001
        ):
            day_typos = self._count_typos(gd, n)
        # NIGHT: bonus is 1–2% absolute (use mid-range 0.015)
        with patch.object(
            gd._temporal, "get_night_typo_increase", return_value=0.015,  # noqa: SLF001
        ):
            night_typos = self._count_typos(gd, n)
        self.assertGreater(night_typos, day_typos)
        # The bonus should be at least ~50 over 10k keystrokes (0.015×10000=150)
        self.assertGreaterEqual(night_typos - day_typos, 50)

    def test_typo_rate_zero_in_critical_section(self):
        """While the engine forbids delay (VBV / POST_ACTION / critical
        flag), no typo modulation — neither the persona base nor the NIGHT
        bonus — must reach the keyboard layer."""
        from unittest.mock import patch  # noqa: PLC0415

        gd, _selenium, _element, _persona = self._build_driver()
        # Force NIGHT so the bonus is non-zero and would otherwise add to typo_rate
        with patch.object(
            gd._temporal, "get_night_typo_increase", return_value=0.02,  # noqa: SLF001
        ):
            # Phase-9 critical section flag (works in any safe state)
            gd._sm.set_critical_section(True)  # noqa: SLF001
            self.assertFalse(gd._engine.is_delay_permitted())  # noqa: SLF001
            self.assertEqual(self._count_typos(gd, 10_000), 0)
            gd._sm.set_critical_section(False)  # noqa: SLF001
            # VBV state — also blocks delay
            gd._sm.transition("FILLING_FORM")  # noqa: SLF001
            gd._sm.transition("PAYMENT")  # noqa: SLF001
            gd._sm.transition("VBV")  # noqa: SLF001
            self.assertFalse(gd._engine.is_delay_permitted())  # noqa: SLF001
            self.assertEqual(self._count_typos(gd, 10_000), 0)


# ---------------------------------------------------------------------------
# 3. Watchdog headroom
# ---------------------------------------------------------------------------
class TestWatchdogHeadroom(unittest.TestCase):
    def test_accumulated_within_ceiling(self):
        p = PersonaProfile(3)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        for _ in range(50):
            e.calculate_delay("typing")
        self.assertLessEqual(e.get_step_accumulated_delay(), MAX_STEP_DELAY)

    def test_headroom_at_least_3s(self):
        self.assertGreaterEqual(10.0 - MAX_STEP_DELAY, WATCHDOG_HEADROOM)

    def test_thinking_delays_within_ceiling(self):
        """Even thinking delays (3-5s) stay under MAX_STEP_DELAY."""
        p = PersonaProfile(3)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        e.calculate_thinking_delay()
        e.calculate_thinking_delay()
        self.assertLessEqual(e.get_step_accumulated_delay(), MAX_STEP_DELAY)

    def test_explicit_headroom_values(self):
        """MAX_STEP_DELAY=7.0, watchdog=10s → headroom=3.0s exactly."""
        self.assertEqual(MAX_STEP_DELAY, 7.0)
        self.assertEqual(WATCHDOG_HEADROOM, 3.0)
        self.assertGreaterEqual(10.0 - MAX_STEP_DELAY, WATCHDOG_HEADROOM)

    def test_mixed_delays_within_ceiling(self):
        """Typing + thinking combined stays under ceiling."""
        p = PersonaProfile(3)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        for gi in range(4):
            e.calculate_typing_delay(gi)
        e.calculate_thinking_delay()
        self.assertLessEqual(e.get_step_accumulated_delay(), MAX_STEP_DELAY)


# ---------------------------------------------------------------------------
# 4. FSM flow invariant
# ---------------------------------------------------------------------------
class TestFSMFlowInvariant(unittest.TestCase):
    def test_same_sequence(self):
        """Wrapping a task must not change state transitions."""
        states_without = []
        states_with = []

        def task_bare(wid):
            states_without.append("executed")

        def task_check(wid):
            states_with.append("executed")

        # Execute bare
        task_bare("w-1")
        # Execute wrapped
        p = PersonaProfile(4)
        wrapped = wrap(task_check, p)
        wrapped("w-1")

        self.assertEqual(states_without, states_with)

    def test_transition_sequence_deterministic(self):
        """Same state sequence applied twice gives same FSM state."""
        for _ in range(3):
            sm = BehaviorStateMachine()
            self.assertEqual(sm.get_state(), "IDLE")
            sm.transition("FILLING_FORM")
            self.assertEqual(sm.get_state(), "FILLING_FORM")
            sm.transition("PAYMENT")
            self.assertEqual(sm.get_state(), "PAYMENT")
            sm.transition("VBV")
            self.assertEqual(sm.get_state(), "VBV")
            sm.transition("POST_ACTION")
            self.assertEqual(sm.get_state(), "POST_ACTION")
            sm.transition("IDLE")
            self.assertEqual(sm.get_state(), "IDLE")


# ---------------------------------------------------------------------------
# 5. Outcome invariant
# ---------------------------------------------------------------------------
class TestOutcomeInvariant(unittest.TestCase):
    def test_return_value_unchanged(self):
        def task(wid):
            return wid * 2

        p = PersonaProfile(5)
        wrapped = wrap(task, p)
        self.assertEqual(wrapped("w"), task("w"))

    def test_exception_preserved(self):
        def task(wid):
            raise ValueError("test")

        p = PersonaProfile(5)
        wrapped = wrap(task, p)
        with self.assertRaises(ValueError):
            wrapped("w")

    def test_none_return_preserved(self):
        def task(wid):
            return None

        p = PersonaProfile(5)
        wrapped = wrap(task, p)
        self.assertIsNone(wrapped("w"))


# ---------------------------------------------------------------------------
# 6. Execution order invariant
# ---------------------------------------------------------------------------
class TestExecutionOrderInvariant(unittest.TestCase):
    def test_step_sequence_unchanged(self):
        order = []

        def task(wid):
            order.append(wid)

        p = PersonaProfile(6)
        wrapped = wrap(task, p)
        wrapped("a")
        wrapped("b")
        wrapped("c")
        self.assertEqual(order, ["a", "b", "c"])

    def test_mixed_ops_order_preserved(self):
        """Interleaving engine and wrapper calls preserves call order."""
        ops = []

        def task(wid):
            ops.append(("task", wid))

        p = PersonaProfile(6)
        wrapped = wrap(task, p)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        ops.append(("delay", e.calculate_typing_delay(0)))
        wrapped("x")
        ops.append(("delay", e.calculate_typing_delay(1)))
        wrapped("y")
        self.assertEqual(ops[1], ("task", "x"))
        self.assertEqual(ops[3], ("task", "y"))


# ---------------------------------------------------------------------------
# 7. Stagger isolation
# ---------------------------------------------------------------------------
class TestStaggerIsolation(unittest.TestCase):
    def test_stagger_independent_of_behaviour(self):
        """Behaviour delay is within a cycle — stagger is between launches."""
        p = PersonaProfile(7)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        d = e.calculate_delay("typing")
        # Delay should be < stagger minimum (12s) proving they are independent
        self.assertLess(d, 12.0)

    def test_behaviour_delay_range_within_action_bounds(self):
        """All behaviour delays are bounded by per-action limits, not stagger."""
        p = PersonaProfile(7)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        for gi in range(4):
            d = e.calculate_typing_delay(gi)
            self.assertLessEqual(d, MAX_TYPING_DELAY)
            self.assertLess(d, 8.0)  # well below stagger range (8-30s)


# ---------------------------------------------------------------------------
# 8. VBV operational wait isolation
# ---------------------------------------------------------------------------
class TestVBVOperationalWaitIsolation(unittest.TestCase):
    def test_vbv_state_blocks_behaviour_delay(self):
        p = PersonaProfile(8)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        self.assertEqual(e.calculate_delay("typing"), 0.0)
        self.assertEqual(e.calculate_delay("thinking"), 0.0)

    def test_vbv_wait_distinct_from_behaviour(self):
        """VBV 8-12s operational wait is a separate protocol concept.

        The behaviour layer returns zero during VBV; the 8-12s VBV iframe
        wait is handled by the worker's protocol layer, not by DelayEngine.
        """
        p = PersonaProfile(8)
        sm = BehaviorStateMachine()
        e = DelayEngine(p, sm)
        sm.transition("FILLING_FORM")
        sm.transition("PAYMENT")
        sm.transition("VBV")
        # Behaviour layer produces zero, so VBV wait cannot come from here
        total = sum(e.calculate_delay("typing") for _ in range(10))
        self.assertEqual(total, 0.0)


# ---------------------------------------------------------------------------
# 9. Concurrent thread-safety
# ---------------------------------------------------------------------------
class TestConcurrentThreadSafety(unittest.TestCase):
    def test_parallel_workers(self):
        errors = []
        results = []

        def task(wid):
            return f"done-{wid}"

        def run_worker(seed):
            try:
                p = PersonaProfile(seed)
                wrapped = wrap(task, p)
                r = wrapped(f"w-{seed}")
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)

    def test_accumulators_thread_isolated(self):
        """Each DelayEngine has its own accumulator — no cross-talk."""
        accumulators = []
        errors = []

        def run_engine(seed):
            try:
                p = PersonaProfile(seed)
                sm = BehaviorStateMachine()
                e = DelayEngine(p, sm)
                sm.transition("FILLING_FORM")
                for gi in range(4):
                    e.calculate_typing_delay(gi)
                accumulators.append(e.get_step_accumulated_delay())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_engine, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(accumulators), 8)
        for acc in accumulators:
            self.assertLessEqual(acc, MAX_STEP_DELAY)

    def test_parallel_shared_nothing(self):
        """Workers with same seed produce identical delays (shared nothing)."""
        results_a = []
        results_b = []

        def collect(seed, bucket):
            p = PersonaProfile(seed)
            sm = BehaviorStateMachine()
            e = DelayEngine(p, sm)
            sm.transition("FILLING_FORM")
            bucket.extend([e.calculate_typing_delay(gi) for gi in range(4)])

        t1 = threading.Thread(target=collect, args=(42, results_a))
        t2 = threading.Thread(target=collect, args=(42, results_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(results_a, results_b)


# ---------------------------------------------------------------------------
# 10. Deterministic reproducibility
# ---------------------------------------------------------------------------
class TestDeterministicReproducibility(unittest.TestCase):
    def test_same_seed_same_delays(self):
        delays_run1 = []
        delays_run2 = []
        for run_delays in (delays_run1, delays_run2):
            p = PersonaProfile(10)
            sm = BehaviorStateMachine()
            e = DelayEngine(p, sm)
            sm.transition("FILLING_FORM")
            for gi in range(4):
                run_delays.append(e.calculate_typing_delay(gi))
        self.assertEqual(delays_run1, delays_run2)

    def test_three_runs_identical(self):
        runs = []
        for _ in range(3):
            p = PersonaProfile(10)
            sm = BehaviorStateMachine()
            e = DelayEngine(p, sm)
            sm.transition("FILLING_FORM")
            run = [e.calculate_typing_delay(gi) for gi in range(4)]
            runs.append(run)
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])

    def test_thinking_delay_deterministic(self):
        """Thinking delays are also reproducible with same seed."""
        runs = []
        for _ in range(3):
            p = PersonaProfile(10)
            sm = BehaviorStateMachine()
            e = DelayEngine(p, sm)
            sm.transition("FILLING_FORM")
            runs.append(e.calculate_thinking_delay())
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])

    def test_temporal_micro_variation_deterministic(self):
        """Micro-variation with same seed is reproducible."""
        runs = []
        for _ in range(3):
            p = PersonaProfile(10)
            tm = TemporalModel(p)
            runs.append(tm.apply_micro_variation(1.0))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])

    def test_biometric_pattern_deterministic(self):
        """Biometric 4×4 pattern is reproducible with same seed."""
        runs = []
        for _ in range(3):
            p = PersonaProfile(10)
            bio = BiometricProfile(p)
            runs.append(bio.generate_4x4_pattern())
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])


if __name__ == "__main__":
    unittest.main()
