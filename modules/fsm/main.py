import re

_states = set()


def add_new_state(state_name: str) -> bool:
    if not isinstance(state_name, str):
        return False
    if state_name == "":
        return False
    if not re.match(r'^[a-zA-Z0-9_]+$', state_name):
        return False
    if state_name.lower() in {"initial", "final", "error"}:
        return False
    if state_name in _states:
        return False
    _states.add(state_name)
    return True