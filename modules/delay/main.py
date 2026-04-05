"""Behavior delay layer – seed-deterministic delay injection.

Delay engine, temporal model, biometrics, and wrapper.
PersonaProfile lives in persona.py (Task 10.1, stdlib-only).
BehaviorStateMachine lives in state.py (already on main).
DelayEngine lives in engine.py (Task 10.3).
"""
import random
import threading
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

_KEYSTROKE_MAX = 0.3


class BiometricProfile:
    """Generate biometric keystroke timing for a worker."""
    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona; self._rnd = random.Random(persona._seed + 2); self._rnd_lock = threading.Lock()
    def generate_keystroke_delay(self, char_index: int) -> float:
        with self._rnd_lock: raw = self._rnd.lognormvariate(-2.5, 0.4)
        return max(0.0, min(raw, _KEYSTROKE_MAX))
    def generate_burst_pattern(self, total_chars: int) -> list:
        delays = []
        for i in range(total_chars):
            if i > 0 and i % 4 == 0:
                with self._rnd_lock: delays.append(min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY))
            else:
                with self._rnd_lock: delays.append(self._rnd.uniform(0.03, 0.08))
        return delays
    def generate_4x4_pattern(self) -> list:
        delays = []
        for group in range(4):
            for _ in range(4):
                with self._rnd_lock: delays.append(self._rnd.uniform(0.03, 0.08))
            if group < 3:
                with self._rnd_lock: delays.append(max(MIN_TYPING_DELAY, min(self._rnd.uniform(0.6, 1.8), MAX_TYPING_DELAY)))
        return delays
    def apply_noise(self, base_delay: float) -> float:
        with self._rnd_lock: return max(0.0, base_delay + self._rnd.gauss(0, 0.10 * base_delay))


from modules.delay.wrapper import wrap  # noqa: F401  — Task 10.5
