"""BehaviorWrapper — Task Function Decorator (Task 10.5).

Wraps ``task_fn`` to inject behavioral delay at SAFE ZONE points
without changing execution logic or outcome.

Thread-safe.  Imports limited to ``modules.delay`` submodules.
Deterministic via seed-based random from PersonaProfile.
"""

import functools
import threading
import time

from modules.delay.persona import PersonaProfile, MAX_TYPING_DELAY, MIN_TYPING_DELAY
from modules.delay.state import BehaviorStateMachine
from modules.delay.engine import DelayEngine, MAX_HESITATION_DELAY
from modules.delay.temporal import TemporalModel

_MIN_THINKING_DELAY = 3.0


def inject_step_delay(
    engine: DelayEngine,
    temporal: TemporalModel,
    action_type: str,
    stop_event=None,
) -> float:
    """Inject behavioral delay for a single action step.

    Call this function once per field/action in a multi-step form to inject
    per-field delays according to the biological simulation model
    (Blueprint §4).

    Parameters
    ----------
    engine : DelayEngine
        Initialised engine instance.
    temporal : TemporalModel
        Initialised temporal model instance.
    action_type : str
        ``"typing"``, ``"click"``, or ``"thinking"``.
    stop_event : threading.Event | None
        When provided, ``stop_event.wait(timeout=delay)`` is used instead
        of ``time.sleep(delay)``.

    Returns
    -------
    float
        Delay requested from ``time.sleep()`` / ``stop_event.wait()`` after
        temporal modifiers, hard clamps, and accumulator headroom are applied.
        Returns ``0.0`` when no delay was injected.

    Rules:

    - Only injects when ``engine.is_delay_permitted() == True``
    - Delay is clamped by ``action_type`` hard limits
    - Temporal modifier and micro-variation are applied
    - Thread-safe (reuses existing locks in engine and temporal)
    """
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
        delay = max(_MIN_THINKING_DELAY, min(delay, MAX_HESITATION_DELAY))
    else:
        return 0.0

    delay = engine.accumulate_delay(delay)
    if delay <= 0:
        return 0.0
    if stop_event is not None:
        stop_event.wait(timeout=delay)
    else:
        time.sleep(delay)
    return delay


def wrap(task_fn, persona: PersonaProfile, stop_event: threading.Event | None = None):
    """Return a wrapped version of task_fn with behavioral delay at SAFE ZONE only.

    ``task_fn`` represents **the full form-fill cycle** (not individual fields).
    To inject per-field delays (recipient email, recipient name, billing email,
    card groups), callers should use :func:`inject_step_delay` directly before
    each field action.

    Two delay injection points are applied around each call to ``task_fn``:

    1. **Pre-form typing delay** (FILLING_FORM context) — simulates the
       hesitation before starting to interact with the form (Blueprint §4).
    2. **Post-fill thinking/hesitation delay** (FILLING_FORM context) —
       simulates the cursor lingering around the COMPLETE PURCHASE button
       for 3–5 s (Blueprint §5).

    The thinking delay is only injected when ``task_fn`` completes without
    raising an exception (non-interference rule, Blueprint §8.7): exceptions
    propagate unchanged, with cleanup running inside the ``finally`` block.
    """
    sm = BehaviorStateMachine()
    engine = DelayEngine(persona, sm)
    temporal = TemporalModel(persona)

    @functools.wraps(task_fn)
    def _wrapped(*args, **kwargs):
        # Injection point 1: typing delay before form interaction.
        sm.transition("FILLING_FORM")
        inject_step_delay(engine, temporal, "typing", stop_event)
        try:
            result = task_fn(*args, **kwargs)
        finally:
            engine.reset_step_accumulator()
            sm.reset()

        # Injection point 2: thinking/hesitation delay after form fill
        # (before submit click).  Only reached when task_fn succeeded.
        # Re-enter FILLING_FORM; accumulator was reset above so the
        # thinking delay is not blocked by the earlier typing delay.
        sm.transition("FILLING_FORM")
        inject_step_delay(engine, temporal, "thinking", stop_event)
        engine.reset_step_accumulator()
        sm.reset()

        return result

    return _wrapped
