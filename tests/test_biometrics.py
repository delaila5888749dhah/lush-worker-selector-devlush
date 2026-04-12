"""Tests for BiometricProfile — Task 10.6."""
import unittest

from modules.delay.biometrics import _KEYSTROKE_MAX
from modules.delay.main import PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY, BiometricProfile


class _BioSetup(unittest.TestCase):
    def setUp(self):
        self.persona = PersonaProfile(42)
        self.bio = BiometricProfile(self.persona)


class TestKeystrokeDelay(_BioSetup):
    def test_within_bounds(self):
        for i in range(100):
            d = self.bio.generate_keystroke_delay(i)
            self.assertGreaterEqual(d, 0.0)
            self.assertLessEqual(d, _KEYSTROKE_MAX)


class TestBurstPattern(_BioSetup):
    def test_length_matches(self):
        pattern = self.bio.generate_burst_pattern(16)
        # 16 chars: pauses at positions i=4,8,12 replace the fast keystroke
        # so total length is still 16
        self.assertEqual(len(pattern), 16)

    def test_pause_at_group_boundaries(self):
        pattern = self.bio.generate_burst_pattern(8)
        # 8 chars: pause at i=4, total length = 8
        self.assertEqual(len(pattern), 8)
        # The pause (index 4) should be larger than typical fast key
        self.assertGreater(pattern[4], 0.1)

    def test_all_positive(self):
        pattern = self.bio.generate_burst_pattern(20)
        for d in pattern:
            self.assertGreater(d, 0.0)


class TestFourByFourPattern(_BioSetup):
    def test_length(self):
        pattern = self.bio.generate_4x4_pattern()
        # 4 groups × 4 digits + 3 pauses = 19
        self.assertEqual(len(pattern), 19)

    def test_pause_clamped(self):
        pattern = self.bio.generate_4x4_pattern()
        # Pauses are at indices 4, 9, 14
        for idx in (4, 9, 14):
            self.assertGreaterEqual(pattern[idx], MIN_TYPING_DELAY)
            self.assertLessEqual(pattern[idx], MAX_TYPING_DELAY)

    def test_fast_keys_small(self):
        pattern = self.bio.generate_4x4_pattern()
        fast_indices = [i for i in range(len(pattern)) if i not in (4, 9, 14)]
        for idx in fast_indices:
            self.assertLess(pattern[idx], 0.1)


class TestNoise(_BioSetup):
    def test_noise_positive(self):
        for _ in range(100):
            n = self.bio.apply_noise(1.0)
            self.assertGreaterEqual(n, 0.0)

    def test_noise_around_base(self):
        values = [self.bio.apply_noise(1.0) for _ in range(200)]
        avg = sum(values) / len(values)
        # Average should be close to 1.0 (within 20%)
        self.assertAlmostEqual(avg, 1.0, delta=0.2)


class TestDeterminism(_BioSetup):
    def test_same_seed_same_pattern(self):
        bio2 = BiometricProfile(PersonaProfile(42))
        p1 = self.bio.generate_4x4_pattern()
        p2 = bio2.generate_4x4_pattern()
        self.assertEqual(p1, p2)


class BiometricProductionPathTests(unittest.TestCase):
    """Verify BiometricProfile is reachable from the delay module public API."""

    def test_biometric_profile_accessible_via_delay_main(self):
        from modules.delay.main import BiometricProfile as BP, PersonaProfile as PP
        p = PP(42)
        bio = BP(p)
        pattern = bio.generate_4x4_pattern()
        self.assertEqual(len(pattern), 19)

    def test_biometric_profile_rng_independent_from_persona(self):
        """BiometricProfile RNG must use a sub-seed, not share persona._rnd."""
        from modules.delay.biometrics import BiometricProfile as BP
        p = PersonaProfile(42)
        # Drain persona._rnd with 100 calls
        for _ in range(100):
            p.get_typing_delay(0)
        bio_after_drain = BP(p)

        # Fresh persona (same seed), create BiometricProfile immediately
        p2 = PersonaProfile(42)
        bio_fresh = BP(p2)

        # Both should produce the same 4×4 pattern because bio uses its own seeded RNG
        self.assertEqual(
            bio_after_drain.generate_4x4_pattern(),
            bio_fresh.generate_4x4_pattern(),
        )


if __name__ == "__main__":
    unittest.main()
