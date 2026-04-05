"""Behavior delay layer – seed-deterministic delay injection.

Persona, delay engine, temporal model, biometrics, and wrapper.
BehaviorStateMachine lives in state.py (already on main).
"""
import random
import threading
import time
from modules.delay.state import BehaviorStateMachine, BEHAVIOR_STATES, _VALID_BEHAVIOR_TRANSITIONS  # noqa: F401

# -- Hard constraints (Blueprint §10, SPEC §10.6) --
MAX_TYPING_DELAY = 1.8; MIN_TYPING_DELAY = 0.6
MAX_HESITATION_DELAY = 5.0; MAX_STEP_DELAY = 7.0; WATCHDOG_HEADROOM = 3.0
DAY_START = 6; DAY_END = 21
NIGHT_SPEED_PENALTY_RANGE = (0.15, 0.30)
NIGHT_HESITATION_INCREASE_RANGE = (0.20, 0.40); NIGHT_TYPO_INCREASE = 0.02
_TYPO_RATE_MIN = 0.02; _TYPO_RATE_MAX = 0.05
_NIGHT_PENALTY_MIN = 0.15; _NIGHT_PENALTY_MAX = 0.30
_FATIGUE_THRESHOLD_MIN = 5; _FATIGUE_THRESHOLD_MAX = 15
_KEYSTROKE_MAX = 0.3
_PERSONA_TYPES = ("fast_typer", "moderate_typer", "slow_typer", "cautious", "impulsive")


class PersonaProfile:
    """Seed-deterministic persona providing behavioral attributes for a worker."""
    def __init__(self, seed: int) -> None:
        self._seed = seed; self._rnd = random.Random(seed); self._rnd_lock = threading.Lock()
        self.persona_type: str = self._rnd.choice(_PERSONA_TYPES)
        self.typing_speed: float = self._rnd.uniform(0.04, 0.12)
        self.typo_rate: float = self._rnd.uniform(_TYPO_RATE_MIN, _TYPO_RATE_MAX)
        self.hesitation_pattern: dict = {
            "min": self._rnd.uniform(0.5, 1.5), "max": self._rnd.uniform(2.0, 5.0)}
        self.active_hours: tuple = (self._rnd.choice((6, 7, 8, 9, 10)), self._rnd.choice((20, 21, 22, 23)))
        self.fatigue_threshold: int = self._rnd.randint(_FATIGUE_THRESHOLD_MIN, _FATIGUE_THRESHOLD_MAX)
        self.night_penalty_factor: float = self._rnd.uniform(_NIGHT_PENALTY_MIN, _NIGHT_PENALTY_MAX)
    def get_typing_delay(self, group_index: int) -> float:
        with self._rnd_lock: base = self._rnd.uniform(MIN_TYPING_DELAY, MAX_TYPING_DELAY)
        factor = max(0.85, 1.0 - group_index * 0.03)
        return max(MIN_TYPING_DELAY, min(base * factor, MAX_TYPING_DELAY))
    def get_hesitation_delay(self) -> float:
        with self._rnd_lock: return self._rnd.uniform(self.hesitation_pattern["min"], self.hesitation_pattern["max"])
    def get_typo_probability(self) -> float: return self.typo_rate
    def to_dict(self) -> dict:
        return {"seed": self._seed, "persona_type": self.persona_type, "typing_speed": self.typing_speed,
                "typo_rate": self.typo_rate, "hesitation_pattern": dict(self.hesitation_pattern),
                "active_hours": self.active_hours, "fatigue_threshold": self.fatigue_threshold,
                "night_penalty_factor": self.night_penalty_factor}


