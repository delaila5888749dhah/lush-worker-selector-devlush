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
    MAX_TYPING_DELAY, MIN_TYPING_DELAY, MIN_THINKING_DELAY, MAX_HESITATION_DELAY,
)
from modules.delay.persona import PersonaProfile
from modules.delay.state import (
    BehaviorStateMachine,
    set_current_sm as _set_current_sm,
    reset_current_sm as _reset_current_sm,
)
from modules.delay.engine import DelayEngine
from modules.delay.temporal import TemporalModel
from modules.delay.biometrics import BiometricProfile

_log = logging.getLogger(__name__)
_INJECTABLE_ACTIONS = frozenset(("typing", "thinking"))


def inject_step_delay(
        engine: DelayEngine,
        temporal: TemporalModel,
        action_type: str,
        stop_event=None,
        cycle_count: int = 0,
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
    cycle_count : int
        Number of completed form-fill cycles for this worker. When > 0
        and above ``persona.fatigue_threshold``, thinking delay is
        increased via :meth:`TemporalModel.apply_fatigue` to simulate
        cumulative fatigue (Blueprint §10). 0 disables fatigue.

    Returns
    -------
    float
        Delay requested from ``time.sleep()`` / ``stop_event.wait()`` after
        temporal modifiers, hard clamps, and accumulator headroom are applied.
        Returns ``0.0`` when no delay was injected.

    Rules:

    - Only injects when ``engine.is_delay_permitted() == True``
    - Delay is clamped by ``action_type`` hard limits
    - Temporal modifier, fatigue (thinking only), and micro-variation are applied
    - Thread-safe (reuses existing locks in engine and temporal)
    """
    # Click: real sleep, bypass accumulator
    if action_type == "click":
        delay = engine.calculate_click_delay()
        if delay > 0:
            _log.debug("inject_step_delay: action=click delay=%.4fs", delay)
            if stop_event is not None:
                stop_event.wait(timeout=delay)
            else:
                time.sleep(delay)
        return delay

    if action_type not in _INJECTABLE_ACTIONS:
        return 0.0

    if not engine.is_delay_permitted():
        return 0.0

    base_delay = engine.get_base_delay(action_type)
    if base_delay <= 0:
        return 0.0

    delay = temporal.apply_temporal_modifier(base_delay, action_type)
    if action_type == "thinking" and cycle_count > 0:
        delay = temporal.apply_fatigue(delay, cycle_count)
    delay = temporal.apply_micro_variation(delay)
    if action_type == "typing":
        delay = max(MIN_TYPING_DELAY, min(delay, MAX_TYPING_DELAY))
    elif action_type == "thinking":
        delay = max(MIN_THINKING_DELAY, min(delay, MAX_HESITATION_DELAY))
    else:
        return 0.0

    delay = engine.accumulate_delay(delay)
    if delay <= 0:
        return 0.0
    if stop_event is not None:
        stop_event.wait(timeout=delay)
    else:
        time.sleep(delay)
    _log.debug("inject_step_delay: action=%s delay=%.4fs", action_type, delay)
    return delay


def inject_card_entry_delays(
    bio: BiometricProfile,
    stop_event: threading.Event | None = None,
    engine: DelayEngine | None = None,
) -> list[float]:
    """Inject per-keystroke biometric delays for a 16-digit card number entry.

    Uses ``BiometricProfile.generate_4x4_pattern()`` to produce 16 delay
    values (4 groups × 4 keystrokes, with inter-group pauses at indices
    3, 7, 11).  Each
    delay is slept individually, simulating realistic per-character timing.

    This is Layer 2 of the behavioral simulation — it supplements the
    Layer 1 group-level typing delay already injected by ``inject_step_delay()``.
    Both layers must be used together for full realism.

    Parameters
    ----------
    bio : BiometricProfile
        Initialised biometric profile instance bound to a worker persona.
    stop_event : threading.Event | None
        When provided, ``stop_event.wait(timeout=delay)`` is used instead
        of ``time.sleep(delay)`` for each keystroke.  Returns early if the
        event is set between keystrokes.
    engine : DelayEngine | None
        When provided, ``engine.is_delay_permitted()`` is checked before
        injecting any delays.  If the engine reports that delay is not
        permitted (e.g. VBV, POST_ACTION, or CRITICAL_SECTION context),
        the function returns ``[]`` immediately without sleeping.  This
        enforces INV-DELAY-02: no delay injection in critical contexts.

    Returns
    -------
    list[float]
        The delay values processed before completion or early exit. Under
        normal execution this contains all 16 generated delay values. If
        ``stop_event`` is set before the loop finishes, the return value
        contains only the delays accumulated before the stop was observed;
        if it is already set before the first iteration, ``[]`` is returned.
        When ``engine`` is provided and delay is not permitted, ``[]`` is
        returned without any sleeping.
        Values are NOT accumulated against the step accumulator — they are
        too small to affect the watchdog budget.
    """
    if engine is not None and not engine.is_delay_permitted():
        return []
    delays = bio.generate_4x4_pattern()
    slept: list[float] = []
    for delay in delays:
        if stop_event is not None and stop_event.is_set():
            break
        if delay <= 0:
            slept.append(0.0)
            continue
        if stop_event is not None:
            stop_event.wait(timeout=delay)
        else:
            time.sleep(delay)
        slept.append(delay)
        _log.debug("inject_card_entry_delays: keystroke delay=%.4fs", delay)
    return slept


def wrap(task_fn, persona: PersonaProfile, stop_event: threading.Event | None = None):
    """Return a wrapped version of task_fn with behavioral delay at SAFE ZONE only.

    ``task_fn`` represents **the full form-fill cycle** (not individual fields).
    To inject per-field delays (recipient email, recipient name, billing email,
    card groups), callers should use :func:`inject_step_delay` directly before
    each field action.

    Two behavioral delay injection points are applied around each call to
    ``task_fn``.  For card-entry realism (Layer 2), callers may additionally
    call :func:`inject_card_entry_delays` with a ``BiometricProfile`` instance
    before each card field fill:

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
    cycle_state = {"count": 0}
    cycle_lock = threading.Lock()
    _log.debug("wrap: persona_type=%s seed=%d", persona.persona_type, persona._seed)

    @functools.wraps(task_fn)
    def _wrapped(*args, **kwargs):
        with cycle_lock:
            cycle_state["count"] += 1
            cycle_count = cycle_state["count"]
        # Phase 5A Task 1: publish this wrapper's SM into the context so
        # downstream code (e.g. modules.cdp.driver.GivexDriver) running
        # inside ``task_fn`` adopts the same instance.  Transitions and
        # critical-section flips applied by the driver therefore affect
        # the same SM the delay engine consults.
        sm_token = _set_current_sm(sm)
        try:
            # Injection point 1: typing delay before form interaction.
            # Both the delay injection and task_fn are inside the try so that
            # the finally cleanup always runs, even if inject_step_delay raises.
            sm.transition("FILLING_FORM")
            try:
                inject_step_delay(engine, temporal, "typing", stop_event,
                                  cycle_count=cycle_count)
                result = task_fn(*args, **kwargs)
            finally:
                _log.debug(
                    "wrap: step_accumulated_delay=%.4fs before reset",
                    engine.get_step_accumulated_delay(),
                )
                engine.reset_step_accumulator()
                # Reset AR(1) drift envelope per Blueprint §10 so each cycle starts neutral.
                temporal.reset_drift()
                sm.reset()
            # (before submit click).  Only reached when task_fn succeeded.
            # Re-enter FILLING_FORM; accumulator was reset above so the
            # thinking delay is not blocked by the earlier typing delay.
            # try/finally ensures accumulator and SM are always cleaned up
            # even if the thinking delay injection raises or is interrupted.
            sm.transition("FILLING_FORM")
            try:
                inject_step_delay(engine, temporal, "thinking", stop_event,
                                  cycle_count=cycle_count)
            finally:
                engine.reset_step_accumulator()
                temporal.reset_drift()
                sm.reset()

            return result
        finally:
            _reset_current_sm(sm_token)

    # Phase 5A Task 1: expose the wrapper's SM/engine on the returned
    # callable so tests (and integration code) can verify the shared
    # instance without breaking the existing single-argument call shape.
    _wrapped.behavior_sm = sm  # type: ignore[attr-defined]
    _wrapped.behavior_engine = engine  # type: ignore[attr-defined]
    return _wrapped
