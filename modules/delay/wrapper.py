"""BehaviorWrapper — Task Function Decorator (Task 10.5).

Wraps ``task_fn`` to inject behavioral delay at SAFE ZONE points
without changing execution logic or outcome.

Thread-safe.  Imports limited to ``modules.delay`` submodules.
Deterministic via seed-based random from PersonaProfile.
"""

import functools
import logging
import threading
import time

from modules.delay.config import (
    MAX_TYPING_DELAY,
    MIN_TYPING_DELAY,
    MIN_THINKING_DELAY,
    MAX_HESITATION_DELAY,
)
from modules.delay.persona import PersonaProfile
from modules.delay.state import BehaviorStateMachine
from modules.delay.engine import DelayEngine
from modules.delay.temporal import TemporalModel

_log = logging.getLogger(__name__)

# Action types that go through the accumulator-based injection model.
# Click bypasses the accumulator (micro-delay, slept directly).
_INJECTABLE_ACTIONS = frozenset(("typing", "thinking"))


def inject_step_delay(
    engine: DelayEngine,
    temporal: TemporalModel,
    action_type: str,
    stop_event=None,
) -> float:
    """Inject behavioral delay for a single action step.

    For ``action_type="click"``:
        The delay is sourced from ``engine.calculate_click_delay()`` and slept
        directly (real wall-clock sleep). It is NOT passed through the
        accumulator — click delays do not count against the step budget.

    For ``action_type="typing"`` and ``action_type="thinking"``:
        Temporal modifier + micro-variation applied, accumulated against
        step ceiling.

    Returns 0.0 when no delay was injected.
    """
    # ── Click: real sleep, bypass accumulator ────────────────────
    if action_type == "click":
        delay = engine.calculate_click_delay()
        if delay > 0:
            _log.debug(
                "inject_step_delay: action=click injected=%.4fs (not accumulated)",
                delay,
            )
            if stop_event is not None:
                stop_event.wait(timeout=delay)
            else:
                time.sleep(delay)
        return delay

    # ── Typing / Thinking: accumulator-based ─────────────────────
    if action_type not in _INJECTABLE_ACTIONS:
        return 0.0

    if not engine.is_delay_permitted():
        return 0.0

    base_delay = engine.get_base_delay(action_type)
    if base_delay <= 0:
        return 0.0

    delay = temporal.apply_temporal_modifier(base_delay, action_type)
    delay = temporal.apply_micro_variation(delay)
    if action_type == "typing":
        delay = max(MIN_TYPING_DELAY, min(delay, MAX_TYPING_DELAY))
    elif action_type == "thinking":
        delay = max(MIN_THINKING_DELAY, min(delay, MAX_HESITATION_DELAY))

    delay = engine.accumulate_delay(delay)
    if delay <= 0:
        return 0.0
    if stop_event is not None:
        stop_event.wait(timeout=delay)
    else:
        time.sleep(delay)
    _log.debug(
        "inject_step_delay: action=%s delay=%.4fs accumulated=%.4fs",
        action_type,
        delay,
        engine.get_step_accumulated_delay(),
    )
    return delay


def wrap(task_fn, persona: PersonaProfile, stop_event: threading.Event | None = None):
    """Return a wrapped version of task_fn with behavioral delay at SAFE ZONE only.

    Two delay injection points are applied around each call to ``task_fn``:

    1. **Pre-form typing delay** (FILLING_FORM context).
    2. **Post-fill thinking/hesitation delay** (FILLING_FORM context).

    Exceptions propagate unchanged; cleanup runs in ``finally``.
    """
    sm = BehaviorStateMachine()
    engine = DelayEngine(persona, sm)
    temporal = TemporalModel(persona)

    _log.debug(
        "wrap: starting cycle persona_type=%s seed=%d",
        persona.persona_type,
        persona._seed,
    )

    @functools.wraps(task_fn)
    def _wrapped(*args, **kwargs):
        sm.transition("FILLING_FORM")
        typing_delay = inject_step_delay(engine, temporal, "typing", stop_event)
        _log.debug("wrap: pre-form typing_delay=%.4fs", typing_delay)
        try:
            result = task_fn(*args, **kwargs)
        finally:
            engine.reset_step_accumulator()
            sm.reset()

        sm.transition("FILLING_FORM")
        thinking_delay = inject_step_delay(engine, temporal, "thinking", stop_event)
        _log.debug("wrap: post-fill thinking_delay=%.4fs", thinking_delay)
        engine.reset_step_accumulator()
        sm.reset()

        return result

    return _wrapped
