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

# Spec archetypes (Blueprint §2 / §8): persona describes the worker as one of
# four demographic profiles — "old", "young", "woman", "man" — and behavioural
# parameters are derived from this archetype rather than sampled uniformly.
_PERSONA_ARCHETYPES = ("old", "young", "woman", "man")
# Backwards-compatible alias retained so external consumers that imported
# ``_PERSONA_TYPES`` continue to resolve.
_PERSONA_TYPES = _PERSONA_ARCHETYPES

# Behavioural parameter table per archetype (Blueprint §8 / §9):
#   - typing_mult / hesitation_mult scale the base sampled delays
#   - fatigue_threshold gives an archetype-specific (min,max) sub-range
#   - night_penalty gives an archetype-specific (min,max) sub-range
# Sub-ranges are clamped to the global config bounds at sample time so env
# overrides of FATIGUE_THRESHOLD_* / NIGHT_PENALTY_* still take precedence.
_ARCHETYPE_PARAMS: dict = {
    "young": {
        "typing_mult": 0.85,
        "hesitation_mult": 0.85,
        "fatigue_threshold": (10, 15),
        "night_penalty": (0.15, 0.22),
    },
    "woman": {
        "typing_mult": 0.95,
        "hesitation_mult": 1.00,
        "fatigue_threshold": (8, 13),
        "night_penalty": (0.18, 0.25),
    },
    "man": {
        "typing_mult": 1.05,
        "hesitation_mult": 1.00,
        "fatigue_threshold": (7, 12),
        "night_penalty": (0.18, 0.25),
    },
    "old": {
        "typing_mult": 1.25,
        "hesitation_mult": 1.20,
        "fatigue_threshold": (5, 9),
        "night_penalty": (0.22, 0.30),
    },
}


def _clamp_range(lo: float, hi: float, gmin: float, gmax: float) -> tuple:
    """Clamp ``(lo, hi)`` into ``[gmin, gmax]`` preserving order."""
    lo2 = max(gmin, min(lo, gmax))
    hi2 = max(gmin, min(hi, gmax))
    if lo2 > hi2:
        lo2, hi2 = hi2, lo2
    return lo2, hi2


class PersonaProfile:
    """Seed-deterministic persona providing behavioral attributes for a worker."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rnd = random.Random(seed)
        self._rnd_lock = threading.Lock()
        # Sample archetype first so all subsequent attributes can be derived
        # from the archetype's parameter table (Blueprint §8 / §9).
        self.persona_archetype: str = self._rnd.choice(_PERSONA_ARCHETYPES)
        # ``persona_type`` is retained as an alias for backwards-compatibility
        # with monitor tags / driver logging that already key on it.
        self.persona_type: str = self.persona_archetype
        params = _ARCHETYPE_PARAMS[self.persona_archetype]

        # typing_speed — base typing delay (in seconds), scaled by the
        # archetype's typing_mult and clamped to [MIN, MAX].  This is now the
        # *active* base used by ``get_typing_delay`` rather than a dead field.
        _typing_base = self._rnd.uniform(MIN_TYPING_DELAY, MAX_TYPING_DELAY)
        self.typing_speed: float = max(
            MIN_TYPING_DELAY,
            min(_typing_base * params["typing_mult"], MAX_TYPING_DELAY),
        )
        self.typo_rate: float = self._rnd.uniform(TYPO_RATE_MIN, TYPO_RATE_MAX)
        # Blueprint §5 / §8.6: hesitation must land inside [3.0, 5.0] s
        # *before* clamping, so the effective distribution is spread across
        # the full band instead of collapsing against the MIN_THINKING_DELAY
        # floor (old range was 0.5–1.5 / 2.0–5.0, which clamped most
        # samples to exactly 3.0 s and felt robotic).
        _mid = (MIN_THINKING_DELAY + MAX_HESITATION_DELAY) / 2.0
        hes_lo = self._rnd.uniform(MIN_THINKING_DELAY, _mid)
        hes_hi = self._rnd.uniform(_mid + 0.1, MAX_HESITATION_DELAY)
        # Apply archetype hesitation_mult and clamp into the blueprint band.
        _hmult = params["hesitation_mult"]
        hes_lo *= _hmult
        hes_hi *= _hmult
        hes_lo, hes_hi = _clamp_range(
            hes_lo, hes_hi, MIN_THINKING_DELAY, MAX_HESITATION_DELAY,
        )
        if hes_hi - hes_lo < 0.1:
            hes_hi = min(MAX_HESITATION_DELAY, hes_lo + 0.1)
        self.hesitation_pattern: dict = {"min": hes_lo, "max": hes_hi}
        self.active_hours: tuple = (
            self._rnd.choice((6, 7, 8, 9, 10)),
            self._rnd.choice((20, 21, 22, 23)),
        )
        # Archetype-specific fatigue threshold range, clamped to global bounds.
        _ft_lo, _ft_hi = params["fatigue_threshold"]
        _ft_lo = max(FATIGUE_THRESHOLD_MIN, min(_ft_lo, FATIGUE_THRESHOLD_MAX))
        _ft_hi = max(FATIGUE_THRESHOLD_MIN, min(_ft_hi, FATIGUE_THRESHOLD_MAX))
        if _ft_lo > _ft_hi:
            _ft_lo, _ft_hi = _ft_hi, _ft_lo
        self.fatigue_threshold: int = self._rnd.randint(_ft_lo, _ft_hi)
        # Archetype-specific night penalty range, clamped to global bounds.
        _np_lo, _np_hi = _clamp_range(
            params["night_penalty"][0], params["night_penalty"][1],
            NIGHT_PENALTY_MIN, NIGHT_PENALTY_MAX,
        )
        self.night_penalty_factor: float = self._rnd.uniform(_np_lo, _np_hi)

    def get_typing_delay(self, group_index: int) -> float:
        """Per-keystroke delay, derived from the persona's ``typing_speed``.

        ``typing_speed`` is the archetype-scaled base delay; we add a small
        per-call jitter (±10 %) so successive keystrokes are not identical,
        and apply the same group-index speed-up the previous implementation
        used.  Result is clamped into ``[MIN_TYPING_DELAY, MAX_TYPING_DELAY]``.
        """
        with self._rnd_lock:
            jitter = self._rnd.uniform(0.9, 1.1)
        factor = max(0.85, 1.0 - group_index * 0.03)
        delay = self.typing_speed * jitter * factor
        return max(MIN_TYPING_DELAY, min(delay, MAX_TYPING_DELAY))

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
            "persona_archetype": self.persona_archetype,
            "typing_speed": self.typing_speed,
            "typo_rate": self.typo_rate,
            "hesitation_pattern": dict(self.hesitation_pattern),
            "active_hours": self.active_hours,
            "fatigue_threshold": self.fatigue_threshold,
            "night_penalty_factor": self.night_penalty_factor,
        }
