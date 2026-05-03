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
        super().__init__(
            f"cycle did not complete: action={action} reason={reason}"
        )


def normalize_action(action) -> str:
    """Return canonical string action regardless of tuple form.

    ``run_cycle()`` may return ``action`` as a plain string (e.g.
    ``"complete"``) or as a tuple like ``("retry_new_card", CardInfo)``.
    Normalise to the leading string token for comparisons.
    """
    if isinstance(action, tuple) and action:
        return str(action[0])
    return str(action)
