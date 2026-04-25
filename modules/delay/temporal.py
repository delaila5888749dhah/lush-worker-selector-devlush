"""TemporalModel — Day/Night Behavior Differentiation (Task 10.4).

Simulates biological time cycles, adjusting worker behavior between
DAY (06:00–21:59) and NIGHT (22:00–05:59) modes based on UTC offset.

Thread-safe via threading.Lock.  Imports are limited to modules
within ``modules.delay``; no imports from outside that package.
Deterministic via random.Random(seed) from PersonaProfile.
"""

import contextvars
import random
import threading
import time

from modules.delay.config import (
    MAX_TYPING_DELAY, MAX_HESITATION_DELAY, MAX_STEP_DELAY,
    DAY_START, DAY_END, NIGHT_SPEED_PENALTY_RANGE,
    NIGHT_HESITATION_INCREASE_RANGE, NIGHT_TYPO_INCREASE_RANGE,
    ENABLE_GRADUAL_DRIFT,
)
from modules.delay.persona import PersonaProfile


# ── UTC offset propagation (Phase 5B Task 1) ────────────────────────────────
# ContextVar approach keeps wrapper/inject_step_delay signatures unchanged.
# integration.worker_task sets this value right after the MaxMind lookup so
# that all temporal computations performed inside the same execution context
# (worker thread) see the proxy-derived UTC offset.
_utc_offset_var: "contextvars.ContextVar[float]" = contextvars.ContextVar(
    "delay_utc_offset_hours", default=0.0
)


def set_utc_offset(offset_hours: float) -> None:
    """Set the UTC offset for temporal computations in the current context.

    *offset_hours* is the proxy-derived UTC offset (e.g. ``-8.0`` for PST).
    The value propagates to :meth:`TemporalModel.apply_temporal_modifier`
    and :meth:`TemporalModel.get_night_typo_increase` for any caller in
    the same ``contextvars`` context (typically the same worker thread)
    that does not pass an explicit ``utc_offset_hours`` argument.
    """
    try:
        _utc_offset_var.set(float(offset_hours))
    except (TypeError, ValueError):
        # Defensive: never let a bad MaxMind payload crash worker setup.
        _utc_offset_var.set(0.0)


def get_utc_offset() -> float:
    """Return the current context's UTC offset (defaults to 0.0)."""
    return _utc_offset_var.get()


