import threading

from modules.common.exceptions import InvalidStateError, InvalidTransitionError
from modules.common.types import State

ALLOWED_STATES = {"ui_lock", "success", "vbv_3ds", "declined"}

_states = {}
_states_lock = threading.Lock()
_current_state = None


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