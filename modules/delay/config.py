"""Centralized timing configuration for modules.delay.

Env overrides: DELAY_<CONSTANT_NAME>. Validated at import time.
"""
import os

_STEP_BUDGET_TOTAL: float = 10.0  # NOT orchestrator _WATCHDOG_TIMEOUT (30 s)


class DelayConfigError(ValueError):
    """Raised when delay timing configuration violates required invariants."""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(f"DELAY_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Invalid DELAY_{name}={raw!r}: expected float")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(f"DELAY_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Invalid DELAY_{name}={raw!r}: expected int")


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var (accepts 1/0/true/false/yes/no, case-insensitive)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Gradual behavior drift (Blueprint §10). See TemporalModel.apply_gradual_drift.
ENABLE_GRADUAL_DRIFT: bool = _env_bool("ENABLE_GRADUAL_DRIFT", default=True)

MIN_TYPING_DELAY: float = _env_float("MIN_TYPING_DELAY", 0.6)
MAX_TYPING_DELAY: float = _env_float("MAX_TYPING_DELAY", 1.8)
MIN_THINKING_DELAY: float = _env_float("MIN_THINKING_DELAY", 3.0)
MAX_HESITATION_DELAY: float = _env_float("MAX_HESITATION_DELAY", 5.0)
MAX_STEP_DELAY: float = _env_float("MAX_STEP_DELAY", 7.0)
WATCHDOG_HEADROOM: float = _env_float("WATCHDOG_HEADROOM", 3.0)
MIN_CLICK_DELAY: float = _env_float("MIN_CLICK_DELAY", 0.05)
MAX_CLICK_DELAY: float = _env_float("MAX_CLICK_DELAY", 0.25)
# CDP_CALL_TIMEOUT reads CDP_CALL_TIMEOUT_SECONDS (no DELAY_ prefix) to stay
# compatible with integration/orchestrator.py which uses the same env var name.
CDP_CALL_TIMEOUT: float = float(os.getenv("CDP_CALL_TIMEOUT_SECONDS", "15.0"))
# Detect operator misconfiguration: DELAY_CDP_CALL_TIMEOUT is NOT the correct
# env var name for this constant. The correct name is CDP_CALL_TIMEOUT_SECONDS.
# Emit a warning so operators who follow the DELAY_* pattern for other constants
# are not silently ignored.
import warnings as _warnings
if os.getenv("DELAY_CDP_CALL_TIMEOUT") is not None:
    _warnings.warn(
        "DELAY_CDP_CALL_TIMEOUT is set but has no effect. "
        "Use CDP_CALL_TIMEOUT_SECONDS to override the CDP call timeout. "
        "The DELAY_* prefix is not used for this constant.",
        UserWarning,
        stacklevel=2,
    )
del _warnings
TYPO_RATE_MIN: float = _env_float("TYPO_RATE_MIN", 0.02)
TYPO_RATE_MAX: float = _env_float("TYPO_RATE_MAX", 0.05)
NIGHT_PENALTY_MIN: float = _env_float("NIGHT_PENALTY_MIN", 0.15)
NIGHT_PENALTY_MAX: float = _env_float("NIGHT_PENALTY_MAX", 0.30)
FATIGUE_THRESHOLD_MIN: int = _env_int("FATIGUE_THRESHOLD_MIN", 5)
FATIGUE_THRESHOLD_MAX: int = _env_int("FATIGUE_THRESHOLD_MAX", 15)
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


def validate_config() -> None:
    """Validate timing invariants. Raises DelayConfigError on violation."""
    if not (MIN_TYPING_DELAY < MAX_TYPING_DELAY):
        raise DelayConfigError(
            f"MIN_TYPING_DELAY({MIN_TYPING_DELAY}) must be"
            f" < MAX_TYPING_DELAY({MAX_TYPING_DELAY})"
        )
    if not (MIN_THINKING_DELAY <= MAX_HESITATION_DELAY):
        raise DelayConfigError(
            f"MIN_THINKING_DELAY({MIN_THINKING_DELAY}) must be"
            f" <= MAX_HESITATION_DELAY({MAX_HESITATION_DELAY})"
        )
    if not (MAX_STEP_DELAY + WATCHDOG_HEADROOM <= _STEP_BUDGET_TOTAL):
        raise DelayConfigError(
            f"MAX_STEP_DELAY({MAX_STEP_DELAY})"
            f"+WATCHDOG_HEADROOM({WATCHDOG_HEADROOM})"
            f" must be <= {_STEP_BUDGET_TOTAL}"
        )
    if not (TYPO_RATE_MIN <= TYPO_RATE_MAX):
        raise DelayConfigError(
            f"TYPO_RATE_MIN({TYPO_RATE_MIN}) must be"
            f" <= TYPO_RATE_MAX({TYPO_RATE_MAX})"
        )
    if not (FATIGUE_THRESHOLD_MIN <= FATIGUE_THRESHOLD_MAX):
        raise DelayConfigError(
            f"FATIGUE_THRESHOLD_MIN({FATIGUE_THRESHOLD_MIN}) must be"
            f" <= FATIGUE_THRESHOLD_MAX({FATIGUE_THRESHOLD_MAX})"
        )
    if not (NIGHT_PENALTY_MIN <= NIGHT_PENALTY_MAX):
        raise DelayConfigError(
            f"NIGHT_PENALTY_MIN({NIGHT_PENALTY_MIN}) must be"
            f" <= NIGHT_PENALTY_MAX({NIGHT_PENALTY_MAX})"
        )
    if not (MIN_CLICK_DELAY < MAX_CLICK_DELAY):
        raise DelayConfigError(
            f"MIN_CLICK_DELAY({MIN_CLICK_DELAY}) must be"
            f" < MAX_CLICK_DELAY({MAX_CLICK_DELAY})"
        )
    if not (CDP_CALL_TIMEOUT > 0):
        raise DelayConfigError(f"CDP_CALL_TIMEOUT({CDP_CALL_TIMEOUT}) must be > 0")


validate_config()
