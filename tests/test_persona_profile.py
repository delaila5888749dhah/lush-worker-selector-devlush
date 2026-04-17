"""Tests for PersonaProfile — Task 10.1."""
import threading
import unittest
import zlib

from modules.delay.persona import (
    PersonaProfile,
    MAX_TYPING_DELAY,
    MIN_TYPING_DELAY,
    TYPO_RATE_MIN,
    TYPO_RATE_MAX,
    NIGHT_PENALTY_MIN,
    NIGHT_PENALTY_MAX,
    FATIGUE_THRESHOLD_MIN,
    FATIGUE_THRESHOLD_MAX,
)


class TestDeterminism(unittest.TestCase):
    """Same seed → same profile (Blueprint §8.6, SPEC §10.6)."""

    def test_same_seed_produces_same_profile(self):
        a = PersonaProfile(42)
        b = PersonaProfile(42)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_different_seed_produces_different_profile(self):
        a = PersonaProfile(1)
        b = PersonaProfile(2)
        self.assertNotEqual(a.to_dict(), b.to_dict())

    def test_deterministic_typing_delay(self):
        a = PersonaProfile(99)
        b = PersonaProfile(99)
        self.assertEqual(a.get_typing_delay(0), b.get_typing_delay(0))
        self.assertEqual(a.get_typing_delay(1), b.get_typing_delay(1))

    def test_deterministic_hesitation_delay(self):
        a = PersonaProfile(99)
        b = PersonaProfile(99)
        self.assertEqual(a.get_hesitation_delay(), b.get_hesitation_delay())


class TestBoundaryValues(unittest.TestCase):
    """Verify all attributes fall within spec ranges."""

    def test_typo_rate_bounds(self):
        for seed in range(50):
            p = PersonaProfile(seed)
            self.assertGreaterEqual(p.typo_rate, TYPO_RATE_MIN)
            self.assertLessEqual(p.typo_rate, TYPO_RATE_MAX)

    def test_night_penalty_bounds(self):
        for seed in range(50):
            p = PersonaProfile(seed)
            self.assertGreaterEqual(p.night_penalty_factor, NIGHT_PENALTY_MIN)
            self.assertLessEqual(p.night_penalty_factor, NIGHT_PENALTY_MAX)

    def test_fatigue_threshold_bounds(self):
        for seed in range(50):
            p = PersonaProfile(seed)
            self.assertGreaterEqual(p.fatigue_threshold, FATIGUE_THRESHOLD_MIN)
            self.assertLessEqual(p.fatigue_threshold, FATIGUE_THRESHOLD_MAX)

    def test_typing_delay_clamped(self):
        p = PersonaProfile(7)
        for gi in range(10):
            d = p.get_typing_delay(gi)
            self.assertGreaterEqual(d, MIN_TYPING_DELAY)
            self.assertLessEqual(d, MAX_TYPING_DELAY)

    def test_hesitation_within_pattern(self):
        p = PersonaProfile(7)
        low = p.hesitation_pattern["min"]
        high = p.hesitation_pattern["max"]
        for _ in range(20):
            d = p.get_hesitation_delay()
            self.assertGreaterEqual(d, low - 1e-9)
            self.assertLessEqual(d, high + 1e-9)

    def test_hesitation_pattern_inside_blueprint_band(self):
        """Blueprint §5 / §8.6: hesitation samples must land inside [3.0, 5.0]
        *before* clamping, so the effective distribution is spread across the
        full band instead of pinning at 3.0 s."""
        from modules.delay.config import MIN_THINKING_DELAY, MAX_HESITATION_DELAY
        for seed in range(30):
            p = PersonaProfile(seed)
            self.assertGreaterEqual(p.hesitation_pattern["min"], MIN_THINKING_DELAY - 1e-9)
            self.assertLessEqual(p.hesitation_pattern["max"], MAX_HESITATION_DELAY + 1e-9)
            self.assertLess(p.hesitation_pattern["min"], p.hesitation_pattern["max"])

    def test_typo_probability_matches_rate(self):
        p = PersonaProfile(7)
        self.assertEqual(p.get_typo_probability(), p.typo_rate)


class TestPersonaTypes(unittest.TestCase):
    """Persona type is one of the allowed catalogue values."""

    def test_persona_type_valid(self):
        for seed in range(50):
            p = PersonaProfile(seed)
            self.assertIn(p.persona_type,
                          ("fast_typer", "moderate_typer", "slow_typer", "cautious", "impulsive"))


class TestActiveHours(unittest.TestCase):
    def test_active_hours_tuple(self):
        p = PersonaProfile(42)
        self.assertIsInstance(p.active_hours, tuple)
        self.assertEqual(len(p.active_hours), 2)
        self.assertGreaterEqual(p.active_hours[0], 6)
        self.assertLessEqual(p.active_hours[1], 23)


class TestToDict(unittest.TestCase):
    def test_keys_present(self):
        d = PersonaProfile(1).to_dict()
        for key in ("seed", "persona_type", "typing_speed", "typo_rate",
                     "hesitation_pattern", "active_hours", "fatigue_threshold",
                     "night_penalty_factor"):
            self.assertIn(key, d)


class TestThreadSafety(unittest.TestCase):
    """Concurrent access must not raise or corrupt state."""

    def test_concurrent_typing_delays(self):
        p = PersonaProfile(42)
        results = []
        errors = []

        def worker():
            try:
                for _ in range(100):
                    d = p.get_typing_delay(0)
                    results.append(d)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 800)
        for d in results:
            self.assertGreaterEqual(d, MIN_TYPING_DELAY)
            self.assertLessEqual(d, MAX_TYPING_DELAY)


class WorkerSeedUniquenessTests(unittest.TestCase):
    """Persona seeds for worker-1 through worker-8 must be unique and non-colliding."""

    def test_worker_seeds_produce_unique_personas(self):
        """Different worker IDs → different seeds → different persona attributes."""
        worker_ids = [f"worker-{i}" for i in range(1, 9)]
        seeds = [zlib.crc32(wid.encode()) & 0xFFFFFFFF for wid in worker_ids]
        self.assertEqual(
            len(set(seeds)),
            len(seeds),
            "All worker seeds must be unique",
        )
        personas = [PersonaProfile(s) for s in seeds]
        profiles = [p.to_dict() for p in personas]
        typing_speeds = [p["typing_speed"] for p in profiles]
        self.assertEqual(
            len(set(round(s, 10) for s in typing_speeds)),
            len(typing_speeds),
            "All worker personas must have distinct typing speeds",
        )

    def test_same_worker_id_always_produces_same_persona(self):
        seed = zlib.crc32(b"worker-1") & 0xFFFFFFFF
        p1 = PersonaProfile(seed)
        p2 = PersonaProfile(seed)
        self.assertEqual(p1.to_dict(), p2.to_dict())

    def test_rng_streams_independent_across_temporal_and_biometrics(self):
        """TemporalModel and BiometricProfile RNG streams must be independent."""
        from modules.delay.temporal import TemporalModel
        from modules.delay.biometrics import BiometricProfile
        p = PersonaProfile(42)
        tm = TemporalModel(p)
        bio = BiometricProfile(p)
        temporal_vals = [tm.apply_micro_variation(1.0) for _ in range(10)]
        bio_vals = [bio.apply_noise(1.0) for _ in range(10)]
        self.assertNotEqual(
            temporal_vals,
            bio_vals,
            "Temporal and biometric RNG streams must be independent",
        )


if __name__ == "__main__":
    unittest.main()
