"""Tests for gradual drift (Phase 5B Task 3, Blueprint §10).

Covers:
  - AR(1) multiplier remains within ±30% of 1.0
  - Consecutive drift multipliers are correlated (autoregressive),
    while raw Gaussian samples are not
  - Drift is only applied to typing/thinking, not click/operational
  - reset_drift() restores fresh state
  - Drift sequence is deterministic per persona seed
  - ENABLE_GRADUAL_DRIFT env flag toggles behavior off
"""
import unittest
from unittest.mock import patch

from modules.delay.persona import PersonaProfile
from modules.delay.temporal import TemporalModel


class TestGradualDriftBounded(unittest.TestCase):
    """AR(1) multiplier must always stay within [1 - cap, 1 + cap]."""

    def test_gradual_drift_multiplier_bounded(self):
        persona = PersonaProfile(42)
        tm = TemporalModel(persona)
        for _ in range(1000):
            tm.apply_gradual_drift(1.0)
            # pylint: disable=protected-access
            self.assertGreaterEqual(tm._drift_multiplier, 1.0 - 0.30 - 1e-9)
            self.assertLessEqual(tm._drift_multiplier, 1.0 + 0.30 + 1e-9)


class TestGradualDriftAutoregressive(unittest.TestCase):
    """Consecutive multipliers correlate; independent draws do not."""

    @staticmethod
    def _correlation(xs, ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denx = sum((x - mx) ** 2 for x in xs) ** 0.5
        deny = sum((y - my) ** 2 for y in ys) ** 0.5
        if denx == 0 or deny == 0:
            return 0.0
        return num / (denx * deny)

    def test_gradual_drift_autoregressive(self):
        persona = PersonaProfile(123)
        tm = TemporalModel(persona)
        multipliers = []
        for _ in range(500):
            tm.apply_gradual_drift(1.0)
            multipliers.append(tm._drift_multiplier)  # pylint: disable=protected-access
        # AR(1) correlation between m[t] and m[t-1] should be > 0.5.
        # With AR coef=0.98 the theoretical lag-1 autocorrelation ≈ 0.98;
        # 0.5 is a conservative lower bound to tolerate finite-sample noise.
        corr = self._correlation(multipliers[:-1], multipliers[1:])
        self.assertGreater(corr, 0.5,
                           f"Expected AR(1) lag-1 correlation >0.5, got {corr:.3f}")


class TestGradualDriftAffectsOnlyTypingThinking(unittest.TestCase):
    """Drift must not apply to click or unknown action types."""

    def test_drift_not_applied_to_click(self):
        persona = PersonaProfile(42)
        tm = TemporalModel(persona)
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            # click: apply_temporal_modifier returns base unchanged on DAY.
            for _ in range(50):
                result = tm.apply_temporal_modifier(0.1, "click")
                self.assertAlmostEqual(result, 0.1, places=6)
            # Drift step counter must remain 0 (no drift call happened).
            # pylint: disable=protected-access
            self.assertEqual(tm._drift_step_count, 0)

    def test_drift_applied_to_typing_and_thinking(self):
        persona = PersonaProfile(42)
        tm = TemporalModel(persona)
        with patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            tm.apply_temporal_modifier(1.0, "typing")
            tm.apply_temporal_modifier(3.0, "thinking")
        # pylint: disable=protected-access
        self.assertEqual(tm._drift_step_count, 2)


class TestDriftResetOnNewCycle(unittest.TestCase):
    """reset_drift() restores multiplier to 1.0 and counter to 0."""

    def test_drift_resets_on_new_cycle(self):
        persona = PersonaProfile(42)
        tm = TemporalModel(persona)
        for _ in range(20):
            tm.apply_gradual_drift(1.0)
        # pylint: disable=protected-access
        self.assertNotEqual(tm._drift_step_count, 0)
        tm.reset_drift()
        self.assertEqual(tm._drift_multiplier, 1.0)
        self.assertEqual(tm._drift_step_count, 0)


class TestDriftDeterministicPerPersonaSeed(unittest.TestCase):
    """Same persona seed → same drift sequence."""

    def test_drift_deterministic_per_persona_seed(self):
        tm_a = TemporalModel(PersonaProfile(99))
        tm_b = TemporalModel(PersonaProfile(99))
        seq_a = []
        seq_b = []
        for _ in range(50):
            tm_a.apply_gradual_drift(1.0)
            tm_b.apply_gradual_drift(1.0)
            # pylint: disable=protected-access
            seq_a.append(tm_a._drift_multiplier)
            seq_b.append(tm_b._drift_multiplier)
        self.assertEqual(seq_a, seq_b)


class TestDriftEnvFlag(unittest.TestCase):
    """When ENABLE_GRADUAL_DRIFT is False, apply_temporal_modifier skips drift."""

    def test_drift_disabled_when_env_flag_off(self):
        persona = PersonaProfile(42)
        tm = TemporalModel(persona)
        with patch("modules.delay.temporal.ENABLE_GRADUAL_DRIFT", False), \
                patch.object(TemporalModel, "get_time_state", return_value="DAY"):
            result = tm.apply_temporal_modifier(1.0, "typing")
        # No drift applied → result equals base, drift counter stays 0.
        self.assertEqual(result, 1.0)
        # pylint: disable=protected-access
        self.assertEqual(tm._drift_step_count, 0)


if __name__ == "__main__":
    unittest.main()
