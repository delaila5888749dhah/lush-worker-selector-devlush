"""Behavior delay layer – seed-deterministic delay injection.

Delay engine, temporal model, biometrics, and wrapper.
PersonaProfile lives in persona.py (Task 10.1, stdlib-only).
BehaviorStateMachine lives in state.py (already on main).
DelayEngine lives in engine.py (Task 10.3).
BiometricProfile lives in biometrics.py (Task 10.6).
"""
from modules.delay.persona import PersonaProfile  # noqa: F401
from modules.delay.persona import MAX_TYPING_DELAY, MIN_TYPING_DELAY  # noqa: F401
from modules.delay.persona import _TYPO_RATE_MIN, _TYPO_RATE_MAX  # noqa: F401
from modules.delay.persona import _NIGHT_PENALTY_MIN, _NIGHT_PENALTY_MAX  # noqa: F401
from modules.delay.persona import _FATIGUE_THRESHOLD_MIN, _FATIGUE_THRESHOLD_MAX  # noqa: F401
from modules.delay.persona import _PERSONA_TYPES  # noqa: F401
from modules.delay.state import BehaviorStateMachine, BEHAVIOR_STATES, _VALID_BEHAVIOR_TRANSITIONS  # noqa: F401
from modules.delay.engine import DelayEngine  # noqa: F401
from modules.delay.engine import MAX_HESITATION_DELAY, MAX_STEP_DELAY, WATCHDOG_HEADROOM  # noqa: F401
from modules.delay.temporal import TemporalModel  # noqa: F401
from modules.delay.temporal import DAY_START, DAY_END  # noqa: F401
from modules.delay.temporal import NIGHT_SPEED_PENALTY_RANGE, NIGHT_HESITATION_INCREASE_RANGE, NIGHT_TYPO_INCREASE  # noqa: F401
from modules.delay.biometrics import BiometricProfile, _KEYSTROKE_MAX  # noqa: F401
from modules.delay.wrapper import wrap  # noqa: F401  — Task 10.5
