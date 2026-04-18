"""Tests for TemporalModel — Task 10.4."""
import time
import unittest
from unittest.mock import patch

from modules.delay.main import (
    PersonaProfile, MAX_TYPING_DELAY, MAX_HESITATION_DELAY, MAX_STEP_DELAY,
    TemporalModel, DAY_START, DAY_END,
    NIGHT_SPEED_PENALTY_RANGE, NIGHT_HESITATION_INCREASE_RANGE, NIGHT_TYPO_INCREASE_RANGE,
)


class _TemporalSetup(unittest.TestCase):
    def setUp(self):
        self.persona = PersonaProfile(42)
        self.tm = TemporalModel(self.persona)


class TestTimeState(_TemporalSetup):
    def test_day_range(self):
        for offset in range(-12, 13):
            state = self.tm.get_time_state(offset)
            self.assertIn(state, ("DAY", "NIGHT"))

    def test_known_day(self):
        gmt = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, 0))
        with patch("modules.delay.temporal.time.gmtime", return_value=gmt):
            self.assertEqual(self.tm.get_time_state(0), "DAY")

    def test_known_night(self):
        gmt = time.struct_time((2026, 1, 1, 3, 0, 0, 3, 1, 0))
        with patch("modules.delay.temporal.time.gmtime", return_value=gmt):
            self.assertEqual(self.tm.get_time_state(0), "NIGHT")


