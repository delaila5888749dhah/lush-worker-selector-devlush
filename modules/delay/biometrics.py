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

# Log-normal parameter centers (Blueprint §9 / K2). Per-persona variance is
# layered on top of these in :class:`BiometricProfile.__init__`.
# exp(_LOGNORM_FAST_MU) ≈ 0.05s median for fast burst keystrokes.
_LOGNORM_MU: float = -2.5
_LOGNORM_SIGMA: float = 0.4
_LOGNORM_FAST_MU: float = -3.0
_LOGNORM_FAST_SIGMA: float = 0.35
# Clamp bounds for fast keystrokes — keep distribution scale consistent with
# the original [0.03, 0.08] range used pre-Phase-5B.
_FAST_MIN: float = 0.03
_FAST_MAX: float = 0.08


class BiometricProfile:
    """Generate biometric keystroke timing for a worker."""

    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona
        # seed+2: independent from persona (seed) and temporal (seed+1).
        self._rnd = random.Random(persona._seed + 2)
        self._rnd_lock = threading.Lock()
        # Per-persona log-normal parameters (Blueprint §9 / K2): each persona
        # gets its own keystroke-distribution shape, not just its own RNG
        # stream. µ is jittered additively, σ multiplicatively, so the
        # distribution location and scale both vary across seeds.
        self._lognorm_mu = _LOGNORM_MU + self._rnd.uniform(-0.2, 0.2)
        self._lognorm_sigma = _LOGNORM_SIGMA * self._rnd.uniform(0.85, 1.15)
        self._lognorm_fast_mu = _LOGNORM_FAST_MU + self._rnd.uniform(-0.2, 0.2)
        self._lognorm_fast_sigma = (
            _LOGNORM_FAST_SIGMA * self._rnd.uniform(0.85, 1.15)
        )

    def generate_keystroke_delay(self, char_index: int) -> float:
        """Inter-keystroke delay, log-normal distribution, clamped."""
        with self._rnd_lock:
            raw = self._rnd.lognormvariate(
                self._lognorm_mu, self._lognorm_sigma
            )
        return max(0.0, min(raw, _KEYSTROKE_MAX))

    def generate_burst_pattern(self, total_chars: int) -> list[float]:
        """Per-keystroke delays for burst-typed fields (Blueprint §9).

        Uses a log-normal distribution that matches real typing
        biomechanics, clamped to ``[_FAST_MIN, _FAST_MAX]``.  Unlike
        :meth:`generate_4x4_pattern` this method does NOT inject any
        inter-group pauses — all keystrokes are fast.
        """
        delays: list[float] = []
        for _ in range(total_chars):
            with self._rnd_lock:
                raw = self._rnd.lognormvariate(
                    self._lognorm_fast_mu, self._lognorm_fast_sigma
                )
            delay = min(max(raw, _FAST_MIN), _FAST_MAX)
            delay = min(delay, MAX_TYPING_DELAY)
            delays.append(delay)
        return delays

    def generate_4x4_pattern(self) -> list[float]:
        """16 delay values for 16 card digits (4 fast → pause → repeat).

        Fast keystrokes use the same log-normal distribution as
        :meth:`generate_burst_pattern`.  Pauses at indices 3, 7, 11
        remain uniform per Blueprint §9 spec (longer dwell between
        4-digit groups).
        """
        delays = self.generate_burst_pattern(16)
        # Override pause positions (indices 3, 7, 11) with longer uniform delay.
        for pause_idx in (3, 7, 11):
            with self._rnd_lock:
                raw_pause = self._rnd.uniform(0.6, 1.8)
            delays[pause_idx] = max(
                MIN_TYPING_DELAY, min(raw_pause, MAX_TYPING_DELAY)
            )
        return delays

    def apply_noise(self, base_delay: float) -> float:
        """Gaussian noise ±10%."""
        with self._rnd_lock:
            return max(0.0, base_delay + self._rnd.gauss(0, 0.10 * base_delay))
