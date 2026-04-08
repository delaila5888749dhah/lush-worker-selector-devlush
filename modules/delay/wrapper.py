"""BehaviorWrapper — Task Function Decorator (Task 10.5).

Wraps ``task_fn`` to inject behavioral delay at SAFE ZONE points
without changing execution logic or outcome.

Thread-safe.  Imports limited to ``modules.delay`` submodules.
Deterministic via seed-based random from PersonaProfile.
"""

import functools
import threading
import time

from modules.delay.persona import PersonaProfile, MAX_TYPING_DELAY
from modules.delay.state import BehaviorStateMachine
from modules.delay.engine import DelayEngine
from modules.delay.temporal import TemporalModel


def wrap(task_fn, persona: PersonaProfile, stop_event: threading.Event | None = None):
    """Return a wrapped version of task_fn with behavioral delay at SAFE ZONE only."""
    sm = BehaviorStateMachine()
    engine = DelayEngine(persona, sm)
    temporal = TemporalModel(persona)

    @functools.wraps(task_fn)
    def _wrapped(*args, **kwargs):
        sm.transition("FILLING_FORM")
        if engine.is_delay_permitted():
            delay = engine.calculate_delay("typing")
            delay = temporal.apply_temporal_modifier(delay, "typing")
            delay = temporal.apply_micro_variation(delay)
            delay = max(0.0, min(delay, MAX_TYPING_DELAY))
            if delay > 0:
                if stop_event is not None:
                    stop_event.wait(timeout=delay)
                else:
                    time.sleep(delay)
        try:
            result = task_fn(*args, **kwargs)
        finally:
            engine.reset_step_accumulator()
            sm.reset()
        return result

    return _wrapped