class TemporalModel:
    """Apply time-of-day, fatigue, and micro-variation modifiers."""

    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona
        # seed+1: intentional offset; worker seeds are CRC32-derived (non-adjacent).
        self._rnd = random.Random(persona._seed + 1)
        self._rnd_lock = threading.Lock()
        # Phase 5B Task 3: AR(1) gradual drift state (slow-varying envelope).
        self._drift_multiplier: float = 1.0
        self._drift_step_count: int = 0
        self._drift_lock = threading.Lock()

    def get_time_state(self, utc_offset_hours: float = 0) -> str:
        """Return ``"DAY"`` or ``"NIGHT"`` based on the persona's active
        hours (Blueprint §10), with midnight wrap-around support.

        DAY is the persona's ``active_hours`` window, inclusive on both
        ends. If ``start > end`` the window wraps past midnight
        (e.g. 22→04 → DAY covers 22..23 and 0..4). When the persona
        exposes no ``active_hours`` attribute, falls back to the global
        ``DAY_START..DAY_END`` constants.

        ``utc_offset_hours`` may be a fractional value (e.g. ``5.5`` for
        IST); only the integer part is consulted for the hour bucket.
        """
        local_hour = (time.gmtime().tm_hour + int(utc_offset_hours)) % 24
        start, end = getattr(self._persona, "active_hours", (DAY_START, DAY_END))
        if start <= end:
            in_day = start <= local_hour <= end
        else:  # wrap-around through midnight
            in_day = local_hour >= start or local_hour <= end
        return "DAY" if in_day else "NIGHT"

    def apply_temporal_modifier(
        self,
        base_delay: float,
        action_type: str,
        utc_offset_hours: "float | None" = None,
    ) -> float:
        """Apply day/night scaling to *base_delay*, clamped by action type.

        Returns 0.0 immediately when *base_delay* is zero or negative (no-op guard).

        NIGHT mode applies different penalties per action type:
        - typing: slowed by ``night_penalty_factor`` (15–30%, Blueprint §10)
        - thinking: increased by ``NIGHT_HESITATION_INCREASE_RANGE`` (20–40%)

        When *utc_offset_hours* is ``None`` the value is read from the
        ``contextvars`` set by :func:`set_utc_offset` (typically populated
        by :mod:`integration.worker_task` after the MaxMind lookup).
        """
        if base_delay <= 0:
            return 0.0
        if utc_offset_hours is None:
            utc_offset_hours = _utc_offset_var.get()
        if self.get_time_state(utc_offset_hours) == "NIGHT":
            if action_type == "thinking":
                with self._rnd_lock:
                    factor = self._rnd.uniform(*NIGHT_HESITATION_INCREASE_RANGE)
                modified = base_delay * (1.0 + factor)
            else:
                modified = base_delay * (1.0 + self._persona.night_penalty_factor)
        else:
            modified = base_delay
        # Phase 5B Task 3: gradual drift only on typing/thinking (not click).
        if ENABLE_GRADUAL_DRIFT and action_type in ("typing", "thinking"):
            modified = self.apply_gradual_drift(modified)
        if action_type == "typing":
            return max(0.0, min(modified, MAX_TYPING_DELAY))
        if action_type == "thinking":
            return max(0.0, min(modified, MAX_HESITATION_DELAY))
        return max(0.0, min(modified, MAX_STEP_DELAY))

    def apply_fatigue(self, base_delay: float, cycle_count: int) -> float:
        """Increase delay after fatigue threshold cycles, clamped to hard limit."""
        if cycle_count <= self._persona.fatigue_threshold:
            return base_delay
        extra = (cycle_count - self._persona.fatigue_threshold) * 0.05
        return min(base_delay + min(extra, 1.0), MAX_STEP_DELAY)

    def apply_micro_variation(self, base_delay: float) -> float:
        """Add ±10% noise to *base_delay*, clamped to a non-negative result."""
        with self._rnd_lock:
            return max(0.0, base_delay * self._rnd.uniform(0.90, 1.10))

    # ── Gradual drift (Blueprint §10, Phase 5B Task 3) ──────────────────────

    # AR(1) drift parameters: slow random walk around 1.0, capped ±30%.
    _DRIFT_AR_COEF: float = 0.98
    _DRIFT_RATE_DEFAULT: float = 0.02
    _DRIFT_CAP_DEFAULT: float = 0.30

    def apply_gradual_drift(
        self,
        base_delay: float,
        *,
        drift_rate: float = _DRIFT_RATE_DEFAULT,
        drift_cap: float = _DRIFT_CAP_DEFAULT,
    ) -> float:
        """AR(1) drift: a slow-varying envelope multiplier (Blueprint §10).

        ``self._drift_multiplier`` random-walks around 1.0 with
        ``_DRIFT_AR_COEF`` mean reversion and per-step Gaussian increment
        of std-dev *drift_rate*. The multiplier is clamped to
        ``[1 - drift_cap, 1 + drift_cap]`` so the envelope never exceeds
        ±30% by default.
        """
        with self._drift_lock:
            self._drift_step_count += 1
            with self._rnd_lock:
                step = self._rnd.gauss(0.0, drift_rate)
            new_mult = (
                self._DRIFT_AR_COEF * self._drift_multiplier
                + (1.0 - self._DRIFT_AR_COEF) * 1.0
                + step
            )
            new_mult = max(1.0 - drift_cap, min(1.0 + drift_cap, new_mult))
            self._drift_multiplier = new_mult
        return base_delay * new_mult

    def reset_drift(self) -> None:
        """Reset drift state to its initial values (call on new cycle)."""
        with self._drift_lock:
            self._drift_multiplier = 1.0
            self._drift_step_count = 0

    def get_current_modifiers(self) -> dict:
        """Return a dict describing the current modifier configuration."""
        return {
            "night_penalty_factor": self._persona.night_penalty_factor,
            "night_hesitation_increase_range": NIGHT_HESITATION_INCREASE_RANGE,
            "night_typo_increase_range": NIGHT_TYPO_INCREASE_RANGE,
            "fatigue_threshold": self._persona.fatigue_threshold,
            "micro_var_range": (0.90, 1.10),
        }

    def get_night_typo_increase(self, utc_offset_hours: "float | None" = None) -> float:
        """Return extra typo probability during NIGHT, 0.0 during DAY.

        Blueprint §10: NIGHT increases typo rate by 1–2% absolute (random in range).
        ``utc_offset_hours=None`` → read from the ContextVar populated by
        :func:`set_utc_offset`.
        """
        if utc_offset_hours is None:
            utc_offset_hours = _utc_offset_var.get()
        if self.get_time_state(utc_offset_hours) == "NIGHT":
            with self._rnd_lock:
                return self._rnd.uniform(*NIGHT_TYPO_INCREASE_RANGE)
        return 0.0
