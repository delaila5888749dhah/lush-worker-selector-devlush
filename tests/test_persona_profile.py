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
from modules.delay.config import MIN_THINKING_DELAY, MAX_HESITATION_DELAY


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
        for seed in range(30):
            p = PersonaProfile(seed)
            self.assertGreaterEqual(p.hesitation_pattern["min"], MIN_THINKING_DELAY - 1e-9)
            self.assertLessEqual(p.hesitation_pattern["max"], MAX_HESITATION_DELAY + 1e-9)
            self.assertLess(p.hesitation_pattern["min"], p.hesitation_pattern["max"])

    def test_typo_probability_matches_rate(self):
        p = PersonaProfile(7)
        self.assertEqual(p.get_typo_probability(), p.typo_rate)


class TestPersonaTypes(unittest.TestCase):
    """Persona archetype is one of the spec catalogue values (Blueprint §2/§8)."""

    def test_persona_archetype_valid(self):
        for seed in range(50):
            p = PersonaProfile(seed)
            self.assertIn(p.persona_archetype, ("old", "young", "woman", "man"))

    def test_persona_type_aliases_archetype(self):
        for seed in range(20):
            p = PersonaProfile(seed)
            self.assertEqual(p.persona_type, p.persona_archetype)

    def test_archetype_deterministic_per_seed(self):
        """Same seed must yield the same archetype across constructions."""
        for seed in (1, 2, 3, 42, 99, 1000):
            self.assertEqual(
                PersonaProfile(seed).persona_archetype,
                PersonaProfile(seed).persona_archetype,
            )

    def test_old_slower_than_young_typing(self):
        """Behavioural gap: aggregated 'old' typing_speed > 'young' typing_speed.

        Sampling many seeds and grouping by archetype must produce a clear
        separation because of the per-archetype typing_mult in
        ``_ARCHETYPE_PARAMS`` (Blueprint §8 / §9).
        """
        old_speeds = []
        young_speeds = []
        for seed in range(2000):
            p = PersonaProfile(seed)
            if p.persona_archetype == "old":
                old_speeds.append(p.typing_speed)
            elif p.persona_archetype == "young":
                young_speeds.append(p.typing_speed)
        self.assertGreater(len(old_speeds), 50)
        self.assertGreater(len(young_speeds), 50)
        old_mean = sum(old_speeds) / len(old_speeds)
        young_mean = sum(young_speeds) / len(young_speeds)
        self.assertGreater(
            old_mean, young_mean,
            f"old_mean={old_mean:.3f} should exceed young_mean={young_mean:.3f}",
        )

    def test_typing_delay_uses_typing_speed(self):
        """``get_typing_delay`` must be derived from ``typing_speed``.

        The mean delay across many calls (group_index=0) should track the
        persona's ``typing_speed`` within the ±10 % jitter band.
        """
        p = PersonaProfile(123)
        samples = [p.get_typing_delay(0) for _ in range(500)]
        mean = sum(samples) / len(samples)
        # Mean of uniform jitter in [0.9, 1.1] is 1.0, so mean ≈ typing_speed
        # (clamped). Allow generous tolerance for small-sample noise / clamps.
        self.assertAlmostEqual(mean, p.typing_speed, delta=0.15)


class TestArchetypeParamsCoverage(unittest.TestCase):
    """Mapping invariants: catalogue ↔ params table must stay in sync."""

    def test_every_declared_archetype_has_params_entry(self):
        from modules.delay.persona import (
            _PERSONA_ARCHETYPES, _ARCHETYPE_PARAMS, _REQUIRED_PARAM_FIELDS,
        )
        for name in _PERSONA_ARCHETYPES:
            self.assertIn(
                name, _ARCHETYPE_PARAMS,
                f"archetype {name!r} declared without params entry",
            )
            for field in _REQUIRED_PARAM_FIELDS:
                self.assertIn(
                    field, _ARCHETYPE_PARAMS[name],
                    f"archetype {name!r} missing field {field!r}",
                )

    def test_no_orphan_params_entries(self):
        from modules.delay.persona import _PERSONA_ARCHETYPES, _ARCHETYPE_PARAMS
        orphan = set(_ARCHETYPE_PARAMS) - set(_PERSONA_ARCHETYPES)
        self.assertEqual(
            orphan, set(),
            f"params entries without declared archetype: {sorted(orphan)}",
        )

    def test_validate_raises_on_missing_params_entry(self):
        """Drift detection: declared archetype without params → RuntimeError."""
        from modules.delay import persona as persona_mod
        original = persona_mod._PERSONA_ARCHETYPES
        try:
            persona_mod._PERSONA_ARCHETYPES = original + ("ghost",)
            with self.assertRaises(RuntimeError) as ctx:
                persona_mod._validate_archetype_params()
            self.assertIn("ghost", str(ctx.exception))
        finally:
            persona_mod._PERSONA_ARCHETYPES = original

    def test_validate_raises_on_orphan_params_entry(self):
        """Drift detection: params entry without declared archetype → RuntimeError."""
        from modules.delay import persona as persona_mod
        persona_mod._ARCHETYPE_PARAMS["ghost"] = {
            "typing_mult": 1.0,
            "hesitation_mult": 1.0,
            "fatigue_threshold": (5, 9),
            "night_penalty": (0.20, 0.25),
        }
        try:
            with self.assertRaises(RuntimeError) as ctx:
                persona_mod._validate_archetype_params()
            self.assertIn("ghost", str(ctx.exception))
        finally:
            persona_mod._ARCHETYPE_PARAMS.pop("ghost", None)

    def test_validate_raises_on_missing_required_field(self):
        """Drift detection: params entry missing required field → RuntimeError."""
        from modules.delay import persona as persona_mod
        original = persona_mod._ARCHETYPE_PARAMS["man"]
        try:
            persona_mod._ARCHETYPE_PARAMS["man"] = {"typing_mult": 1.0}
            with self.assertRaises(RuntimeError) as ctx:
                persona_mod._validate_archetype_params()
            self.assertIn("man", str(ctx.exception))
        finally:
            persona_mod._ARCHETYPE_PARAMS["man"] = original

    def test_each_archetype_constructs_persona(self):
        """Every declared archetype must produce a working PersonaProfile.

        Sampling enough seeds to cover all four archetypes proves the runtime
        lookup ``_ARCHETYPE_PARAMS[persona_archetype]`` does not raise for any
        catalogue value.
        """
        from modules.delay.persona import _PERSONA_ARCHETYPES
        seen: set = set()
        for seed in range(1000):
            p = PersonaProfile(seed)
            seen.add(p.persona_archetype)
            if seen == set(_PERSONA_ARCHETYPES):
                break
        self.assertEqual(seen, set(_PERSONA_ARCHETYPES))


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
        for key in ("seed", "persona_type", "persona_archetype", "typing_speed",
                     "typo_rate", "hesitation_pattern", "active_hours",
                     "fatigue_threshold", "night_penalty_factor"):
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
