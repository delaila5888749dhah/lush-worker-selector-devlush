"""BiometricProfile — Behavioral Anti-Detection Layer (Task 10.6).

Integration status: available for use by card-entry delay injection paths,
including ``inject_card_entry_delays()`` in ``modules/delay/wrapper.py``.
This module does not itself guarantee that those paths are wired in
production.

Generates biometric keystroke timing (log-normal distribution, burst
patterns, 4×4 card-entry rhythm, Gaussian noise) on top of the delay
engine.  Layer 2 — supplements, never replaces Layer 1.

Thread-safe via threading.Lock.  Deterministic via random.Random(seed).
Imports limited to ``modules.delay`` submodules (stdlib only).
"""

import random
import threading

from modules.delay.persona import PersonaProfile
from modules.delay.config import MIN_TYPING_DELAY, MAX_TYPING_DELAY

_KEYSTROKE_MAX: float = 0.3


class BiometricProfile:
    """Generate biometric keystroke timing for a worker."""

    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona
        # seed+2: independent from persona (seed) and temporal (seed+1).
        self._rnd = random.Random(persona._seed + 2)
        self._rnd_lock = threading.Lock()

    def generate_keystroke_delay(self, char_index: int) -> float:
        """Inter-keystroke delay, log-normal distribution, clamped."""
        with self._rnd_lock:
            raw = self._rnd.lognormvariate(-2.5, 0.4)
        return max(0.0, min(raw, _KEYSTROKE_MAX))

    def generate_burst_pattern(self, total_chars: int) -> list[float]:
        """Delay list for each character with burst rhythm."""
        delays: list[float] = []
        for i in range(total_chars):
            if i > 0 and i % 4 == 0:
                with self._rnd_lock:
                    delays.append(min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY))
            else:
                with self._rnd_lock:
                    delays.append(self._rnd.uniform(0.03, 0.08))
        return delays

    def generate_4x4_pattern(self) -> list[float]:
        """16 delay values for 16 card digits (4 fast → pause → repeat)."""
        delays: list[float] = []
        for group in range(4):
            for _ in range(4):
                with self._rnd_lock:
                    delays.append(self._rnd.uniform(0.03, 0.08))
            if group < 3:
                with self._rnd_lock:
                    delays.append(
                        max(MIN_TYPING_DELAY,
                            min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY)))
        return delays

    def apply_noise(self, base_delay: float) -> float:
        """Gaussian noise ±10%."""
        with self._rnd_lock:
            return max(0.0, base_delay + self._rnd.gauss(0, 0.10 * base_delay))
