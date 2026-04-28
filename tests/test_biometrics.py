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
        # 16 chars → 16 delays (no inter-group pauses in burst).
        self.assertEqual(len(pattern), 16)

    def test_burst_delays_clamped_to_fast_bounds(self):
        """All burst delays must lie within the fast keystroke band [0.03, 0.08]."""
        from modules.delay.biometrics import _FAST_MIN, _FAST_MAX
        pattern = self.bio.generate_burst_pattern(64)
        for d in pattern:
            self.assertGreaterEqual(d, _FAST_MIN - 1e-9)
            self.assertLessEqual(d, _FAST_MAX + 1e-9)

    def test_all_positive(self):
        pattern = self.bio.generate_burst_pattern(20)
        for d in pattern:
            self.assertGreater(d, 0.0)

    def test_burst_pattern_deterministic_per_seed(self):
        """Same persona seed → same burst sequence."""
        bio2 = BiometricProfile(PersonaProfile(42))
        self.assertEqual(
            self.bio.generate_burst_pattern(32),
            bio2.generate_burst_pattern(32),
        )

    def test_burst_delays_fit_lognormal_distribution(self):
        """Mean and variance of clamped log-normal samples fall in expected bands.

        Theoretical (un-clamped):
            mean ≈ exp(mu + sigma²/2),  exp(-3.0 + 0.35²/2) ≈ 0.0530
        After clamping to [0.03, 0.08]:
            empirical mean stays in roughly [0.04, 0.07] band.
        Standard deviation must be > 0 (non-degenerate distribution).
        """
        import statistics
        n = 10_000
        # Use a fresh seeded biometric profile so this assertion is deterministic.
        bio = BiometricProfile(PersonaProfile(123))
        delays = bio.generate_burst_pattern(n)
        mean = statistics.fmean(delays)
        stdev = statistics.pstdev(delays)
        self.assertGreater(mean, 0.04)
        self.assertLess(mean, 0.07)
        self.assertGreater(stdev, 0.0)


class TestFourByFourPattern(_BioSetup):
    def test_length(self):
        pattern = self.bio.generate_4x4_pattern()
        # 4 groups × 4 digits, pauses at indices 3/7/11 = 16
        self.assertEqual(len(pattern), 16)

    def test_pause_clamped(self):
        pattern = self.bio.generate_4x4_pattern()
        # Pauses are at indices 3, 7, 11
        for idx in (3, 7, 11):
            self.assertGreaterEqual(pattern[idx], MIN_TYPING_DELAY)
            self.assertLessEqual(pattern[idx], MAX_TYPING_DELAY)

    def test_fast_keys_small(self):
        pattern = self.bio.generate_4x4_pattern()
        fast_indices = [i for i in range(len(pattern)) if i not in (3, 7, 11)]
        for idx in fast_indices:
            self.assertLess(pattern[idx], 0.1)

    def test_4x4_pattern_pauses_still_uniform(self):
        """Pauses (indices 3,7,11) sample uniform[0.6,1.8]; fast keys log-normal[0.03,0.08].

        The two distributions must remain distinguishable: pause ranges sit
        well above 0.5s, fast keystrokes well below 0.1s.
        """
        from modules.delay.biometrics import _FAST_MAX
        bio = BiometricProfile(PersonaProfile(7))
        pause_samples = []
        fast_samples = []
        for _ in range(200):
            pat = bio.generate_4x4_pattern()
            for idx in (3, 7, 11):
                pause_samples.append(pat[idx])
            for idx in [i for i in range(16) if i not in (3, 7, 11)]:
                fast_samples.append(pat[idx])
        # All pause samples must be at least 0.5s; all fast samples at most _FAST_MAX.
        self.assertGreaterEqual(min(pause_samples), 0.5)
        self.assertLessEqual(max(fast_samples), _FAST_MAX + 1e-9)


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


class TestPerPersonaLognormParams(unittest.TestCase):
    """K2 — log-normal µ/σ must vary per persona, not just the RNG stream."""

    def test_lognorm_params_vary_per_seed(self):
        """Distribution shape (µ/σ) and empirical mean differ across seeds."""
        import statistics

        seeds = (1, 2, 3, 7, 42, 123, 999)
        mus = set()
        sigmas = set()
        fast_mus = set()
        fast_sigmas = set()
        means = []
        for s in seeds:
            bio = BiometricProfile(PersonaProfile(s))
            mus.add(round(bio._lognorm_mu, 9))
            sigmas.add(round(bio._lognorm_sigma, 9))
            fast_mus.add(round(bio._lognorm_fast_mu, 9))
            fast_sigmas.add(round(bio._lognorm_fast_sigma, 9))
            means.append(statistics.fmean(bio.generate_burst_pattern(2_000)))

        # Per-persona variance: each parameter is sampled independently per
        # seed, so all seven seeds should yield distinct values.
        self.assertEqual(len(mus), len(seeds))
        self.assertEqual(len(sigmas), len(seeds))
        self.assertEqual(len(fast_mus), len(seeds))
        self.assertEqual(len(fast_sigmas), len(seeds))

        # Empirical distribution mean must differ across seeds (not just RNG
        # stream offset). Spread of empirical means must be non-trivial.
        self.assertEqual(len(set(round(m, 6) for m in means)), len(seeds))
        self.assertGreater(max(means) - min(means), 1e-3)

    def test_lognorm_params_stable_per_seed(self):
        """Same seed → identical µ/σ (per-persona, deterministic)."""
        a = BiometricProfile(PersonaProfile(42))
        b = BiometricProfile(PersonaProfile(42))
        self.assertEqual(a._lognorm_mu, b._lognorm_mu)
        self.assertEqual(a._lognorm_sigma, b._lognorm_sigma)
        self.assertEqual(a._lognorm_fast_mu, b._lognorm_fast_mu)
        self.assertEqual(a._lognorm_fast_sigma, b._lognorm_fast_sigma)


class BiometricProductionPathTests(unittest.TestCase):
    """Verify BiometricProfile is reachable from the delay module public API."""

    def test_biometric_profile_accessible_via_delay_main(self):
        from modules.delay.main import BiometricProfile as BP, PersonaProfile as PP
        p = PP(42)
        bio = BP(p)
        pattern = bio.generate_4x4_pattern()
        self.assertEqual(len(pattern), 16)

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
