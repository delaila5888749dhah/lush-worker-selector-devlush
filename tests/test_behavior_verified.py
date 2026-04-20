"""PR-4 M8/M9/M10 — Behavior layer verification tests."""
import statistics
import unittest
from unittest.mock import patch

from modules.delay import wrapper as delay_wrapper
from modules.delay.config import (
    MIN_TYPING_DELAY, MAX_TYPING_DELAY, NIGHT_TYPO_INCREASE_RANGE,
)
from modules.delay.persona import PersonaProfile
from modules.delay.temporal import TemporalModel


class TypingDelayDistributionTests(unittest.TestCase):
    def test_typing_delay_distribution_is_lognormal_or_gaussian(self):
        """M8: get_typing_delay samples are peaked around the midpoint, not flat.

        We reject the null hypothesis that the distribution is uniform by
        checking that the empirical stddev is distinguishable from the
        uniform stddev = range/sqrt(12) ≈ 0.289·range.  A gaussian with
        σ = range/6 gives ≈ 0.17·range.  We therefore require:

            stddev_observed < 0.25 · range
        """
        p = PersonaProfile(seed=1234)
        samples = [p.get_typing_delay(0) for _ in range(5000)]
        rng = MAX_TYPING_DELAY - MIN_TYPING_DELAY
        mean = statistics.fmean(samples)
        sd = statistics.pstdev(samples)
        # Mean concentrated near midpoint, not skewed by clamping.
        self.assertAlmostEqual(
            mean, (MIN_TYPING_DELAY + MAX_TYPING_DELAY) / 2.0, delta=rng * 0.1,
        )
        # Narrower than a uniform over the same range.
        self.assertLess(sd, 0.25 * rng,
                        f"stddev={sd:.3f} suggests uniform distribution")
        # All samples must be clamped to the allowed band.
        self.assertTrue(all(MIN_TYPING_DELAY <= s <= MAX_TYPING_DELAY for s in samples))


class NightTypoBumpTests(unittest.TestCase):
    def test_night_typo_bump_1_to_2_percent(self):
        """M9: NIGHT adds typo probability in [0.01, 0.02]."""
        self.assertEqual(NIGHT_TYPO_INCREASE_RANGE, (0.01, 0.02))
        p = PersonaProfile(seed=42)
        t = TemporalModel(p)
        with patch.object(t, "get_time_state", return_value="NIGHT"):
            samples = [t.get_night_typo_increase() for _ in range(200)]
        self.assertTrue(all(0.01 <= s <= 0.02 for s in samples))
        # DAY must return 0.0.
        with patch.object(t, "get_time_state", return_value="DAY"):
            self.assertEqual(t.get_night_typo_increase(), 0.0)


class FatigueCycleCountTests(unittest.TestCase):
    def test_fatigue_invoked_with_cycle_count(self):
        """M10: wrapper.inject_step_delay forwards cycle_count to apply_fatigue."""
        persona = PersonaProfile(seed=99)
        temporal = TemporalModel(persona)
        seen = []

        def spy(base_delay, cycle_count):
            seen.append(cycle_count)
            return base_delay

        from modules.delay.engine import DelayEngine  # noqa: PLC0415
        from modules.delay.state import BehaviorStateMachine  # noqa: PLC0415

        sm = BehaviorStateMachine()
        engine = DelayEngine(persona, sm)
        sm.transition("FILLING_FORM")
        with patch.object(temporal, "apply_fatigue", side_effect=spy), \
             patch("time.sleep"):
            delay_wrapper.inject_step_delay(
                engine, temporal, "thinking",
                stop_event=None, cycle_count=17,
            )
        # apply_fatigue should have been called with cycle_count=17.
        self.assertIn(17, seen)


if __name__ == "__main__":
    unittest.main()
