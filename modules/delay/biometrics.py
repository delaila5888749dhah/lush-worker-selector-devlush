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

# Phase 5B Task 2 — log-normal parameters for fast-burst keystrokes
# (Blueprint §9 "inter-keystroke delay").  ``exp(-3.0) ≈ 0.0498s`` — the
# median sits near the middle of the legacy ``[0.03, 0.08]`` uniform
# window, and the tight sigma keeps most samples inside the clamp.
_LOGNORM_FAST_MU: float = -3.0
_LOGNORM_FAST_SIGMA: float = 0.35
_FAST_MIN: float = 0.03
_FAST_MAX: float = 0.08


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

    def _fast_keystroke_delay(self) -> float:
        """Sample one fast-burst keystroke delay (log-normal, clamped)."""
        with self._rnd_lock:
            raw = self._rnd.lognormvariate(_LOGNORM_FAST_MU, _LOGNORM_FAST_SIGMA)
        delay = min(max(raw, _FAST_MIN), _FAST_MAX)
        return min(delay, MAX_TYPING_DELAY)

    def generate_burst_pattern(self, total_chars: int) -> list[float]:
        """Delay list for each character with burst rhythm.

        Fast keystrokes use a log-normal distribution (matches real typing
        biomechanics — Blueprint §9) clamped to ``[_FAST_MIN, _FAST_MAX]``.
        Group-boundary pauses (every 4 chars) keep the existing uniform
        distribution to preserve the "burst → pause → burst" rhythm.
        """
        delays: list[float] = []
        for i in range(total_chars):
            if i > 0 and i % 4 == 0:
                with self._rnd_lock:
                    delays.append(min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY))
            else:
                delays.append(self._fast_keystroke_delay())
        return delays

    def generate_4x4_pattern(self) -> list[float]:
        """16 delay values for 16 card digits (4 fast → pause → repeat).

        Fast keystrokes (indices 0-2, 4-6, 8-10, 12-15) are log-normally
        distributed per Blueprint §9; pauses at indices 3, 7, 11 retain
        their uniform distribution (spec §9 "inter-group hesitation").
        """
        delays: list[float] = []
        for i in range(16):
            if i in (3, 7, 11):
                with self._rnd_lock:
                    delays.append(
                        max(MIN_TYPING_DELAY,
                            min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY)))
            else:
                delays.append(self._fast_keystroke_delay())
        return delays

    def apply_noise(self, base_delay: float) -> float:
        """Gaussian noise ±10%."""
        with self._rnd_lock:
            return max(0.0, base_delay + self._rnd.gauss(0, 0.10 * base_delay))
