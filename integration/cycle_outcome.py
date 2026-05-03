"""Integration-local cycle outcome exceptions and helpers.

Kept out of ``modules/common/exceptions.py`` to avoid RULE 6
VERSIONING_ENFORCEMENT — this is an internal control-flow signal
between :mod:`integration.worker_task` and :mod:`integration.runtime`,
not a public spec contract.
"""


class CycleDidNotCompleteError(RuntimeError):
    """Raised when ``run_cycle()`` returned a non-complete action.

    Used to signal to :func:`integration.runtime._worker_fn` that a
    cycle did not complete successfully (e.g. ``abort_cycle``,
    ``await_3ds``, ``retry``, ``retry_new_card``) so the runtime
    accounts the cycle as an error rather than a success.
    """

    def __init__(self, action: str, reason: str = ""):
        self.action = action
        self.reason = reason
        msg = f"cycle did not complete: action={action}"
        if reason:
            msg += " reason=<redacted>"
        super().__init__(msg)


KNOWN_RUN_CYCLE_ACTIONS = frozenset({
    "complete",
    "abort_cycle",
    "await_3ds",
    "retry",
    "retry_new_card",
})

KNOWN_RUN_CYCLE_TUPLE_ACTIONS = frozenset({"retry_new_card"})


def normalize_action(action) -> str:
    """Return canonical action token; fail loud on malformed action values.

    ``run_cycle()`` may return ``action`` as a plain string (e.g.
    ``"complete"``) or as a tuple like ``("retry_new_card", CardInfo)``.
    Normalize to the leading string token for comparisons.

    Do not stringify arbitrary objects, because repr/str may contain
    sensitive context and would turn a contract bug into loggable data.
    """
    if isinstance(action, tuple):
        if len(action) == 0:
            raise ValueError("empty run_cycle action tuple")
        if not isinstance(action[0], str):
            raise ValueError("run_cycle action tuple must start with string token")
        token = action[0]
        if token not in KNOWN_RUN_CYCLE_ACTIONS:
            raise ValueError("unknown run_cycle tuple action token")
        if token not in KNOWN_RUN_CYCLE_TUPLE_ACTIONS:
            raise ValueError("run_cycle action token does not support tuple form")
    elif isinstance(action, str):
        token = action
    else:
        raise ValueError("malformed run_cycle action type")

    if token not in KNOWN_RUN_CYCLE_ACTIONS:
        raise ValueError("unknown run_cycle action token")
    return token
