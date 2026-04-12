"""Centralized timing configuration for the delay module.

All timing constants used across persona.py, engine.py, temporal.py, and
wrapper.py are defined here as a single source of truth.

Environment-variable overrides use the convention::

    DELAY_<CONSTANT_NAME>

For example: ``DELAY_MIN_TYPING_DELAY=0.5`` or ``DELAY_MAX_STEP_DELAY=6.0``.

Constants are validated at import time; ``validate_config()`` can be called
explicitly to re-run the same checks (useful in tests and at startup).
"""

import os

# ── Internal watchdog ceiling (from orchestrator / runtime spec) ──────────────
_WATCHDOG_TIMEOUT: float = 10.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(f"DELAY_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(f"DELAY_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── Typing ────────────────────────────────────────────────────────────────────
MIN_TYPING_DELAY: float = _env_float("MIN_TYPING_DELAY", 0.6)
MAX_TYPING_DELAY: float = _env_float("MAX_TYPING_DELAY", 1.8)

# ── Hesitation / thinking ─────────────────────────────────────────────────────
MIN_THINKING_DELAY: float = _env_float("MIN_THINKING_DELAY", 3.0)
MAX_HESITATION_DELAY: float = _env_float("MAX_HESITATION_DELAY", 5.0)

# ── Step-level accumulator ceiling ────────────────────────────────────────────
MAX_STEP_DELAY: float = _env_float("MAX_STEP_DELAY", 7.0)

# ── Watchdog headroom ─────────────────────────────────────────────────────────
WATCHDOG_HEADROOM: float = _env_float("WATCHDOG_HEADROOM", 3.0)

# ── Click delay (spatial / reaction offset, NOT accumulated) ──────────────────
MIN_CLICK_DELAY: float = _env_float("MIN_CLICK_DELAY", 0.05)
MAX_CLICK_DELAY: float = _env_float("MAX_CLICK_DELAY", 0.25)

# ── Focus delay (initial attention before form interaction) ───────────────────
MIN_FOCUS_DELAY: float = _env_float("MIN_FOCUS_DELAY", 0.3)
MAX_FOCUS_DELAY: float = _env_float("MAX_FOCUS_DELAY", 0.8)

# ── Typing burst (per-keystroke within a group) ───────────────────────────────
MIN_TYPING_BURST_DELAY: float = _env_float("MIN_TYPING_BURST_DELAY", 0.03)
MAX_TYPING_BURST_DELAY: float = _env_float("MAX_TYPING_BURST_DELAY", 0.08)

# ── Navigation delay (between page sections / scroll) ────────────────────────
MIN_NAVIGATION_DELAY: float = _env_float("MIN_NAVIGATION_DELAY", 0.5)
MAX_NAVIGATION_DELAY: float = _env_float("MAX_NAVIGATION_DELAY", 1.5)

# ── Persona attribute ranges ──────────────────────────────────────────────────
TYPO_RATE_MIN: float = _env_float("TYPO_RATE_MIN", 0.02)
TYPO_RATE_MAX: float = _env_float("TYPO_RATE_MAX", 0.05)
NIGHT_PENALTY_MIN: float = _env_float("NIGHT_PENALTY_MIN", 0.15)
NIGHT_PENALTY_MAX: float = _env_float("NIGHT_PENALTY_MAX", 0.30)
FATIGUE_THRESHOLD_MIN: int = _env_int("FATIGUE_THRESHOLD_MIN", 5)
FATIGUE_THRESHOLD_MAX: int = _env_int("FATIGUE_THRESHOLD_MAX", 15)

# ── Temporal ──────────────────────────────────────────────────────────────────
DAY_START: int = _env_int("DAY_START", 6)
DAY_END: int = _env_int("DAY_END", 21)
NIGHT_SPEED_PENALTY_RANGE: tuple = (
    _env_float("NIGHT_SPEED_PENALTY_RANGE_MIN", 0.15),
    _env_float("NIGHT_SPEED_PENALTY_RANGE_MAX", 0.30),
)
NIGHT_HESITATION_INCREASE_RANGE: tuple = (
    _env_float("NIGHT_HESITATION_INCREASE_RANGE_MIN", 0.20),
    _env_float("NIGHT_HESITATION_INCREASE_RANGE_MAX", 0.40),
)
NIGHT_TYPO_INCREASE_RANGE: tuple = (
    _env_float("NIGHT_TYPO_INCREASE_RANGE_MIN", 0.01),
    _env_float("NIGHT_TYPO_INCREASE_RANGE_MAX", 0.02),
)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_config() -> None:
    """Validate all timing invariants.

    Raises ``ValueError`` with a descriptive message if any invariant is
    violated.  Called automatically at import time and can be called again
    after modifying module-level constants in tests.
    """
    if not (MIN_TYPING_DELAY < MAX_TYPING_DELAY):
        raise ValueError(
            f"MIN_TYPING_DELAY ({MIN_TYPING_DELAY}) must be < MAX_TYPING_DELAY ({MAX_TYPING_DELAY})"
        )
    if not (MIN_THINKING_DELAY <= MAX_HESITATION_DELAY):
        raise ValueError(
            f"MIN_THINKING_DELAY ({MIN_THINKING_DELAY}) must be <= MAX_HESITATION_DELAY ({MAX_HESITATION_DELAY})"
        )
    if not (MAX_STEP_DELAY + WATCHDOG_HEADROOM <= _WATCHDOG_TIMEOUT):
        raise ValueError(
            f"MAX_STEP_DELAY ({MAX_STEP_DELAY}) + WATCHDOG_HEADROOM ({WATCHDOG_HEADROOM})"
            f" = {MAX_STEP_DELAY + WATCHDOG_HEADROOM} must be <= _WATCHDOG_TIMEOUT ({_WATCHDOG_TIMEOUT})"
        )
    if not (TYPO_RATE_MIN <= TYPO_RATE_MAX):
        raise ValueError(
            f"TYPO_RATE_MIN ({TYPO_RATE_MIN}) must be <= TYPO_RATE_MAX ({TYPO_RATE_MAX})"
        )
    if not (FATIGUE_THRESHOLD_MIN <= FATIGUE_THRESHOLD_MAX):
        raise ValueError(
            f"FATIGUE_THRESHOLD_MIN ({FATIGUE_THRESHOLD_MIN}) must be <= FATIGUE_THRESHOLD_MAX ({FATIGUE_THRESHOLD_MAX})"
        )
    if not (NIGHT_PENALTY_MIN <= NIGHT_PENALTY_MAX):
        raise ValueError(
            f"NIGHT_PENALTY_MIN ({NIGHT_PENALTY_MIN}) must be <= NIGHT_PENALTY_MAX ({NIGHT_PENALTY_MAX})"
        )
    if not (MIN_CLICK_DELAY < MAX_CLICK_DELAY):
        raise ValueError(
            f"MIN_CLICK_DELAY ({MIN_CLICK_DELAY}) must be < MAX_CLICK_DELAY ({MAX_CLICK_DELAY})"
        )
    if not (MIN_FOCUS_DELAY < MAX_FOCUS_DELAY):
        raise ValueError(
            f"MIN_FOCUS_DELAY ({MIN_FOCUS_DELAY}) must be < MAX_FOCUS_DELAY ({MAX_FOCUS_DELAY})"
        )
    if not (MIN_NAVIGATION_DELAY < MAX_NAVIGATION_DELAY):
        raise ValueError(
            f"MIN_NAVIGATION_DELAY ({MIN_NAVIGATION_DELAY}) must be < MAX_NAVIGATION_DELAY ({MAX_NAVIGATION_DELAY})"
        )


# Validate at import time.
validate_config()
