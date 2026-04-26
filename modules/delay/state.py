"""BehaviorState FSM — Context-aware state machine for delay decisions (Task 10.2).

Tracks the behavioral context of a worker within a cycle.  The delay engine
uses the current BehaviorState to decide *type* and *magnitude* of delay.

Five mandatory states (SPEC-6 §10.2):
  IDLE          — between actions, awaiting next step
  FILLING_FORM  — form field interaction (recipient, billing)
  PAYMENT       — payment data entry (card number, CVV)
  VBV           — 3DS iframe handling (critical — zero delay)
  POST_ACTION   — after submit, waiting for result (critical — zero delay)

Thread-safe via threading.Lock.  No cross-module imports.

Module-level constant ``CRITICAL_SECTION`` (Blueprint §8.3, INV-DELAY-02)
exposes the canonical frozenset of behavior states that mandate zero
delay (``{"VBV", "POST_ACTION"}``).  It is the single source of truth
used by :meth:`BehaviorStateMachine.is_critical_context` and is
re-exported via :mod:`modules.delay.main` for downstream consumers.
"""

import contextvars
import logging
import threading
from typing import Optional

_logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────

BEHAVIOR_STATES = {"IDLE", "FILLING_FORM", "PAYMENT", "VBV", "POST_ACTION"}

# Canonical frozenset of behavior states that mandate zero delay
# (Blueprint §8.3, INV-DELAY-02).  Module-level public constant — the
# authoritative source of truth referenced by :meth:`is_critical_context`
# and re-exported via :mod:`modules.delay.main`.
CRITICAL_SECTION = frozenset({"VBV", "POST_ACTION"})

_VALID_BEHAVIOR_TRANSITIONS = {
    "IDLE": {"FILLING_FORM"},
    "FILLING_FORM": {"PAYMENT", "IDLE"},
    "PAYMENT": {"VBV", "POST_ACTION", "IDLE"},
    "VBV": {"POST_ACTION", "IDLE"},
    "POST_ACTION": {"IDLE"},
}

_CRITICAL_CONTEXTS = CRITICAL_SECTION  # backward-compat alias
_SAFE_CONTEXTS = {"IDLE", "FILLING_FORM", "PAYMENT"}

# ── BehaviorStateMachine ─────────────────────────────────────────


class BehaviorStateMachine:
    """Context-aware state machine for behavioral delay decisions.

    Parameters
    ----------
    initial_state : str
        Starting state.  Must be in *BEHAVIOR_STATES*.  Defaults to ``"IDLE"``.
    """

    def __init__(self, initial_state: str = "IDLE") -> None:
        if initial_state not in BEHAVIOR_STATES:
            raise ValueError(
                f"invalid initial state {initial_state!r}; "
                f"must be one of {sorted(BEHAVIOR_STATES)}"
            )
        self._lock = threading.Lock()
        self._state: str = initial_state
        self._in_critical_section: bool = False

    # ── transitions ──────────────────────────────────────────────

    def transition(self, new_state: str) -> bool:
        """Attempt a state transition.

        Returns *True* if the transition was valid and applied, *False*
        otherwise.  Invalid target states or disallowed transitions are
        silently rejected (logged at DEBUG level).
        """
        if new_state not in BEHAVIOR_STATES:
            _logger.debug(
                "transition rejected: %r not in BEHAVIOR_STATES", new_state
            )
            return False

        with self._lock:
            allowed = _VALID_BEHAVIOR_TRANSITIONS.get(self._state, set())
            if new_state not in allowed:
                _logger.debug(
                    "transition rejected: %s -> %s not allowed",
                    self._state,
                    new_state,
                )
                return False
            self._state = new_state
            return True

    # ── queries ──────────────────────────────────────────────────

    def get_state(self) -> str:
        """Return the current behavior state."""
        with self._lock:
            return self._state

    def is_critical_context(self) -> bool:
        """Return *True* when in VBV or POST_ACTION (zero-delay zones), or when
        the worker is flagged as being in a Phase-9 CRITICAL_SECTION.

        For full delay-safety evaluation (FSM state + critical-section flag),
        prefer :meth:`is_safe_for_delay` which is the authoritative check used
        by the delay engine.
        """
        with self._lock:
            return self._state in CRITICAL_SECTION or self._in_critical_section

    def is_safe_for_delay(self) -> bool:
        """Return *True* when delay injection is permitted.

        Safe when behavior state is IDLE, FILLING_FORM, or PAYMENT **and**
        the worker is not flagged as being in a Phase-9 CRITICAL_SECTION.
        """
        with self._lock:
            return (
                self._state in _SAFE_CONTEXTS
                and not self._in_critical_section
            )

    # ── critical-section flag (Phase 9 interop) ─────────────────

    def set_critical_section(self, active: bool) -> None:
        """Mark whether the worker is currently in a Phase-9 CRITICAL_SECTION.

        Called by the wrapper / integration layer so the behavior FSM can
        honour the zero-delay rule without importing from *integration*.
        """
        with self._lock:
            self._in_critical_section = bool(active)

    # ── lifecycle ────────────────────────────────────────────────

    def reset(self) -> None:
        """Reset the machine to IDLE and clear the critical-section flag."""
        with self._lock:
            self._state = "IDLE"
            self._in_critical_section = False


# ── Shared-instance context (Phase 5A Task 1) ─────────────────────
#
# A :class:`contextvars.ContextVar` so the behaviour wrapper and the
# CDP driver layer can share a single :class:`BehaviorStateMachine`
# without changing call signatures across ``integration``/``modules``
# boundaries.  ``modules.delay.wrapper.wrap`` publishes its SM into
# this context for the duration of each wrapped task call;
# ``GivexDriver.__init__`` reads it via :func:`get_current_sm` so
# transitions issued from driver methods affect the same SM the
# delay engine uses for safety decisions.
#
# The default is ``None`` so call-sites must explicitly fall back to
# constructing their own SM (preserving existing test behaviour).

_current_sm: "contextvars.ContextVar[Optional[BehaviorStateMachine]]" = (
    contextvars.ContextVar("modules.delay.state._current_sm", default=None)
)


def get_current_sm() -> Optional[BehaviorStateMachine]:
    """Return the :class:`BehaviorStateMachine` for the current context.

    Returns ``None`` when no SM has been published in the current
    context (e.g. the caller is running outside of a behaviour wrapper
    or in a unit test that constructs the driver directly).
    """
    return _current_sm.get()


def set_current_sm(sm: Optional[BehaviorStateMachine]):
    """Publish *sm* as the active state machine for the current context.

    Returns the :class:`contextvars.Token` that must be passed to
    :func:`reset_current_sm` to restore the previous value.  Callers
    are expected to use a ``try``/``finally`` pair to guarantee the
    token is always reset even when the wrapped task raises.
    """
    return _current_sm.set(sm)


def reset_current_sm(token) -> None:
    """Restore the previous value of the current-SM context variable.

    *token* must be the :class:`contextvars.Token` returned by the
    matching :func:`set_current_sm` call.
    """
    _current_sm.reset(token)
