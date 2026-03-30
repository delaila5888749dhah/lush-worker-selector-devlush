import re


class State:
    def __init__(self, name: str):
        self.name = name


_states = set()
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_RESERVED_NAMES = {"initial", "final", "error"}


def add_new_state(state_name: str) -> bool:
    if state_name is None or not isinstance(state_name, str):
        return False
    if state_name == "":
        return False
    if _VALID_NAME_RE.fullmatch(state_name) is None:
        return False
    if state_name.lower() in _RESERVED_NAMES:
        return False
    if state_name in _states:
        return False
    _states.add(state_name)
    return True


def reset_states() -> None:
    _states.clear()