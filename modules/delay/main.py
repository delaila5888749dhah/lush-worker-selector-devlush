"""Behavior delay layer – seed-deterministic delay injection.

Delay engine, temporal model, biometrics, and wrapper.
PersonaProfile lives in persona.py (Task 10.1, stdlib-only).
BehaviorStateMachine lives in state.py (already on main).
DelayEngine lives in engine.py (Task 10.3).
BiometricProfile lives in biometrics.py (Task 10.6).
"""
from modules.delay.persona import PersonaProfile  # noqa: F401
from modules.delay.persona import MAX_TYPING_DELAY, MIN_TYPING_DELAY  # noqa: F401
from modules.delay.persona import TYPO_RATE_MIN, TYPO_RATE_MAX  # noqa: F401
from modules.delay.persona import NIGHT_PENALTY_MIN, NIGHT_PENALTY_MAX  # noqa: F401
from modules.delay.persona import FATIGUE_THRESHOLD_MIN, FATIGUE_THRESHOLD_MAX  # noqa: F401
from modules.delay.state import BehaviorStateMachine, BEHAVIOR_STATES  # noqa: F401
from modules.delay.engine import DelayEngine  # noqa: F401
from modules.delay.engine import MAX_HESITATION_DELAY, MAX_STEP_DELAY  # noqa: F401
from modules.delay.config import WATCHDOG_HEADROOM  # noqa: F401
from modules.delay.temporal import TemporalModel  # noqa: F401
from modules.delay.temporal import DAY_START, DAY_END  # noqa: F401
from modules.delay.temporal import NIGHT_SPEED_PENALTY_RANGE, NIGHT_HESITATION_INCREASE_RANGE, NIGHT_TYPO_INCREASE_RANGE  # noqa: F401
from modules.delay.biometrics import BiometricProfile  # noqa: F401
from modules.delay.wrapper import wrap  # noqa: F401  — Task 10.5
from modules.delay.wrapper import inject_card_entry_delays  # noqa: F401
from modules.delay.config import (  # noqa: F401
    validate_config,
    MIN_CLICK_DELAY,
    MAX_CLICK_DELAY,
    MIN_THINKING_DELAY,
    CDP_CALL_TIMEOUT,
)

__all__ = [
    "PersonaProfile",
    "MAX_TYPING_DELAY",
    "MIN_TYPING_DELAY",
    "TYPO_RATE_MIN",
    "TYPO_RATE_MAX",
    "NIGHT_PENALTY_MIN",
    "NIGHT_PENALTY_MAX",
    "FATIGUE_THRESHOLD_MIN",
    "FATIGUE_THRESHOLD_MAX",
    "BehaviorStateMachine",
    "BEHAVIOR_STATES",
    "DelayEngine",
    "MAX_HESITATION_DELAY",
    "MAX_STEP_DELAY",
    "WATCHDOG_HEADROOM",
    "TemporalModel",
    "DAY_START",
    "DAY_END",
    "NIGHT_SPEED_PENALTY_RANGE",
    "NIGHT_HESITATION_INCREASE_RANGE",
    "NIGHT_TYPO_INCREASE_RANGE",
    "BiometricProfile",
    "wrap",
    "inject_card_entry_delays",
    "validate_config",
    "MIN_CLICK_DELAY",
    "MAX_CLICK_DELAY",
    "MIN_THINKING_DELAY",
    "CDP_CALL_TIMEOUT",
]