class DelayEngine:
    """Calculate bounded delays for worker actions."""
    def __init__(self, persona: PersonaProfile, state_machine: BehaviorStateMachine) -> None:
        self._persona = persona; self._state_machine = state_machine
        self._step_accumulated: float = 0.0; self._lock = threading.Lock()
    def calculate_typing_delay(self, group_index: int) -> float:
        if not self.is_delay_permitted(): return 0.0
        raw = self._persona.get_typing_delay(group_index)
        return self._accumulate(max(MIN_TYPING_DELAY, min(raw, MAX_TYPING_DELAY)))
    def calculate_click_delay(self) -> float: return 0.0
    def calculate_thinking_delay(self) -> float:
        if not self.is_delay_permitted(): return 0.0
        raw = self._persona.get_hesitation_delay()
        return self._accumulate(min(raw, MAX_HESITATION_DELAY))
    def calculate_delay(self, action_type: str) -> float:
        if action_type == "typing": return self.calculate_typing_delay(0)
        if action_type == "click": return self.calculate_click_delay()
        if action_type == "thinking": return self.calculate_thinking_delay()
        return 0.0
    def get_step_accumulated_delay(self) -> float:
        with self._lock: return self._step_accumulated
    def reset_step_accumulator(self) -> None:
        with self._lock: self._step_accumulated = 0.0
    def is_delay_permitted(self) -> bool:
        if not self._state_machine.is_safe_for_delay(): return False
        with self._lock: return self._step_accumulated < MAX_STEP_DELAY
    def _accumulate(self, delay: float) -> float:
        with self._lock:
            headroom = MAX_STEP_DELAY - self._step_accumulated
            if headroom <= 0: return 0.0
            actual = min(delay, headroom); self._step_accumulated += actual; return actual


class TemporalModel:
    """Apply time-of-day, fatigue, and micro-variation modifiers."""
    def __init__(self, persona: PersonaProfile) -> None:
        self._persona = persona; self._rnd = random.Random(persona._seed + 1); self._rnd_lock = threading.Lock()
    @staticmethod
    def get_time_state(utc_offset_hours: int) -> str:
        local_hour = (time.gmtime().tm_hour + utc_offset_hours) % 24
        return "DAY" if DAY_START <= local_hour <= DAY_END else "NIGHT"
    def apply_temporal_modifier(self, base_delay: float, action_type: str, utc_offset_hours: int = 0) -> float:
        modified = base_delay * (1.0 + self._persona.night_penalty_factor) if self.get_time_state(utc_offset_hours) == "NIGHT" else base_delay
        if action_type == "typing": return min(modified, MAX_TYPING_DELAY)
        if action_type == "thinking": return min(modified, MAX_HESITATION_DELAY)
        return min(modified, MAX_STEP_DELAY)
    def apply_fatigue(self, base_delay: float, cycle_count: int) -> float:
        if cycle_count <= self._persona.fatigue_threshold: return base_delay
        return base_delay + min((cycle_count - self._persona.fatigue_threshold) * 0.05, 1.0)
    def apply_micro_variation(self, base_delay: float) -> float:
        with self._rnd_lock: return base_delay * self._rnd.uniform(0.90, 1.10)
    def get_current_modifiers(self) -> dict:
        return {"night_penalty_factor": self._persona.night_penalty_factor,
                "fatigue_threshold": self._persona.fatigue_threshold, "micro_var_range": (0.90, 1.10)}


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


def wrap(task_fn, persona: PersonaProfile):
    """Return a wrapped version of task_fn with behavioral delay at SAFE ZONE only."""
    sm = BehaviorStateMachine(); engine = DelayEngine(persona, sm); temporal = TemporalModel(persona)
    def _wrapped(worker_id):
        sm.transition("FILLING_FORM")
        if engine.is_delay_permitted():
            delay = engine.calculate_delay("typing")
            delay = temporal.apply_temporal_modifier(delay, "typing")
            delay = temporal.apply_micro_variation(delay)
            delay = max(0.0, min(delay, MAX_TYPING_DELAY))
            if delay > 0: time.sleep(delay)
        result = task_fn(worker_id)
        engine.reset_step_accumulator(); sm.reset()
        return result
    return _wrapped
