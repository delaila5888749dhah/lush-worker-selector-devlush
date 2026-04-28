"""FSM module — finite state machine for worker payment flows.

Legacy global API is deprecated. Use `initialize_for_worker()`, `transition_for_worker()`,
`get_current_state_for_worker()`, `cleanup_worker()` for production multi-worker usage.
"""
from datetime import datetime, timezone
import enum
import functools
import inspect
import logging
import os
import threading
import warnings

from modules.common.exceptions import InvalidStateError, InvalidTransitionError
from modules.common.types import State

_logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class PaymentState(enum.StrEnum):
    """Canonical FSM payment-state identifiers.

    Inheriting from :class:`enum.StrEnum` (Python 3.11+) gives each member a
    string value identity: ``PaymentState.SUCCESS == "success"`` is ``True``
    and ``hash(PaymentState.SUCCESS) == hash("success")``. This guarantees
    backward compatibility with callers that pass raw strings while providing
    type safety and a single source of truth **for state identifiers**.

    Note: transition topology (:data:`_VALID_PAYMENT_TRANSITIONS`) is still
    declared separately and is not derived from this enum.
    """

    UI_LOCK = "ui_lock"
    SUCCESS = "success"
    VBV_3DS = "vbv_3ds"
    DECLINED = "declined"
    VBV_CANCELLED = "vbv_cancelled"


# `ALLOWED_STATES` is a ``frozenset[str]`` of the underlying string values so
# that existing membership checks (``name in ALLOWED_STATES``) keep working
# with raw strings AND with :class:`PaymentState` members (StrEnum members
# hash/compare equal to their string values).
#
# Note: the container type changed from a mutable ``set`` (pre-refactor) to
# an immutable ``frozenset``; callers that mutated it in-place must instead
# build their own ``set(ALLOWED_STATES)`` copy.
ALLOWED_STATES: frozenset[str] = frozenset(s.value for s in PaymentState)

_VALID_PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    PaymentState.UI_LOCK.value: {PaymentState.SUCCESS.value, PaymentState.DECLINED.value, PaymentState.VBV_3DS.value},
    PaymentState.VBV_3DS.value: {PaymentState.SUCCESS.value, PaymentState.DECLINED.value, PaymentState.VBV_CANCELLED.value},
    PaymentState.SUCCESS.value: set(),
    PaymentState.DECLINED.value: set(),
    PaymentState.VBV_CANCELLED.value: set(),
}

# States from which no further transitions are permitted.  Workers that have
# reached a terminal state must not be advanced by late callbacks or retries.
# `vbv_cancelled` is terminal at the FSM layer — card-swap/refill is handled
# by the orchestrator (`handle_outcome`) at a higher level, not via an FSM
# transition.
#
# Stored as raw string values so that ``current.name in TERMINAL_STATES``
# (where ``current.name`` is a plain ``str``) is a direct string-set lookup
# and does not rely on ``StrEnum`` hash/equality alignment.
TERMINAL_STATES: frozenset[str] = frozenset(
    {PaymentState.SUCCESS.value, PaymentState.DECLINED.value, PaymentState.VBV_CANCELLED.value}
)


def _normalize_state(state_name: "str | PaymentState") -> str:
    """Return the canonical string form of *state_name*.

    Accepts either a raw ``str`` (or ``str`` subclass, including
    :class:`PaymentState`) and returns a **plain** ``str``. This keeps
    :class:`State` ``.name`` a plain string and preserves backward
    compatibility with callers passing raw strings.

    Any input that is not a ``str`` instance is rejected with
    :class:`TypeError` so foreign objects cannot leak through the
    membership check and end up stored in :attr:`State.name`.
    """
    if isinstance(state_name, PaymentState):
        return state_name.value
    if isinstance(state_name, str):
        # Coerce ``str`` subclasses (e.g. unrelated ``StrEnum`` members) to a
        # plain ``str`` so downstream consumers always observe ``type(name) is str``.
        return str(state_name)
    raise TypeError(
        f"state_name must be str or PaymentState, got {type(state_name).__name__}"
    )

