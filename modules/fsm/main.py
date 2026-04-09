import threading

from modules.common.exceptions import InvalidStateError, InvalidTransitionError
from modules.common.types import State

ALLOWED_STATES = {"ui_lock", "success", "vbv_3ds", "declined"}

_VALID_PAYMENT_TRANSITIONS: dict[str, set[str]] = {
    "ui_lock": {"success", "declined", "vbv_3ds"},
    "vbv_3ds": {"success", "declined"},
    "success": set(),
    "declined": set(),
}

# Per-worker registry: worker_id → {"states": {}, "current": None}
_registry: dict[str, dict] = {}
_registry_lock = threading.Lock()

# Legacy global state kept for backward compatibility with code that does not
# pass a worker_id (e.g. single-worker scenarios and existing tests).
_states: dict = {}
_states_lock = threading.Lock()
_current_state = None


# ── Per-worker API ──────────────────────────────────────────────


def initialize_for_worker(worker_id: str) -> None:
    """Reset and re-register all allowed states for *worker_id*."""
    with _registry_lock:
        _registry[worker_id] = {"states": {}, "current": None}
    for state_name in ALLOWED_STATES:
        add_state_for_worker(worker_id, state_name)


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
        if entry is None or target_state not in entry["states"]:
            raise InvalidTransitionError(f"state '{target_state}' not registered for worker '{worker_id}'")
        current = entry["current"]
        if current is not None:
            current_name = current.name
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


def add_new_state(state_name):
    if state_name not in ALLOWED_STATES:
        raise InvalidStateError(f"state '{state_name}' is not in ALLOWED_STATES")
    with _states_lock:
        if state_name in _states:
            raise ValueError(f"state '{state_name}' already exists")
        state = State(name=state_name)
        _states[state_name] = state
        return state


def get_current_state():
    with _states_lock:
        return _current_state


def transition_to(target_state):
    global _current_state
    if target_state not in ALLOWED_STATES:
        raise InvalidStateError(f"state '{target_state}' is not in ALLOWED_STATES")
    with _states_lock:
        if target_state not in _states:
            raise InvalidTransitionError(f"state '{target_state}' not registered")
        _current_state = _states[target_state]
        return _current_state


def reset_states():
    global _current_state
    with _states_lock:
        _states.clear()
        _current_state = None


def reset_registry():
    """Clear all per-worker FSM state. Intended for testing."""
    with _registry_lock:
        _registry.clear()