class TestTemporalModifier(_TemporalSetup):
    def test_non_positive_base_returns_zero(self):
        """apply_temporal_modifier() returns 0.0 for zero or negative base delay."""
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            self.assertEqual(self.tm.apply_temporal_modifier(0.0, "typing"), 0.0)
            self.assertEqual(self.tm.apply_temporal_modifier(-1.0, "thinking"), 0.0)

    def test_day_no_penalty(self):
        base = 1.0
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            modified = self.tm.apply_temporal_modifier(base, "typing")
        self.assertEqual(modified, base)

    def test_night_penalty_applied(self):
        base = 1.0
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            modified = self.tm.apply_temporal_modifier(base, "typing")
        self.assertGreater(modified, base)

    def test_typing_clamped(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            modified = self.tm.apply_temporal_modifier(MAX_TYPING_DELAY, "typing")
        self.assertLessEqual(modified, MAX_TYPING_DELAY)

    def test_thinking_clamped(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            modified = self.tm.apply_temporal_modifier(MAX_HESITATION_DELAY, "thinking")
        self.assertLessEqual(modified, MAX_HESITATION_DELAY)


class TestFatigue(_TemporalSetup):
    def test_no_fatigue_below_threshold(self):
        base = 1.0
        result = self.tm.apply_fatigue(base, 0)
        self.assertEqual(result, base)

    def test_fatigue_above_threshold(self):
        base = 1.0
        # Use a high cycle count to trigger fatigue
        result = self.tm.apply_fatigue(base, self.persona.fatigue_threshold + 10)
        self.assertGreater(result, base)

    def test_fatigue_capped(self):
        base = 1.0
        result = self.tm.apply_fatigue(base, self.persona.fatigue_threshold + 1000)
        # Extra should be at most 1.0
        self.assertLessEqual(result - base, 1.0 + 1e-9)

    def test_fatigue_clamped_to_hard_limit(self):
        """apply_fatigue() must never exceed MAX_STEP_DELAY (Blueprint §10 safety)."""
        result = self.tm.apply_fatigue(MAX_HESITATION_DELAY, self.persona.fatigue_threshold + 1000)
        self.assertLessEqual(result, MAX_STEP_DELAY)


class TestMicroVariation(_TemporalSetup):
    def test_variation_bounds(self):
        base = 1.0
        for _ in range(100):
            v = self.tm.apply_micro_variation(base)
            self.assertGreaterEqual(v, 0.8)  # generous lower bound
            self.assertLessEqual(v, 1.2)     # generous upper bound

    def test_deterministic(self):
        tm2 = TemporalModel(PersonaProfile(42))
        a = self.tm.apply_micro_variation(1.0)
        b = tm2.apply_micro_variation(1.0)
        self.assertEqual(a, b)

    def test_negative_base_is_clamped_to_zero(self):
        """apply_micro_variation() clamps negative base delay to 0.0."""
        self.assertEqual(self.tm.apply_micro_variation(-1.0), 0.0)


class TestGetCurrentModifiers(_TemporalSetup):
    def test_keys(self):
        mods = self.tm.get_current_modifiers()
        self.assertIn("night_penalty_factor", mods)
        self.assertIn("fatigue_threshold", mods)
        self.assertIn("micro_var_range", mods)
        self.assertIn("night_hesitation_increase_range", mods)
        self.assertIn("night_typo_increase_range", mods)

    def test_night_typo_value(self):
        mods = self.tm.get_current_modifiers()
        self.assertEqual(mods["night_typo_increase_range"], NIGHT_TYPO_INCREASE_RANGE)


class TestNightHesitationIncrease(_TemporalSetup):
    def test_night_thinking_uses_hesitation_range(self):
        """NIGHT thinking penalty must be within NIGHT_HESITATION_INCREASE_RANGE."""
        base = 3.0
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            modified = self.tm.apply_temporal_modifier(base, "thinking")
        # Must be increased by at least 20% (lower bound of range)
        self.assertGreaterEqual(modified, base * (1.0 + NIGHT_HESITATION_INCREASE_RANGE[0]) - 1e-9)
        # Must be clamped to MAX_HESITATION_DELAY
        self.assertLessEqual(modified, MAX_HESITATION_DELAY)


class TestNightTypoIncrease(_TemporalSetup):
    def test_night_typo_increase(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            increase = self.tm.get_night_typo_increase()
        self.assertGreaterEqual(increase, NIGHT_TYPO_INCREASE_RANGE[0] - 1e-9)
        self.assertLessEqual(increase, NIGHT_TYPO_INCREASE_RANGE[1] + 1e-9)

    def test_night_typo_increase_is_random_in_range(self):
        """Multiple NIGHT calls must produce values across the 1–2% range (not fixed)."""
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            results = {self.tm.get_night_typo_increase() for _ in range(20)}
        # All values must be within range
        for v in results:
            self.assertGreaterEqual(v, NIGHT_TYPO_INCREASE_RANGE[0] - 1e-9)
            self.assertLessEqual(v, NIGHT_TYPO_INCREASE_RANGE[1] + 1e-9)
        # With 20 calls from a seeded RNG, we should see at least 2 distinct values
        self.assertGreater(len(results), 1, "get_night_typo_increase() must be random, not fixed")

    def test_day_typo_no_increase(self):
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            increase = self.tm.get_night_typo_increase()
        self.assertEqual(increase, 0.0)


# ── New test classes (GAP-TM1 → GAP-TM7) ─────────────────────────────────────

class TestTimeStateBoundary(unittest.TestCase):
    """Verify persona-driven DAY/NIGHT window (Blueprint §10, active_hours)."""

    def setUp(self):
        # Persona seed 42 → active_hours == (10, 20).
        self.persona = PersonaProfile(42)
        self.tm = TemporalModel(self.persona)
        self._start, self._end = self.persona.active_hours

    def _state_at_hour(self, hour: int) -> str:
        gmt = time.struct_time((2026, 1, 1, hour, 0, 0, 3, 1, 0))
        with patch("modules.delay.temporal.time.gmtime", return_value=gmt):
            return self.tm.get_time_state(0)

    def test_hour_before_start_is_night(self):
        """One hour before persona active_hours[0] must be NIGHT."""
        self.assertEqual(self._state_at_hour((self._start - 1) % 24), "NIGHT")

    def test_start_hour_is_day(self):
        """persona active_hours[0] must be DAY (inclusive)."""
        self.assertEqual(self._state_at_hour(self._start), "DAY")

    def test_end_hour_is_day(self):
        """persona active_hours[1] must be DAY (inclusive)."""
        self.assertEqual(self._state_at_hour(self._end), "DAY")

    def test_hour_after_end_is_night(self):
        """One hour after persona active_hours[1] must be NIGHT."""
        self.assertEqual(self._state_at_hour((self._end + 1) % 24), "NIGHT")

    def test_midnight_is_night(self):
        """Hour 0 (midnight) must be NIGHT for persona seed 42."""
        self.assertEqual(self._state_at_hour(0), "NIGHT")

    def test_noon_is_day(self):
        """Hour 12 (noon) must be DAY for persona seed 42."""
        self.assertEqual(self._state_at_hour(12), "DAY")

    def test_wrap_around_schedule(self):
        """Wrap-around schedule (start > end) covers the late/early window."""
        # Simulate a persona with wrap-around schedule 22..04.
        self.persona.active_hours = (22, 4)
        self.assertEqual(self._state_at_hour(22), "DAY")
        self.assertEqual(self._state_at_hour(23), "DAY")
        self.assertEqual(self._state_at_hour(0), "DAY")
        self.assertEqual(self._state_at_hour(4), "DAY")
        self.assertEqual(self._state_at_hour(5), "NIGHT")
        self.assertEqual(self._state_at_hour(12), "NIGHT")
        self.assertEqual(self._state_at_hour(21), "NIGHT")


class TestNightPenaltyRange(unittest.TestCase):
    """NIGHT typing penalty must be within NIGHT_SPEED_PENALTY_RANGE (15–30%)."""

    def test_night_typing_penalty_within_range(self):
        """Night typing modifier = base * (1 + penalty_factor), penalty in [0.15, 0.30]."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 1.0
            with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
                modified = tm.apply_temporal_modifier(base, "typing")
            penalty = modified / base - 1.0
            self.assertGreaterEqual(penalty, NIGHT_SPEED_PENALTY_RANGE[0] - 1e-9,
                f"seed={seed}: night typing penalty below lower bound")
            self.assertLessEqual(penalty, NIGHT_SPEED_PENALTY_RANGE[1] + 1e-9,
                f"seed={seed}: night typing penalty above upper bound")

    def test_day_typing_no_penalty_multiple_seeds(self):
        """DAY must not apply any typing penalty for all tested seeds."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 1.0
            with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
                modified = tm.apply_temporal_modifier(base, "typing")
            self.assertEqual(modified, base,
                f"seed={seed}: DAY typing must not apply any penalty")

    def test_night_penalty_varies_across_seeds(self):
        """Different seeds must produce at least some different night penalty values."""
        penalties = set()
        for seed in range(10):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
                modified = tm.apply_temporal_modifier(1.0, "typing")
            penalties.add(round(modified, 10))
        self.assertGreater(len(penalties), 1,
            "All seeds produce identical night penalty — likely broken persona RNG")


class TestNightHesitationPenaltyRange(unittest.TestCase):
    """NIGHT thinking penalty must be within NIGHT_HESITATION_INCREASE_RANGE (20–40%)."""

    def test_night_hesitation_penalty_within_range(self):
        """Night thinking penalty must stay within NIGHT_HESITATION_INCREASE_RANGE."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 3.0
            with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
                modified = tm.apply_temporal_modifier(base, "thinking")
            if modified >= MAX_HESITATION_DELAY - 1e-9:
                continue  # clamped — skip ratio check
            ratio = modified / base - 1.0
            self.assertGreaterEqual(ratio, NIGHT_HESITATION_INCREASE_RANGE[0] - 1e-9,
                f"seed={seed}: thinking penalty below lower bound")
            self.assertLessEqual(ratio, NIGHT_HESITATION_INCREASE_RANGE[1] + 1e-9,
                f"seed={seed}: thinking penalty above upper bound")

    def test_day_hesitation_no_penalty_multiple_seeds(self):
        """DAY must not apply any thinking penalty for all tested seeds."""
        for seed in range(10):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 3.0
            with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
                modified = tm.apply_temporal_modifier(base, "thinking")
            self.assertEqual(modified, base,
                f"seed={seed}: DAY thinking must not apply any penalty")


class TestFatigueMultiPersona(unittest.TestCase):
    """apply_fatigue() must behave correctly across all persona types and seeds."""

    def test_fatigue_not_triggered_at_threshold_for_all_seeds(self):
        """At exactly fatigue_threshold cycles, no extra delay for any seed."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 1.0
            result = tm.apply_fatigue(base, persona.fatigue_threshold)
            self.assertEqual(result, base,
                f"seed={seed}: fatigue triggered at threshold, expected no extra delay")

    def test_fatigue_triggers_one_above_threshold_for_all_seeds(self):
        """1 cycle above threshold must add extra delay for any seed."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            base = 1.0
            result = tm.apply_fatigue(base, persona.fatigue_threshold + 1)
            self.assertGreater(result, base,
                f"seed={seed}: fatigue not triggered at threshold+1")

    def test_fatigue_clamped_to_max_step_delay_for_all_seeds(self):
        """Extreme fatigue must clamp at MAX_STEP_DELAY for any seed."""
        for seed in range(20):
            persona = PersonaProfile(seed)
            tm = TemporalModel(persona)
            result = tm.apply_fatigue(MAX_STEP_DELAY, persona.fatigue_threshold + 1000)
            self.assertEqual(result, MAX_STEP_DELAY,
                f"seed={seed}: fatigue did not clamp to MAX_STEP_DELAY")


class TestMicroVariationMultiSeed(unittest.TestCase):
    """Micro-variation must be reproducible and within ±10% for multiple seeds."""

    def test_reproducible_across_5_seeds(self):
        """Same seed → same micro-variation sequence (verified for seeds 0–4)."""
        sequence_length = 5
        for seed in range(5):
            tm1 = TemporalModel(PersonaProfile(seed))
            tm2 = TemporalModel(PersonaProfile(seed))
            seq1 = [tm1.apply_micro_variation(1.0) for _ in range(sequence_length)]
            seq2 = [tm2.apply_micro_variation(1.0) for _ in range(sequence_length)]
            self.assertEqual(seq1, seq2, f"seed={seed}: micro-variation sequence not reproducible")

    def test_different_seeds_produce_different_values(self):
        """Different seeds must produce at least some different micro-variation values."""
        values = set()
        for seed in range(10):
            tm = TemporalModel(PersonaProfile(seed))
            values.add(tm.apply_micro_variation(1.0))
        self.assertGreater(len(values), 1,
            "All seeds produce identical micro-variation — likely broken RNG")

    def test_variation_within_10_percent_for_multiple_seeds(self):
        """micro_variation must stay within ±10% for all tested seeds."""
        for seed in range(20):
            tm = TemporalModel(PersonaProfile(seed))
            for _ in range(50):
                v = tm.apply_micro_variation(1.0)
                self.assertGreaterEqual(v, 0.9 - 1e-9,
                    f"seed={seed}: micro-variation below 0.9 (lower bound)")
                self.assertLessEqual(v, 1.1 + 1e-9,
                    f"seed={seed}: micro-variation above 1.1 (upper bound)")


class TestDayNightExplicitPatch(unittest.TestCase):
    """Apply temporal modifiers with explicit DAY/NIGHT patch — fully deterministic."""

    def setUp(self):
        self.persona = PersonaProfile(42)
        self.tm = TemporalModel(self.persona)

    def test_day_typing_no_penalty(self):
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            result = self.tm.apply_temporal_modifier(1.0, "typing")
        self.assertEqual(result, 1.0)

    def test_night_typing_penalty_applied(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            result = self.tm.apply_temporal_modifier(1.0, "typing")
        self.assertGreater(result, 1.0)

    def test_night_typing_clamped_to_max(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            result = self.tm.apply_temporal_modifier(MAX_TYPING_DELAY, "typing")
        self.assertLessEqual(result, MAX_TYPING_DELAY)

    def test_day_thinking_no_penalty(self):
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            result = self.tm.apply_temporal_modifier(3.0, "thinking")
        self.assertEqual(result, 3.0)

    def test_night_thinking_penalty_applied(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            result = self.tm.apply_temporal_modifier(3.0, "thinking")
        self.assertGreater(result, 3.0)

    def test_night_thinking_clamped_to_max(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            result = self.tm.apply_temporal_modifier(MAX_HESITATION_DELAY, "thinking")
        self.assertLessEqual(result, MAX_HESITATION_DELAY)

    def test_night_typo_increase_in_range(self):
        with patch.object(TemporalModel, "get_time_state", return_value="NIGHT"):
            increase = self.tm.get_night_typo_increase()
        self.assertGreaterEqual(increase, NIGHT_TYPO_INCREASE_RANGE[0] - 1e-9)
        self.assertLessEqual(increase, NIGHT_TYPO_INCREASE_RANGE[1] + 1e-9)

    def test_day_typo_no_increase(self):
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            increase = self.tm.get_night_typo_increase()
        self.assertEqual(increase, 0.0)


if __name__ == "__main__":
    unittest.main()
