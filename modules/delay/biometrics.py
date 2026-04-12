"""BiometricProfile — Behavioral Anti-Detection Layer (Task 10.6).

Generates biometric keystroke timing (log-normal distribution, burst
patterns, 4×4 card-entry rhythm, Gaussian noise) on top of the delay
engine.  Layer 2 — supplements, never replaces Layer 1.

Thread-safe via threading.Lock.  Deterministic via random.Random(seed).
Imports limited to ``modules.delay`` submodules (stdlib only).
"""

import hashlib
import random
import threading

from modules.delay.persona import PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY

_KEYSTROKE_MAX: float = 0.3


class BiometricProfile:
    """Generate biometric keystroke timing for a worker."""

    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona
        # Hash-derived sub-seed reduces inter-stream correlation with TemporalModel.
        _sub_seed = int(
            hashlib.sha256(f"{persona._seed}:biometrics".encode()).hexdigest(), 16
        ) % (2 ** 32)
        self._rnd = random.Random(_sub_seed)
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