# Per-worker registry: worker_id → {"states": {}, "current": None}
_registry: dict[str, dict] = {}
_registry_lock = threading.Lock()

# Legacy global state kept for backward compatibility with code that does not
# pass a worker_id (e.g. single-worker scenarios and existing tests).
_states: dict = {}
_legacy_global_lock = threading.Lock()  # pylint: disable=invalid-name
_current_state = None


# ── Per-worker API ──────────────────────────────────────────────


def initialize_for_worker(worker_id: str) -> None:
    """Reset and re-register all allowed states for *worker_id*."""
    with _registry_lock:
        _registry[worker_id] = {
            "states": {name: State(name=name) for name in ALLOWED_STATES},
            "current": None,
        }


def add_state_for_worker(worker_id: str, state_name: "str | PaymentState") -> State:
    state_name = _normalize_state(state_name)
    if state_name not in ALLOWED_STATES:
        raise InvalidStateError(f"state '{state_name}' is not in ALLOWED_STATES")
    with _registry_lock:
        entry = _registry.get(worker_id)
        if entry is None:
            raise ValueError(f"worker '{worker_id}' not initialized")
        if state_name in entry["states"]:
            raise ValueError(f"state '{state_name}' already exists for worker '{worker_id}'")
        state = State(name=state_name)
        entry["states"][state_name] = state
        return state


def get_current_state_for_worker(worker_id: str) -> "State | None":
    with _registry_lock:
        return _registry.get(worker_id, {}).get("current")


def transition_for_worker(
    worker_id: str,
    target_state: "str | PaymentState",
    trace_id: "str | None" = None,
) -> State:
    """Transition *worker_id* to *target_state*.

    *target_state* accepts either a raw ``str`` or a :class:`PaymentState`
    member; both are accepted for backward compatibility.

    *trace_id* is an optional correlation identifier that is included in the
    structured log line emitted on every successful transition (and on every
    rejection by :data:`_VALID_PAYMENT_TRANSITIONS`). When omitted, ``"-"`` is
    logged so the field is always present and grep-able.

    Log format follows the canonical 6-field pipe-delimited shape
    ``timestamp | worker_id | trace_id | state | action | status``
    (see ``.github/AI_CONTEXT.md`` and ``integration/runtime.py::_log_event``):
    successful transitions emit action ``FSM_TRANSITION`` with
    ``status=success from=<prev> to=<target>``; rejected transitions emit
    action ``FSM_TRANSITION_REJECTED`` at WARN with
    ``status=rejected from=<prev> to=<target> reason=<terminal|out_of_band>``.
    """
    target_state = _normalize_state(target_state)
    if target_state not in ALLOWED_STATES:
        raise InvalidStateError(f"state '{target_state}' is not in ALLOWED_STATES")
    _trace = trace_id if trace_id is not None else "-"
    with _registry_lock:
        entry = _registry.get(worker_id)
        if entry is None:
            raise InvalidTransitionError(f"worker '{worker_id}' not initialized")
        if target_state not in entry["states"]:
            raise InvalidTransitionError(f"state '{target_state}' not registered for worker '{worker_id}'")
        current = entry["current"]
        prev_name = current.name if current is not None else "-"
        if current is not None:
            current_name = current.name
            if current_name in TERMINAL_STATES:
                _logger.warning(
                    "%s | %s | %s | %s | %s | %s",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    worker_id, _trace, current_name, "FSM_TRANSITION_REJECTED",
                    f"status=rejected from={current_name} to={target_state} reason=terminal",
                )
                raise ValueError(
                    f"Invalid transition from {current_name} to {target_state}: "
                    f"'{current_name}' is a terminal state"
                )
            allowed_targets = _VALID_PAYMENT_TRANSITIONS.get(current_name, set())
            if target_state not in allowed_targets:
                _logger.warning(
                    "%s | %s | %s | %s | %s | %s",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    worker_id, _trace, current_name, "FSM_TRANSITION_REJECTED",
                    f"status=rejected from={current_name} to={target_state} reason=out_of_band",
                )
                raise ValueError(f"Invalid transition from {current_name} to {target_state}")
        entry["current"] = entry["states"][target_state]
        _logger.info(
            "%s | %s | %s | %s | %s | %s",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            worker_id, _trace, target_state, "FSM_TRANSITION",
            f"status=success from={prev_name} to={target_state}",
        )
        return entry["current"]


