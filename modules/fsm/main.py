"""FSM module — finite state machine for worker payment flows.

Legacy global API is deprecated. Use `initialize_for_worker()`, `transition_for_worker()`,
`get_current_state_for_worker()`, `cleanup_worker()` for production multi-worker usage.
"""
import functools
import inspect
import logging
import os
import threading
import warnings

from modules.common.exceptions import InvalidStateError, InvalidTransitionError
from modules.common.types import State

_logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

ALLOWED_STATES = {"ui_lock", "success", "vbv_3ds", "declined"}

_VALID_PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    "ui_lock": {"success", "declined", "vbv_3ds"},
    "vbv_3ds": {"success", "declined"},
    "success": set(),
    "declined": set(),
}

# States from which no further transitions are permitted.  Workers that have
# reached a terminal state must not be advanced by late callbacks or retries.
TERMINAL_STATES: frozenset = frozenset({"success", "declined"})

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


def add_state_for_worker(worker_id: str, state_name: str) -> State:
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


def transition_for_worker(worker_id: str, target_state: str) -> State:
    if target_state not in ALLOWED_STATES:
        raise InvalidStateError(f"state '{target_state}' is not in ALLOWED_STATES")
    with _registry_lock:
        entry = _registry.get(worker_id)
        if entry is None:
            raise InvalidTransitionError(f"worker '{worker_id}' not initialized")
        if target_state not in entry["states"]:
            raise InvalidTransitionError(f"state '{target_state}' not registered for worker '{worker_id}'")
        current = entry["current"]
        if current is not None:
            current_name = current.name
            if current_name in TERMINAL_STATES:
                raise ValueError(
                    f"Invalid transition from {current_name} to {target_state}: "
                    f"'{current_name}' is a terminal state"
                )
            allowed_targets = _VALID_PAYMENT_TRANSITIONS.get(current_name, set())
            if target_state not in allowed_targets:
                raise ValueError(f"Invalid transition from {current_name} to {target_state}")
        entry["current"] = entry["states"][target_state]
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
