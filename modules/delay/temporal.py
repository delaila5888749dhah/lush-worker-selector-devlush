"""TemporalModel — Day/Night Behavior Differentiation (Task 10.4).

Simulates biological time cycles, adjusting worker behavior between
DAY (06:00–21:59) and NIGHT (22:00–05:59) modes based on UTC offset.

Thread-safe via threading.Lock.  Imports are limited to modules
within ``modules.delay``; no imports from outside that package.
Deterministic via random.Random(seed) from PersonaProfile.
"""

import random
import threading
import time

from modules.delay.persona import PersonaProfile, MAX_TYPING_DELAY
from modules.delay.engine import MAX_HESITATION_DELAY, MAX_STEP_DELAY

# ── Constants (Blueprint §10, SPEC §10.4) ────────────────────────
DAY_START: int = 6
DAY_END: int = 21
NIGHT_SPEED_PENALTY_RANGE: tuple = (0.15, 0.30)
NIGHT_HESITATION_INCREASE_RANGE: tuple = (0.20, 0.40)
NIGHT_TYPO_INCREASE_RANGE: tuple = (0.01, 0.02)


class TemporalModel:
    """Apply time-of-day, fatigue, and micro-variation modifiers."""

    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona
        self._rnd = random.Random(persona._seed + 1)
        self._rnd_lock = threading.Lock()

    @staticmethod
    def get_time_state(utc_offset_hours: int) -> str:
        """Return ``"DAY"`` or ``"NIGHT"`` based on UTC offset."""
        local_hour = (time.gmtime().tm_hour + utc_offset_hours) % 24
        return "DAY" if DAY_START <= local_hour <= DAY_END else "NIGHT"

    def apply_temporal_modifier(
        self, base_delay: float, action_type: str, utc_offset_hours: int = 0
    ) -> float:
        """Apply day/night scaling to *base_delay*, clamped by action type.

        NIGHT mode applies different penalties per action type:
        - typing: slowed by ``night_penalty_factor`` (15–30%, Blueprint §10)
        - thinking: increased by ``NIGHT_HESITATION_INCREASE_RANGE`` (20–40%)
        """
        if self.get_time_state(utc_offset_hours) == "NIGHT":
            if action_type == "thinking":
                with self._rnd_lock:
                    factor = self._rnd.uniform(*NIGHT_HESITATION_INCREASE_RANGE)
                modified = base_delay * (1.0 + factor)
            else:
                modified = base_delay * (1.0 + self._persona.night_penalty_factor)
        else:
            modified = base_delay
        if action_type == "typing":
            return min(modified, MAX_TYPING_DELAY)
        if action_type == "thinking":
            return min(modified, MAX_HESITATION_DELAY)
        return min(modified, MAX_STEP_DELAY)

    def apply_fatigue(self, base_delay: float, cycle_count: int) -> float:
        """Increase delay after fatigue threshold cycles, clamped to hard limit."""
        if cycle_count <= self._persona.fatigue_threshold:
            return base_delay
        extra = (cycle_count - self._persona.fatigue_threshold) * 0.05
        return min(base_delay + min(extra, 1.0), MAX_STEP_DELAY)

    def apply_micro_variation(self, base_delay: float) -> float:
        """Add ±10% noise to *base_delay*."""
        with self._rnd_lock:
            return base_delay * self._rnd.uniform(0.90, 1.10)

    def get_current_modifiers(self) -> dict:
        """Return a dict describing the current modifier configuration."""
        return {
            "night_penalty_factor": self._persona.night_penalty_factor,
            "night_hesitation_increase_range": NIGHT_HESITATION_INCREASE_RANGE,
            "night_typo_increase_range": NIGHT_TYPO_INCREASE_RANGE,
            "fatigue_threshold": self._persona.fatigue_threshold,
            "micro_var_range": (0.90, 1.10),
        }

    def get_night_typo_increase(self, utc_offset_hours: int = 0) -> float:
        """Return extra typo probability during NIGHT, 0.0 during DAY.

        Blueprint §10: NIGHT increases typo rate by 1–2% absolute (random in range).
        """
        if self.get_time_state(utc_offset_hours) == "NIGHT":
            with self._rnd_lock:
                return self._rnd.uniform(*NIGHT_TYPO_INCREASE_RANGE)
        return 0.0