def cleanup_worker(worker_id: str) -> None:
    """Remove all FSM state for *worker_id*."""
    with _registry_lock:
        _registry.pop(worker_id, None)


# ── Legacy global API (backward compat) ────────────────────────


def _is_legacy_allowed() -> bool:
    """Return True when FSM_ALLOW_LEGACY is explicitly enabled.

    Defaults to disabled so that production builds never expose the legacy API.
    Set ``FSM_ALLOW_LEGACY=1`` (or ``true``/``yes``) in non-production environments
    to re-enable the legacy global API with deprecation warnings.
    """
    return os.environ.get("FSM_ALLOW_LEGACY", "").strip().lower() in ("1", "true", "yes")


def _legacy_warn(func):
    @functools.wraps(func)
    def _wrapper(*args, **kwargs):
        if not _is_legacy_allowed():
            raise RuntimeError(
                f"FSM legacy global API '{func.__name__}' is disabled. "
                "Set FSM_ALLOW_LEGACY=1 to enable (not for production use)."
            )
        stack = inspect.stack()
        # stack[0]=_wrapper, stack[1]=decorated func call, stack[2]=actual caller
        frame = stack[2] if len(stack) > 2 else stack[-1]
        caller_info = f"{frame.filename}:{frame.lineno} in {frame.function}"
        _logger.warning(
            "FSM legacy global API '%s' called — use per-worker API instead. Caller: %s",
            func.__name__,
            caller_info,
        )
        return func(*args, **kwargs)
    return _wrapper


@_legacy_warn
def add_new_state(state_name):
    """Add *state_name* to the legacy global registry.

    .. deprecated::
        Use :func:`add_state_for_worker` instead.
    """
    warnings.warn(
        "add_new_state() is deprecated — use add_state_for_worker() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    state_name = _normalize_state(state_name)
    with _legacy_global_lock:
        if state_name not in ALLOWED_STATES:
            raise InvalidStateError(f"state '{state_name}' is not in ALLOWED_STATES")
        if state_name in _states:
            raise ValueError(f"state '{state_name}' already exists")
        state = State(name=state_name)
        _states[state_name] = state
        return state


@_legacy_warn
def get_current_state():
    """Return the current legacy global FSM state.

    .. deprecated::
        Use :func:`get_current_state_for_worker` instead.
    """
    warnings.warn(
        "get_current_state() is deprecated — use get_current_state_for_worker() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    with _legacy_global_lock:
        return _current_state


@_legacy_warn
def transition_to(target_state):
    """Transition the legacy global FSM to *target_state*.

    .. deprecated::
        Use :func:`transition_for_worker` instead.
    """
    global _current_state
    warnings.warn(
        "transition_to() is deprecated — use transition_for_worker() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    target_state = _normalize_state(target_state)
    with _legacy_global_lock:
        if target_state not in ALLOWED_STATES:
            raise InvalidStateError(f"state '{target_state}' is not in ALLOWED_STATES")
        if target_state not in _states:
            raise InvalidTransitionError(f"state '{target_state}' not registered")
        _current_state = _states[target_state]
        return _current_state


@_legacy_warn
def reset_states():
    """Clear the legacy global FSM state.

    .. deprecated::
        Use :func:`cleanup_worker` to remove state for a specific worker, or
        :func:`reset_registry` to clear all per-worker state (e.g. in tests).
    """
    global _current_state
    warnings.warn(
        "reset_states() is deprecated — use cleanup_worker() or reset_registry() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    with _legacy_global_lock:
        _states.clear()
        _current_state = None


def reset_registry():
    """Clear all per-worker FSM state. Intended for testing."""
    with _registry_lock:
        _registry.clear()
