import threading

from spec.schema import State

ALLOWED_STATES = {"ui_lock", "success", "vbv_3ds", "declined"}

_states: dict[str, State] = {}
_current: State | None = None
_states_lock = threading.Lock()


def add_new_state(state_name: str) -> State:
    with _states_lock:
        if state_name not in ALLOWED_STATES:
            raise ValueError(f"'{state_name}' is not in ALLOWED_STATES")
        if state_name in _states:
            raise ValueError(f"'{state_name}' already exists in registry")
        state = State(name=state_name)
        _states[state_name] = state
        global _current
        _current = state
        return state


def get_current_state() -> State | None:
    with _states_lock:
        return _current


def transition_to(target_state: str) -> State:
    with _states_lock:
        if target_state not in ALLOWED_STATES:
            raise ValueError(f"'{target_state}' is not in ALLOWED_STATES")
        if target_state not in _states:
            raise ValueError(f"'{target_state}' not found in registry")
        global _current
        _current = _states[target_state]
        return _current


def reset_states() -> None:
    with _states_lock:
        _states.clear()
        global _current
        _current = None