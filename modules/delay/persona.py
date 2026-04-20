"""PersonaProfile — Seed-Based Persona Generation (Task 10.1).

Deterministic worker personality from seed.
Timing constants from modules.delay.config.
"""
import random
import threading

from modules.delay.config import (
    MAX_TYPING_DELAY, MIN_TYPING_DELAY, TYPO_RATE_MIN, TYPO_RATE_MAX,
    NIGHT_PENALTY_MIN, NIGHT_PENALTY_MAX, FATIGUE_THRESHOLD_MIN,
    FATIGUE_THRESHOLD_MAX, MIN_CLICK_DELAY, MAX_CLICK_DELAY,
    MIN_THINKING_DELAY, MAX_HESITATION_DELAY,
)

_PERSONA_TYPES = ("fast_typer", "moderate_typer", "slow_typer", "cautious", "impulsive")


class PersonaProfile:
    """Seed-deterministic persona providing behavioral attributes for a worker."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rnd = random.Random(seed)
        self._rnd_lock = threading.Lock()
        self.persona_type: str = self._rnd.choice(_PERSONA_TYPES)
        self.typing_speed: float = self._rnd.uniform(0.04, 0.12)
        self.typo_rate: float = self._rnd.uniform(TYPO_RATE_MIN, TYPO_RATE_MAX)
        # Blueprint §5 / §8.6: hesitation must land inside [3.0, 5.0] s
        # *before* clamping, so the effective distribution is spread across
        # the full band instead of collapsing against the MIN_THINKING_DELAY
        # floor (old range was 0.5–1.5 / 2.0–5.0, which clamped most
        # samples to exactly 3.0 s and felt robotic).
        _mid = (MIN_THINKING_DELAY + MAX_HESITATION_DELAY) / 2.0
        hes_lo = self._rnd.uniform(MIN_THINKING_DELAY, _mid)
        hes_hi = self._rnd.uniform(_mid + 0.1, MAX_HESITATION_DELAY)
        self.hesitation_pattern: dict = {
            "min": hes_lo,
            "max": max(hes_hi, hes_lo + 0.1),
        }
        self.active_hours: tuple = (
            self._rnd.choice((6, 7, 8, 9, 10)),
            self._rnd.choice((20, 21, 22, 23)),
        )
        self.fatigue_threshold: int = self._rnd.randint(FATIGUE_THRESHOLD_MIN, FATIGUE_THRESHOLD_MAX)
        self.night_penalty_factor: float = self._rnd.uniform(NIGHT_PENALTY_MIN, NIGHT_PENALTY_MAX)

    def get_typing_delay(self, group_index: int) -> float:
        """Return a single typing-group delay (s).

        Blueprint §10 (M8): the per-group distribution is **gaussian**
        around the midpoint of ``[MIN_TYPING_DELAY, MAX_TYPING_DELAY]``
        with stddev set so the band ≈ 99.7% (±3σ) of the distribution.
        Samples are clamped to the hard min/max so the result is always
        inside the contractual range.
        """
        lo, hi = MIN_TYPING_DELAY, MAX_TYPING_DELAY
        mu = (lo + hi) / 2.0
        sigma = (hi - lo) / 6.0
        with self._rnd_lock:
            base = self._rnd.gauss(mu, sigma)
        factor = max(0.85, 1.0 - group_index * 0.03)
        return max(lo, min(base * factor, hi))

    def get_hesitation_delay(self) -> float:
        with self._rnd_lock:
            return self._rnd.uniform(self.hesitation_pattern["min"], self.hesitation_pattern["max"])

    def get_click_delay(self) -> float:
        """Reaction-time offset for click actions (0.05–0.25 s)."""
        with self._rnd_lock:
            return self._rnd.uniform(MIN_CLICK_DELAY, MAX_CLICK_DELAY)

    def get_typo_probability(self) -> float:
        return self.typo_rate

    def to_dict(self) -> dict:
        return {
            "seed": self._seed,
            "persona_type": self.persona_type,
            "typing_speed": self.typing_speed,
            "typo_rate": self.typo_rate,
            "hesitation_pattern": dict(self.hesitation_pattern),
            "active_hours": self.active_hours,
            "fatigue_threshold": self.fatigue_threshold,
            "night_penalty_factor": self.night_penalty_factor,
        }
